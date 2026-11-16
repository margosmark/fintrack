"""YNAB migration — preview-first import of a YNAB budget export (docs/38).

Two routes, one plan:

* ``POST /imports/ynab/preview`` — parse + map, return the FULL plan (accounts,
  categories, targets, per-row mapping, the complete unmappable list). It
  writes nothing, so it is safe to run repeatedly before deciding.
* ``POST /imports/ynab/commit`` — parse + map + apply in ONE transaction.

Upload validation order mirrors docs/03 (rate limit → Content-Length → byte
cap → parse → row cap); every failure is the ``{detail, code}`` envelope
(docs/08), never a bare 500.
"""

import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, owned_or_404
from app.core.errors import api_error
from app.core.ratelimit import IMPORT_MUTATE_LIMIT, IMPORT_UPLOAD_LIMIT, human_paced_limit
from app.importers.ynab import YnabParseError, parse_ynab
from app.models.accounts import Account, AccountType
from app.models.users import User
from app.schemas.ynab import YnabBatchResult, YnabCommitResult, YnabMigrationPlan
from app.services.ynab_migration import PlanOptions, apply_plan, build_plan

router = APIRouter(prefix="/imports/ynab", tags=["imports"])

MAX_UPLOAD_BYTES = 5_000_000  # same file cap as POST /imports/upload (docs/03)


def _refuse_when_encrypted(user: User, capability: str) -> None:
    """Fail closed for an `encryption_required` tenant (docs/38 §8, docs/33 rule 7d).

    `commit_ynab` runs SERVER-side over a file that carries no ciphertext, so
    `users.encryption_required` can never be satisfied here — the refusal is
    keyed on ENROLMENT ALONE and names the capability plus the reason, never
    "unlock the vault", because unlocking does not make a server-side upload
    able to write ciphers.

    The message is byte-identical to `api/imports.py::_refuse_when_encrypted`
    (also duplicated in `api/trades.py`); this router keeps its own copy rather
    than importing a private helper from a sibling router. The parity is pinned
    by `tests/test_ynab_migration.py::test_ynab_refusal_carries_the_shared_server_side_phrase`,
    which compares the two helpers' details directly.
    """
    if user.encryption_required:
        raise api_error(
            422,
            f"{capability} is unavailable while client-side encryption is enabled: "
            "this path runs server-side and cannot safely write encrypted data, so "
            "nothing was saved",
            "encryption_required",
        )


MAX_BODY_BYTES = 5_500_000  # file cap + multipart overhead
MAX_YNAB_ROWS = 50_000  # a YNAB budget is one file, not one account
_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")


def _parses(sample: str, fmt: str) -> bool:
    try:
        return datetime.strptime(sample, fmt) is not None
    except ValueError:
        return False


def _reject_oversize_body(request: Request) -> None:
    """413 on the Content-Length header, before the body is read (docs/03 L6)."""
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        raise api_error(413, "File exceeds the 5 MB limit", "file_too_large")


async def _read_upload(request: Request, file: UploadFile) -> bytes:
    _reject_oversize_body(request)
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise api_error(413, "File exceeds the 5 MB limit", "file_too_large")
    return raw


def _csv_options(
    date_format: str | None,
    account_type: str | None,
    currency: str | None,
) -> None:
    """Validate the CSV-only overrides up front; 422 names the bad field."""
    if account_type is not None:
        try:
            AccountType(account_type)
        except ValueError:
            valid = ", ".join(t.value for t in AccountType)
            raise api_error(
                422,
                f"Unknown account_type `{account_type}`; expected one of: {valid}",
                "invalid_account_type",
            ) from None
    if currency is not None and not _CURRENCY_RE.match(currency):
        raise api_error(422, "currency must be a 3-letter ISO code", "invalid_currency")
    if date_format is not None:
        has_year = "%Y" in date_format or "%y" in date_format
        if not (has_year and "%m" in date_format and "%d" in date_format):
            raise api_error(
                422,
                "date_format must include a year (%Y or %y), a month (%m) and a day (%d)",
                "invalid_date_format",
            )
        # A pattern that parses NO sample is worse than a rejected upload:
        # every row would land in `invalid_date` instead of the file being refused.
        samples = (
            "31/12/2024",
            "2024-12-31",
            "12/31/2024",
            "2024/12/31",
            "31-12-2024",
            "12-31-2024",
            "31.12.2024",
            "2024.12.31",
        )
        if not any(_parses(sample, date_format) for sample in samples):
            raise api_error(
                422,
                f"date_format `{date_format}` did not match any sample date",
                "invalid_date_format",
            )


def _parse(raw: bytes, date_format: str | None, account_type: str | None, currency: str | None):
    try:
        budget = parse_ynab(
            raw, date_format=date_format, account_type=account_type, currency=currency
        )
    except YnabParseError as exc:
        raise api_error(422, str(exc), "ynab_parse_error") from None
    if budget.source_rows > MAX_YNAB_ROWS:
        raise api_error(413, f"File contains more than {MAX_YNAB_ROWS} rows", "too_many_rows")
    return budget


@router.post("/preview", response_model=YnabMigrationPlan)
@human_paced_limit(IMPORT_UPLOAD_LIMIT)
async def preview_ynab(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="YNAB budget export (JSON) or register CSV")],
    account_id: Annotated[
        int | None, Form(description="Target account for CSV rows with no Account column")
    ] = None,
    account_type: Annotated[
        str | None, Form(description="Account type assumed for CSV rows")
    ] = None,
    currency: Annotated[str | None, Form(description="ISO currency for a CSV register")] = None,
    date_format: Annotated[str | None, Form(description="strptime pattern for CSV dates")] = None,
) -> YnabMigrationPlan:
    """Full mapping, zero writes — nothing is committed until /commit."""
    raw = await _read_upload(request, file)
    _csv_options(date_format, account_type, currency)
    fallback = owned_or_404(Account, account_id, current_user, db) if account_id else None
    opts = PlanOptions(
        default_currency=(currency or "CAD").upper(),
        fallback_account_id=fallback.id if fallback else None,
        file_name=file.filename or "ynab-export",
    )
    budget = _parse(raw, date_format, account_type, currency)
    return build_plan(db, current_user, budget, opts).plan


@router.post("/commit", response_model=YnabCommitResult, status_code=201)
@human_paced_limit(IMPORT_MUTATE_LIMIT)
async def commit_ynab(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="YNAB budget export (JSON) or register CSV")],
    account_id: Annotated[
        int | None, Form(description="Target account for CSV rows with no Account column")
    ] = None,
    account_type: Annotated[
        str | None, Form(description="Account type assumed for CSV rows")
    ] = None,
    currency: Annotated[str | None, Form(description="ISO currency for a CSV register")] = None,
    date_format: Annotated[str | None, Form(description="strptime pattern for CSV dates")] = None,
) -> YnabCommitResult:
    """Apply the plan: accounts/categories/tags/targets + one staged batch per account."""
    # E2EE tenants' payee/amount columns are cipher-only (docs/33); a
    # server-side file upload cannot produce ciphertext, so it is refused
    # rather than writing plaintext into an encrypted tenant (docs/38 §8).
    # The capability phrase is the one `ServerSideBlocked.tsx` renders
    # pre-emptively for `import:ynab`, so the gate and this 422 agree.
    _refuse_when_encrypted(current_user, "Importing a YNAB budget file")
    raw = await _read_upload(request, file)
    _csv_options(date_format, account_type, currency)
    fallback = owned_or_404(Account, account_id, current_user, db) if account_id else None
    opts = PlanOptions(
        default_currency=(currency or "CAD").upper(),
        fallback_account_id=fallback.id if fallback else None,
        file_name=file.filename or "ynab-export",
    )
    budget = _parse(raw, date_format, account_type, currency)
    resolved = build_plan(db, current_user, budget, opts)
    batches, net_new, duplicates_skipped = apply_plan(db, current_user, budget, resolved)
    return YnabCommitResult(
        plan=resolved.plan,
        batches=[YnabBatchResult(**batch) for batch in batches],
        net_new=net_new,
        duplicates_skipped=duplicates_skipped,
    )

"""imports — staged batch pipeline: upload → pending_review → commit|discard (docs/03)."""

import csv
import re
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api._offloop import blocking
from app.api.deps import (
    Pagination,
    get_current_user,
    get_db,
    get_pagination,
    owned_or_404,
    paginate,
)
from app.core.config import get_settings
from app.core.errors import api_error
from app.core.ratelimit import IMPORT_MUTATE_LIMIT, IMPORT_UPLOAD_LIMIT, human_paced_limit
from app.importers.base import AccountView, build_dedup_hash
from app.importers.csv_profile import (
    CsvLineTooLongError,
    CsvProfile,
    CsvProfileError,
    CsvTooComplexError,
    MissingProfileError,
    check_csv_complexity,
    profile_error_message,
)
from app.importers.ofx import OfxParseError
from app.importers.qif import QifParseError
from app.importers.registry import detect_importer
from app.llm.base import build_provider
from app.models.accounts import Account
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.institutions import Institution
from app.models.trades import Trade
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.imports import (
    CommitBatchBody,
    DryRunResponse,
    DryRunRow,
    DryRunSummary,
    FieldRole,
    FormatMapping,
    ImportBatchPage,
    ImportBatchPublic,
    NearDuplicateMatchPublic,
    NetWorthCommitResult,
    NetWorthPlanResponse,
    PreviewRow,
    SuggestProfileResponse,
    UploadResponse,
)
from app.services import fund_enrichment
from app.services.column_mapping import suggest_column_mapping
from app.services.fx import cached_cad_amount
from app.services.import_dedup import RowKey, find_near_duplicates
from app.services.import_staging import stage_rows
from app.services.networth_import import (
    NetWorthImportError,
    apply_plan,
    build_plan,
    parse_networth_file,
)
from app.services.session_release import release_before_provider_wait
from app.services.txn_children import unlink_transaction_children
from app.tax.acb import rebuild_user_pools

router = APIRouter(prefix="/imports", tags=["imports"])

MAX_UPLOAD_BYTES = 5_000_000  # file content cap (docs/03)
# Content-Length cap: file cap + multipart overhead. Checked first because it
# is an O(1) header read (audit L6 partial — see _reject_oversize_body).
MAX_BODY_BYTES = 5_500_000


def _refuse_when_encrypted(user: User, capability: str) -> None:
    """M181 (docs/33 § "OUTSIDE THE ENVELOPE (M79)"): fail closed.

    `stage_rows` writes the mandatory PLAINTEXT columns, so a statement staged
    server-side can never satisfy an opted-in tenant's ciphertext contract —
    `users.encryption_required` only ever *checks* that ciphers are present, and
    a parsed file has none. Refuse the whole request before anything is read or
    written, exactly like `api/ynab.py::commit_ynab` does. The copy names the
    capability and the reason; it never offers "unlock the vault" as a remedy,
    because unlocking does not make a server-side import able to write ciphers
    (docs/33 §Server-first computing rule 7d).
    """
    if user.encryption_required:
        raise api_error(
            422,
            f"{capability} is unavailable while client-side encryption is enabled: "
            "this path runs server-side and cannot safely write encrypted data, so "
            "nothing was saved",
            "encryption_required",
        )


def _reject_oversize_body(request: Request) -> None:
    """Reject an oversized upload on its Content-Length before anything reads it.

    Cheapest upload check first (docs/03 "Upload validation order"). RESIDUAL
    (accepted, audit L6): FastAPI parses the multipart body before the route
    runs, so an over-cap body is still buffered once by the ASGI layer —
    a true streaming abort needs custom middleware, which is out of scope;
    nginx's ``client_max_body_size`` remains the network backstop.
    """
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        raise api_error(413, "File exceeds the 5 MB limit", "file_too_large")


def _parse_file(raw: bytes, context: AccountView) -> tuple[str, list]:
    """Detect format, apply CSV hard caps, parse — always the 422 envelope (docs/03).

    Order (cheapest first): sniff -> O(n) line/quote caps -> parse. Every
    parser failure maps to a stable code; none can escape as a bare 500
    (audit L5). Returns ``(format, parsed_rows)`` — the format label feeds the
    dry-run preview's column-role mapping.
    """
    match = detect_importer(raw)
    if match is None:
        raise api_error(422, "Unrecognized file format", "unsupported_format")
    fmt, importer = match
    try:
        if fmt == "csv":
            check_csv_complexity(raw)  # pre-parse line/quote caps (audit H2)
        return fmt, importer.parse(raw, context)
    except MissingProfileError as exc:
        raise api_error(422, str(exc), "missing_importer_profile") from None
    except CsvLineTooLongError as exc:
        raise api_error(422, str(exc), "csv_line_too_long") from None
    except CsvTooComplexError as exc:
        raise api_error(422, str(exc), "csv_too_complex") from None
    except CsvProfileError as exc:
        raise api_error(422, f"Could not parse file: {exc}", "parse_error") from None
    except csv.Error as exc:
        raise api_error(422, f"Could not parse file: {exc}", "csv_parse_error") from None
    except OfxParseError as exc:
        raise api_error(422, f"Could not parse file: {exc}", "ofx_parse_error") from None
    except QifParseError as exc:
        raise api_error(422, f"Could not parse file: {exc}", "qif_parse_error") from None
    except (ValueError, LookupError) as exc:
        # Residual third-party parser failures keep the same envelope; the
        # code follows the sniffed format (csv profiles are validated, so a
        # csv-path ValueError here is malformed input, not a bug to 500 on).
        code = {
            "ofx": "ofx_parse_error",
            "qif": "qif_parse_error",
        }.get(fmt, "csv_parse_error")
        raise api_error(422, f"Could not parse file: {exc}", code) from None


def _resolve_context(
    db: Session, account: Account, profile_json: str | None
) -> tuple[AccountView, str]:
    """Build the importer context: institution name + effective CSV profile.

    Shared by ``/upload`` and ``/preview`` so a dry run resolves exactly the
    profile the real upload would use (wizard override beats the stored one).
    """
    institution = db.get(Institution, account.institution_id) if account.institution_id else None
    institution_name = institution.name if institution else ""
    if profile_json is not None:
        try:
            profile_obj = CsvProfile.model_validate_json(profile_json)
        except ValidationError as exc:
            # M111: pydantic's own record is internals ("1 validation error for
            # CsvProfile … input_value=…"); the envelope carries the actionable
            # line instead, so the mapping the user must fix is named.
            raise api_error(
                422, f"Invalid profile: {profile_error_message(exc)}", "invalid_profile"
            ) from None
        importer_profile: dict | None = profile_obj.model_dump()
    else:
        importer_profile = institution.importer_profile if institution else None
    return AccountView(
        currency=account.currency, importer_profile=importer_profile
    ), institution_name


def _current_profile(context: AccountView) -> CsvProfile | None:
    """The validated profile behind *context*, or None for a structured file."""
    payload = context.importer_profile
    if payload is None:
        return None
    if isinstance(payload, CsvProfile):
        return payload
    if isinstance(payload, dict):
        return CsvProfile.model_validate(payload)
    return CsvProfile.model_validate_json(str(payload))


# Column-role mapping shown by the dry-run preview BEFORE anything is
# committed (docs/03). Structured formats have a fixed role table; CSV roles
# come from the institution's mapping profile.
_STRUCTURED_ROLES: dict[str, tuple[tuple[str, str], ...]] = {
    "ofx": (
        ("date", "DTPOSTED"),
        ("amount_native", "TRNAMT"),
        ("raw_description", "PAYEE, else MEMO"),
        ("currency", "CURDEF"),
        ("external_ref", "FITID"),
    ),
    "qif": (
        ("date", "D"),
        ("amount_native", "T"),
        ("raw_description", "P, else M, else L"),
        ("external_ref", "N"),
    ),
}


def _mapping_for(fmt: str, context: AccountView) -> FormatMapping:
    if fmt != "csv":
        return FormatMapping(
            format=fmt,
            kind="structured",
            fields=[FieldRole(field=f, source=s) for f, s in _STRUCTURED_ROLES[fmt]],
        )
    profile = _current_profile(context)
    if profile is None:
        # No profile yet: the upload path raises missing_importer_profile and
        # the wizard collects one — the preview reports the roles it would need.
        return FormatMapping(
            format="csv",
            kind="csv_profile",
            fields=[
                FieldRole(field=f, source="(not mapped yet)")
                for f in ("date", "raw_description", "amount_native")
            ],
            mode="signed",
        )
    fields = [
        FieldRole(field=field, source=str(column)) for field, column in profile.columns.items()
    ]
    split = "debit" in profile.columns and "credit" in profile.columns
    return FormatMapping(
        format="csv",
        kind="csv_profile",
        fields=fields,
        mode="split" if split else "signed",
        date_format=profile.date_format,
        decimal_comma=profile.decimal_comma,
        credits_positive=profile.credits_positive,
        signs=profile.signs,
        headerless=profile.headerless,
    )


def _near_public(match) -> NearDuplicateMatchPublic | None:
    if match is None:
        return None
    return NearDuplicateMatchPublic(
        source=match.source,
        transaction_id=match.transaction_id,
        date=match.date,
        amount_native=match.amount_native,
        currency=match.currency,
        raw_description=match.raw_description,
        delta_days=match.delta_days,
        similarity=match.similarity,
    )


@router.post("/suggest-profile", response_model=SuggestProfileResponse)
@human_paced_limit(IMPORT_UPLOAD_LIMIT)
async def suggest_profile(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="CSV file to analyze")],
) -> SuggestProfileResponse:
    """Analyze CSV headers + sample rows to suggest column mappings."""
    _reject_oversize_body(request)  # 1. Content-Length (cheapest, L6 partial)
    if not (file.filename or "").lower().endswith(".csv"):
        raise api_error(422, "Only CSV files are supported", "not_csv")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:  # 2. byte cap
        raise api_error(413, "File exceeds the 5 MB limit", "file_too_large")

    # M149: the LLM call below waits up to 60 s. It runs in a worker thread so
    # the loop keeps serving everything else (docs/01 § "Sessions and the
    # event loop").
    def core() -> SuggestProfileResponse:
        try:
            check_csv_complexity(raw)  # 3. line/quote caps (H2)
            resolved = build_provider(current_user, get_settings())
            # M175 (docs/07 §Provider-wait ledger): `chat()` inside
            # `suggest_column_mapping` waits up to 60 s, and this request's
            # session still holds its auth read open (idle in transaction) —
            # one pinned pool slot per concurrent upload (the M148 shape).
            # Read-only across the wait: nothing here writes, and the response
            # is exactly the mapping parsed below.
            release_before_provider_wait(db)
            result = suggest_column_mapping(raw, resolved.provider, resolved.model)  # 4. parse
        except CsvLineTooLongError as exc:
            raise api_error(422, str(exc), "csv_line_too_long") from None
        except CsvTooComplexError as exc:
            raise api_error(422, str(exc), "csv_too_complex") from None
        except csv.Error as exc:
            raise api_error(422, f"Could not parse file: {exc}", "csv_parse_error") from None
        return SuggestProfileResponse(**result)

    return await blocking(core)()


@router.post("/preview", response_model=DryRunResponse)
@human_paced_limit(IMPORT_UPLOAD_LIMIT)
async def preview_import(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="Statement export (OFX/QFX, QIF or CSV)")],
    account_id: Annotated[int, Form()],
    profile_json: Annotated[str | None, Form()] = None,
) -> DryRunResponse:
    """Dry-run: full staged parse with ZERO writes (docs/03 preview contract).

    Runs the exact upload validation chain — Content-Length, byte cap, sniff,
    CSV line caps, parse, row cap — then reports the column-role mapping,
    every row's verdict (new / duplicate / near_duplicate) and the cached-FX
    result, WITHOUT touching the database. ``Confirm`` is a separate call to
    ``POST /imports/upload``; cancel means simply not calling it, so an
    abandoned preview leaves no batch, no staged row, and no dedup row behind.
    """
    _reject_oversize_body(request)
    # A DB read before the body is read: 404 a foreign account without paying
    # for the upload. M149 — off the loop like everything else that touches
    # the database from an `async def` (docs/01 § "Sessions and the event loop").
    account = await blocking(owned_or_404)(Account, account_id, current_user, db)
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise api_error(413, "File exceeds the 5 MB limit", "file_too_large")

    # M149: the parse, the per-row duplicate query (up to 2000 of them) and the
    # near-duplicate sweep are ordinary blocking work. On the loop they freeze
    # every other request for the whole run.
    def core() -> DryRunResponse:
        context, _institution_name = _resolve_context(db, account, profile_json)
        fmt, parsed = _parse_file(raw, context)
        if len(parsed) > 2000:
            raise api_error(413, "File contains more than 2000 rows", "too_many_rows")

        # Verdict pass — reads only. Exact duplicates use the same canonical hash
        # the upload computes, so a preview and the confirm that follows it agree.
        exact_flags: list[bool] = []
        row_keys: list[RowKey] = []
        unresolved = 0
        cad_amounts: list[object | None] = []
        seen_canonical: set[str] = set()
        for index, txn in enumerate(parsed):
            currency = txn.currency or account.currency
            cad_amount = cached_cad_amount(db, currency, txn.date, txn.amount_native)
            cad_amounts.append(cad_amount)
            if cad_amount is None:
                unresolved += 1
            canonical = build_dedup_hash(account.id, txn)
            exists = (
                db.exec(
                    select(Transaction.id)
                    .where(Transaction.account_id == account.id)
                    .where(Transaction.dedup_hash == canonical)
                    .limit(1)
                ).first()
                is not None
            )
            duplicate = exists or canonical in seen_canonical
            if not duplicate:
                seen_canonical.add(canonical)
            exact_flags.append(duplicate)
            if not duplicate:
                row_keys.append(
                    RowKey(
                        index=index,
                        date=txn.date,
                        amount_native=txn.amount_native,
                        currency=currency,
                        payee=txn.raw_description,
                    )
                )
        near = find_near_duplicates(db, account.id, row_keys)

        rows: list[DryRunRow] = []
        counts = {"new": 0, "duplicate": 0, "near_duplicate": 0}
        for index, txn in enumerate(parsed):
            match = near.get(index)
            verdict = "duplicate" if exact_flags[index] else ("near_duplicate" if match else "new")
            counts[verdict] += 1
            rows.append(
                DryRunRow(
                    index=index,
                    date=txn.date,
                    amount_native=txn.amount_native,
                    currency=txn.currency or account.currency,
                    raw_description=txn.raw_description,
                    cad_amount=cad_amounts[index],
                    verdict=verdict,
                    near_duplicate_match=_near_public(match)
                    if verdict == "near_duplicate"
                    else None,
                )
            )

        return DryRunResponse(
            format=fmt,
            mapping=_mapping_for(fmt, context),
            rows=rows,
            summary=DryRunSummary(
                total=len(parsed),
                new=counts["new"],
                duplicates=counts["duplicate"],
                near_duplicates=counts["near_duplicate"],
                unresolved=unresolved,
            ),
        )

    return await blocking(core)()


# ── net-worth file import (docs/58 §B) ─────────────────────────────────────
# Registered ABOVE the `/{batch_id}` routes: FastAPI matches plain path params
# as `[^/]+`, so `/imports/networth/commit` would otherwise be swallowed as a
# batch id named "networth" and answered with a validation 422 (the ordering
# lesson of docs/38 §2).

_NETWORTH_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")


def _validate_networth_form(currency: str) -> None:
    """Validate the form fields up front; 422 names the bad field."""
    if not _NETWORTH_CURRENCY_RE.match(currency or ""):
        raise api_error(422, "currency must be a 3-letter ISO code", "invalid_currency")


@router.post("/networth/preview", response_model=NetWorthPlanResponse)
@human_paced_limit(IMPORT_UPLOAD_LIMIT)
async def networth_preview(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="ProjectionLab JSON export or balance CSV")],
    currency: Annotated[str, Form()] = "CAD",
    account_name: Annotated[str | None, Form()] = None,
) -> NetWorthPlanResponse:
    """Map a net-worth file to accounts and return the FULL plan (docs/58 §B).

    Writes nothing — safe to run repeatedly before deciding, like every
    other preview here.
    """
    _validate_networth_form(currency)
    _reject_oversize_body(request)
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise api_error(413, "File exceeds the 5 MB limit", "file_too_large")

    def core() -> NetWorthPlanResponse:
        try:
            prepared = parse_networth_file(
                raw,
                file_name=file.filename or "networth",
                account_name=(account_name or "").strip() or None,
            )
            plan, _ = build_plan(db, current_user.id, prepared, currency.upper())
        except NetWorthImportError as exc:
            raise api_error(exc.status, exc.detail, exc.code) from None
        return NetWorthPlanResponse(**plan)

    return await blocking(core)()


@router.post("/networth/commit", response_model=NetWorthCommitResult)
@human_paced_limit(IMPORT_MUTATE_LIMIT)
async def networth_commit(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="ProjectionLab JSON export or balance CSV")],
    currency: Annotated[str, Form()] = "CAD",
    account_name: Annotated[str | None, Form()] = None,
) -> NetWorthCommitResult:
    """Map + apply in ONE transaction: create the accounts the plan names.

    Existing accounts matched by name are reused, never overwritten (merge
    review, docs/58 §B); re-committing the same file therefore creates 0.
    """
    _validate_networth_form(currency)
    # The E2EE write boundary, before a single byte is read (docs/38 §8).
    _refuse_when_encrypted(current_user, "Importing a net-worth file")
    _reject_oversize_body(request)
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise api_error(413, "File exceeds the 5 MB limit", "file_too_large")

    def core() -> NetWorthCommitResult:
        try:
            prepared = parse_networth_file(
                raw,
                file_name=file.filename or "networth",
                account_name=(account_name or "").strip() or None,
            )
            plan, entries = build_plan(db, current_user.id, prepared, currency.upper())
            applied = apply_plan(db, current_user.id, entries)
        except NetWorthImportError as exc:
            raise api_error(exc.status, exc.detail, exc.code) from None
        return NetWorthCommitResult(**plan, created=applied["created"], reused=applied["reused"])

    return await blocking(core)()


@router.post("/upload", response_model=UploadResponse, status_code=201)
@human_paced_limit(IMPORT_UPLOAD_LIMIT)
async def upload(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="Statement export (OFX/QFX or CSV)")],
    account_id: Annotated[int, Form()],
    profile_json: Annotated[str | None, Form()] = None,
) -> UploadResponse:
    _reject_oversize_body(request)  # 1. Content-Length (cheapest, L6 partial)
    account = await blocking(owned_or_404)(Account, account_id, current_user, db)
    # M181: ownership first (a foreign id stays an indistinguishable 404), then
    # the E2EE write boundary — before a single byte of the file is read.
    _refuse_when_encrypted(current_user, "Importing a statement file")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:  # 2. byte cap
        raise api_error(413, "File exceeds the 5 MB limit", "file_too_large")

    # M149: parse + staging + commit are blocking work (docs/01 § "Sessions and
    # the event loop"), so the whole body after the read runs in a worker
    # thread. The session is used there sequentially — never concurrently —
    # which is the same contract get_current_user's hop already relies on.
    def core() -> UploadResponse:
        # Wizard-confirmed profile override beats the institution's stored one;
        # both paths share the dry run's resolver so preview == upload.
        context, institution_name = _resolve_context(db, account, profile_json)
        _, parsed = _parse_file(raw, context)  # 3./4. line+quote caps then parse (H2/L5)

        # Step 5 (row cap) and the whole staging loop now live in ONE place shared
        # with bank sync — `services/import_staging.stage_rows` (docs/03 + docs/39
        # § Ingestion parity). A file upload and a provider pull stage identically:
        # same dedup bridge, same cache-only CAD rule, same `import_batches`
        # lifecycle, same 2000-row cap (413 `too_many_rows` fires before any row
        # is written).
        staged = stage_rows(
            db,
            user=current_user,
            account=account,
            institution_name=institution_name,
            file_name=file.filename or "upload",
            rows=parsed,
        )
        # M71: near-duplicate verdicts (docs/03). `stage_rows` already reported the
        # exact-hash duplicates; every OTHER row is compared against committed AND
        # staged rows — including the other rows of this file — and flagged, never
        # dropped. RowKey.index is the staged-row position, so preview row i pairs
        # with `near[i]`.
        near = find_near_duplicates(
            db,
            account.id,
            [
                RowKey(
                    index=i,
                    date=row.raw.date,
                    amount_native=row.raw.amount_native,
                    currency=row.currency,
                    payee=row.raw.raw_description,
                    id=row.txn_id,
                )
                for i, row in enumerate(staged.rows)
                if not row.duplicate
            ],
        )
        preview = [
            PreviewRow(
                id=row.txn_id,
                date=row.raw.date,
                amount_native=row.raw.amount_native,
                currency=row.currency,
                raw_description=row.raw.raw_description,
                duplicate=row.duplicate,
                cad_amount=row.cad_amount,
                near_duplicate=near.get(i) is not None,
                near_duplicate_match=_near_public(near.get(i)),
            )
            for i, row in enumerate(staged.rows)
        ]
        db.commit()
        db.refresh(staged.batch)
        batch_public = ImportBatchPublic.model_validate(staged.batch)
        return UploadResponse(**batch_public.model_dump(), preview=preview)

    return await blocking(core)()


@router.get("", response_model=ImportBatchPage)
def list_batches(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
    account_id: int | None = Query(default=None),
) -> ImportBatchPage:
    """Batch history for the Imports page (tenancy via ImportBatch.user_id)."""
    stmt = (
        select(ImportBatch)
        .where(ImportBatch.user_id == current_user.id)
        .order_by(ImportBatch.id.desc())
    )
    if account_id is not None:
        stmt = stmt.where(ImportBatch.account_id == account_id)
    rows, total = paginate(db, stmt, limit=pagination.limit, offset=pagination.offset)
    return ImportBatchPage(
        items=[ImportBatchPublic.model_validate(b) for b in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/{batch_id}", response_model=ImportBatchPublic)
def get_batch(
    batch_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ImportBatch:
    return owned_or_404(ImportBatch, batch_id, current_user, db)


@router.get("/{batch_id}/rows", response_model=list[PreviewRow])
def get_batch_rows(
    batch_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[PreviewRow]:
    """Staged rows of a batch for review/commit.

    Pending rows are excluded from the transactions list (live scope, docs/03),
    so this is the only window into them — used by the Imports UI to open a
    pending_review batch for review after the upload session has ended.
    """
    batch = owned_or_404(ImportBatch, batch_id, current_user, db)
    rows = db.exec(
        select(Transaction).where(Transaction.import_batch_id == batch.id).order_by(Transaction.id)
    ).all()
    # Re-opening a pending batch re-runs the near-duplicate verdict so the
    # flag survives the upload session (docs/03). Exact duplicates keep
    # verdict precedence and are never re-labelled.
    near = find_near_duplicates(
        db,
        batch.account_id,
        [
            RowKey(
                index=i,
                date=t.date,
                amount_native=t.amount_native,
                currency=t.currency,
                payee=t.raw_description,
                id=t.id,
            )
            for i, t in enumerate(rows)
            if not t.is_duplicate
        ],
    )
    out: list[PreviewRow] = []
    for i, t in enumerate(rows):
        match = near.get(i)
        out.append(
            PreviewRow(
                id=t.id,
                date=t.date,
                amount_native=t.amount_native,
                currency=t.currency,
                raw_description=t.raw_description,
                duplicate=t.is_duplicate,
                cad_amount=t.cad_amount,
                near_duplicate=match is not None,
                near_duplicate_match=_near_public(match),
            )
        )
    return out


def _batch_security_ids(db: Session, batch_id: int) -> list[int]:
    """Distinct securities the batch staged trades for (M147 enrichment
    candidates — deduped so one ticker appearing in 40 rows queues once)."""
    return [
        row[0]
        for row in db.execute(
            select(Trade.security_id)
            .where(Trade.import_batch_id == batch_id, Trade.security_id.is_not(None))
            .distinct()
        ).all()
    ]


def _batch_transaction_ids(db: Session, batch_id: int) -> list[int]:
    return [
        row[0]
        for row in db.execute(
            select(Transaction.id).where(Transaction.import_batch_id == batch_id)
        ).all()
    ]


def _purge_batch_rows(db: Session, batch: ImportBatch) -> None:
    """Delete everything the batch staged — transactions AND trades (docs/03).

    Both row kinds are the user's data, and `import_batches` rows are never
    deleted, so nothing cascades: a delete that only touches `transactions`
    leaves the batch's trades linked to a `reverted`/`discarded` batch forever
    (M112 — prod batch 176 reverted with 71 trades still in `trades`, reachable
    from every trades surface while being invisible to the staging filters).

    Children come first (`services.txn_children`, the docs/29 F3 child set) —
    tag links, LLM suggestions and reconciliation matches hold non-cascading
    FKs, so skipping them is an IntegrityError, not a silent orphan. Any paired
    `transfer_pair_id` unlinks itself (ON DELETE SET NULL, migration 0010).

    Trades feed `acb_pools`, so a non-zero trades delete rebuilds them inside
    this same transaction (docs/02: `rebuild_user_pools` is the only write path
    and never commits) — otherwise the cache keeps the pre-revert numbers and a
    non-registered account shows a stale `book_cost_cad`.
    """
    txn_ids = _batch_transaction_ids(db, batch.id)
    unlink_transaction_children(db, txn_ids, user_id=batch.user_id)
    if txn_ids:
        db.execute(sa_delete(Transaction).where(Transaction.id.in_(txn_ids)))
    trades_deleted = db.execute(sa_delete(Trade).where(Trade.import_batch_id == batch.id)).rowcount
    if trades_deleted:
        rebuild_user_pools(db, batch.user_id)


@router.post("/{batch_id}/commit", response_model=ImportBatchPublic)
@human_paced_limit(IMPORT_MUTATE_LIMIT)
def commit_batch(
    request: Request,
    batch_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    background_tasks: BackgroundTasks,
    body: CommitBatchBody | None = None,
) -> ImportBatch:
    batch = owned_or_404(ImportBatch, batch_id, current_user, db)
    if batch.status != ImportBatchStatus.pending_review:
        raise api_error(
            409,
            f"Batch is {batch.status}, only pending_review batches can be committed",
            "batch_not_pending_review",
        )
    # M181: the enrol-after-staging hole. A batch staged while the tenant was
    # ordinary must not be released into the ledger once the flag is on — the
    # staged rows are plaintext, and committing only flips the batch to
    # committed. `discard`/`revert` stay open (removal only).
    _refuse_when_encrypted(current_user, "Committing a staged import batch")
    rows = db.exec(select(Transaction).where(Transaction.import_batch_id == batch.id)).all()
    include = set(body.include_ids) if body else set()
    for r in rows:
        if r.is_duplicate:
            r.force_include = r.id in include
    delete_ids = [r.id for r in rows if r.is_duplicate and not r.force_include]
    if delete_ids:
        unlink_transaction_children(db, delete_ids, user_id=batch.user_id)
        db.execute(sa_delete(Transaction).where(Transaction.id.in_(delete_ids)))
    dup_total = sum(1 for r in rows if r.is_duplicate)
    forced = sum(1 for r in rows if r.is_duplicate and r.id in include)
    total = len(rows)
    batch.duplicates_skipped = dup_total - forced
    batch.imported = total - (dup_total - forced)
    batch.status = ImportBatchStatus.committed  # immutable thereafter (docs/03)
    db.commit()
    db.refresh(batch)
    # M147: any fund this batch introduced (or already held) with ZERO
    # composition rows gets its enrichment scheduled in the background —
    # same provider chain as the manual import endpoint (docs/05). The
    # task degrades silently, so a commit never fails on market data.
    fund_enrichment.enqueue(db, background_tasks, batch.user_id, _batch_security_ids(db, batch.id))
    return batch


@router.post("/{batch_id}/discard", response_model=ImportBatchPublic)
@human_paced_limit(IMPORT_MUTATE_LIMIT)
def discard_batch(
    request: Request,
    batch_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ImportBatch:
    batch = owned_or_404(ImportBatch, batch_id, current_user, db)
    if batch.status != ImportBatchStatus.pending_review:
        raise api_error(
            409,
            f"Batch is {batch.status}, only pending_review batches can be discarded",
            "batch_not_pending_review",
        )
    _purge_batch_rows(db, batch)
    batch.status = ImportBatchStatus.discarded
    db.commit()
    db.refresh(batch)
    return batch


@router.post("/{batch_id}/revert", response_model=ImportBatchPublic)
@human_paced_limit(IMPORT_MUTATE_LIMIT)
def revert_batch(
    request: Request,
    batch_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ImportBatch:
    batch = owned_or_404(ImportBatch, batch_id, current_user, db)
    if batch.status != ImportBatchStatus.committed:
        raise api_error(
            409,
            f"Batch is {batch.status}; only committed batches can be reverted",
            "batch_not_committed",
        )
    # Delete the batch's transactions AND trades — both are the data the user
    # is taking back (docs/03 §Staged-review flow). The ON DELETE SET NULL on
    # transfer_pair_id (migration 0010) unlinks any paired transactions.
    _purge_batch_rows(db, batch)
    batch.status = ImportBatchStatus.reverted
    db.commit()
    db.refresh(batch)
    return batch

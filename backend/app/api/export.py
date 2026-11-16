"""export — whole-tenant JSON snapshot, ZIP bundle, OFX statement, schedule.

Three read paths plus a schedule, all caller-scoped and all pure Postgres:

- `GET /export/full` — the machine snapshot (docs/11 step 2): every table in
  the model registry scoped to the caller as one JSON object keyed by table
  name. The table set is DERIVED from `app.models` / `SQLModel.metadata` —
  never a hand-maintained list — minus the explicit EXCLUDED_TABLES dict
  below, so tables added after migration M7 (budgets, goals, debts, tags,
  etf_holdings, fundamentals, ...) export automatically (audit M13: the old
  pinned list silently dropped 14 of them, and this payload is also what the
  encrypted browser backup stores — docs/33's lost-passphrase recovery
  guarantee, so an omitted table was unrecoverable). Primary and foreign keys
  are preserved verbatim so the snapshot can be re-imported (`POST
  /backup/restore`).

- `GET /export/zip` — the one-click export (M75, docs/11 § "One-click
  export"): the SAME registry-derived coverage rendered as one ZIP of CSVs
  plus a JSON manifest, but under the app's VISIBILITY rules — committed rows
  only, staged (pending-review / discarded / reverted) rows excluded. The
  split is deliberate: `/export/full` must keep every row because a backup
  restores what the database holds, while the file a human carries away must
  match what that human can see in the app (docs/03 staging).

- `GET /export/ofx` — one account's committed transactions as an OFX 1.02
  statement that round-trips through our own importer (docs/03) with zero
  net-new rows after dedup.

- `GET|PUT /export/schedule` — the scheduled-export schedule (M75), the M56
  pattern: five `users` columns that roam across devices, enforced again by
  DB CHECKs (docs/11 § "Scheduled export").

Scoping: a table with a `user_id` column filters on it, a junction table
filters through the parent row that owns it, and fx_rates ships UNFILTERED
(shared reference cache with no user_id — docs/02 tenancy exception).
refresh_tokens and mcp_tokens are never exported; hashed_password and the
write-only llm_api_key are stripped from the caller's user row. No LLM or
market-data dependency anywhere in here, so every path works under privacy
mode, and all four are reads, so the demo tenant can use them (the demo write
guard covers mutations only).
"""

import json
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Response
from sqlalchemy import Select, or_
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlmodel import SQLModel, select

import app.models as model_package
from app.api.deps import get_current_user, get_db, owned_or_404
from app.core.errors import api_error
from app.models import (
    BillReminder,
    BillReminderMatchTerm,
    Rule,
    RuleTag,
    Transaction,
    TransactionEvent,
    TransactionReceipt,
    TransactionTag,
)
from app.models.accounts import Account
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.trades import Trade
from app.models.users import ExportFrequency, User
from app.schemas.export import ScheduleRead, ScheduleUpdate
from app.services.export_bundle import (
    MANIFEST_NAME,
    build_zip,
    export_zip_filename,
    render_csv,
    render_manifest,
    render_ofx,
    slugify,
    validate_vault,
)
from app.services.trade_filters import committed_trade_conditions
from app.services.txn_filters import committed_tx_conditions, identity_tx_conditions

router = APIRouter(prefix="/export", tags=["export"])

# The COMPLETE, reviewed exclusion list — everything not named here ships, so
# a table added to the models package reaches the export automatically.
# Every entry is an auth secret, live security state, or alembic bookkeeping
# (which the registry never contains). Adding an entry is a deliberate,
# reviewable decision: tests/test_export_registry.py pins this exact set, so a
# new exclusion cannot land silently next to a new model.
EXCLUDED_TABLES: dict[str, str] = {
    "refresh_tokens": "SHA-256 refresh-token hashes are auth secrets (docs/11 step 2)",
    "backup_artifacts": (
        "client-side-encrypted backup blobs (M56, docs/11) — exporting them "
        "would nest every backup inside the next one and grow without bound; "
        "they are the artifact store, not domain data"
    ),
    "mcp_tokens": "MCP bearer-token hashes are auth secrets (docs/11 step 2)",
    "alembic_version": "alembic bookkeeping — not a model, listed for completeness",
    # M74 (docs/38): second-factor + SSO rows are auth plumbing, not content.
    # The TOTP seed (Fernet ciphertext under JWT_SECRET) and recovery-code
    # hashes are credentials; passkey rows carry BYTEA key material and an
    # oidc_links snapshot would replay a link the owner severed. None of it
    # leaves the server, and a restore keeps the LIVE rows instead (docs/11).
    "totp_factors": "TOTP seeds are second-factor secrets — never leave the server (docs/38)",
    "recovery_codes": "single-use recovery-code hashes are auth secrets (docs/38)",
    "webauthn_credentials": "passkey key material + credential state, not content (docs/38)",
    "oidc_links": "SSO identity links are live security state, not content (docs/38)",
    # M168 (docs/07 §Provider registry): coverage rows are ISSUER verdicts
    # about the world, shared by every tenant — server-side configuration,
    # not the caller's content. `fx_rates` ships whole because a restore
    # gap-fills it into the restored tenant's own picture; this table is
    # rebuilt from migration `0050` + discovery instead, and letting a
    # snapshot write it would hand every tenant a way to edit the fund
    # registry the whole server reads (app/api/backup.py refuses the key).
    "provider_fund_registry": (
        "operator-level fund-coverage registry (M168, docs/07) — issuer "
        "verdicts shared by every tenant, not the caller's content"
    ),
    # M173 (docs/28 decision 5): the benchmark price series is market data
    # shared by every tenant (`fx_rates`' operator level). Excluded for the
    # same reason as the registry above — accepting it in a restore would let
    # one tenant's crafted snapshot write the closes every tenant's
    # vs-Benchmark panel then reports as the market's own answer.
    "benchmark_prices": (
        "shared benchmark price series (M173, docs/28) — market data, not the caller's content"
    ),
}

# Exported UNFILTERED: shared reference data with no user_id (docs/02 tenancy
# exception). Only fx_rates qualifies; every other table is caller-scoped.
UNSCOPED_TABLES: frozenset[str] = frozenset({"fx_rates"})

# Junction tables carry no user_id, so each is scoped through the parent row
# that owns it: table -> (child FK column, parent model). Shipping any one of
# them whole would leak one tenant's links into another tenant's snapshot.
VIA_PARENT: dict[str, tuple[InstrumentedAttribute, type[SQLModel]]] = {
    "rule_tags": (cast(InstrumentedAttribute, RuleTag.rule_id), Rule),
    "transaction_tags": (
        cast(InstrumentedAttribute, TransactionTag.transaction_id),
        Transaction,
    ),
    # M121 (docs/25 §M121, docs/11): a bill's term list is tenant data too —
    # scoped through `bill_reminders`, the row that owns it, so an export can
    # never ship one tenant's match terms inside another's snapshot.
    "bill_reminder_match_terms": (
        cast(InstrumentedAttribute, BillReminderMatchTerm.bill_reminder_id),
        BillReminder,
    ),
}

# What `/export/zip` filters out. Published inside the manifest so the file
# documents its own population (docs/11 § visibility), and so /help can quote
# the same list instead of paraphrasing it.
VISIBILITY_FILTERS: dict[str, str] = {
    "transactions": "committed batches + manual rows only (staged rows excluded)",
    "trades": "committed batches + manual rows only (staged rows excluded)",
    "import_batches": "status = committed only",
    "transaction_tags": "tags of committed/manual transactions only",
    # M106 (docs/02 §transaction_events): a staged row's receipts are as
    # invisible as the row itself — the file a human carries away must never
    # narrate rows that page does not show (docs/03 staging). Receipts of rows
    # that no longer EXIST still ship: the `deleted` receipt is the one event
    # whose whole purpose is to outlive its subject.
    "transaction_events": (
        "receipts of committed/manual transactions + receipts whose row is gone"
    ),
}

# Columns never written to a CSV: the auth secrets `/export/full` strips from
# the user row (docs/11 step 2). Dropped from the header too, so the manifest
# never advertises a column that is always empty.
STRIPPED_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": ("hashed_password", "llm_api_key"),
}


def _column(model: type[SQLModel], name: str) -> InstrumentedAttribute:
    """SQLAlchemy column attribute for a mapped model field.

    Reached through getattr because SQLModel's Pydantic typing presents class
    fields as plain Python types while they are SQLAlchemy columns at runtime.
    """
    return cast(InstrumentedAttribute, getattr(model, name))


def _model_registry() -> dict[str, type[SQLModel]]:
    """table name -> SQLModel class for every table the ORM actually maps.

    `app.models` maps every model into SQLModel.metadata on import (docs/01),
    so enumerating the package can never drift from the schema: a model added
    by a later migration is exported the day it lands. Membership in
    `SQLModel.metadata.tables` filters out table-less SQLModel bases such as
    TimestampMixin, which claim a `__tablename__` without a table.
    """
    registry: dict[str, type[SQLModel]] = {}
    for attribute in dir(model_package):
        candidate = getattr(model_package, attribute)
        if not isinstance(candidate, type) or not issubclass(candidate, SQLModel):
            continue
        table_name = getattr(candidate, "__tablename__", None)
        if isinstance(table_name, str) and table_name in SQLModel.metadata.tables:
            registry[table_name] = candidate
    return registry


_MODELS: dict[str, type[SQLModel]] = _model_registry()

# Fail-closed drift guard: a table SQLAlchemy knows about but that no
# importable model class resolves to could not be selected, so it would be
# silently missing from every export — the exact defect M13 flagged.
_UNRESOLVED_TABLES: frozenset[str] = frozenset(SQLModel.metadata.tables) - frozenset(_MODELS)


def _jsonable(row: object) -> dict:
    # mode="json" renders datetimes as ISO-8601, enums as values, and Decimals
    # as exact strings — full ledger precision survives the round-trip.
    dumped = row.model_dump(mode="json")
    return dumped


def _visibility_filter(statement: Select, table_name: str, user_id: int) -> Select:
    """Apply the app's committed-only read scope to one table's statement.

    Single source: `services/txn_filters.py::committed_tx_conditions` and
    `services/trade_filters.py::committed_trade_conditions` — the same
    predicates every live read uses (docs/03 staging, docs/29 F1). The ZIP can
    therefore never disagree with the Transactions page about which rows
    exist; anything not yet committed simply is not in the file.
    """
    if table_name == "transactions":
        return statement.outerjoin(
            ImportBatch, Transaction.import_batch_id == ImportBatch.id
        ).where(*committed_tx_conditions(user_id))
    if table_name == "trades":
        return statement.outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id).where(
            *committed_trade_conditions(user_id)
        )
    if table_name == "import_batches":
        return statement.where(ImportBatch.status == ImportBatchStatus.committed)
    if table_name == "transaction_receipts":
        # M108 P4: a receipt image on a staged row is as invisible as the row.
        # Same shape as `transaction_tags` above — one subquery, one predicate.
        visible_transactions = (
            select(Transaction.id)
            .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
            .where(*committed_tx_conditions(user_id))
        )
        return statement.where(TransactionReceipt.transaction_id.in_(visible_transactions))
    if table_name == "transaction_tags":
        # A staged transaction's tag links are as invisible as the row itself.
        visible_transactions = (
            select(Transaction.id)
            .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
            .where(*committed_tx_conditions(user_id))
        )
        return statement.where(TransactionTag.transaction_id.in_(visible_transactions))
    if table_name == "transaction_events":
        # M106: a staged row's receipts are as invisible as the row itself.
        # `tx_id` carries no FK, so the predicate is written out here instead
        # of riding a join the way `transaction_tags` does.
        #
        # The OR arm is the point of the whole table: a receipt whose row is
        # GONE (the user deleted it) still ships. Otherwise `deleted` — the
        # one event whose entire purpose is to be the surviving witness —
        # would be the single receipt the file drops, and "edits never
        # silently vanish" would quietly stop at DELETE.
        visible_transactions = (
            select(Transaction.id)
            .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
            .where(*committed_tx_conditions(user_id))
        )
        # "Gone" is global, not per-tenant: `tx_id` carries no FK (docs/02), so
        # a receipt naming a row that still EXISTS somewhere — this tenant's
        # staged row (arm 1 says no) or any other tenant's row (this arm says
        # no) — is not a surviving witness and must not ship. Reading arm 2 as
        # "not MY transactions" would export a foreign receipt into a file
        # that also carries its own tenant's rows, and docs/11's prose says
        # "receipts whose row no longer exists", which is this predicate.
        all_transactions = select(Transaction.id)
        return statement.where(
            or_(
                TransactionEvent.tx_id.in_(visible_transactions),
                TransactionEvent.tx_id.not_in(all_transactions),
            )
        )
    return statement


def collect_rows(
    db: Session,
    user: User,
    *,
    committed_only: bool,
) -> dict[str, list[dict]]:
    """table name -> JSON rows for every exported table, caller-scoped.

    `committed_only=False` is the full snapshot behind `/export/full` and the
    encrypted backup (byte-identical payload to the pre-M75 endpoint — the
    restore round trip depends on that). `committed_only=True` is the
    visibility-respecting population behind `/export/zip`.
    """
    if _UNRESOLVED_TABLES:
        raise api_error(
            500,
            f"Model-registry drift: no model class resolves for table(s) "
            f"{sorted(_UNRESOLVED_TABLES)}; they cannot be exported",
            "export_registry_drift",
        )

    snapshot: dict[str, list[dict]] = {}

    user_row = _jsonable(user)
    # Auth secrets never leave the server (docs/11 step 2).
    user_row.pop("hashed_password", None)
    user_row.pop("llm_api_key", None)
    snapshot["users"] = [user_row]

    for table_name in sorted(_MODELS):
        if table_name == "users" or table_name in EXCLUDED_TABLES:
            continue  # users: emitted above with secrets stripped; exclusions: auth secrets
        model = _MODELS[table_name]
        statement = select(model)  # type: ignore[var-annotated]
        if "user_id" in model.model_fields:
            statement = statement.where(_column(model, "user_id") == user.id)
        elif table_name in UNSCOPED_TABLES:
            pass  # shared reference cache — whole table by design (docs/02)
        elif table_name in VIA_PARENT:
            fk_column, parent = VIA_PARENT[table_name]
            parent_ids = select(_column(parent, "id")).where(_column(parent, "user_id") == user.id)
            statement = statement.where(fk_column.in_(parent_ids))
        else:
            # Fail closed: a table with no scoping rule must never ship whole.
            raise api_error(
                500,
                f"No scoping rule for table {table_name!r}; refusing to export it unfiltered",
                "export_missing_scoping",
            )
        if committed_only:
            statement = _visibility_filter(statement, table_name, user.id)  # type: ignore[arg-type]
        statement = statement.order_by(_column(model, "id"))
        rows = db.exec(statement).all()  # type: ignore[attr-defined]
        snapshot[table_name] = [_jsonable(row) for row in rows]

    return snapshot


def _columns(table_name: str) -> list[str]:
    """CSV header: the model's own field order (which is its column order).

    Auth secrets are dropped from the header, not merely blanked: the manifest
    advertises these columns, and a column that is always empty is a lie about
    the file's schema (docs/11 step 2 strips `hashed_password`/`llm_api_key`
    from the user row for the same reason).
    """
    stripped = STRIPPED_COLUMNS.get(table_name, ())
    return [name for name in _MODELS[table_name].model_fields if name not in stripped]


@router.get("/full")
def export_full(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    snapshot = collect_rows(db, current_user, committed_only=False)

    today = datetime.now(UTC).date().isoformat()
    return Response(
        content=json.dumps(snapshot),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="fin-tracker-export-{today}.json"'},
    )


@router.get("/zip")
def export_zip(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    vault: str = "locked",
) -> Response:
    """One-click full export: one ZIP of one CSV per table + a JSON manifest.

    `vault` is the session's vault state as the caller knows it (docs/11 §
    "Export & the vault"): `locked` — the DEFAULT, fail closed — withholds a
    cipher-backed field's plaintext as `E2EE:<ciphertext>`; `unlocked` ships
    the stored plaintext. Defaulting to `locked` means a client that never
    says can never turn an opted-in E2EE user's first export into a silent
    plaintext dump of exactly the fields the app refuses to render while
    locked (docs/33 A16).
    """
    validate_vault(vault)

    snapshot = collect_rows(db, current_user, committed_only=True)

    entries: list[tuple[str, bytes]] = []
    files: list[dict] = []
    masked_cells = 0
    for table_name in sorted(snapshot):
        columns = _columns(table_name)
        payload, masked = render_csv(table_name, columns, snapshot[table_name], vault)
        masked_cells += masked
        entries.append((f"{table_name}.csv", payload))
        files.append(
            {
                "name": f"{table_name}.csv",
                "table": table_name,
                "rows": len(snapshot[table_name]),
                "columns": columns,
            }
        )

    manifest = render_manifest(
        generated_at=datetime.now(UTC),
        user_id=current_user.id,
        email=current_user.email,
        vault=vault,
        visibility="committed",
        files=files,
        masked_cells=masked_cells,
        excluded_tables=EXCLUDED_TABLES,
        filters=VISIBILITY_FILTERS,
    )
    entries.insert(0, (MANIFEST_NAME, manifest))

    return Response(
        content=build_zip(entries),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{export_zip_filename()}"',
            "X-Export-Masked-Cells": str(masked_cells),
        },
    )


@router.get("/ofx")
def export_ofx(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    account_id: int,
    vault: str = "locked",
) -> Response:
    """One account's committed transactions as an OFX 1.02 statement.

    One statement per account on purpose: dedup is keyed by `(account_id,
    dedup_hash)`, so a file mixing accounts could never re-import clean. The
    round trip is pinned by `tests/test_export_ofx.py` (0 net-new rows after
    dedup), and the description written to `<NAME>` is the raw_description
    collapsed to single spaces — whitespace-neutral for `build_dedup_hash`.
    """
    validate_vault(vault)
    account = owned_or_404(Account, account_id, current_user, db)

    statement = (
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        # M107 (docs/44 §7): IDENTITY — the statement keeps its LINE COUNT, so
        # a split's parent re-imports as `0 net-new` and the children (whose
        # hashes are salted) never reach the file at all.
        .where(*identity_tx_conditions(current_user.id))
        .where(Transaction.account_id == account.id)
        .order_by(Transaction.date, Transaction.id)
    )
    rows = [_jsonable(row) for row in db.exec(statement).all()]  # type: ignore[attr-defined]

    payload = render_ofx(
        account_id=account.id,
        account_type=str(account.type),
        currency=account.currency,
        rows=rows,
        vault=vault,
    )
    filename = f"fin-tracker-{slugify(account.name, f'account-{account.id}')}.ofx"
    return Response(
        content=payload,
        media_type="application/x-ofx",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _schedule_read(user: User) -> ScheduleRead:
    return ScheduleRead(
        frequency=user.export_frequency,
        hour=user.export_hour,
        weekday=user.export_weekday,
        day_of_month=user.export_day_of_month,
        keep_last_n=user.export_keep_last_n,
    )


@router.get("/schedule", response_model=ScheduleRead)
def get_schedule(
    current_user: Annotated[User, Depends(get_current_user)],
) -> ScheduleRead:
    return _schedule_read(current_user)


@router.put("/schedule", response_model=ScheduleRead)
def put_schedule(
    payload: ScheduleUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ScheduleRead:
    # Range/enum checks live HERE (not in Pydantic) so the envelope carries
    # `export_invalid_schedule`, the code docs/11 pins for this endpoint —
    # deliberately the M56 shape (docs/11 § Scheduled export), so the two
    # schedules behave identically for a reader moving between them.
    valid_frequencies = {frequency.value for frequency in ExportFrequency}
    if payload.frequency not in valid_frequencies:
        raise api_error(
            422,
            f"export_frequency {payload.frequency!r} is not one of {sorted(valid_frequencies)}",
            "export_invalid_schedule",
        )
    ranges = (
        ("export_hour", payload.hour, 0, 23),
        ("export_weekday", payload.weekday, 0, 6),
        ("export_day_of_month", payload.day_of_month, 1, 28),
        ("export_keep_last_n", payload.keep_last_n, 1, 20),
    )
    for name, value, low, high in ranges:
        if not low <= value <= high:
            raise api_error(
                422,
                f"{name} must be between {low} and {high}, got {value}",
                "export_invalid_schedule",
            )

    current_user.export_frequency = ExportFrequency(payload.frequency)
    current_user.export_hour = payload.hour
    current_user.export_weekday = payload.weekday
    current_user.export_day_of_month = payload.day_of_month
    current_user.export_keep_last_n = payload.keep_last_n
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return _schedule_read(current_user)

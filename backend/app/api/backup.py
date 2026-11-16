"""backup — scheduled client-side-encrypted backup + restore (M56, docs/11).

The server's job here is custody of opaque ciphertext plus schedule and
retention bookkeeping. It never sees a passphrase, KEK or DEK, and it never
decodes `ciphertext` — the artifact is AES-GCM under the caller's vault DEK,
encrypted in the browser before upload (docs/33 key-holding model). A restore
therefore posts the *decrypted* payload: the server cannot do that step for
anyone.

Contract: docs/11 § "Scheduled backup & restore (M56)" — every failure is a
`{detail, code}` envelope, tenancy failures are 404 `not_found` with no
existence hint, and ALL restore validation happens before the first write, so
a rejected restore leaves the tenant untouched.
"""

import logging
import re
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete as sa_delete
from sqlalchemy import desc, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlmodel import SQLModel, select

from app.api.deps import get_current_user, get_db
from app.api.export import _MODELS, EXCLUDED_TABLES
from app.api.users import delete_user_rows
from app.core.errors import api_error
from app.models import (
    FxRate,
    McpToken,
    OidcLink,
    RecoveryCode,
    RefreshToken,
    TotpFactor,
    WebAuthnCredential,
)
from app.models.backup import BackupArtifact
from app.models.users import BackupFrequency, User
from app.schemas.backup import (
    ArtifactCreate,
    ArtifactDetail,
    ArtifactList,
    ArtifactPublic,
    RestoreRequest,
    RestoreResponse,
    ScheduleRead,
    ScheduleUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backup", tags=["backup"])

# Pinned in docs/11: the artifact must fit under nginx's
# `client_max_body_size 10m` (frontend/Dockerfile). Raise both together.
MAX_ARTIFACT_CHARS = 8_000_000

# daily `2026-09-28`, weekly ISO week `2026-W39`, monthly `2026-09`.
PERIOD_RE = re.compile(r"^\d{4}-(?:\d{2}-\d{2}|W\d{2}|\d{2})$")

KEEP_LAST_N_RANGE = (1, 20)

# The restore confirmation literal (docs/11): a typed word, so a stray POST
# cannot wipe a tenant by accident.
RESTORE_CONFIRM = "restore"
RESTORE_VERSION = 1
RESTORE_KIND = "fintrack-backup"

# Never restored — live identity / security state, not content (docs/11):
# `users` is the row you restore INTO; replaying an old `vault_envelopes`
# snapshot would un-revoke a revoked device (docs/33 revoke runbook).
RESTORE_SKIP_TABLES = ("users", "vault_envelopes")

# Live auth state a restore must PRESERVE, never wipe-and-replace: signing
# the user out, killing MCP access, or silently dropping their second factor
# / passkeys / SSO link would turn a data restore into a security downgrade.
# Kept rows are exactly the exportable set's complement here — the four M74
# tables (docs/38) are excluded from the snapshot (app/api/export.py
# EXCLUDED_TABLES), so keeping them is the ONLY way they survive a restore.
RESTORE_KEEP_MODELS: tuple[type, ...] = (
    RefreshToken,
    McpToken,
    TotpFactor,
    RecoveryCode,
    WebAuthnCredential,
    OidcLink,
)

# Never accepted inside a snapshot: auth secrets, alembic bookkeeping, and the
# artifact store itself (a backup must not contain the backups). The M74
# tables join the auth secrets: a crafted snapshot carrying them would either
# inject second-factor material or replay a severed SSO link (docs/38).
RESTORE_FORBIDDEN_TABLES = frozenset(
    {
        "refresh_tokens",
        "mcp_tokens",
        "alembic_version",
        "backup_artifacts",
        "totp_factors",
        "recovery_codes",
        "webauthn_credentials",
        "oidc_links",
        # M168: operator-level coverage data (docs/07 §Provider registry).
        # Excluded from the snapshot, so it cannot be restored either — and
        # refusing it is not a formality: accepting it would let any tenant's
        # crafted payload overwrite the fund registry the WHOLE server reads.
        "provider_fund_registry",
        # M173 (docs/28 decision 5): the shared benchmark price series —
        # excluded from the snapshot (app/api/export.py), so a snapshot
        # carrying it is refused rather than allowed to rewrite the closes
        # every tenant's vs-Benchmark panel reads.
        "benchmark_prices",
    }
)

# Every table `GET /export/full` can emit — what a complete snapshot covers.
EXPORTABLE_TABLES = frozenset(_MODELS) - frozenset(EXCLUDED_TABLES)


def _period_is_real(period: str) -> bool:
    """Shape AND calendar — `2026-13-99` matches the regex but is no date.

    A period is the artifact's idempotency key (one row per period), so a
    value the scheduler can never regenerate would silently never match.
    """
    if not PERIOD_RE.match(period):
        return False
    try:
        if "W" in period:  # weekly ISO week: 2026-W39
            year, week = period.split("-W")
            date.fromisocalendar(int(year), int(week), 1)
        elif len(period) == 10:  # daily: 2026-09-28
            date.fromisoformat(period)
        else:  # monthly: 2026-09
            date.fromisoformat(f"{period}-01")
    except ValueError:
        return False
    return True


def _schedule_read(user: User) -> ScheduleRead:
    return ScheduleRead(
        frequency=user.backup_frequency,
        hour=user.backup_hour,
        weekday=user.backup_weekday,
        day_of_month=user.backup_day_of_month,
        keep_last_n=user.backup_keep_last_n,
    )


def _owned_artifact(db: Session, artifact_id: int, user: User) -> BackupArtifact:
    row = db.get(BackupArtifact, artifact_id)
    # Foreign and unknown ids are deliberately indistinguishable (404 both).
    if row is None or row.user_id != user.id:
        raise api_error(404, "Backup artifact not found", "not_found")
    return row


def _prune(db: Session, user: User) -> None:
    """Keep the newest `keep_last_n` artifacts; drop the rest (docs/11).

    Deletion is permanent — retention is the only automatic delete in this
    feature, and it only ever touches `backup_artifacts`.
    """
    keep = user.backup_keep_last_n
    rows = list(
        db.exec(
            select(BackupArtifact)
            .where(BackupArtifact.user_id == user.id)
            .order_by(desc(BackupArtifact.created_at), desc(BackupArtifact.id))
        ).all()
    )
    doomed = [row.id for row in rows[keep:] if row.id is not None]
    if doomed:
        db.exec(sa_delete(BackupArtifact).where(BackupArtifact.id.in_(doomed)))


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
    # `backup_invalid_schedule`, the code docs/11 pins for this endpoint.
    valid_frequencies = {frequency.value for frequency in BackupFrequency}
    if payload.frequency not in valid_frequencies:
        raise api_error(
            422,
            f"backup_frequency {payload.frequency!r} is not one of {sorted(valid_frequencies)}",
            "backup_invalid_schedule",
        )
    ranges = (
        ("backup_hour", payload.hour, 0, 23),
        ("backup_weekday", payload.weekday, 0, 6),
        ("backup_day_of_month", payload.day_of_month, 1, 28),
        (
            "backup_keep_last_n",
            payload.keep_last_n,
            KEEP_LAST_N_RANGE[0],
            KEEP_LAST_N_RANGE[1],
        ),
    )
    for name, value, low, high in ranges:
        if not low <= value <= high:
            raise api_error(
                422,
                f"{name} must be between {low} and {high}, got {value}",
                "backup_invalid_schedule",
            )

    current_user.backup_frequency = BackupFrequency(payload.frequency)
    current_user.backup_hour = payload.hour
    current_user.backup_weekday = payload.weekday
    current_user.backup_day_of_month = payload.day_of_month
    current_user.backup_keep_last_n = payload.keep_last_n
    db.add(current_user)
    # Lowering keep_last_n prunes in the SAME transaction (docs/11 retention).
    _prune(db, current_user)
    db.commit()
    db.refresh(current_user)
    return _schedule_read(current_user)


@router.get("/artifacts", response_model=ArtifactList)
def list_artifacts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ArtifactList:
    # Metadata only: a list must stay cheap when each artifact is megabytes.
    rows = db.exec(
        select(BackupArtifact)
        .where(BackupArtifact.user_id == current_user.id)
        .order_by(desc(BackupArtifact.created_at), desc(BackupArtifact.id))
    ).all()
    return ArtifactList(items=[ArtifactPublic.model_validate(row) for row in rows])


@router.post("/artifacts", status_code=201, response_model=ArtifactPublic)
def create_artifact(
    payload: ArtifactCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ArtifactPublic:
    if not _period_is_real(payload.period):
        raise api_error(
            422,
            f"period {payload.period!r} must be a real calendar date shaped like "
            "2026-09-28 (daily), 2026-W39 (weekly) or 2026-09 (monthly)",
            "backup_invalid_period",
        )
    # Size before content: a huge body fails on the cheap check first.
    if len(payload.ciphertext) > MAX_ARTIFACT_CHARS:
        raise api_error(
            422,
            f"ciphertext exceeds {MAX_ARTIFACT_CHARS} characters",
            "backup_too_large",
        )
    parts = payload.ciphertext.split(".")
    if len(parts) != 3 or parts[0] != "v1" or not parts[1] or not parts[2]:
        raise api_error(
            422,
            "ciphertext must be a v1.<base64(iv)>.<base64(ciphertext)> field envelope",
            "backup_invalid_envelope",
        )

    now = datetime.now(UTC)
    # Upsert on (user_id, period): the scheduler's idempotency key. Two open
    # tabs racing for the same period converge on ONE row, and "Back up now"
    # refreshes the current period instead of failing with a duplicate error.
    db.exec(
        pg_insert(BackupArtifact)
        .values(
            user_id=current_user.id,
            period=payload.period,
            ciphertext=payload.ciphertext,
            size_bytes=len(payload.ciphertext),
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "period"],
            set_={
                "ciphertext": payload.ciphertext,
                "size_bytes": len(payload.ciphertext),
                "created_at": now,
                "updated_at": now,
            },
        )
    )
    row = db.exec(
        select(BackupArtifact).where(
            BackupArtifact.user_id == current_user.id,
            BackupArtifact.period == payload.period,
        )
    ).one()
    _prune(db, current_user)
    db.commit()
    db.refresh(row)
    return ArtifactPublic.model_validate(row)


@router.get("/artifacts/{artifact_id}", response_model=ArtifactDetail)
def get_artifact(
    artifact_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ArtifactDetail:
    return ArtifactDetail.model_validate(_owned_artifact(db, artifact_id, current_user))


@router.delete("/artifacts/{artifact_id}", status_code=204)
def delete_artifact(
    artifact_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    row = _owned_artifact(db, artifact_id, current_user)
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.post("/restore", response_model=RestoreResponse)
def restore_backup(
    payload: RestoreRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RestoreResponse:
    if payload.confirm != RESTORE_CONFIRM:
        raise api_error(
            422,
            'confirm must be exactly "restore" — this replaces every domain row in the account',
            "restore_confirm_required",
        )

    body = payload.payload
    snapshot = body.snapshot
    if (
        body.v != RESTORE_VERSION
        or body.kind != RESTORE_KIND
        or not isinstance(snapshot, dict)
        or not snapshot
        or not all(isinstance(rows, list) for rows in snapshot.values())
    ):
        raise api_error(
            422,
            "not a fin-tracker backup payload: expected "
            '{v: 1, kind: "fintrack-backup", period, exported_at, snapshot: {table: [rows]}}',
            "restore_invalid_payload",
        )

    # ---- Phase 1: validate EVERYTHING before the first write -------------
    for table in snapshot:
        if table in RESTORE_FORBIDDEN_TABLES:
            raise api_error(422, f"table {table!r} is never restored", "restore_forbidden_table")
        if table not in _MODELS:
            raise api_error(
                422,
                f"unknown table {table!r} in snapshot — refusing to restore it",
                "restore_unknown_table",
            )

    insert_tables = [
        table for table in snapshot if table not in RESTORE_SKIP_TABLES and table != "fx_rates"
    ]
    order = _insert_order(insert_tables)

    validated: dict[str, list[Any]] = {}
    for table in order:
        model = _MODELS[table]
        built: list[Any] = []
        for index, row in enumerate(snapshot[table]):
            if not isinstance(row, dict):
                raise api_error(
                    422,
                    f"{table}[{index}] is not an object",
                    "restore_invalid_row",
                )
            data = dict(row)
            if "user_id" in data:
                # Re-home every row on the caller: a snapshot from an older
                # account (or a re-registration) must never land on someone
                # else's id, and FKs inside the snapshot never point at users.
                data["user_id"] = current_user.id
            try:
                built.append(model.model_validate(data))
            except PydanticValidationError as exc:
                first = exc.errors()[0]
                location = ".".join(str(part) for part in first["loc"])
                raise api_error(
                    422,
                    f"{table}[{index}].{location}: {first['msg']}",
                    "restore_invalid_row",
                ) from None
        validated[table] = built

    fx_rows = _validate_fx(snapshot.get("fx_rates", []))
    absent_tables = sorted(EXPORTABLE_TABLES - set(snapshot))

    # ---- Phase 2: one transaction — wipe, insert, gap-fill, fix sequences --
    try:
        # The shared FK-safe cascade (docs/20), keeping live auth state
        # (RESTORE_KEEP_MODELS) so a restore neither signs you out, kills MCP
        # access, nor drops a second factor / passkey / SSO link. `users`,
        # `vault_envelopes`, `fx_rates` and `backup_artifacts` are untouched.
        delete_user_rows(db, current_user, keep=RESTORE_KEEP_MODELS)
        for table in order:
            if validated[table]:
                db.add_all(validated[table])
                # Flush per table: SQLAlchemy's unit of work sorts mappers by
                # ITS OWN dependency graph (relationships, not FK metadata),
                # and these SQLModel tables declare none — one big flush at the
                # end would emit INSERTs in an arbitrary order and trip
                # `budgets.category_id` before `categories` lands. Flushing
                # here pins the documented FK-safe order; still one
                # transaction, so an IntegrityError rolls the whole wipe back.
                db.flush()
        fx_inserted = _gapfill_fx(db, fx_rows)
        db.flush()
        _advance_sequences(db, [table for table in order if validated[table]])
        db.commit()
    except IntegrityError as exc:
        # The envelope message is spec-pinned; the actual constraint is for
        # the operator log — a rejected restore is rare and always diagnostic.
        logger.warning("restore rejected (%s): %s", type(exc.orig).__name__, exc.orig)
        db.rollback()
        raise api_error(
            422,
            "snapshot rows violate a foreign-key, unique or NOT NULL "
            "constraint; nothing was changed",
            "restore_integrity_error",
        ) from exc

    return RestoreResponse(
        restored={table: len(rows) for table, rows in validated.items()},
        absent_tables=absent_tables,
        skipped_tables=[table for table in RESTORE_SKIP_TABLES if table in snapshot],
        fx_rates_inserted=fx_inserted,
    )


def _insert_order(tables: list[str]) -> list[str]:
    """FK-dependency order (parents first), deterministic alphabetical ties.

    Computed from `SQLModel.metadata` rather than a hand-kept list, so a table
    added by a future migration lands in a valid position automatically. A
    cycle cannot be ordered → fail closed before any write.
    """
    remaining = set(tables)
    deps: dict[str, set[str]] = {}
    metadata = SQLModel.metadata.tables
    for table in remaining:
        if table not in metadata:
            raise api_error(422, f"unknown table {table!r} in snapshot", "restore_unknown_table")
        deps[table] = {
            fk.column.table.name
            for fk in metadata[table].foreign_keys
            if fk.column.table.name in remaining and fk.column.table.name != table
        }
    order: list[str] = []
    while remaining:
        ready = sorted(table for table in remaining if deps[table] <= set(order))
        if not ready:
            cyclic = sorted(table for table in remaining if deps[table] & set(remaining))
            raise api_error(
                422,
                f"cannot order tables {cyclic} — foreign-key cycle in the snapshot",
                "restore_integrity_error",
            )
        order.extend(ready)
        remaining -= set(ready)
    return order


def _validate_fx(rows: list[Any]) -> list[dict[str, Any]]:
    """Coerce `fx_rates` rows for a gap-fill insert (id dropped, see below)."""
    table = FxRate.__tablename__
    columns = SQLModel.metadata.tables[table].columns
    values: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise api_error(422, f"fx_rates[{index}] is not an object", "restore_invalid_row")
        data = {key: value for key, value in row.items() if key in columns and key != "id"}
        try:
            parsed = FxRate.model_validate(data)
        except PydanticValidationError as exc:
            first = exc.errors()[0]
            location = ".".join(str(part) for part in first["loc"])
            raise api_error(
                422,
                f"fx_rates[{index}].{location}: {first['msg']}",
                "restore_invalid_row",
            ) from None
        # `id` is deliberately dropped: the shared cache keeps ITS serials, and
        # reusing snapshot ids would collide with rows that already exist.
        values.append({key: getattr(parsed, key) for key in data})
    return values


def _gapfill_fx(db: Session, rows: list[dict[str, Any]]) -> int:
    """Insert snapshot rates only where the shared cache has none.

    `ON CONFLICT (base, quote, date) DO NOTHING` = the standing fx rule in
    reverse: never wipe the shared table, never overwrite a row that is
    already there (manual rows keep winning, docs/02 / AGENTS §2).
    """
    if not rows:
        return 0
    # `RETURNING id`, not `rowcount`: psycopg/SQLAlchemy report -1 for the
    # batched executemany form, and a skipped row returns nothing — so the
    # count of returned ids IS the number of rows actually inserted.
    result = db.execute(
        pg_insert(FxRate)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["base", "quote", "date"])
        .returning(SQLModel.metadata.tables["fx_rates"].c.id)
    )
    return len(result.all())


def _advance_sequences(db: Session, tables: list[str]) -> None:
    """setval each `id` sequence to the restored MAX(id).

    Without this the next API write would reuse id 1 and collide with a
    restored primary key. Only runs for tables that actually received rows.
    """
    for table in tables:
        if "id" not in SQLModel.metadata.tables[table].columns:
            continue
        # `Session.exec` (SQLModel) takes no params; `Session.execute` does.
        sequence = db.execute(
            text("SELECT pg_get_serial_sequence(:table, 'id') AS seq"),
            {"table": table},
        ).one()[0]
        if not sequence:
            continue
        db.execute(
            # `:sequence::regclass` would parse as one broken token; CAST is
            # the unambiguous spelling. `table` is interpolated — it comes
            # from the fixed `_MODELS` registry above, never from the request.
            text(
                "SELECT setval(CAST(:sequence AS regclass), "  # noqa: S608 -- _MODELS registry above
                f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
            ),
            {"sequence": sequence},
        )

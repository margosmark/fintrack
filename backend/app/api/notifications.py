"""notifications — alert settings, the in-app inbox, delivery channels (docs/08, docs/35, docs/37).

The five M31 legacy `alert_type`s have NO generator and nothing reads their
`enabled` beyond this settings list (docs/35, "Plain statement of what is NOT
wired"). The three generator-backed types — `budget_threshold`, `bill_due`,
`tax_loss` — are the only sources the inbox unions.

Gating rule (docs/35 decision 3): budget generation runs UNCONDITIONALLY before
any toggle is consulted, so `spending_alerts` audit rows keep being written
while `budget_threshold` is OFF. The surface is gated, not the data.

M59 (docs/37) adds the outbound half: `notification_channels` (two disabled
transports per user), `delivery_enabled` as a second per-type switch, and the
auditable outbox behind `GET/POST /notifications/deliveries*`. Delivery never
runs inside a request — routes only configure, cancel and inspect; the worker
(or `POST /notifications/deliveries/run`) sends.
"""

import base64
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import MAX_OFFSET, get_current_user, get_db

# `_ET` comes from the generator so the inbox's "today" lower bound is the SAME
# boundary `_check_alerts` dedups on (one definition, no drift).
from app.api.spending_alerts import _ET, _check_alerts
from app.core.config import get_settings
from app.core.errors import api_error
from app.models.accounts import Account
from app.models.bill_reminders import BillReminder
from app.models.budgets import Budget
from app.models.categories import Category
from app.models.notifications import (
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationInboxRead,
    NotificationSetting,
)
from app.models.spending_alerts import SpendingAlert
from app.models.users import User
from app.schemas.notifications import (
    DELIVERY_STATUSES,
    GENERATOR_ALERT_TYPES,
    LEGACY_ALERT_TYPES,
    NotificationChannelPublic,
    NotificationChannelUpdate,
    NotificationDeliveryAttemptPublic,
    NotificationDeliveryPage,
    NotificationDeliveryPublic,
    NotificationInboxItem,
    NotificationInboxPage,
    NotificationInboxReadAllResult,
    NotificationInboxReadResult,
    NotificationRunResult,
    NotificationSettingPublic,
    NotificationSettingUpdate,
)

# Local import is safe: notify_delivery imports this module's `_collect` lazily
# inside run_once (docs/37), so there is no import cycle.
from app.services.notify_delivery import (
    cancel_pending,
    channel_configured,
    ensure_channels,
    run_once,
    validate_webhook_target,
)
from app.services.tax_alerts import compute_tax_loss_alerts
from app.services.txn_filters import today_et

_EMAIL_ADAPTER = TypeAdapter(EmailStr)

router = APIRouter(prefix="/notifications", tags=["notifications"])

# alert_type -> default `enabled`. The legacy five seed disabled exactly as M31
# shipped them; the three generator-backed types seed ENABLED, because an alert
# nobody can see by default is dead UI (docs/35 decision 2).
DEFAULT_TYPES: list[tuple[str, bool]] = [
    *((atype, False) for atype in LEGACY_ALERT_TYPES),
    *((atype, True) for atype in GENERATOR_ALERT_TYPES),
]

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_CURSOR_VERSION = 1
_KIND_PREFIXES = ("budget_threshold", "bill_due", "tax_loss")


def _ensure_defaults(db: Session, user_id: int) -> None:
    """Create any missing notification settings for this user (idempotent)."""
    existing = db.exec(
        select(NotificationSetting.alert_type).where(NotificationSetting.user_id == user_id)
    ).all()
    existing_types = set(existing)
    for atype, enabled in DEFAULT_TYPES:
        if atype not in existing_types:
            # `delivery_enabled` seeds as a copy of `enabled` (docs/37): the
            # legacy five stay inert, the three real types are deliverable
            # the moment a user enables a channel.
            db.add(
                NotificationSetting(
                    user_id=user_id,
                    alert_type=atype,
                    enabled=enabled,
                    delivery_enabled=enabled,
                )
            )
    db.commit()


def _enabled_types(db: Session, user_id: int) -> set[str]:
    """Generator types whose toggle is ON. A missing row means ON (docs/35)."""
    rows = db.exec(
        select(NotificationSetting.alert_type, NotificationSetting.enabled).where(
            NotificationSetting.user_id == user_id,
            NotificationSetting.alert_type.in_(GENERATOR_ALERT_TYPES),
        )
    ).all()
    found = {alert_type for alert_type, enabled in rows if enabled}
    return found | (set(GENERATOR_ALERT_TYPES) - {alert_type for alert_type, _ in rows})


@dataclass
class _Item:
    """Internal union row before it becomes a `NotificationInboxItem`."""

    id: str
    kind: str
    severity: str
    title: str
    body: str
    link: str
    amount_cad: str | None
    occurred_at: datetime
    read: bool
    read_at: datetime | None


def _read_state(db: Session, user_id: int) -> dict[str, datetime]:
    rows = db.exec(
        select(NotificationInboxRead.item_key, NotificationInboxRead.read_at).where(
            NotificationInboxRead.user_id == user_id
        )
    ).all()
    return {key: read_at for key, read_at in rows if read_at is not None}


def _whole_pct(value: Decimal) -> str:
    """Whole percent, ties up — the SAME rounding `Budgets.tsx` applies to
    `pct_used` (`Math.round(pct)% used`).

    M61 #3: the Dashboard alert and the Budgets card must print an identical
    number for a period/category. The card rounds; if the body printed
    `201.94%` next to a card reading `202%`, the surfaces would still disagree
    (docs/35 `body` format).
    """
    return str(int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _budget_items(db: Session, user_id: int, today: date, reads: dict) -> list[_Item]:
    """Today's `spending_alerts` rows — at most one per budget (docs/26 daily
    dedup), so this is exactly the user's current budget status."""
    # Same ET-midnight lower bound `_check_alerts` uses for its daily dedup.
    et_midnight = datetime.combine(today, time.min, tzinfo=_ET)
    alerts = list(
        db.exec(
            select(SpendingAlert)
            .where(
                SpendingAlert.user_id == user_id,
                SpendingAlert.created_at >= et_midnight,
            )
            .order_by(SpendingAlert.created_at.desc())
        ).all()
    )
    if not alerts:
        return []
    budget_ids = {a.budget_id for a in alerts}
    budgets = {
        b.id: b
        for b in db.exec(
            select(Budget).where(Budget.user_id == user_id, Budget.id.in_(budget_ids))
        ).all()
    }
    cat_ids = {b.category_id for b in budgets.values()}
    cat_names = {
        c.id: c.name
        for c in db.exec(
            select(Category).where(Category.user_id == user_id, Category.id.in_(cat_ids))
        ).all()
    }
    items = []
    for alert in alerts:
        budget = budgets.get(alert.budget_id)
        name = cat_names.get(budget.category_id) if budget is not None else None
        occurred = alert.created_at or datetime.now(UTC)
        key = f"budget_threshold:{alert.id}"
        items.append(
            _Item(
                id=key,
                kind="budget_threshold",
                severity=alert.alert_type,  # warning | critical
                title=f"Budget alert: {name or 'budget'}",
                body=(
                    f"Spent {alert.spent} of {alert.budget_amount} CAD "
                    f"({_whole_pct(alert.actual_pct)}% of limit; "
                    f"threshold {_whole_pct(alert.threshold_pct)}%)"
                ),
                link="/budgets",
                amount_cad=str(alert.spent),
                occurred_at=occurred,
                read=key in reads,
                read_at=reads.get(key),
            )
        )
    return items


def _bill_items(db: Session, user_id: int, today: date, reads: dict) -> list[_Item]:
    """Bills inside their reminder window (`next_due_date <= today + remind`),
    including overdue ones (docs/25 decision 5)."""
    rows = list(
        db.exec(
            select(BillReminder).where(
                BillReminder.user_id == user_id,
                BillReminder.enabled.is_(True),
            )
        ).all()
    )
    items = []
    for bill in rows:
        try:
            due = date.fromisoformat(bill.next_due_date)
        except (TypeError, ValueError):
            continue  # docs/25 pins invalid_due_date at write time; skip, never 500
        remind = max(bill.remind_days_before or 0, 0)
        window = due - timedelta(days=remind)
        if window > today:
            continue
        key = f"bill_due:{bill.id}"
        if due < today:
            severity, title = "critical", f"Bill overdue: {bill.name}"
        elif due == today:
            severity, title = "critical", f"Bill due today: {bill.name}"
        else:
            severity, title = "warning", f"Bill due soon: {bill.name}"
        body = f"Due {bill.next_due_date}"
        if bill.amount is not None:
            body += f" — {bill.amount} CAD"
        items.append(
            _Item(
                id=key,
                kind="bill_due",
                severity=severity,
                title=title,
                body=body,
                # M116 (docs/35/37): a due bill now drills into its OWN page —
                # the matched payments and the period chart live at
                # `/bills/{id}`, so the inbox lands the user on the thing the
                # notification is about instead of the index.
                link=f"/bills/{bill.id}",
                amount_cad=str(bill.amount) if bill.amount is not None else None,
                occurred_at=datetime.combine(window, time.min, tzinfo=UTC),
                read=key in reads,
                read_at=reads.get(key),
            )
        )
    return items


def _tax_items(db: Session, user_id: int, today: date, reads: dict) -> list[_Item]:
    """Computed tax-loss opportunities — one item per (security, account)."""
    alerts = compute_tax_loss_alerts(db, user_id, today)
    occurred = datetime.combine(today, time.min, tzinfo=UTC)
    items = []
    for alert in alerts:
        key = f"tax_loss:{alert['security_id']}:{alert['account_id']}"
        risk = bool(alert["superficial_loss_risk"])
        body = (
            f"Unrealized loss {alert['unrealized_loss_cad']} CAD in "
            f"{alert['account_name']} "
            f"(estimated tax benefit {alert['estimated_tax_benefit_cad']} CAD)"
        )
        if risk:
            body += " — superficial-loss risk"
        items.append(
            _Item(
                id=key,
                kind="tax_loss",
                severity="warning" if risk else "info",
                title=f"Tax-loss opportunity: {alert['symbol']}",
                body=body,
                link="/taxes",
                amount_cad=str(alert["unrealized_loss_cad"]),
                occurred_at=occurred,
                read=key in reads,
                read_at=reads.get(key),
            )
        )
    return items


def _collect(db: Session, user_id: int) -> list[_Item]:
    """Assemble the gated union (docs/35 "Sources" + "Gating").

    Budget generation runs FIRST and unconditionally: `_check_alerts` writes
    `spending_alerts` audit rows even when `budget_threshold` is toggled OFF.
    The other two sources are pure reads of rows/derivations that already
    exist, so they are skipped when their toggle is off (nothing is lost).
    """
    _ensure_defaults(db, user_id)
    today = today_et()
    _check_alerts(db, user_id)  # generation — always (docs/35 decision 3)
    enabled = _enabled_types(db, user_id)
    reads = _read_state(db, user_id)

    items: list[_Item] = []
    if "budget_threshold" in enabled:
        items.extend(_budget_items(db, user_id, today, reads))
    if "bill_due" in enabled:
        items.extend(_bill_items(db, user_id, today, reads))
    if "tax_loss" in enabled:
        items.extend(_tax_items(db, user_id, today, reads))
    items.sort(key=lambda i: (i.occurred_at, i.id), reverse=True)
    return items


def _encode_cursor(item: _Item) -> str:
    payload = {"v": _CURSOR_VERSION, "at": item.occurred_at.isoformat(), "k": item.id}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return raw.rstrip("=")


def _decode_cursor(raw: str) -> tuple[datetime, str]:
    """Any failure -> 422 `invalid_cursor` (never a bare 500, docs/35)."""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        obj = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(obj, dict) or obj.get("v") != _CURSOR_VERSION:
            raise ValueError("not a v1 cursor object")
        at = datetime.fromisoformat(str(obj["at"]))
        key = obj["k"]
        if at.tzinfo is None or not isinstance(key, str):
            raise ValueError("cursor fields malformed")
        return at, key
    except Exception:
        raise api_error(422, "Invalid cursor", "invalid_cursor") from None


def _resolve_owned(db: Session, user_id: int, item_id: str) -> None:
    """Tenancy check for `POST /notifications/inbox/{id}/read` (docs/35).

    Unknown, malformed or foreign ids are all `404 not_found` — a foreign id
    must never reveal that the row exists elsewhere.
    """
    kind, sep, rest = item_id.partition(":")
    if not sep or kind not in _KIND_PREFIXES:
        raise api_error(404, "Notification not found", "not_found")

    def _owned(model, row_id: int) -> None:
        row = db.get(model, row_id)
        if row is None or row.user_id != user_id:
            raise api_error(404, "Notification not found", "not_found")

    if kind in ("budget_threshold", "bill_due"):
        if not rest.isdigit():
            raise api_error(404, "Notification not found", "not_found")
        _owned(SpendingAlert if kind == "budget_threshold" else BillReminder, int(rest))
        return
    # tax_loss:<security_id>:<accounts.id> — the account is the tenancy boundary
    parts = rest.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise api_error(404, "Notification not found", "not_found")
    _owned(Account, int(parts[1]))


def _mark_read(db: Session, user_id: int, item_key: str) -> datetime:
    """Insert read state idempotently (`ON CONFLICT DO NOTHING`)."""
    db.execute(
        pg_insert(NotificationInboxRead)
        .values(user_id=user_id, item_key=item_key, read_at=datetime.now(UTC))
        .on_conflict_do_nothing(index_elements=["user_id", "item_key"])
    )
    db.commit()
    row = db.exec(
        select(NotificationInboxRead.read_at).where(
            NotificationInboxRead.user_id == user_id,
            NotificationInboxRead.item_key == item_key,
        )
    ).first()
    return row if row is not None else datetime.now(UTC)


@router.get("", response_model=list[NotificationSettingPublic])
def list_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[NotificationSetting]:
    _ensure_defaults(db, current_user.id)
    return list(
        db.exec(
            select(NotificationSetting).where(NotificationSetting.user_id == current_user.id)
        ).all()
    )


@router.get("/inbox", response_model=NotificationInboxPage)
def inbox(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> NotificationInboxPage:
    """Union of the three gated sources, cursor-paginated (docs/35).

    `total` and `unread_count` cover the WHOLE gated inbox, not the page, so
    `?limit=1` is enough to drive the AppShell badge.
    """
    all_items = _collect(db, current_user.id)
    total = len(all_items)
    unread = sum(1 for item in all_items if not item.read)

    position: tuple[datetime, str] | None = None
    if cursor is not None and cursor != "":
        at, key = _decode_cursor(cursor)
        position = (at, key)
    remaining = all_items
    if position is not None:
        remaining = [i for i in all_items if (i.occurred_at, i.id) < position]
    page = remaining[:limit]
    next_cursor = _encode_cursor(page[-1]) if len(remaining) > limit else None

    return NotificationInboxPage(
        items=[
            NotificationInboxItem(
                id=item.id,
                kind=item.kind,
                severity=item.severity,
                title=item.title,
                body=item.body,
                link=item.link,
                amount_cad=item.amount_cad,
                occurred_at=item.occurred_at.isoformat(),
                read=item.read,
                read_at=item.read_at.isoformat() if item.read_at is not None else None,
            )
            for item in page
        ],
        total=total,
        unread_count=unread,
        limit=limit,
        cursor=cursor,
        next_cursor=next_cursor,
    )


@router.post("/inbox/read-all", response_model=NotificationInboxReadAllResult)
def read_all(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> NotificationInboxReadAllResult:
    """Mark every currently-visible (gated) inbox item read (docs/35).

    Items hidden by an OFF toggle keep their existing read state — the surface
    is gated, and read state is data.
    """
    unread = [item for item in _collect(db, current_user.id) if not item.read]
    for item in unread:
        _mark_read(db, current_user.id, item.id)
    return NotificationInboxReadAllResult(marked=len(unread), unread_count=0)


@router.post("/inbox/{item_id}/read", response_model=NotificationInboxReadResult)
def read_one(
    item_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> NotificationInboxReadResult:
    """Mark one inbox item read. Idempotent; tenancy-strict (docs/35)."""
    _resolve_owned(db, current_user.id, item_id)
    read_at = _mark_read(db, current_user.id, item_id)
    return NotificationInboxReadResult(id=item_id, read=True, read_at=read_at.isoformat())


def _channel_public(row: NotificationChannel, settings) -> NotificationChannelPublic:
    return NotificationChannelPublic(
        id=row.id or 0,
        user_id=row.user_id,
        channel=row.channel,
        enabled=row.enabled,
        target=row.target,
        privacy_opt_in=row.privacy_opt_in_at is not None,
        configured=channel_configured(row.channel, row.target, settings),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _delivery_public(
    row: NotificationDelivery, attempts: list[NotificationDeliveryAttempt]
) -> NotificationDeliveryPublic:
    return NotificationDeliveryPublic(
        id=row.id or 0,
        channel=row.channel,
        alert_type=row.alert_type,
        item_key=row.item_key,
        status=row.status,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        last_error_code=row.last_error_code,
        sent_at=row.sent_at,
        dead_at=row.dead_at,
        retracted=row.retracted_at is not None,
        created_at=row.created_at,
        updated_at=row.updated_at,
        attempt_log=[
            NotificationDeliveryAttemptPublic(
                attempt=a.attempt,
                outcome=a.outcome,
                error_code=a.error_code,
                http_status=a.http_status,
                detail=a.detail,
                created_at=a.created_at,
            )
            for a in sorted(attempts, key=lambda a: a.attempt)
        ],
    )


@router.get("/channels", response_model=list[NotificationChannelPublic])
def list_channels(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[NotificationChannelPublic]:
    """Seed (idempotent) and list this user's transports — both disabled until
    the user turns one on (docs/37 decision 3)."""
    settings = get_settings()
    return [_channel_public(row, settings) for row in ensure_channels(db, current_user)]


@router.patch("/channels/{channel_id}", response_model=NotificationChannelPublic)
def update_channel(
    channel_id: int,
    payload: NotificationChannelUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> NotificationChannelPublic:
    """Enable/disable a transport, retarget it, or set/clear the privacy opt-in.

    The whole new state is projected and validated BEFORE anything is written,
    so a 409/422 leaves the stored row exactly as it was (docs/37).
    """
    row = db.get(NotificationChannel, channel_id)
    if row is None or row.user_id != current_user.id:
        raise api_error(404, "Channel not found", "not_found")
    settings = get_settings()

    target = row.target if payload.target is None else payload.target
    opt_in_at = row.privacy_opt_in_at
    if payload.privacy_opt_in is not None:
        opt_in_at = datetime.now(UTC) if payload.privacy_opt_in else None
    enabled = row.enabled if payload.enabled is None else payload.enabled

    if payload.target is not None and row.channel == "email":
        try:
            _EMAIL_ADAPTER.validate_python(payload.target)
        except ValidationError:
            raise api_error(
                422, "target must be a valid email address", "validation_error"
            ) from None
    if payload.target is not None and row.channel == "webhook":
        validate_webhook_target(payload.target, settings=settings)
    if enabled:
        if not channel_configured(row.channel, target, settings):
            raise api_error(
                409,
                f"The {row.channel} transport is not configured on this install",
                "channel_not_configured",
            )
        if payload.enabled is True and current_user.privacy_mode and opt_in_at is None:
            # Only an explicit `enabled: true` demands the opt-in — clearing it
            # (or retargeting) must always stay possible, that's the off switch.
            raise api_error(
                409,
                "Privacy mode is on: pass privacy_opt_in=true to allow outbound delivery",
                "privacy_opt_in_required",
            )

    was_enabled = row.enabled
    row.target = target
    row.privacy_opt_in_at = opt_in_at
    row.enabled = enabled
    db.add(row)
    db.commit()
    db.refresh(row)
    if was_enabled and not enabled:
        # Explicit user action: drop this channel's queue immediately (docs/37).
        cancel_pending(db, current_user.id, channel=row.channel)
        db.refresh(row)
    return _channel_public(row, settings)


@router.get("/deliveries", response_model=NotificationDeliveryPage)
def list_deliveries(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    # Own query params (not `get_pagination`), so the M98b F-4 ceiling has to
    # be repeated here: without `le=` an `offset >= 2**63` reached Postgres as
    # a bigint OFFSET it rejects -> the catch-all 500, not a 422.
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    status: Annotated[str | None, Query()] = None,
) -> NotificationDeliveryPage:
    """Auditable outbox view — every send attempt with its outcome (docs/37)."""
    if status is not None and status not in DELIVERY_STATUSES:
        raise api_error(
            422, "status must be one of pending|sent|dead|cancelled", "validation_error"
        )
    count_q = (
        select(func.count())
        .select_from(NotificationDelivery)
        .where(NotificationDelivery.user_id == current_user.id)
    )
    if status is not None:
        count_q = count_q.where(NotificationDelivery.status == status)
    total = db.exec(count_q).one()
    q = select(NotificationDelivery).where(NotificationDelivery.user_id == current_user.id)
    if status is not None:
        q = q.where(NotificationDelivery.status == status)
    rows = list(
        db.exec(q.order_by(NotificationDelivery.id.desc()).limit(limit).offset(offset)).all()
    )
    attempts: dict[int, list[NotificationDeliveryAttempt]] = {}
    if rows:
        found = db.exec(
            select(NotificationDeliveryAttempt)
            .where(
                NotificationDeliveryAttempt.user_id == current_user.id,
                NotificationDeliveryAttempt.delivery_id.in_([r.id for r in rows]),
            )
            .order_by(NotificationDeliveryAttempt.attempt)
        ).all()
        for attempt in found:
            attempts.setdefault(attempt.delivery_id, []).append(attempt)
    return NotificationDeliveryPage(
        items=[_delivery_public(row, attempts.get(row.id, [])) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/deliveries/run", response_model=NotificationRunResult)
def run_delivery_now(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> NotificationRunResult:
    """Scan + dispatch for the CALLER only (docs/37) — the operator/MCP trigger
    for the same code path the worker runs on a timer."""
    return NotificationRunResult(**run_once(db, user_id=current_user.id))


@router.post("/deliveries/{delivery_id}/retry", response_model=NotificationDeliveryPublic)
def retry_delivery(
    delivery_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> NotificationDeliveryPublic:
    """Re-arm a dead-lettered delivery: attempts reset, history preserved."""
    row = db.get(NotificationDelivery, delivery_id)
    if row is None or row.user_id != current_user.id:
        raise api_error(404, "Delivery not found", "not_found")
    if row.status != "dead":
        raise api_error(409, "Only a dead delivery can be retried", "delivery_not_retryable")
    row.status = "pending"
    row.attempts = 0
    row.next_attempt_at = datetime.now(UTC)
    row.dead_at = None
    row.last_error = None
    row.last_error_code = None
    db.add(row)
    db.commit()
    db.refresh(row)
    attempts = list(
        db.exec(
            select(NotificationDeliveryAttempt)
            .where(NotificationDeliveryAttempt.delivery_id == row.id)
            .order_by(NotificationDeliveryAttempt.attempt)
        ).all()
    )
    return _delivery_public(row, attempts)


@router.patch("/{setting_id}", response_model=NotificationSettingPublic)
def update_setting(
    setting_id: int,
    payload: NotificationSettingUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> NotificationSetting:
    ns = db.get(NotificationSetting, setting_id)
    if ns is None or ns.user_id != current_user.id:
        raise api_error(404, "Setting not found", "not_found")
    if payload.enabled is not None:
        ns.enabled = payload.enabled
    if payload.delivery_enabled is not None:
        ns.delivery_enabled = payload.delivery_enabled
    if payload.config is not None:
        ns.config = payload.config
    db.add(ns)
    db.commit()
    db.refresh(ns)
    if payload.enabled is False or payload.delivery_enabled is False:
        # Toggling a type off stops BOTH the inbox item and its delivery (docs/37
        # decision 8) — queued rows for this type are dropped immediately.
        cancel_pending(db, current_user.id, alert_type=ns.alert_type)
        db.refresh(ns)
    return ns

"""notifications — settings, in-app inbox and delivery shapes (docs/35, docs/37)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

# alert_type values. The first five are M31 legacy types with NO generator;
# the last three are the generator-backed types M51 added (docs/35).
LEGACY_ALERT_TYPES = (
    "large_transaction",
    "low_balance",
    "dividend_received",
    "price_alert",
    "monthly_summary",
)
GENERATOR_ALERT_TYPES = ("budget_threshold", "bill_due", "tax_loss")

# M59 (docs/37): transport names + outbox statuses.
CHANNEL_NAMES = ("email", "webhook")
DELIVERY_STATUSES = ("pending", "sent", "dead", "cancelled")


class NotificationSettingPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    alert_type: str
    enabled: bool
    delivery_enabled: bool
    config: dict | None = None


class NotificationSettingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    delivery_enabled: bool | None = None
    config: dict | None = None


class NotificationInboxItem(BaseModel):
    """One union item from budget_threshold / bill_due / tax_loss (docs/35)."""

    id: str
    kind: str  # budget_threshold | bill_due | tax_loss
    severity: str  # critical | warning | info
    title: str
    body: str
    link: str
    amount_cad: str | None = None  # Decimal-string
    occurred_at: str  # ISO-8601 UTC, sort key
    read: bool
    read_at: str | None = None  # ISO-8601 UTC


class NotificationInboxPage(BaseModel):
    """House pagination envelope ({items, total, limit, ...}) with `cursor`
    replacing `offset` — see docs/35 "Ordering and cursor"."""

    items: list[NotificationInboxItem]
    total: int  # items in the whole gated inbox (ignores limit/cursor)
    unread_count: int  # read=false items across the whole gated inbox
    limit: int
    cursor: str | None = None  # echo of the request cursor
    next_cursor: str | None = None  # opaque cursor for the next page, else null


class NotificationInboxReadResult(BaseModel):
    id: str
    read: bool = True
    read_at: str  # ISO-8601 UTC


class NotificationInboxReadAllResult(BaseModel):
    marked: int  # previously-unread items now marked read
    unread_count: int  # always 0


# --- M59 delivery (docs/37) --------------------------------------------------


class NotificationChannelPublic(BaseModel):
    """One transport row. Built explicitly in the route (not `from_attributes`)
    because `privacy_opt_in` / `configured` are derived, not columns."""

    id: int
    user_id: int
    channel: str  # email | webhook
    enabled: bool
    target: str | None = None
    privacy_opt_in: bool = False  # privacy_opt_in_at IS NOT NULL
    configured: bool = False  # would enabling be accepted right now?
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NotificationChannelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    target: str | None = None
    privacy_opt_in: bool | None = None


class NotificationDeliveryAttemptPublic(BaseModel):
    attempt: int
    outcome: str  # sent | retryable | fatal
    error_code: str | None = None
    http_status: int | None = None
    detail: str | None = None
    created_at: datetime | None = None


class NotificationDeliveryPublic(BaseModel):
    id: int
    channel: str
    alert_type: str
    item_key: str
    status: str  # pending | sent | dead | cancelled
    attempts: int
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    last_error_code: str | None = None
    sent_at: datetime | None = None
    dead_at: datetime | None = None
    retracted: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    attempt_log: list[NotificationDeliveryAttemptPublic] = []


class NotificationDeliveryPage(BaseModel):
    """House pagination envelope ({items, total, limit, offset}) — the outbox is
    a real table, so offset paging is correct here (docs/35's cursor rule is
    about the re-derived union)."""

    items: list[NotificationDeliveryPublic]
    total: int
    limit: int
    offset: int


class NotificationRunResult(BaseModel):
    scanned_users: int = 0
    enqueued: int = 0
    retracted: int = 0
    attempted: int = 0
    sent: int = 0
    retrying: int = 0
    dead: int = 0
    cancelled: int = 0

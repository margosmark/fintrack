"""notifications — alert settings, in-app inbox read state, delivery outbox (docs/35, docs/37)."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, UniqueConstraint, func, text
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class NotificationSetting(TimestampMixin, table=True):
    __tablename__ = "notification_settings"
    # One setting per user per alert type (migration 0018, `uq_notif_user_type`).
    __table_args__ = (UniqueConstraint("user_id", "alert_type", name="uq_notif_user_type"),)
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    # Five legacy types (M31, NO generator — docs/35) plus the three
    # generator-backed types M51 added: budget_threshold, bill_due, tax_loss.
    alert_type: str = Field(max_length=50)
    enabled: bool = Field(default=True)
    # M59 (docs/37): independent per-type delivery switch, seeded as a copy of
    # `enabled` (legacy five false, generator three true). Delivery also needs
    # an enabled channel, so this alone never sends anything.
    delivery_enabled: bool = Field(default=False)
    config: dict | None = Field(default=None, sa_type=JSON)  # threshold amounts, etc.


class NotificationInboxRead(TimestampMixin, table=True):
    """Read state for one inbox item (docs/35).

    `item_key` is the inbox item `id` verbatim
    (`budget_threshold:<id>` | `bill_due:<id>` | `tax_loss:<sec>:<acct>`), so
    marking read is idempotent through `uq_inbox_read_user_key`. Independent of
    `spending_alerts.acknowledged`, which still gates only the legacy
    `GET /budgets/alerts` response.
    """

    __tablename__ = "notification_inbox_reads"
    __table_args__ = (UniqueConstraint("user_id", "item_key", name="uq_inbox_read_user_key"),)
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    item_key: str = Field(max_length=120)
    read_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )


class NotificationChannel(TimestampMixin, table=True):
    """One outbound transport configured by a user (docs/37).

    Exactly two rows per user (`email`, `webhook`), both seeded disabled —
    nothing leaves the machine until a user turns one on. Holds NO secret:
    SMTP credentials are env-only and the webhook signing key is derived from
    env at send time. `privacy_opt_in_at` is written only by an explicit
    `privacy_opt_in: true` PATCH, which is what makes privacy-mode delivery an
    informed per-channel choice instead of an inherited one.
    """

    __tablename__ = "notification_channels"
    __table_args__ = (UniqueConstraint("user_id", "channel", name="uq_notif_channel_user_kind"),)
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    channel: str = Field(max_length=20)  # email | webhook
    enabled: bool = Field(default=False)
    target: str | None = Field(default=None, max_length=320)  # address / webhook URL
    privacy_opt_in_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class NotificationDelivery(TimestampMixin, table=True):
    """Outbox row: one inbox item x one channel (docs/37).

    `uq_delivery_active` is partial (`WHERE retracted_at IS NULL`): a retracted
    row keeps the audit trail but stops blocking a fresh delivery when the same
    inbox item later reappears in the union.
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        Index(
            "uq_delivery_active",
            "user_id",
            "channel",
            "item_key",
            unique=True,
            postgresql_where=text("retracted_at IS NULL"),
        ),
        Index(
            "ix_notification_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    channel: str = Field(max_length=20)  # email | webhook
    alert_type: str = Field(max_length=50)
    item_key: str = Field(max_length=120)  # inbox item id verbatim
    # Payload snapshot (docs/37): retries re-send identical bytes.
    severity: str = Field(max_length=20)
    occurred_at: datetime = Field(sa_type=DateTime(timezone=True))
    link: str = Field(max_length=50)
    status: str = Field(default="pending", max_length=20)  # pending|sent|dead|cancelled
    attempts: int = Field(default=0)
    next_attempt_at: datetime = Field(
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )
    last_error: str | None = None
    last_error_code: str | None = Field(default=None, max_length=50)
    sent_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    dead_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    retracted_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class NotificationDeliveryAttempt(TimestampMixin, table=True):
    """One real send attempt and its outcome (docs/37) — the audit trail.

    `outcome` ∈ sent | retryable | fatal. A dispatch blocked BEFORE any I/O
    (eligibility lost) writes no attempt row: it lands on the delivery itself
    as `status=cancelled` + `last_error_code=delivery_not_eligible`. `user_id`
    is denormalised from the delivery so the export registry (docs/11) and
    `delete_user_rows` both cover this table.
    """

    __tablename__ = "notification_delivery_attempts"
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    delivery_id: int = Field(foreign_key="notification_deliveries.id", index=True)
    attempt: int  # 1-based send sequence
    outcome: str = Field(max_length=20)  # sent | retryable | fatal
    error_code: str | None = Field(default=None, max_length=50)
    http_status: int | None = None
    detail: str | None = None  # sanitized, <=500 chars, never the payload

"""M59 notification delivery: delivery_enabled + channels/outbox/attempt log (docs/37).

Revision ID: 0030_notification_delivery
Revises: 0029_backup_artifacts (M56 landed first — this card was the second
lander of the 0028-parent race flagged in docs/37 decision 10, so it
re-parented instead of the other way round)
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_notification_delivery"
down_revision: str | None = "0029_backup_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Per-type delivery toggle, seeded as a copy of `enabled` (docs/37):
    #    legacy five false, generator three true, nothing new leaves the machine
    #    because no channel is enabled by default.
    op.add_column(
        "notification_settings",
        sa.Column("delivery_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("UPDATE notification_settings SET delivery_enabled = enabled")

    # 2. Transports (no secrets stored — credentials live in env, docs/37).
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("target", sa.String(length=320), nullable=True),
        sa.Column("privacy_opt_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "channel", name="uq_notif_channel_user_kind"),
    )

    # 3. Outbox. The partial UNIQUE is what lets a re-armed (retracted) inbox
    #    item be delivered again while an active row still blocks duplicates.
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("alert_type", sa.String(length=50), nullable=False),
        sa.Column("item_key", sa.String(length=120), nullable=False),
        # Payload snapshot taken at enqueue (docs/37): retries re-send the
        # same bytes and dispatch never re-derives the inbox union.
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("link", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.String(length=50), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "uq_delivery_active",
        "notification_deliveries",
        ["user_id", "channel", "item_key"],
        unique=True,
        postgresql_where=sa.text("retracted_at IS NULL"),
    )
    op.create_index(
        "ix_notification_deliveries_due",
        "notification_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # 4. Attempt log — one row per REAL send attempt (docs/37). `user_id` is
    #    denormalised so export scoping (docs/11) and delete_user_rows cover it.
    op.create_table(
        "notification_delivery_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column(
            "delivery_id",
            sa.Integer(),
            sa.ForeignKey("notification_deliveries.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("notification_delivery_attempts")
    op.drop_index("ix_notification_deliveries_due", table_name="notification_deliveries")
    op.drop_index("uq_delivery_active", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_table("notification_channels")
    op.drop_column("notification_settings", "delivery_enabled")

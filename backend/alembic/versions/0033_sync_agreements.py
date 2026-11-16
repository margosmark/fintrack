"""M79: bank-sync readiness — sync_agreements + per-account status columns (docs/39).

Revision ID: 0033_sync_agreements
Revises: 0033_tx_cleared (M76, which pushed to origin/main first).
Landed parented to the HEAD THAT EXISTED, per the M59 precedent in
0030_notification_delivery: `alembic upgrade head` must never see two heads at
container boot. Two cards in flight each claimed a revision off the same
parent; this one is the second to reach origin/main, so it re-parents onto M76's
chain exactly as it originally re-parented onto M75's. Never edit a revision
that origin/main has already shipped — the incoming one re-parents.
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_sync_agreements"
down_revision: str | None = "0033_tx_cleared"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Consent records (docs/39 § Data model). Deliberately NO credential
    #    column: `consent_id` is the provider's own reference, not a secret.
    op.create_table(
        "sync_agreements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("consent_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("account_refs", sa.JSON(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_status", sa.String(length=32), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "provider", "consent_id", name="uq_sync_agreement_consent"),
    )

    # 2. Status-page groundwork (docs/39 item 4): per-account sync health.
    #    Nullable on purpose — NULL `provider_status` means "not linked to a
    #    live agreement", which is a state, not missing data.
    op.add_column("accounts", sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("accounts", sa.Column("provider_status", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "provider_status")
    op.drop_column("accounts", "last_sync_at")
    op.drop_table("sync_agreements")

"""add notification_inbox_reads (M51 inbox read state, docs/35).

Revision ID: 0028_add_inbox_reads
Revises: 0027_vault_envelopes
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_add_inbox_reads"
down_revision: str | None = "0027_vault_envelopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_inbox_reads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("item_key", sa.String(length=120), nullable=False),
        sa.Column(
            "read_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "item_key", name="uq_inbox_read_user_key"),
    )


def downgrade() -> None:
    op.drop_table("notification_inbox_reads")

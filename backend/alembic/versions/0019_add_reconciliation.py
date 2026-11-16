"""add reconciliation_items table.

Revision ID: 0019_add_reconciliation
Revises: 0018_add_notifications
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_add_reconciliation"
down_revision: str | None = "0018_add_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column(
            "account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False, index=True
        ),
        sa.Column("transaction_id", sa.Integer(), sa.ForeignKey("transactions.id"), nullable=True),
        sa.Column("statement_date", sa.String(length=10), nullable=False),
        sa.Column("statement_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "statement_description", sa.String(length=500), nullable=False, server_default=""
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="unmatched"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("reconciliation_items")

"""bounded tenant-scoped raw dividend history (performance read path)

Revision ID: 0055_dividend_history
Revises: 0054_bill_due_anchor
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0055_dividend_history"
down_revision: str | None = "0054_bill_due_anchor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dividend_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("security_id", sa.Integer(), sa.ForeignKey("securities.id"), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("provider_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(16), nullable=False, server_default="yfinance"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("amount > 0", name="ck_dividend_history_amount_positive"),
        sa.CheckConstraint("source = 'yfinance'", name="ck_dividend_history_source"),
        sa.UniqueConstraint(
            "user_id",
            "security_id",
            "source",
            "provider_order",
            name="uq_dividend_history_order",
        ),
    )
    op.create_index(
        "ix_dividend_history_user_security_date",
        "dividend_history",
        ["user_id", "security_id", "ex_date"],
    )
    op.create_table(
        "dividend_history_refresh",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("security_id", sa.Integer(), sa.ForeignKey("securities.id"), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="unavailable"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "security_id", name="uq_dividend_history_refresh"),
        sa.CheckConstraint(
            "state IN ('pending', 'refreshing', 'available', 'empty', 'unavailable')",
            name="ck_dividend_history_refresh_state",
        ),
    )
    op.create_index(
        "ix_dividend_history_refresh_pending",
        "dividend_history_refresh",
        ["state", "last_attempt_at"],
    )


def downgrade() -> None:
    op.drop_table("dividend_history_refresh")
    op.drop_table("dividend_history")

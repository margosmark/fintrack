"""add bill_reminders, spending_alerts, tax_projections, debts tables.

Revision ID: 0021_add_bills_alerts
Revises: 0020_add_goals
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_add_bills_alerts"
down_revision: str | None = "0020_add_goals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bill_reminders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id"), nullable=True),
        sa.Column("frequency", sa.String(length=20), nullable=False),
        sa.Column("next_due_date", sa.String(length=10), nullable=False),
        sa.Column("remind_days_before", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "spending_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("budget_id", sa.Integer(), sa.ForeignKey("budgets.id"), nullable=False),
        sa.Column("alert_type", sa.String(length=20), nullable=False),
        sa.Column("threshold_pct", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("actual_pct", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("spent", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("budget_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("acknowledged", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "tax_projections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("province", sa.String(length=2), nullable=False, server_default="ON"),
        sa.Column(
            "estimated_income",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "deductions", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column("capital_gains_override", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("dividend_income_override", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "year", name="uq_tax_proj_user_year"),
    )

    op.create_table(
        "debts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("debt_type", sa.String(length=50), nullable=False),
        sa.Column("original_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("interest_rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("minimum_payment", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("payment_frequency", sa.String(length=20), nullable=True),
        sa.Column("start_date", sa.String(length=10), nullable=True),
        sa.Column("end_date", sa.String(length=10), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("debts")
    op.drop_table("tax_projections")
    op.drop_table("spending_alerts")
    op.drop_table("bill_reminders")

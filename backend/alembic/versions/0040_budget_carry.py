"""M110 budget rollover: budgets.carry + the frozen budget_carryovers table.

Revision ID: 0040_budget_carry
Revises: 0039_tx_review_events (current head)
Create Date: 2026-10-06

Numbered 0040 (not 0038/0039): M108's `0038_saved_report_views` and M106's
`0039_tx_review_events` both landed on origin/main first and are already the
deployed lineage — this revises the tip instead of forking a second alembic
branch (the `0033_tx_cleared` / `0037_recurring` precedent; AGENTS §6 says
re-check `alembic heads` after every rebase).

One column and one table (docs/02 §budgets, docs/09 §Budget carry).

`budgets.carry` is the per-category opt-in (`off` | `forward`, default `off`) —
`server_default` backfills every existing budget to `off`, so a row that never
opted in computes exactly the numbers it did before this migration, and the
CHECK keeps any client (or MCP write) to the two values.

`budget_carryovers` is the audit ledger the honesty rule needs: the `carried_in`
a budget carried INTO one period, written once when that period is first read
after the boundary and never recomputed. One row per (budget, period_start);
`amount` is signed (a deficit carries too — a debt to pay down first).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0040_budget_carry"
down_revision: str | None = "0039_tx_review_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "budgets",
        sa.Column(
            "carry",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'off'"),
        ),
    )
    op.create_check_constraint(
        "ck_budgets_carry",
        "budgets",
        "carry IN ('off','forward')",
    )

    op.create_table(
        "budget_carryovers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("budget_id", sa.Integer(), sa.ForeignKey("budgets.id"), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # The freeze: one snapshot per budget per target period.
        sa.UniqueConstraint("budget_id", "period_start", name="uq_budget_carryovers_budget_period"),
    )


def downgrade() -> None:
    op.drop_table("budget_carryovers")
    op.drop_constraint("ck_budgets_carry", "budgets", type_="check")
    op.drop_column("budgets", "carry")

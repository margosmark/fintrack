"""M96 B2: recurring-tag columns on transactions + recurring_suppressions (docs/09).

Revision ID: 0037_recurring
Revises: 0036_dashboard_preset (current head)
Create Date: 2026-10-04

Numbered 0037 (not 0036): M97's `0036_dashboard_preset` landed on origin/main
first and is already the deployed head — this revises it, so `alembic upgrade
head` runs after the deployed head instead of forking a second branch (the
`0033_tx_cleared` precedent).

Three nullable/defaulted columns on `transactions` (docs/02 §transactions) —
`is_recurring` is the flag the UI badges, `recurring_cadence` the fitted
cadence (non-native enum, docs/09 §Recurring prompt), `recurring_next_date`
the group's next expected occurrence — plus the suppression table a DECLINE
writes so the same group never re-prompts. The suppression table is the only
new table: detection itself is computed on demand, never stored.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037_recurring"
down_revision: str | None = "0036_dashboard_preset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default, not a Python default: the table already has rows, and a
    # NOT NULL column without one cannot be added over them.
    op.add_column(
        "transactions",
        sa.Column(
            "is_recurring",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Non-native enum == VARCHAR(16) in this dialect (AGENTS §2): widening the
    # cadence set is a constraint rebuild, not a table rewrite.
    op.add_column(
        "transactions",
        sa.Column("recurring_cadence", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column("recurring_next_date", sa.Date(), nullable=True),
    )

    op.create_table(
        "recurring_suppressions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("group_key", sa.String(length=120), nullable=False),
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
        # Idempotent decline, and the index the candidates read needs anyway.
        sa.UniqueConstraint("user_id", "group_key", name="uq_recurring_suppressions_user_group"),
    )


def downgrade() -> None:
    op.drop_table("recurring_suppressions")
    op.drop_column("transactions", "recurring_next_date")
    op.drop_column("transactions", "recurring_cadence")
    op.drop_column("transactions", "is_recurring")

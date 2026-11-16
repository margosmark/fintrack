"""add bill_reminders match-rule columns (M116, docs/25 §M116 D1).

Revision ID: 0044_bill_match_rules
Revises: 0043_forecast_scenarios
Create Date: 2026-10-08

Three columns on the existing table, no side table: a bill's match rule
describes ONE bill, so it belongs on `bill_reminders` the same way M107 put a
split's four columns on `transactions` (docs/44 D1) — every reader stays one
field away from correctness and nothing has to be JOINed in.

`match_term` is NOT NULL: "this bill has no rule" is not a state the product
can answer "how many payments did I find?" from, so there is no NULL to
interpret. Existing rows are backfilled from `name` (their natural default —
`POST /bills` derives the same value when the request omits `match_term`),
truncated to the column's 120-char bound so the backfill can never fail on an
M40-era 200-char name.

No index is added here. The rule is evaluated as an index-friendly ILIKE
prefilter over the EXISTING `pg_trgm` GIN index on
`transactions.normalized_payee` (migration 0004, docs/02 §transactions) plus
the per-user predicate; measured with EXPLAIN against a 10k-row ledger before
deciding (docs/25 §M116 verification).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044_bill_match_rules"
down_revision: str | None = "0043_forecast_scenarios"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bill_reminders",
        sa.Column(
            "match_mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'contains'"),
        ),
    )
    op.add_column(
        "bill_reminders",
        sa.Column("amount_tolerance", sa.Numeric(precision=12, scale=2), nullable=True),
    )
    # Backfill AFTER the column exists so the NOT NULL add and the fill are
    # one statement per row, and before `match_term` becomes non-nullable.
    op.add_column(
        "bill_reminders",
        sa.Column("match_term", sa.String(length=120), nullable=True),
    )
    op.execute(
        "UPDATE bill_reminders "
        "SET match_term = left(name, 120) "
        "WHERE match_term IS NULL OR match_term = ''"
    )
    op.alter_column("bill_reminders", "match_term", nullable=False)
    # A CHECK keeps the documented 120-char bound true in the TABLE, not only
    # at the API seam — the same "the constraint is the contract" rule the
    # split checksums follow (docs/44 §4.1).
    op.create_check_constraint(
        "ck_bill_reminders_match_term_len",
        "bill_reminders",
        "length(match_term) BETWEEN 1 AND 120",
    )
    op.create_check_constraint(
        "ck_bill_reminders_tolerance_nonneg",
        "bill_reminders",
        "amount_tolerance IS NULL OR amount_tolerance >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_bill_reminders_tolerance_nonneg", "bill_reminders", type_="check")
    op.drop_constraint("ck_bill_reminders_match_term_len", "bill_reminders", type_="check")
    op.drop_column("bill_reminders", "match_term")
    op.drop_column("bill_reminders", "amount_tolerance")
    op.drop_column("bill_reminders", "match_mode")

"""benchmark_prices: a shared index series for the vs-Benchmark panel (M173).

Revision ID: 0052_benchmark_prices
Revises: 0051_merge_two_heads
Create Date: 2026-10-12

``services/benchmarks.py::compute_benchmark_comparison`` shipped as an HONEST
STUB (M65): ``benchmark_return = 0`` and ``benchmark_points = 0``, with the
comment that full yfinance integration needed a shared-securities design. The
stub plus the client's ``benchmark_points >= 2`` gate worked as designed — the
panel says "No benchmark data for this window" instead of echoing the
portfolio return as fabricated alpha — but the comparison itself was never
real (Mark's report: every benchmark 0%).

This revision creates the series it needs: ``benchmark_prices``
(docs/02 §benchmark_prices, docs/28 decision 5, docs/07 §Benchmark series).

Keyed to a BENCHMARK symbol, NOT to ``securities``: the four picks
(SPY/XIU/XBB/XAW) are not held and must not appear in any tenant's security
picker — a design that the M65 comment's "shared securities" reading would have
required and that this table deliberately avoids.

No ``user_id``: a close for ``SPY`` on a date is universal, exactly like a
USD/CAD rate (the operator level of AGENTS §2, alongside ``fx_rates`` and
``provider_fund_registry``).

The table ships EMPTY. Rows are written by the sweeper
(``services/benchmark_refresh.py``) and the operator backfill
(``scripts/backfill_benchmarks.py``) — seeding closes here would freeze a price
into a migration and make the panel's numbers a property of the deploy date,
not of the market.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0052_benchmark_prices"
down_revision: str | None = "0051_merge_two_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "benchmark_prices"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("close", sa.Numeric(20, 8), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint("symbol", "date", name="uq_benchmark_prices_symbol_date"),
        # No separate index on `symbol`: the unique constraint LEADS with it,
        # so a per-symbol scan (and its ORDER BY date) is already served
        # (docs/02 — the same reasoning that keeps `securities` un-indexed).
    )


def downgrade() -> None:
    op.drop_table(TABLE)

"""securities: dead-symbol bookkeeping (M141).

Revision ID: 0048_securities_dead_symbol
Revises: 0047_trade_currency
Create Date: 2026-10-10

Three nullable/defaulted columns on `securities` (docs/02 §securities,
docs/07 §Dead symbols):

* ``quote_miss_count INT NOT NULL DEFAULT 0`` — consecutive HARD
  "quote not found" outcomes. Every other outcome (rate-limit, timeout,
  a cached read) leaves it untouched, so ``3`` means Yahoo said three
  times that the symbol does not exist, not three flaky mornings.
* ``dead_symbol BOOLEAN NOT NULL DEFAULT FALSE`` — the fan-out stop flag.
  Once set, the automatic price/fundamentals fan-out skips the row entirely,
  which is what stops the 404 noise (Mark's APOTEX produced four identical
  "Quote not found for symbol: APOTEX" log lines in one day).
* ``dead_symbol_reason TEXT NULL`` — the last "quote not found" verdict,
  verbatim, so the Detail page can state a real reason instead of the
  forever-"<nothing has been fetched> yet" promise.

The migration backfills NOTHING: an existing row has never missed, so it is
correctly ``0 / FALSE / NULL`` and keeps being fetched exactly as before. A
security that DOES start resolving clears the counter and the flag by itself
the moment a close comes back (``services/market_data.py::_record_quote_outcome``)
— no manual action, no repair script.

The columns live on `securities` rather than a side table because the state is
per-(user, symbol, exchange) and every consumer already loads the Security row
it is about to fetch for; a lookup table would add a join to the hot fan-out
path to save three columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0048_securities_dead_symbol"
down_revision: str | None = "0047_trade_currency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "securities",
        sa.Column("quote_miss_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "securities",
        sa.Column("dead_symbol", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("securities", sa.Column("dead_symbol_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("securities", "dead_symbol_reason")
    op.drop_column("securities", "dead_symbol")
    op.drop_column("securities", "quote_miss_count")

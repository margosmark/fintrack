"""trades.currency: the row's own currency (M132).

Revision ID: 0047_trade_currency
Revises: 0046_multi_term_match
Create Date: 2026-10-09

One nullable column on `trades` (docs/02 §trades):

* ``trades.currency`` (VARCHAR(3), NULLable) — the currency ``price_native``
  and ``commission`` are STATED in, i.e. the row's own currency as the broker
  exported it. A USD buy inside a CAD account stores ``USD`` here, and the
  docs/05 §Portfolio value cash leg converts that row at ITS currency instead
  of face-summing it into the account's (M132 — the account-731 cash line
  reading ``-582.73`` where Wealthsimple shows ``$0.27``).

NULL is a real state, not a missing default: a row written before M132 carries
no per-row currency and every consumer reads NULL as the ACCOUNT's currency —
exactly the pre-M132 behaviour (``trade.currency or account.currency``).

The migration deliberately backfills NOTHING. ``securities.listing_currency``
is NOT a sound proxy for a row's currency: real account 731's VIU rows are CAD
while their security row says USD, because the listing currency is written once
at security creation and later imports never update it. Copying it into
``trades`` would fabricate per-row currency for financial data, which AGENTS §3
and docs/02 refuse. Rows gain their currency from the importer (every future
import) or from a repair that re-reads the SOURCE FILE —
``backend/scripts/backfill_trade_currency.py`` — whose per-row currency column
is the authority.

The CHECK keeps "an ISO-4217 code" true in the TABLE, not only at the API seam
(the same rule as docs/44 §4.1's length bound).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0047_trade_currency"
down_revision: str | None = "0046_multi_term_match"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("currency", sa.String(length=3), nullable=True))
    op.create_check_constraint(
        "ck_trades_currency_iso_code",
        "trades",
        "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_trades_currency_iso_code", "trades", type_="check")
    op.drop_column("trades", "currency")

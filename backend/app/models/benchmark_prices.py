"""benchmark_prices — cached market-index closes for the vs-Benchmark panel (M173, docs/28).

Keyed ONLY to a benchmark symbol, never to a user's `securities` row. A
benchmark is not something anyone holds: creating a security for `SPY` would
put a fund in every tenant's picker that no trade explains, and the four
benchmark picks have no `securities` rows today precisely because that design
was refused (docs/28 decision 5).

**No `user_id`** — a close for `SPY` on a date is universal, exactly like a
USD/CAD rate, so this is the third operator-level table (AGENTS §2: `fx_rates`,
`provider_fund_registry`, and this cache). It is shared: one fetch writes one
row and every tenant's panel reads it, which is also why the refresh worker is
skipped outright while any user has `privacy_mode` on (docs/11 — a shared row
cannot be un-fetched for one tenant).

**The frame is the PROVIDER's split-adjusted close**, not `prices`' as-traded
one. `prices` un-adjusts because a close there has to match the ledger's own
quantities (docs/07 §Price frame); a benchmark has no ledger, and un-adjusting
would turn a 2:1 split into a -50% "return". A benchmark series is a price
INDEX, so it stays in the frame the index is quoted in.

`source='yfinance'` rows are insert-only (manual wins, AGENTS §2): a re-run of
the backfill adds missing dates and rewrites nothing.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import Column, Enum, Numeric, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin
from app.models.prices import PriceSource

__all__ = ["BenchmarkPrice"]


class BenchmarkPrice(TimestampMixin, table=True):
    __tablename__ = "benchmark_prices"
    __table_args__ = (UniqueConstraint("symbol", "date", name="uq_benchmark_prices_symbol_date"),)

    id: int | None = Field(default=None, primary_key=True)
    #: The BENCHMARK key (`SPY`, `XIU`) — what the API's `symbol` param carries
    #: and what a chip selects — NOT the venue ticker (`XIU.TO`). The venue
    #: ticker lives in `services/benchmarks.py::BENCHMARKS`, so re-pointing a
    #: benchmark at another listing is a code change, not a re-key of the rows.
    #: Deliberately NOT `index=True`: the unique constraint below LEADS with
    #: `symbol`, so the per-symbol scan and its `ORDER BY date` are already
    #: served (the same reasoning that keeps `securities` un-indexed, docs/02).
    symbol: str = Field(max_length=32)
    date: date  # type: ignore[assignment]  # sqlmodel shadows the type name
    close: Decimal = Field(sa_type=Numeric(20, 8))
    #: The venue's own quote currency for this close (`USD` for SPY, `CAD` for
    #: the three TSX funds). The return conversion reads it — never a client.
    currency: str = Field(max_length=3)
    source: PriceSource = Field(
        sa_column=Column(Enum(PriceSource, native_enum=False, length=32), nullable=False)
    )

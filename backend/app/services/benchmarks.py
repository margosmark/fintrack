"""Investment benchmarks — compare portfolio vs market indexes (docs/28).

**Real series, not a placeholder (M173).** Until M173 this module hard-stubbed
``benchmark_return = 0`` / ``benchmark_points = 0`` — an honest stub (M65: a
numeric 0 with 0 points, gated client-side so it could never read as alpha),
but the comparison itself did not exist. Now it is computed from a cached
benchmark price series:

* **The series** lives in ``benchmark_prices`` (docs/02), keyed to a benchmark
  SYMBOL — never to a user's ``securities`` row. A benchmark is not held: a
  security for ``SPY`` would put a fund in every tenant's picker that no trade
  explains, which is the "shared securities design" docs/28 decision 5 refused.
  The table is a shared cache with no ``user_id`` (AGENTS §2, alongside
  ``fx_rates``).
* **The venue tickers** are pinned per benchmark in :data:`BENCHMARKS`. Where
  an index is Canadian, the CANADIAN listing is the comparison (``XIU.TO`` /
  ``XBB.TO`` / ``XAW.TO`` on the TSX); ``SPY`` stays the US listing. A bare
  ticker can resolve to a different instrument (the M144 ``VUS`` case), so the
  benchmark fetch never fans out and never search-heals.
* **The return is FX-INCLUSIVE and expressed in the reporting currency.**
  ``SPY`` is quoted in USD, so a CAD investor's benchmark return is
  ``(close_end x fx_end) / (close_start x fx_start) - 1`` — the same cached
  ``fx_rates`` book every other CAD figure reads (docs/07), so the number
  includes the currency effect the investor actually experienced. The listing
  currency and the reporting currency both ride the payload so the UI can say
  which it is showing.
* **It is a PRICE return** (distributions excluded) — the honest label the
  panel carries (docs/28 §Benchmark calculation).

Reads are cache-only: this module never opens a network call. Fetching happens
in the sweeper (``services/benchmark_refresh.py``) and the operator backfill
(``scripts/backfill_benchmarks.py``), both of which go through
``market_data.fetch_benchmark_history`` — the single yfinance seam.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.benchmark_prices import BenchmarkPrice
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.prices import PriceSource
from app.models.securities import QuoteOutcome
from app.models.trades import Trade
from app.models.users import User
from app.services import fx, market_data, valuation

log = logging.getLogger(__name__)

_CENTS = Decimal("0.01")

#: The reporting currency every CAD figure in the app is expressed in
#: (docs/01/12). A benchmark whose venue quotes in another currency is
#: converted INTO this one, so the comparison is like-for-like.
REPORTING_CURRENCY = "CAD"

#: How far back a fresh install's benchmark series reaches when the ledger has
#: no committed trade to anchor it (docs/28 decision 5: "go from the earliest
#: portfolio trade date"). One year is a usable default window; the sweeper
#: widens it to the ledger's own first trade the moment one exists.
DEFAULT_LOOKBACK_DAYS = 365

#: The four picks (docs/28 decision 1) with the LISTING VENUE each is compared
#: on. `ticker` is the exact yfinance spelling — the Canadian funds are the TSX
#: listings (`.TO`), never the bare stem, and `currency` is the currency those
#: closes are quoted in (the return conversion reads it, never a client).
BENCHMARKS: dict[str, dict[str, str]] = {
    "SPY": {
        "name": "S&P 500",
        "ticker": "SPY",
        "currency": "USD",
        "venue": "NYSE Arca",
    },
    "XIU": {
        "name": "TSX Composite",
        "ticker": "XIU.TO",
        "currency": "CAD",
        "venue": "TSX",
    },
    "XBB": {
        "name": "Canadian Bond",
        "ticker": "XBB.TO",
        "currency": "CAD",
        "venue": "TSX",
    },
    "XAW": {
        "name": "Global ex-Canada",
        "ticker": "XAW.TO",
        "currency": "CAD",
        "venue": "TSX",
    },
}

#: Iteration order for the sweeper (and the four chips the panel offers).
BENCHMARK_SYMBOLS: tuple[str, ...] = tuple(BENCHMARKS)


# ---------------------------------------------------------------------------
# Cache reads (never a network call — AGENTS §2)
# ---------------------------------------------------------------------------


def normalize_symbol(symbol: str) -> str:
    """The registry key a caller's `symbol` param names (case-insensitive)."""
    return symbol.strip().upper()


def closes_in_window(
    session: Session, symbol: str, start_date: dt.date, end_date: dt.date
) -> list[BenchmarkPrice]:
    """The cached closes inside ``[start_date, end_date]``, date-ordered.

    ``benchmark_points`` is exactly this list's length (M65: the client gates
    the EXCESS cell on ``>= 2`` of them).
    """
    return list(
        session.exec(
            select(BenchmarkPrice)
            .where(
                BenchmarkPrice.symbol == normalize_symbol(symbol),
                BenchmarkPrice.date >= start_date,
                BenchmarkPrice.date <= end_date,
            )
            .order_by(BenchmarkPrice.date)  # type: ignore[attr-defined]
        ).all()
    )


def series_bounds(session: Session, symbol: str) -> tuple[dt.date | None, dt.date | None]:
    """``(first, last)`` cached date for a symbol — a pure read.

    ``(None, None)`` means no series has been cached yet, which is what the
    sweeper's "is this symbol behind?" decision reads and what the panel
    reports as coverage.
    """
    key = normalize_symbol(symbol)
    first = session.exec(
        select(BenchmarkPrice.date)
        .where(BenchmarkPrice.symbol == key)
        .order_by(BenchmarkPrice.date)  # type: ignore[attr-defined]
    ).first()
    last = session.exec(
        select(BenchmarkPrice.date)
        .where(BenchmarkPrice.symbol == key)
        .order_by(BenchmarkPrice.date.desc())  # type: ignore[attr-defined]
    ).first()
    return first, last


def newest_cached_date(session: Session, symbol: str) -> dt.date | None:
    """Newest date the cache holds — the sweeper's freshness read."""
    return series_bounds(session, symbol)[1]


def needed_start(session: Session, today: dt.date) -> dt.date:
    """Earliest date the series must cover (docs/28 decision 5, M144's rule).

    One shared series serves every tenant's window, so it starts where the
    LONGEST-reaching portfolio does: the earliest COMMITTED trade in the
    ledger. A fresh install has none, and falls back to
    :data:`DEFAULT_LOOKBACK_DAYS` back so a tick still writes something usable.

    The committed-only predicate is `deep_backfill._committed_only`'s, minus
    its user half — this walks every tenant in one statement, exactly as the
    M144 sweep does.
    """
    committed = or_(
        ImportBatch.status == ImportBatchStatus.committed,
        Trade.import_batch_id.is_(None),
    )
    first = session.exec(
        select(Trade.date)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(committed)
        .order_by(Trade.date)  # type: ignore[attr-defined]
    ).first()
    if first is None:
        return today - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS)
    return min(first, today)


def privacy_blocks(session: Session) -> bool:
    """True when ANY user has ``privacy_mode`` on — no benchmark fetch runs.

    ``benchmark_prices`` is a SHARED cache with no ``user_id`` (docs/02), so
    there is no honest per-tenant answer: the fetch either leaves the machine
    or it does not. docs/11's promise wins over freshness, and the panel's
    honest "no benchmark data" state is what makes the resulting stale series
    visible instead of silently wrong (the M150 `fx_rates` precedent).
    """
    row = session.exec(
        select(User.id).where(User.privacy_mode.is_(True)).limit(1)  # type: ignore[arg-type]
    ).first()
    return row is not None


# ---------------------------------------------------------------------------
# Fetch + cache (the only network path for a benchmark — the sweeper calls it)
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkBackfillResult:
    """What ONE benchmark's backfill did (logged per symbol, docs/07)."""

    symbol: str
    ticker: str | None = None
    currency: str | None = None
    start: dt.date | None = None
    end: dt.date | None = None
    inserted: int = 0
    #: Machine-readable skip token; `None` when closes landed.
    skipped: str | None = None
    #: Human detail for a skip (the reason, or the exception message).
    detail: str | None = None

    def describe(self) -> str:
        if self.skipped is not None:
            suffix = f" ({self.detail})" if self.detail else ""
            return f"{self.symbol}: skipped {self.skipped}{suffix}"
        return (
            f"{self.symbol} ({self.ticker}): {self.start} -> {self.end}, "
            f"{self.inserted} close(s) inserted"
        )


def _cache_closes(
    session: Session,
    symbol: str,
    currency: str,
    closes: dict[dt.date, Decimal],
    *,
    commit: bool = True,
) -> int:
    """Insert-only: only the ``(symbol, date)`` rows the cache lacks (AGENTS §2).

    A re-run therefore adds nothing, and a ``source='manual'`` row (were one
    ever written) is never overwritten by a fetch — the same manual-wins rule
    ``prices`` and ``fx_rates`` carry.
    """
    rows = [
        {
            "symbol": normalize_symbol(symbol),
            "date": day,
            "close": close,
            "currency": currency,
            "source": PriceSource.yfinance.value,
        }
        for day, close in sorted(closes.items())
    ]
    if not rows:
        return 0
    table = BenchmarkPrice.__table__
    statement = (
        pg_insert(table)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["symbol", "date"])
        .returning(table.c.id)
    )
    inserted = len(session.execute(statement).scalars().all())
    if inserted and commit:
        session.commit()
    return inserted


def backfill_symbol(
    session: Session,
    symbol: str,
    start: dt.date,
    end: dt.date,
    *,
    dry_run: bool = False,
    commit: bool = True,
) -> BenchmarkBackfillResult:
    """Fetch ONE benchmark's series for ``[start, end]`` and cache it.

    Never raises: a privacy refusal (the M11 403 envelope) and a dead socket
    are both a REPORTED skip, so a sweep over the four picks is never aborted
    by one of them (docs/07 §Backfill depth).
    """
    key = normalize_symbol(symbol)
    result = BenchmarkBackfillResult(symbol=key, start=start, end=end)
    meta = BENCHMARKS.get(key)
    if meta is None:
        result.skipped = "unknown_benchmark"
        result.detail = f"no venue is pinned for {key!r}"
        return result
    result.ticker = meta["ticker"]
    result.currency = meta["currency"]
    # M175 (docs/07 §Provider-wait ledger): every caller READS first —
    # `benchmark_refresh.run_once`'s `symbols_behind`, `backfill_all`'s
    # `privacy_blocks`/`needed_start`, the CLI's own bounds query — and the
    # fetch is the next act, so the sweep's session sat `idle in transaction`
    # for the first symbol's fetch (and stayed open across any symbol whose
    # fetch ended in `not_found`, since nothing commits then). It is the last
    # database contact before the wait, and no caller reaches it with
    # uncommitted work: the writes are `_cache_closes` below, which commits
    # itself.
    market_data.release_before_provider_wait(session)
    # The verdict reason reported below must be THIS fetch's, never a
    # previous one's (the rule `backfill_range` follows).
    market_data.reset_quote_verdict()
    try:
        closes, outcome = market_data.fetch_benchmark_history(meta["ticker"], start, end)
    except Exception as exc:  # the seam raises its own envelope
        result.skipped = "fetch_refused"
        result.detail = str(exc)
        return result
    if not closes:
        result.skipped = "not_found" if outcome is QuoteOutcome.not_found else "fetch_unavailable"
        result.detail = market_data.last_quote_verdict()[1]
        return result
    if dry_run:
        result.inserted = len(closes)
        return result
    result.inserted = _cache_closes(session, key, meta["currency"], closes, commit=commit)
    log.info(
        "benchmark backfill: %s (%s) %s -> %s, %d close(s), skipped=%s, inserted=%d",
        key,
        meta["ticker"],
        start,
        end,
        len(closes),
        result.skipped,
        result.inserted,
    )
    return result


def backfill_all(
    session: Session,
    *,
    today: dt.date | None = None,
    start: dt.date | None = None,
    symbols: list[str] | None = None,
    dry_run: bool = False,
    commit: bool = True,
    respect_privacy: bool = True,
) -> list[BenchmarkBackfillResult]:
    """Backfill the benchmark series (docs/07 §Benchmark series).

    ``symbols`` narrows the sweep (the scheduled worker passes only the
    symbols that are behind); ``None`` covers all four. ``respect_privacy``
    (default) refuses the WHOLE sweep while any tenant has ``privacy_mode`` on
    — the cache is shared, so there is no per-tenant answer to honour, and
    fetching anyway would take one tenant's privacy decision away from the
    others.
    """
    today = today or dt.date.today()
    wanted = [normalize_symbol(s) for s in (symbols if symbols is not None else BENCHMARK_SYMBOLS)]
    if respect_privacy and privacy_blocks(session):
        return [
            BenchmarkBackfillResult(
                symbol=key,
                skipped="privacy_blocked",
                detail="a tenant has privacy_mode on (docs/11)",
            )
            for key in wanted
        ]
    window_start = start or needed_start(session, today)
    return [
        backfill_symbol(session, key, window_start, today, dry_run=dry_run, commit=commit)
        for key in wanted
    ]


# ---------------------------------------------------------------------------
# The comparison (cache-only, docs/28)
# ---------------------------------------------------------------------------


def _close_in_reporting_currency(session: Session, row: BenchmarkPrice) -> Decimal | None:
    """One close expressed in the reporting currency, using ONLY the cache.

    The rate comes from the ROW's own stated currency — the fact the fetch
    wrote from the pinned venue table — never from a caller's assumption about
    what the listing should be.

    ``None`` when the pair has no cached row on or before that date: a missing
    rate is an honest gap, never a face-value comparison across currencies
    (the same rule `services/valuation.py` applies to a holding's market
    value). No fetch — this runs on a served path.
    """
    if row.currency.upper() == REPORTING_CURRENCY:
        return row.close
    rate_row = fx.latest_rate(
        session, row.currency, REPORTING_CURRENCY, row.date, allow_fetch=False
    )
    if rate_row is None:
        return None
    return row.close * rate_row.rate


def _fx_inclusive_return(
    session: Session, first: BenchmarkPrice, last: BenchmarkPrice
) -> Decimal | None:
    """Price return between two closes, in the reporting currency (docs/28).

    ``(end x fx_end) / (start x fx_start) - 1`` — the FX effect is INSIDE the
    return, because that is the comparison a CAD investor faces when the
    index they are being compared against is quoted in USD. A same-currency
    benchmark (the three TSX funds) reduces to the plain price ratio.

    ``None`` when either endpoint cannot be expressed in the reporting
    currency: the panel shows no comparison rather than a number built from a
    missing rate. Rounded half-even to two places, the module's existing
    convention for this endpoint.
    """
    start_value = _close_in_reporting_currency(session, first)
    end_value = _close_in_reporting_currency(session, last)
    if start_value is None or end_value is None or start_value == 0:
        return None
    return ((end_value - start_value) / start_value * 100).quantize(
        _CENTS, rounding=ROUND_HALF_EVEN
    )


def compute_benchmark_comparison(
    session: Session,
    user_id: int,
    start_date: dt.date,
    end_date: dt.date,
    benchmark_symbol: str = "SPY",
) -> dict:
    """Portfolio return vs benchmark return, with the honest excess (docs/28).

    The portfolio side is unchanged (M29/M65): market value at both dates from
    the committed ledger, quantized to two places. The benchmark side is now
    REAL — the cached series' first close at/after ``start_date`` against its
    last close at/before ``end_date``, FX-inclusive into the reporting
    currency (docs/28 §Benchmark calculation).

    Honesty gates (the M65 contract, kept and extended):

    * ``benchmark_return_pct`` is ``null`` — never a numeric 0 — unless at
      least two closes landed AND both can be expressed in the reporting
      currency. A placeholder 0 next to a real portfolio return reads as
      alpha.
    * ``excess_return_pct`` is ``null`` whenever the benchmark return is, so
      the two can never disagree.
    * ``benchmark_points`` is the TRUE count of closes in the window, so the
      client's ``>= 2`` gate keeps working; ``benchmark_clipped`` says the
      requested window starts before the cached series does, and the panel
      names the coverage instead of silently comparing a shorter period.
    """
    start_positions = valuation.compute_positions(session, user_id, start_date)
    end_positions = valuation.compute_positions(session, user_id, end_date)

    start_value = sum(
        (p.get("market_value_cad") or Decimal("0") for p in start_positions),
        Decimal("0"),
    )
    end_value = sum(
        (p.get("market_value_cad") or Decimal("0") for p in end_positions),
        Decimal("0"),
    )

    portfolio_return = (
        (end_value - start_value) / start_value * 100 if start_value > 0 else Decimal("0")
    )
    portfolio_return = portfolio_return.quantize(_CENTS, rounding=ROUND_HALF_EVEN)

    key = normalize_symbol(benchmark_symbol)
    meta = BENCHMARKS.get(key)
    series = closes_in_window(session, key, start_date, end_date)
    series_start, series_end = series_bounds(session, key)

    benchmark_return: Decimal | None = None
    if meta is not None and len(series) >= 2:
        benchmark_return = _fx_inclusive_return(session, series[0], series[-1])

    excess_return = (
        None
        if benchmark_return is None
        else (portfolio_return - benchmark_return).quantize(_CENTS, rounding=ROUND_HALF_EVEN)
    )

    # "Clipped" is about the START of the window: the series begins late, so
    # the comparison covers a shorter period than asked. A window that ends
    # after the newest cached close is the ordinary "the market has not
    # published today's bar yet" case, which `benchmark_series_end` reports
    # without calling it clipped.
    clipped = series_start is not None and series_start > start_date

    return {
        "portfolio_start": str(start_value),
        "portfolio_end": str(end_value),
        "portfolio_return_pct": str(portfolio_return),
        "benchmark_symbol": key,
        "benchmark_name": meta["name"] if meta else key,
        "benchmark_return_pct": None if benchmark_return is None else str(benchmark_return),
        "benchmark_points": len(series),
        # Which currency the benchmark return is EXPRESSED in, and which the
        # venue quotes it in — the panel labels the comparison with both, so
        # an FX-inclusive SPY number is never mistaken for a USD one. The
        # listing currency is the SERIES' own (what the closes were actually
        # quoted in), falling back to the pinned venue table when nothing is
        # cached yet.
        "benchmark_currency": REPORTING_CURRENCY,
        "benchmark_listing_currency": (
            series[0].currency if series else (meta["currency"] if meta else None)
        ),
        "benchmark_series_start": series_start.isoformat() if series_start else None,
        "benchmark_series_end": series_end.isoformat() if series_end else None,
        "benchmark_clipped": clipped,
        "excess_return_pct": None if excess_return is None else str(excess_return),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }

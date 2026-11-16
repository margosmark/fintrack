"""M173: the vs-Benchmark panel gets a REAL series (docs/28 decision 5).

Until M173 ``compute_benchmark_comparison`` returned a hard-coded
``benchmark_return = 0`` / ``benchmark_points = 0`` — an honest stub, gated
client-side so it could never read as alpha, but never a comparison. These
pins cover the four things that make it real:

* the four picks are pinned to their actual LISTING VENUES (SPY on NYSE Arca in
  USD, ``XIU.TO``/``XBB.TO``/``XAW.TO`` on the TSX in CAD) and cached against a
  BENCHMARK symbol, never a user ``securities`` row;
* the return is FX-INCLUSIVE into the reporting currency and hand-recomputed
  here from the closes and the cached rates;
* a missing rate, a short series or a window before the coverage is an honest
  ``null`` with a true ``benchmark_points`` — never a numeric 0 that reads as
  alpha;
* the served path and the sweeper are cache-only / privacy-gated as documented.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import ROUND_HALF_EVEN, Decimal

from sqlmodel import select

import app.services.market_data as market_data
from app.core.security import create_access_token
from app.models.accounts import Account, AccountType
from app.models.benchmark_prices import BenchmarkPrice
from app.models.fx_rates import FxRate, FxSource
from app.models.prices import Price, PriceSource
from app.models.securities import QuoteOutcome, Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.services import benchmark_refresh, benchmarks

START = dt.date(2025, 1, 2)
END = dt.date(2025, 1, 31)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _user(session, *, privacy_mode: bool = False) -> User:
    user = User(
        email=f"{uuid.uuid4()}@example.com",
        hashed_password="x",
        privacy_mode=privacy_mode,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _holding(session, user: User, *, symbol: str = "VEQT") -> tuple[Account, Security]:
    """A 10-share CAD position worth 1000 at START and 1100 at END (+10%)."""
    account = Account(user_id=user.id, name="TFSA", type=AccountType.tfsa, currency="CAD")
    security = Security(
        user_id=user.id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind=SecurityKind.etf,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(account)
    session.add(security)
    session.commit()
    session.refresh(account)
    session.refresh(security)
    trade = Trade(
        user_id=user.id,
        account_id=account.id,
        security_id=security.id,
        date=START,
        action=TradeAction.buy,
        quantity=Decimal("10"),
        price_native=Decimal("100"),
        currency="CAD",
    )
    session.add(trade)
    for day, price in ((START, Decimal("100")), (END, Decimal("110"))):
        session.add(
            Price(
                user_id=user.id,
                security_id=security.id,
                date=day,
                price=price,
                source=PriceSource.manual,
            )
        )
    session.commit()
    return account, security


def _cache_only_positions(monkeypatch) -> None:
    """Positions read the CACHE — no `ensure_recent_price` fetch in a unit pin."""

    def fake_ensure_recent_price(db, sec, today, **_kwargs):
        return market_data.newest_price(db, sec.id, today)

    monkeypatch.setattr(market_data, "ensure_recent_price", fake_ensure_recent_price)


def _benchmark_row(
    session, symbol: str, day: dt.date, close: str, currency: str = "CAD"
) -> BenchmarkPrice:
    row = BenchmarkPrice(
        symbol=symbol,
        date=day,
        close=Decimal(close),
        currency=currency,
        source=PriceSource.yfinance,
    )
    session.add(row)
    session.commit()
    return row


def _fx(session, day: dt.date, rate: str, *, base: str = "USD") -> FxRate:
    row = FxRate(base=base, quote="CAD", date=day, rate=Decimal(rate), source=FxSource.boc_valet)
    session.add(row)
    session.commit()
    return row


def _stub_fetch(monkeypatch, series: dict[str, dict[dt.date, Decimal]]) -> list[tuple]:
    """Replace the yfinance seam with a frozen series; record every call.

    `series` is keyed by the VENUE TICKER (`SPY`, `XIU.TO`, …) — the spelling
    the seam actually receives, so a test that keys by benchmark name would
    silently stub nothing.
    """
    calls: list[tuple] = []

    def fake(symbol, start, end):
        calls.append((symbol, start, end))
        closes = {d: c for d, c in series.get(symbol, {}).items() if start <= d <= end}
        return closes, QuoteOutcome.ok if closes else QuoteOutcome.not_found

    monkeypatch.setattr(market_data, "fetch_benchmark_history", fake)
    return calls


def _daily(closes: list[str], *, first: dt.date = START) -> dict[dt.date, Decimal]:
    return {first + dt.timedelta(days=i): Decimal(c) for i, c in enumerate(closes)}


def _venue_series(closes: list[str]) -> dict[str, dict[dt.date, Decimal]]:
    """The same frozen closes for all four picks, keyed by venue ticker."""
    return {
        benchmarks.BENCHMARKS[key]["ticker"]: _daily(closes) for key in benchmarks.BENCHMARK_SYMBOLS
    }


# ---------------------------------------------------------------------------
# The picks and their venues
# ---------------------------------------------------------------------------


def test_the_four_picks_are_pinned_to_their_real_listing_venues() -> None:
    """docs/28 decision 5: the CANADIAN listing where the index is CA-relevant."""
    assert benchmarks.BENCHMARK_SYMBOLS == ("SPY", "XIU", "XBB", "XAW")
    spy = benchmarks.BENCHMARKS["SPY"]
    assert (spy["ticker"], spy["currency"], spy["venue"]) == ("SPY", "USD", "NYSE Arca")
    for key in ("XIU", "XBB", "XAW"):
        meta = benchmarks.BENCHMARKS[key]
        assert meta["ticker"] == f"{key}.TO", "the TSX listing, never the bare stem"
        assert meta["currency"] == "CAD"
        assert meta["venue"] == "TSX"


def test_a_benchmark_is_never_a_user_security(session, monkeypatch) -> None:
    """The cache keys on the BENCHMARK symbol; no `securities` row is created."""
    _stub_fetch(monkeypatch, _venue_series(["10", "11"]))
    benchmarks.backfill_all(session, today=END, start=START)
    symbols = {row.symbol for row in session.exec(select(BenchmarkPrice)).all()}
    assert symbols == set(benchmarks.BENCHMARK_SYMBOLS)
    assert session.exec(select(Security)).all() == []


# ---------------------------------------------------------------------------
# Backfill + coverage
# ---------------------------------------------------------------------------


def test_backfill_seeds_all_four_and_is_insert_only(session, monkeypatch) -> None:
    series = _venue_series(["10", "11", "12"])
    calls = _stub_fetch(monkeypatch, series)

    results = benchmarks.backfill_all(session, today=END, start=START)

    assert [r.symbol for r in results] == list(benchmarks.BENCHMARK_SYMBOLS)
    assert all(r.skipped is None for r in results)
    assert all(r.inserted == 3 for r in results)
    assert len(calls) == 4
    # The venue ticker — not the benchmark key — is what reaches yfinance.
    assert {call[0] for call in calls} == {"SPY", "XIU.TO", "XBB.TO", "XAW.TO"}
    for key in benchmarks.BENCHMARK_SYMBOLS:
        assert len(benchmarks.closes_in_window(session, key, START, END)) == 3
        assert benchmarks.series_bounds(session, key) == (START, START + dt.timedelta(days=2))

    # Insert-only: a re-run adds nothing (docs/28 — the cache is manual-wins).
    again = benchmarks.backfill_all(session, today=END, start=START)
    assert sum(r.inserted for r in again) == 0


def test_every_chip_produces_points_over_the_window(session, monkeypatch) -> None:
    """>= 2 points per pick — the client's M65 gate opens for all four."""
    _cache_only_positions(monkeypatch)
    user = _user(session)
    _holding(session, user)
    # A flat USD/CAD, so the USD-listed SPY chip is comparable too (the three
    # TSX picks need no rate at all).
    _fx(session, START, "1.40")
    _fx(session, END, "1.40")
    series = _venue_series(["10", "11", "12"])
    _stub_fetch(monkeypatch, series)
    benchmarks.backfill_all(session, today=END, start=START)

    for key in benchmarks.BENCHMARK_SYMBOLS:
        result = benchmarks.compute_benchmark_comparison(session, user.id, START, END, key)
        assert result["benchmark_points"] == 3, key
        assert result["benchmark_return_pct"] == "20.00", key
        assert result["benchmark_name"] == benchmarks.BENCHMARKS[key]["name"]


# ---------------------------------------------------------------------------
# The return math
# ---------------------------------------------------------------------------


def test_benchmark_return_is_fx_inclusive_and_hand_computed(session, monkeypatch) -> None:
    """SPY is USD: the CAD investor's return includes the currency effect.

    hand math: closes 580.00 (2025-01-02) and 609.00 (2025-01-31) at cached
    USD/CAD 1.43 and 1.50 → (609 x 1.50) / (580 x 1.43) - 1 = 10.1398…% →
    10.14. The USD-only reading (609/580 - 1 = 5.00%) is asserted to DIFFER, so
    this test fails if the conversion is ever dropped.
    """
    _cache_only_positions(monkeypatch)
    user = _user(session)
    _holding(session, user)
    _benchmark_row(session, "SPY", START, "580.00", "USD")
    _benchmark_row(session, "SPY", END, "609.00", "USD")
    _fx(session, START, "1.43")
    _fx(session, END - dt.timedelta(days=1), "1.50")

    result = benchmarks.compute_benchmark_comparison(session, user.id, START, END, "SPY")

    expected = (
        (Decimal("609.00") * Decimal("1.50")) / (Decimal("580.00") * Decimal("1.43")) - 1
    ) * 100
    expected = expected.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    assert expected == Decimal("10.14")
    assert result["benchmark_return_pct"] == str(expected)
    assert result["benchmark_return_pct"] != "5.00"  # the un-converted reading
    assert result["benchmark_listing_currency"] == "USD"
    assert result["benchmark_currency"] == "CAD"
    # portfolio +10.00% against a +10.14% benchmark → -0.14 excess, computed
    # SERVER-side and expressed as a Decimal string like every other pct field.
    assert result["portfolio_return_pct"] == "10.00"
    assert result["excess_return_pct"] == "-0.14"


def test_a_cad_benchmark_needs_no_fx_row(session, monkeypatch) -> None:
    """XIU/XBB/XAW quote in the reporting currency: a plain price ratio."""
    _cache_only_positions(monkeypatch)
    user = _user(session)
    _holding(session, user)
    _benchmark_row(session, "XIU", START, "30.00", "CAD")
    _benchmark_row(session, "XIU", END, "33.00", "CAD")

    result = benchmarks.compute_benchmark_comparison(session, user.id, START, END, "XIU")

    assert result["benchmark_return_pct"] == "10.00"
    assert result["benchmark_listing_currency"] == "CAD"
    assert result["excess_return_pct"] == "0.00"


def test_a_missing_rate_is_an_honest_gap_not_a_face_value_number(session, monkeypatch) -> None:
    """No cached USD/CAD: the return is null — never a USD number in a CAD cell."""
    _cache_only_positions(monkeypatch)
    user = _user(session)
    _holding(session, user)
    _benchmark_row(session, "SPY", START, "580.00", "USD")
    _benchmark_row(session, "SPY", END, "609.00", "USD")

    result = benchmarks.compute_benchmark_comparison(session, user.id, START, END, "SPY")

    assert result["benchmark_points"] == 2  # the truth, whatever else is missing
    assert result["benchmark_return_pct"] is None
    assert result["excess_return_pct"] is None  # never portfolio-minus-a-placeholder


def test_a_window_before_coverage_is_clipped_and_reports_no_return(session, monkeypatch) -> None:
    _cache_only_positions(monkeypatch)
    user = _user(session)
    _holding(session, user)
    later = dt.date(2025, 6, 2)
    _benchmark_row(session, "SPY", later, "600.00", "CAD")
    _benchmark_row(session, "SPY", later + dt.timedelta(days=1), "606.00", "CAD")

    result = benchmarks.compute_benchmark_comparison(session, user.id, START, END, "SPY")

    assert result["benchmark_points"] == 0
    assert result["benchmark_return_pct"] is None
    assert result["excess_return_pct"] is None
    assert result["benchmark_clipped"] is True
    assert result["benchmark_series_start"] == later.isoformat()
    assert result["benchmark_series_end"] == (later + dt.timedelta(days=1)).isoformat()


def test_a_covered_window_is_not_clipped(session, monkeypatch) -> None:
    _cache_only_positions(monkeypatch)
    user = _user(session)
    _holding(session, user)
    _benchmark_row(session, "XIU", START - dt.timedelta(days=5), "29.00", "CAD")
    _benchmark_row(session, "XIU", END, "33.00", "CAD")

    result = benchmarks.compute_benchmark_comparison(session, user.id, START, END, "XIU")

    assert result["benchmark_clipped"] is False
    assert result["benchmark_points"] == 1  # only END is inside the window
    assert result["benchmark_return_pct"] is None  # one point is not a return


def test_an_unknown_symbol_answers_empty_rather_than_inventing_a_series() -> None:
    assert benchmarks.normalize_symbol(" spy ") == "SPY"
    assert "NOPE" not in benchmarks.BENCHMARKS


# ---------------------------------------------------------------------------
# Privacy + the cache-only read path
# ---------------------------------------------------------------------------


def test_privacy_mode_blocks_the_whole_sweep(session, monkeypatch) -> None:
    """A SHARED cache cannot be fetched for one tenant and not another (docs/11)."""
    _user(session, privacy_mode=True)
    monkeypatch.setattr(
        market_data,
        "fetch_benchmark_history",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch under privacy_mode")),
    )

    results = benchmarks.backfill_all(session, today=END, start=START)

    assert [r.skipped for r in results] == ["privacy_blocked"] * 4
    assert session.exec(select(BenchmarkPrice)).all() == []


def test_the_sweeper_skips_every_symbol_under_privacy(session, monkeypatch) -> None:
    _user(session, privacy_mode=True)
    monkeypatch.setattr(
        market_data,
        "fetch_benchmark_history",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch under privacy_mode")),
    )

    stats = benchmark_refresh.run_once(session, today=END)

    assert stats.privacy_blocked is True
    assert stats.refreshed == []
    assert stats.rows_added == 0


def test_the_sweeper_only_fetches_symbols_that_are_behind(session, monkeypatch) -> None:
    calls = _stub_fetch(
        monkeypatch,
        _venue_series(["10", "11"]),
    )
    _benchmark_row(session, "SPY", END, "600.00", "USD")  # already current

    stats = benchmark_refresh.run_once(session, today=END)

    assert stats.current == ["SPY"]
    assert set(stats.updated) == {"XIU", "XBB", "XAW"}
    assert {call[0] for call in calls} == {"XIU.TO", "XBB.TO", "XAW.TO"}


def test_the_served_path_opens_no_network_call(client, session, monkeypatch) -> None:
    """Performance contract: reads are cache-only; fetches live in the sweeper."""
    _cache_only_positions(monkeypatch)
    user = _user(session)
    _holding(session, user)
    _benchmark_row(session, "SPY", START, "580.00", "CAD")
    _benchmark_row(session, "SPY", END, "609.00", "CAD")

    def boom(*args, **kwargs):
        raise AssertionError("the benchmark read path must never fetch")

    monkeypatch.setattr(market_data, "fetch_benchmark_history", boom)
    monkeypatch.setattr(market_data, "fetch_history", boom)
    monkeypatch.setattr(market_data, "fetch_latest_close", boom)

    token = create_access_token(user.id)
    response = client.get(
        "/api/v1/portfolio/benchmarks",
        params={"start_date": START.isoformat(), "end_date": END.isoformat(), "symbol": "SPY"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["benchmark_points"] == 2
    assert body["benchmark_return_pct"] == "5.00"
    assert body["excess_return_pct"] == "5.00"


def test_wire_shape_carries_the_honesty_fields(client, session, monkeypatch) -> None:
    """The payload the panel reads: strings for money/pct, null when unknown."""
    _cache_only_positions(monkeypatch)
    user = _user(session)
    _holding(session, user)
    token = create_access_token(user.id)

    response = client.get(
        "/api/v1/portfolio/benchmarks",
        params={"start_date": START.isoformat(), "end_date": END.isoformat(), "symbol": "SPY"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["benchmark_points"] == 0
    assert body["benchmark_return_pct"] is None
    assert body["excess_return_pct"] is None
    assert body["benchmark_currency"] == "CAD"
    assert body["benchmark_listing_currency"] == "USD"
    assert body["benchmark_series_start"] is None
    assert body["benchmark_clipped"] is False
    assert body["portfolio_return_pct"] == "10.00"


# ---------------------------------------------------------------------------
# The frame (M149's other half)
# ---------------------------------------------------------------------------


class _FakeIndex:
    def __init__(self, day: dt.date) -> None:
        self._day = day

    def date(self) -> dt.date:
        return self._day


class _FakeFrame:
    """Just enough of a DataFrame for `_history_rows` (docs/07 seam)."""

    empty = False

    def __init__(self, rows: list[tuple[dt.date, dict]]) -> None:
        self._rows = [(_FakeIndex(day), cells) for day, cells in rows]

    def iterrows(self):
        return iter(self._rows)


def _stub_ticker(monkeypatch, frame: _FakeFrame) -> None:
    class _Ticker:
        def history(self, **_kwargs):
            return frame

    monkeypatch.setattr(market_data, "_ticker", lambda symbol: _Ticker())


def test_the_benchmark_frame_keeps_the_providers_split_adjusted_close(monkeypatch) -> None:
    """A 2:1 split must not read as -50% on the benchmark side.

    `prices` un-adjusts to match the ledger's quantities (docs/07 §Price frame);
    a benchmark has no ledger, so it keeps the provider's frame — the same
    post-split close on both sides of the event.
    """
    frame = _FakeFrame(
        [
            (dt.date(2025, 1, 2), {"Close": 100.0, "Stock Splits": 0.0}),
            (dt.date(2025, 1, 14), {"Close": 104.0, "Stock Splits": 0.0}),
            (dt.date(2025, 1, 15), {"Close": 52.5, "Stock Splits": 2.0}),
            (dt.date(2025, 1, 31), {"Close": 55.0, "Stock Splits": 0.0}),
        ]
    )
    _stub_ticker(monkeypatch, frame)

    benchmark_closes, outcome = market_data.fetch_benchmark_history("SPY", START, END)
    ledger_closes = market_data.fetch_history("TEST", START, END)

    assert outcome is QuoteOutcome.ok
    assert benchmark_closes[dt.date(2025, 1, 2)] == Decimal("100.0")
    assert benchmark_closes[dt.date(2025, 1, 31)] == Decimal("55.0")
    # The ledger frame un-adjusts the pre-split bars — the difference is the point.
    assert ledger_closes[dt.date(2025, 1, 2)] == Decimal("200.0")

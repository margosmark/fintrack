"""M141 — no-quote symbols must not hang the UI in "yet"-forever (docs/07 §Dead symbols).

The reported defect (Mark, live): adding "APOTEX" — Apotex went private in
2022, no exchange listing — created a security + a watchlist row, and the app
then showed "No prices yet — nothing has been fetched for this symbol **yet**"
over a big blue "Refresh prices" button that could never succeed, while the
fan-out logged four identical yfinance 404s in one day.

The fix has three halves, each pinned here:

1. A fetch failure is CLASSIFIED, not swallowed — "quote not found" is a
   verdict about the symbol; a rate-limit is a verdict about the network.
   Three consecutive verdicts set ``dead_symbol``.
2. The automatic fan-out (positions, ``POST /portfolio/refresh``, the
   security-overview auto-refresh) stops fetching a dead row. The manual
   "Try refresh" is the ONE path that still probes it, so the flag can be
   disproved and clears itself on the next real close.
3. The add form gets a lightweight quote probe (``GET /securities/quote-check``)
   that warns without blocking.

Network edges are monkeypatched throughout (docs/07 network discipline); the
classification itself is unit-tested against the REAL yfinance exception
classes so a library change cannot silently turn a dead symbol back into an
unbounded retry loop.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.market_data as market_data
from app.models.prices import Price, PriceSource
from app.models.securities import QuoteOutcome, Security
from app.models.users import User
from app.models.watchlist import WatchlistItem
from app.services import benchmarks, deep_backfill, split_repair
from app.services.market_data import CandidateSeries

API = "/api/v1"
SEC_URL = f"{API}/securities"
D = Decimal


@pytest.fixture(autouse=True)
def _no_search_fallback(monkeypatch) -> None:
    """M142: `fetch_history` falls back to Yahoo's SEARCH endpoint once both
    candidates miss. This module pins the dead-symbol VERDICT, which is a
    verdict about the STORED spelling, so the fallback is pinned to "no hit"
    and no test here can reach a network through it (docs/07)."""
    monkeypatch.setattr(market_data, "_search_equity_quotes", lambda query: [])


# The verdict yfinance 1.6.0 actually raises for APOTEX (LIVE PROBE,
# 2026-10-10): `YFTzMissingError("$APOTEX: possibly delisted; no timezone
# found")`, with the HTTP layer logging `404 Quote not found for symbol:
# APOTEX`. Subclassed from YFTickerMissingError, which is the base the
# classifier keys on.
DEAD_MESSAGE = "$APOTEX: possibly delisted; no timezone found"


# --- classification (unit, real yfinance exception classes) -------------------


def test_delisted_exception_is_a_verdict_about_the_symbol() -> None:
    from yfinance.exceptions import YFTzMissingError

    outcome, reason = market_data._classify_history_error(YFTzMissingError("APOTEX"))
    assert outcome is QuoteOutcome.not_found
    assert "delisted" in reason


def test_prices_missing_exception_is_also_not_found() -> None:
    from yfinance.exceptions import YFPricesMissingError

    outcome, _ = market_data._classify_history_error(YFPricesMissingError("AAPL1.BE", "1d, 5d"))
    assert outcome is QuoteOutcome.not_found


def test_rate_limit_is_never_a_verdict_about_the_symbol() -> None:
    from yfinance.exceptions import YFRateLimitError

    outcome, _ = market_data._classify_history_error(YFRateLimitError())
    assert outcome is QuoteOutcome.unavailable


def test_unknown_exception_is_unavailable_not_not_found() -> None:
    """A socket error must not march a live symbol toward the dead flag."""
    outcome, _ = market_data._classify_history_error(ConnectionResetError("connection reset"))
    assert outcome is QuoteOutcome.unavailable


def test_one_flaky_candidate_dominates_the_not_founds() -> None:
    """`APOTEX` + `APOTEX.TO` both 404 = a verdict. One network failure out of
    two means we never actually learned anything about the symbol."""
    assert (
        market_data._combine_outcomes([QuoteOutcome.not_found, QuoteOutcome.not_found])
        is QuoteOutcome.not_found
    )
    assert (
        market_data._combine_outcomes([QuoteOutcome.not_found, QuoteOutcome.unavailable])
        is QuoteOutcome.unavailable
    )
    assert market_data._combine_outcomes([QuoteOutcome.not_found, QuoteOutcome.ok]) is (
        QuoteOutcome.ok
    )
    assert market_data._combine_outcomes([]) is QuoteOutcome.unknown


def test_fetch_history_publishes_its_verdict_on_the_context(monkeypatch) -> None:
    """The session-aware seams read the verdict from the request context — the
    fetch layer itself has no session (docs/07)."""
    import yfinance

    class MissingTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **_kwargs):
            from yfinance.exceptions import YFTzMissingError

            raise YFTzMissingError(self.symbol)

    monkeypatch.setattr(yfinance, "Ticker", MissingTicker)
    assert market_data.fetch_history("APOTEX", date(2026, 1, 1), date(2026, 10, 10)) == {}
    assert market_data._last_quote_outcome.get() is QuoteOutcome.not_found
    assert "delisted" in (market_data._last_quote_reason.get() or "")


def test_fetch_provenance_does_not_survive_failed_or_guarded_calls(monkeypatch) -> None:
    """Only the provider answer for the current fetch may stamp a cache write."""
    import pandas as pd
    import yfinance

    frame = pd.DataFrame({"Close": [11.5]}, index=pd.to_datetime(["2026-10-09"]))

    class Ticker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **_kwargs):
            if self.symbol == "AAPL":
                return frame
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    start, end = date(2026, 10, 1), date(2026, 10, 10)

    assert market_data.fetch_history("AAPL", start, end)
    assert market_data.last_fetch_at() is not None

    assert market_data.fetch_history("FAILED", start, end) == {}
    assert market_data.last_fetch_at() is None

    # A freshness/cache-only return cannot inherit provenance either.
    market_data._last_fetch_at.set(datetime.now(UTC))
    cached_price = SimpleNamespace(date=end)
    cached_security = Security(
        id=1,
        user_id=1,
        symbol="AAPL",
        name="Apple Inc.",
        kind="stock",
        listing_currency="USD",
        exchange="",
    )
    monkeypatch.setattr(market_data, "newest_price", lambda *_args: cached_price)
    assert market_data.ensure_recent_price(None, cached_security, end) is cached_price
    assert market_data.last_fetch_at() is None

    # The guard clears stale state before refusing an external call.
    market_data._last_fetch_at.set(datetime.now(UTC))
    market_data.bind_privacy_lock(True)
    try:
        with pytest.raises(Exception, match="Privacy mode blocks"):
            market_data.fetch_history("BLOCKED", start, end)
        assert market_data.last_fetch_at() is None
    finally:
        market_data.bind_privacy_lock(False)


def test_fetch_history_publishes_ok_when_a_close_comes_back(monkeypatch) -> None:
    import pandas as pd
    import yfinance

    frame = pd.DataFrame({"Close": [11.5]}, index=pd.to_datetime(["2026-10-09"]))

    class LiveTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **_kwargs):
            return frame

    monkeypatch.setattr(yfinance, "Ticker", LiveTicker)
    closes = market_data.fetch_history("AAPL", date(2026, 10, 1), date(2026, 10, 10))
    assert closes == {date(2026, 10, 9): D("11.5")}
    assert market_data._last_quote_outcome.get() is QuoteOutcome.ok
    assert market_data._last_quote_reason.get() is None


# --- record_quote_outcome (the counter state machine) -------------------------


def _security(session: Session, user: User, symbol: str = "APOTEX") -> Security:
    row = Security(
        user_id=user.id,
        symbol=symbol,
        name=f"{symbol} Inc.",
        kind="stock",
        listing_currency="USD",
        exchange="",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_three_consecutive_not_founds_set_the_flag(session: Session, client: TestClient) -> None:
    uid = _user(session, client)
    security = _security(session, session.get(User, uid))
    for i in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, security, QuoteOutcome.not_found, DEAD_MESSAGE)
        session.refresh(security)
        expected = i + 1
        assert security.quote_miss_count == expected
        assert security.dead_symbol is (expected >= market_data.DEAD_SYMBOL_MISSES)
    assert security.dead_symbol is True
    assert security.dead_symbol_reason == DEAD_MESSAGE


def test_a_rate_limit_never_advances_the_counter(session: Session, client: TestClient) -> None:
    uid = _user(session, client)
    security = _security(session, session.get(User, uid), "FLAKY1")
    for _ in range(5):
        market_data.record_quote_outcome(session, security, QuoteOutcome.unavailable, "429")
        session.refresh(security)
    assert security.quote_miss_count == 0
    assert security.dead_symbol is False


def test_unknown_neither_advances_nor_clears(session: Session, client: TestClient) -> None:
    """A monkeypatched seam or a cached read reaches this with `unknown`; that
    is evidence of nothing and must move neither direction."""
    uid = _user(session, client)
    security = _security(session, session.get(User, uid), "UNKN1")
    market_data.record_quote_outcome(session, security, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.refresh(security)
    assert security.quote_miss_count == 1
    market_data.record_quote_outcome(session, security, QuoteOutcome.unknown, None)
    session.refresh(security)
    assert security.quote_miss_count == 1  # neither advanced nor reset


def test_a_real_quote_clears_the_flag_without_manual_action(
    session: Session, client: TestClient
) -> None:
    """Auto-clear: the card's "auto-clear without manual action if a symbol
    ever DOES return a real quote"."""
    uid = _user(session, client)
    security = _security(session, session.get(User, uid), "REVIVE1")
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, security, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.refresh(security)
    assert security.dead_symbol is True

    market_data.record_quote_outcome(session, security, QuoteOutcome.ok, None)
    session.refresh(security)
    assert security.quote_miss_count == 0
    assert security.dead_symbol is False
    assert security.dead_symbol_reason is None


# --- the fan-out gate ---------------------------------------------------------


def _user(session: Session, client: TestClient) -> int:
    """Register + log in, returning the user id (the session-scoped fixtures
    below build securities directly rather than through the API)."""
    headers = _auth(client)
    return int(client.get(f"{API}/users/me", headers=headers).json()["id"])


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m141-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


# Captured at import, BEFORE `offline_market` stubs the seams: the tests that
# exercise the dead-symbol gate need the REAL `backfill_range` / `ensure_recent_price`
# (the gate lives inside them), with only the yfinance leaf beneath faked.
REAL_BACKFILL_RANGE = market_data.backfill_range
REAL_ENSURE_RECENT_PRICE = market_data.ensure_recent_price


@pytest.fixture(autouse=True)
def offline_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """No yfinance in this module (docs/07 network discipline)."""
    monkeypatch.setattr(market_data, "backfill_range", lambda *a, **k: 0)
    monkeypatch.setattr(market_data, "ensure_fundamentals", lambda *a, **k: None)
    monkeypatch.setattr(market_data, "fetch_latest_close", lambda symbol, today=None: None)


def _restore_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the real gate-carrying functions back (see REAL_* above)."""
    monkeypatch.setattr(market_data, "backfill_range", REAL_BACKFILL_RANGE)
    monkeypatch.setattr(market_data, "ensure_recent_price", REAL_ENSURE_RECENT_PRICE)


def test_backfill_skips_a_dead_symbol_and_stops_the_404_fanout(
    session: Session, client: TestClient, monkeypatch
) -> None:
    """The log-noise fix: after the flag, `backfill_range` makes NO network
    call at all — not a failed one, none."""
    headers = _auth(client)
    uid = client.get(f"{API}/users/me", headers=headers).json()["id"]
    security = _security(session, session.get(User, uid), "DEADA")
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, security, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.refresh(security)
    assert security.dead_symbol is True

    calls: list[str] = []

    def record(symbol, start_date, end_date):
        calls.append(symbol)
        return {}

    monkeypatch.setattr(market_data, "fetch_history", record)
    inserted = market_data.backfill_range(
        session, security, date.today() - timedelta(days=365), date.today()
    )
    assert inserted == 0
    assert calls == []  # the fan-out STOPPED


def test_backfill_still_probes_a_dead_symbol_when_forced(
    session: Session, client: TestClient, monkeypatch
) -> None:
    """`probe_dead=True` is the manual "Try refresh" — the escape hatch."""
    headers = _auth(client)
    uid = client.get(f"{API}/users/me", headers=headers).json()["id"]
    security = _security(session, session.get(User, uid), "DEADB")
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, security, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.refresh(security)
    _restore_gates(monkeypatch)

    calls: list[str] = []
    monkeypatch.setattr(
        market_data, "fetch_history", lambda symbol, s, e: calls.append(symbol) or {}
    )
    market_data.backfill_range(
        session, security, date.today() - timedelta(days=365), date.today(), probe_dead=True
    )
    assert calls == [security.symbol]


def test_a_successful_forced_probe_clears_the_flag_end_to_end(
    session: Session, client: TestClient, monkeypatch
) -> None:
    """A relisted symbol heals with no manual flag editing."""
    import pandas as pd

    headers = _auth(client)
    uid = client.get(f"{API}/users/me", headers=headers).json()["id"]
    security = _security(session, session.get(User, uid), "RELIST")
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, security, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.refresh(security)
    assert security.dead_symbol is True
    _restore_gates(monkeypatch)

    import yfinance

    frame = pd.DataFrame({"Close": [42.0]}, index=pd.to_datetime([date.today()]))

    class LiveTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **_kwargs):
            return frame

    monkeypatch.setattr(yfinance, "Ticker", LiveTicker)
    market_data.backfill_range(
        session, security, date.today() - timedelta(days=30), date.today(), probe_dead=True
    )
    session.refresh(security)
    assert security.dead_symbol is False
    assert security.quote_miss_count == 0


def test_ensure_recent_price_honours_the_gate_even_when_forced(
    session: Session, client: TestClient, monkeypatch
) -> None:
    """The market-hours fan-out passes ``force=True`` for FRESHNESS. It must
    still be gated — that is the exact path that produced Mark's 404s."""
    headers = _auth(client)
    uid = client.get(f"{API}/users/me", headers=headers).json()["id"]
    security = _security(session, session.get(User, uid), "GATED1")
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, security, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.refresh(security)

    calls: list[str] = []
    monkeypatch.setattr(
        market_data, "fetch_latest_close", lambda symbol, today=None: calls.append(symbol)
    )
    market_data.ensure_recent_price(session, security, date.today(), force=True)
    assert calls == []

    # ...but `probe_dead=True` gets through.
    market_data.ensure_recent_price(session, security, date.today(), force=True, probe_dead=True)
    assert calls == [security.symbol]


def test_ensure_fundamentals_serves_a_dead_symbol_from_cache(
    session: Session, client: TestClient, monkeypatch
) -> None:
    headers = _auth(client)
    uid = client.get(f"{API}/users/me", headers=headers).json()["id"]
    security = _security(session, session.get(User, uid), "FUND1")
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, security, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.refresh(security)

    calls: list[str] = []
    monkeypatch.setattr(
        market_data, "fetch_fundamentals", lambda symbol: calls.append(symbol) or None
    )
    assert market_data.ensure_fundamentals(session, security, date.today()) is None
    assert calls == []  # no Ticker.info 404 either


# --- the verdict must belong to THIS call ------------------------------------

_STALE_VERDICT_REASON = "$S11.TO: possibly delisted; no timezone found"


def _leave_a_previous_fetchs_verdict() -> None:
    """Leave the CONTEXT carrying a verdict some EARLIER fetch published.

    The starting poison for every seam pin in this section: a seam that
    resets before its own fetch must read `unknown` and book nothing even
    though the context still says `not_found`.
    """
    market_data._last_quote_outcome.set(QuoteOutcome.not_found)
    market_data._last_quote_reason.set(_STALE_VERDICT_REASON)


def test_a_seam_books_only_the_verdict_its_own_fetch_produced(
    session: Session, client: TestClient, monkeypatch
) -> None:
    """The M141 verdict rides the request context — so it outlives the call
    that published it AND the test that published it.

    A seam whose fetch never reaches yfinance publishes nothing (a test
    double, which is also how a caller substitutes its fetch), so it must read
    `unknown` and book NOTHING. Reading a previous fetch's verdict instead
    records a miss this call never saw — and `record_quote_outcome` commits,
    so a stale `not_found` also commits whatever the caller had pending: the
    dry-run rollback boundary `scripts/repair_symbol.py` relies on (the
    t_001ed90d order-dependent red).
    """
    uid = _user(session, client)
    security = _security(session, session.get(User, uid), "STALEV")
    _restore_gates(monkeypatch)

    try:
        _leave_a_previous_fetchs_verdict()
        monkeypatch.setattr(market_data, "fetch_history", lambda symbol, s, e: {})
        market_data.backfill_range(
            session, security, date.today() - timedelta(days=30), date.today()
        )
        session.refresh(security)
        assert security.quote_miss_count == 0, (
            "backfill_range booked a verdict its own fetch never produced"
        )
        assert security.dead_symbol_reason is None

        _leave_a_previous_fetchs_verdict()
        monkeypatch.setattr(market_data, "fetch_latest_close", lambda symbol, today=None: None)
        market_data.ensure_recent_price(session, security, date.today(), force=True)
        session.refresh(security)
        assert security.quote_miss_count == 0, (
            "ensure_recent_price booked a verdict its own fetch never produced"
        )
        assert security.dead_symbol_reason is None
    finally:
        # Whatever the pin did to the context, it must not hand its own
        # poison to the next test in this process.
        market_data.reset_quote_verdict()


def test_deep_backfill_seam_books_only_the_verdict_its_own_fetch_produced(
    session: Session, client: TestClient, monkeypatch
) -> None:
    """The same rule at the sweep's own fetch seam, `deep_backfill.backfill_security`.

    Its fetch is substituted (a double that publishes nothing), so whatever the
    context still carries from a previous call must NOT be booked onto the row
    when the seam reads `last_quote_verdict()` before `record_quote_outcome`.
    """
    uid = _user(session, client)
    security = _security(session, session.get(User, uid), "STALEDB")
    # The watchlist entry is the row's need-date (docs/07 §Backfill depth), so
    # `_prefetch` lets this row reach its fetch instead of skipping it.
    session.add(WatchlistItem(user_id=uid, security_id=security.id, created_at=datetime.now(UTC)))
    session.commit()

    try:
        _leave_a_previous_fetchs_verdict()
        # One candidate and no ledger evidence: `choose_series` answers
        # `no_evidence` and picks it — so the seam REACHES its
        # `record_quote_outcome`, the exact line this pin covers.
        monkeypatch.setattr(
            market_data,
            "fetch_history_candidates",
            lambda symbol, start, end: [
                CandidateSeries(security.symbol, "USD", {date.today(): D("12.34")})
            ],
        )
        outcome = deep_backfill.backfill_security(session, security, date.today())
        session.refresh(security)
        assert outcome.skipped is None, outcome.detail
        assert security.quote_miss_count == 0, (
            "deep_backfill.backfill_security booked a verdict its own fetch never produced"
        )
        assert security.dead_symbol_reason is None
    finally:
        market_data.reset_quote_verdict()


def test_split_repair_seam_books_only_the_verdict_its_own_fetch_produced(
    session: Session, client: TestClient, monkeypatch
) -> None:
    """Same rule at `split_repair.repair_split_window` — and that seam books the
    verdict BEFORE it looks at what came back, so a substituted fetch that
    answers nothing still must not book the previous call's miss.
    """
    uid = _user(session, client)
    security = _security(session, session.get(User, uid), "STALES")

    try:
        _leave_a_previous_fetchs_verdict()
        monkeypatch.setattr(market_data, "fetch_history", lambda symbol, start, end: {})
        result = split_repair.repair_split_window(
            session, security, date.today() - timedelta(days=30), date.today()
        )
        session.refresh(security)
        assert result.refused == "no_closes"  # the double really did answer: nothing
        assert security.quote_miss_count == 0, (
            "split_repair.repair_split_window booked a verdict its own fetch never produced"
        )
        assert security.dead_symbol_reason is None
    finally:
        market_data.reset_quote_verdict()


def test_benchmark_seam_reports_only_the_reason_its_own_fetch_produced(
    session: Session, monkeypatch
) -> None:
    """`benchmarks.backfill_symbol` books no security columns (its cache is the
    shared `benchmark_prices` table), so its verdict surface is the `detail` it
    reports when a fetch answers nothing: a fetch that published nothing must
    report no reason — never the previous call's, which the context still
    carries.
    """
    try:
        _leave_a_previous_fetchs_verdict()
        monkeypatch.setattr(
            market_data,
            "fetch_benchmark_history",
            lambda ticker, start, end: ({}, QuoteOutcome.unknown),
        )
        result = benchmarks.backfill_symbol(
            session, "SPY", date.today() - timedelta(days=30), date.today()
        )
        assert result.skipped == "fetch_unavailable"  # the double's own answer: no closes
        assert result.detail is None, (
            "benchmarks.backfill_symbol reported a reason its own fetch never produced"
        )
    finally:
        market_data.reset_quote_verdict()


# --- POST /portfolio/refresh --------------------------------------------------


def _create(client: TestClient, headers: dict[str, str], symbol: str) -> dict:
    body = client.post(
        SEC_URL,
        json={
            "symbol": symbol,
            "name": f"{symbol} Inc.",
            "kind": "stock",
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
        headers=headers,
    )
    assert body.status_code == 201, body.text
    return body.json()


def _watch(client: TestClient, headers: dict[str, str], security_id: int) -> None:
    body = client.post(f"{API}/watchlist", json={"security_id": security_id}, headers=headers)
    assert body.status_code == 201, body.text


def test_refresh_sweep_skips_dead_rows_and_reports_the_skip(
    session: Session, client: TestClient, monkeypatch
) -> None:
    """docs/07: "the daily sweep SKIPS dead_symbol rows". The response names
    the skip so the UI can say what it did NOT do."""
    headers = _auth(client)
    live = _create(client, headers, "LIVEQ")
    dead = _create(client, headers, "DEADQ")
    _watch(client, headers, live["id"])
    _watch(client, headers, dead["id"])

    row = session.get(Security, dead["id"])
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, row, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.refresh(row)
    assert row.dead_symbol is True

    queued: list[str] = []
    monkeypatch.setattr(
        market_data,
        "backfill_range",
        lambda _s, security, _a, _b, **_k: queued.append(security.symbol) or 0,
    )

    response = client.post(f"{API}/portfolio/refresh", headers=headers)
    assert response.status_code == 202
    assert response.json() == {"queued": 1, "skipped_dead": 1}
    assert queued == ["LIVEQ"]


def test_positions_fanout_does_not_queue_a_dead_symbol(
    session: Session, client: TestClient, monkeypatch
) -> None:
    """The market-hours background fan-out — the one that logged the 404s."""
    headers = _auth(client)
    dead = _create(client, headers, "DEADF")
    _watch(client, headers, dead["id"])
    row = session.get(Security, dead["id"])
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, row, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.refresh(row)

    import app.api.portfolio as portfolio

    monkeypatch.setattr(portfolio, "_markets_are_open", lambda: True)
    seen: list[str] = []
    monkeypatch.setattr(
        market_data,
        "ensure_recent_price",
        lambda _s, _sec, _today, **_k: seen.append("fetched"),
    )

    response = client.get(f"{API}/portfolio/positions", headers=headers)
    assert response.status_code == 200
    assert seen.count("fetched") == 0  # the dead row was never queued


# --- GET /securities/quote-check ----------------------------------------------


def test_quote_check_warns_for_a_delisted_symbol(client: TestClient, monkeypatch) -> None:
    headers = _auth(client)
    monkeypatch.setattr(
        market_data, "probe_quote", lambda symbol: (QuoteOutcome.not_found, DEAD_MESSAGE)
    )
    body = client.get(f"{SEC_URL}/quote-check", params={"symbol": "apotex"}, headers=headers)
    assert body.status_code == 200
    assert body.json() == {
        "symbol": "APOTEX",
        "quote_found": False,
        "reason": DEAD_MESSAGE,
    }


def test_quote_check_is_silent_for_a_live_symbol(client: TestClient, monkeypatch) -> None:
    headers = _auth(client)
    monkeypatch.setattr(market_data, "probe_quote", lambda symbol: (QuoteOutcome.ok, None))
    body = client.get(f"{SEC_URL}/quote-check", params={"symbol": "AAPL"}, headers=headers)
    assert body.status_code == 200
    assert body.json() == {"symbol": "AAPL", "quote_found": True, "reason": None}


def test_quote_check_never_reports_a_network_failure_as_no_quote(
    client: TestClient, monkeypatch
) -> None:
    """`quote_found: null` — "we could not ask" is not "there is no quote".
    The add form must not block on a flaky connection."""
    headers = _auth(client)
    monkeypatch.setattr(
        market_data, "probe_quote", lambda symbol: (QuoteOutcome.unavailable, "429")
    )
    body = client.get(f"{SEC_URL}/quote-check", params={"symbol": "AAPL"}, headers=headers)
    assert body.status_code == 200
    assert body.json()["quote_found"] is None


def test_quote_check_is_never_a_hard_block(client: TestClient, monkeypatch) -> None:
    """A 200 always — even for a symbol Yahoo has never heard of. Adding a
    never-fetched symbol is legitimate (docs/07)."""
    headers = _auth(client)
    monkeypatch.setattr(
        market_data, "probe_quote", lambda symbol: (QuoteOutcome.not_found, DEAD_MESSAGE)
    )
    assert (
        client.get(
            f"{SEC_URL}/quote-check", params={"symbol": "NEVERLISTED"}, headers=headers
        ).status_code
        == 200
    )


def test_probe_quote_unit_classifies_the_real_library(client: TestClient, monkeypatch) -> None:
    """The probe uses the SAME classifier as the fan-out, so the add-form
    warning and the dead-symbol flag can never disagree."""
    import yfinance
    from yfinance.exceptions import YFTzMissingError

    class MissingTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **_kwargs):
            raise YFTzMissingError(self.symbol)

    monkeypatch.setattr(yfinance, "Ticker", MissingTicker)
    outcome, reason = market_data.probe_quote("APOTEX")
    assert outcome is QuoteOutcome.not_found
    assert "delisted" in (reason or "")


def test_probe_quote_privacy_mode_raises_the_standard_403(client: TestClient, monkeypatch) -> None:
    headers = _auth(client)
    monkeypatch.setattr(
        market_data, "_privacy_lock", SimpleNamespace(get=lambda: True, set=lambda _v: None)
    )
    body = client.get(f"{SEC_URL}/quote-check", params={"symbol": "AAPL"}, headers=headers)
    assert body.status_code == 403
    assert body.json()["code"] == "privacy_mode_blocks_external_api"


# --- the payload carries the state --------------------------------------------


def test_security_read_carries_the_dead_symbol_state(session: Session, client: TestClient) -> None:
    headers = _auth(client)
    created = _create(client, headers, "CARRY1")
    assert created["dead_symbol"] is False
    assert created["quote_miss_count"] == 0
    assert created["dead_symbol_reason"] is None

    row = session.get(Security, created["id"])
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, row, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.refresh(row)

    fetched = client.get(f"{SEC_URL}/{created['id']}", headers=headers).json()
    assert fetched["dead_symbol"] is True
    assert fetched["quote_miss_count"] == market_data.DEAD_SYMBOL_MISSES
    assert fetched["dead_symbol_reason"] == DEAD_MESSAGE


def test_watchlist_row_says_no_public_quote_not_no_price(
    session: Session, client: TestClient
) -> None:
    """The watchlist half of the honesty fix: "no price" reads as waiting."""
    headers = _auth(client)
    created = _create(client, headers, "WATCHD")
    _watch(client, headers, created["id"])
    row = session.get(Security, created["id"])
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, row, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.refresh(row)

    items = client.get(f"{API}/watchlist", headers=headers).json()["items"]
    target = next(item for item in items if item["symbol"] == "WATCHD")
    assert target["dead_symbol"] is True
    assert target["dead_symbol_reason"] == DEAD_MESSAGE
    assert target["latest_price"] is None


def test_security_overview_reports_the_flag_and_skips_the_auto_fetch(
    session: Session, client: TestClient, monkeypatch
) -> None:
    """The Detail page's "standard request" — it must state the reason AND
    must not burn a 404 answering it."""
    headers = _auth(client)
    created = _create(client, headers, "OVERVW")
    row = session.get(Security, created["id"])
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, row, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.refresh(row)

    calls: list[str] = []
    monkeypatch.setattr(
        market_data,
        "fetch_latest_close",
        lambda symbol, today=None: calls.append(symbol),
    )
    monkeypatch.setattr(
        market_data, "fetch_fundamentals", lambda symbol: calls.append(symbol) or None
    )
    monkeypatch.setattr(
        market_data, "fetch_history", lambda symbol, s, e: calls.append(symbol) or {}
    )

    body = client.get(f"{SEC_URL}/{created['id']}/overview", headers=headers)
    assert body.status_code == 200
    overview = body.json()
    assert overview["security"]["dead_symbol"] is True
    assert overview["security"]["dead_symbol_reason"] == DEAD_MESSAGE
    assert overview["latest"] is None  # honest: nothing has ever come back
    assert calls == []  # ...and answering that cost zero network calls


def test_security_overview_still_returns_cached_prices_for_a_dead_symbol(
    session: Session, client: TestClient, monkeypatch
) -> None:
    """A dead symbol with a manually-entered price keeps showing it — the flag
    stops the FETCH, never the display (manual wins, docs/07)."""
    headers = _auth(client)
    created = _create(client, headers, "CACHED1")
    uid = client.get(f"{API}/users/me", headers=headers).json()["id"]
    row = session.get(Security, created["id"])
    session.add(
        Price(
            user_id=uid,
            security_id=row.id,
            date=date.today() - timedelta(days=2),
            price=D("7.77"),
            source=PriceSource.manual,
        )
    )
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, row, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.commit()
    session.refresh(row)

    monkeypatch.setattr(
        market_data,
        "fetch_latest_close",
        lambda symbol, today=None: (_ for _ in ()).throw(AssertionError("network!")),
    )
    body = client.get(f"{SEC_URL}/{created['id']}/overview", headers=headers)
    assert body.status_code == 200
    # prices.price is Numeric(18,8) — the wire value carries the column scale.
    assert body.json()["latest"]["price"] == "7.77000000"
    assert body.json()["latest"]["source"] == "manual"


# --- manual refresh is the escape hatch ---------------------------------------


def test_manual_refresh_probes_a_dead_symbol_and_can_revive_it(
    session: Session, client: TestClient, monkeypatch
) -> None:
    """`POST /securities/{id}/refresh` is the ONE automatic-looking control
    that still reaches a dead row — the disabled-with-reason UI keeps a
    "Try refresh" for exactly this."""
    import pandas as pd
    import yfinance

    headers = _auth(client)
    created = _create(client, headers, "TRYRSH")
    row = session.get(Security, created["id"])
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, row, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.commit()
    session.refresh(row)
    assert row.dead_symbol is True
    _restore_gates(monkeypatch)

    frame = pd.DataFrame({"Close": [3.21]}, index=pd.to_datetime([date.today()]))

    class LiveTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **_kwargs):
            return frame

        @property
        def info(self) -> dict:
            return {}

    monkeypatch.setattr(yfinance, "Ticker", LiveTicker)

    body = client.post(f"{SEC_URL}/{created['id']}/refresh", headers=headers)
    assert body.status_code == 200
    assert body.json()["security"]["dead_symbol"] is False
    assert body.json()["security"]["quote_miss_count"] == 0


def test_manual_refresh_rearms_the_flag_when_the_symbol_is_still_dead(
    session: Session, client: TestClient, monkeypatch
) -> None:
    import yfinance
    from yfinance.exceptions import YFTzMissingError

    headers = _auth(client)
    created = _create(client, headers, "STILLD")
    row = session.get(Security, created["id"])
    for _ in range(market_data.DEAD_SYMBOL_MISSES):
        market_data.record_quote_outcome(session, row, QuoteOutcome.not_found, DEAD_MESSAGE)
    session.commit()

    class MissingTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **_kwargs):
            raise YFTzMissingError(self.symbol)

        @property
        def info(self) -> dict:
            return {}

    monkeypatch.setattr(yfinance, "Ticker", MissingTicker)
    body = client.post(f"{SEC_URL}/{created['id']}/refresh", headers=headers)
    assert body.status_code == 200
    assert body.json()["security"]["dead_symbol"] is True


# --- migration / schema -------------------------------------------------------


def test_new_securities_start_healthy(session: Session, client: TestClient) -> None:
    """The migration backfills nothing — a pre-existing row reads 0/FALSE/NULL
    and keeps being fetched exactly as before."""
    headers = _auth(client)
    created = _create(client, headers, "FRESH0")
    assert created["dead_symbol"] is False
    assert created["quote_miss_count"] == 0
    assert WatchlistItem is not None  # imported for the schema round-trip above

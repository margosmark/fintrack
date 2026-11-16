"""M149 — the market-data deadline is ours, and it reaches the transport.

docs/07 promises a "5 s timeout" for yfinance, and docs/01 § "Sessions and the
event loop" lists market data among the bounded egress clients ("a session is
never held across an unbounded network call"). Neither claim was ours to make
at the call site: this module built `Ticker(sym)` with no session, so the
number that actually bounded a stalled Yahoo was **yfinance's own default** —
30 s in 1.6.0, undocumented, and free to move with the version (AGENTS §6).
Three attempts, times two candidates, times 30 s is minutes of a sync route's
request held on one dead connection. The module's `HISTORY_TIMEOUT_SECONDS = 5`
was a constant nothing read.

`market_data._yf_session()` re-types yfinance's own default session into a
subclass that forces `timeout=5` on `Session.request` — the one method every
yfinance call, cookie handshake included, ends in — and installs it on the
`YfData` singleton, which is where yfinance keeps the ONE session it re-uses
for every ticker. These tests pin that it ARRIVES there (patch the transport
and read back what it was handed), that it overrides the 30 s yfinance passes
in rather than defaulting beside it, and that every fetch seam still routes
through the deadline ticker.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import inspect

import pytest

from app.models.securities import QuoteOutcome
from app.services import market_data

BOGUS = "ZZZZNOPE"


@pytest.fixture()
def recorded_transport(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace the session's BACKEND with a recorder that never touches network.

    The patch lands on the class `_DeadlineSession` inherits from, so the
    module's own `request` override still runs — which is the point: what the
    recorder sees is what the override asked the transport for.
    """
    backend = type(market_data._yf_session()).__mro__[1]
    seen: list[object] = []

    def record(*args: object, **kwargs: object) -> object:
        seen.append(kwargs.get("timeout", "<missing>"))
        raise RuntimeError("recording transport — no network in this test")

    monkeypatch.setattr(backend, "request", record)
    return seen


def test_the_deadline_reaches_the_transport(recorded_transport: list[object]) -> None:
    """`probe_quote` degrades to `unavailable`, and the transport saw OUR number."""
    outcome, reason = market_data.probe_quote(BOGUS)
    assert outcome is QuoteOutcome.unavailable, reason
    assert recorded_transport, "no request reached the transport at all"
    assert set(recorded_transport) == {market_data.YF_TIMEOUT_SECONDS}, (
        "the market-data deadline did not reach the client — a stalled Yahoo "
        "would be bounded by whatever yfinance happens to pass instead"
    )


def test_the_override_beats_the_value_yfinance_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `setdefault` here would be a no-op: yfinance always sends 30.

    Driven through `_ticker` — the real `Ticker`, the real `YfData` singleton,
    its `_set_session` type check and yfinance's own `timeout=30` all in play.
    """
    backend = type(market_data._yf_session()).__mro__[1]
    seen: list[object] = []

    def record(*args: object, **kwargs: object) -> object:
        seen.append(kwargs.get("timeout", "<missing>"))
        raise RuntimeError("recording transport — no network in this test")

    monkeypatch.setattr(backend, "request", record)

    with contextlib.suppress(Exception):
        _ = market_data._ticker(BOGUS).info

    assert seen, "Ticker.info never reached the transport"
    assert set(seen) == {market_data.YF_TIMEOUT_SECONDS}
    assert 30 not in seen, "yfinance's own 30 s default reached the transport instead of ours"


def test_the_deadline_is_installed_on_the_shared_session() -> None:
    """The deadline lives on yfinance's ONE process-wide session.

    `YfData` is a singleton that re-uses a single session for every ticker, so
    this is the seam that covers current and future call sites alike — and it
    is why the constructor's signature had to stay `Ticker(sym)`.
    """
    import yfinance.data as yf_data

    deadline_session = market_data._yf_session()
    shared = yf_data.YfData()  # no args: returns the singleton, re-sets nothing
    assert shared._session is deadline_session, (
        "the deadline session is not the one yfinance actually uses"
    )
    assert type(shared._session).__name__ == "_DeadlineSession"


def test_every_fetch_seam_routes_through_the_deadline_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every fetch seam builds its Ticker through `_ticker` (behavioral).

    A double that records construction and raises on use — the module degrades
    to "no data" for every one of them, which is also the documented contract.
    """
    import yfinance

    built: list[str] = []

    class _DeadTicker:
        def __init__(self, symbol: str) -> None:
            built.append(symbol)

        def __getattr__(self, name: str) -> object:
            raise RuntimeError("no network in this test")

    monkeypatch.setattr(yfinance, "Ticker", _DeadTicker)
    monkeypatch.setattr(market_data, "_http_search_json", lambda query: None)

    end = dt.date.today()
    start = end - dt.timedelta(days=5)
    for call in (
        lambda: market_data.probe_quote(BOGUS),
        lambda: market_data.fetch_history(BOGUS, start, end),
        lambda: market_data.fetch_fundamentals(BOGUS),
        lambda: market_data.fetch_fund_holdings(BOGUS),
        lambda: market_data.fetch_dividend_history(BOGUS),
    ):
        with contextlib.suppress(Exception):
            call()

    assert len(built) >= 5, f"not every fetch seam built a Ticker: {built}"
    assert BOGUS in built
    # Every candidate is the symbol the seam was asked about, plus the `.TO`
    # suffix probe the quote paths add for bare Canadian tickers.
    assert set(built) <= {BOGUS, f"{BOGUS}.TO"}, built


def test_the_only_ticker_constructor_is_the_deadline_helper() -> None:
    """Tripwire for the seam the behavioral tests above cannot reach.

    `resolve_symbol` also drives Yahoo's SEARCH endpoint, so exercising it here
    would need the network; reading the call sites is how `fetch_fundamentals`
    (a DB-writing seam) and any future one stay covered. Shape check, labelled
    as such — the behavioral pins above are the real proof.
    """
    source = inspect.getsource(market_data)
    constructors = [line.strip() for line in source.splitlines() if "yfinance.Ticker(" in line]
    assert len(constructors) == 1, (
        "every Ticker must be built by `_ticker`, or that call site is bounded "
        f"by yfinance's default instead of ours: {constructors}"
    )
    seams = [
        line.strip()
        for line in source.splitlines()
        if "_ticker(" in line and "def _ticker" not in line
    ]
    assert len(seams) == 6, seams

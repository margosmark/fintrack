"""M187 — the scheduled price refresh keeps TODAY's bar moving (docs/07).

The request fan-out (docs/07 §Market-hours optimization) deliberately stops at
a close already dated today: `ensure_recent_price`'s once-per-day rule is what
stops a positions page load from polling yfinance per security per request.
Left alone, that freezes a security's bar at whatever the FIRST market-hours
page load of the day saw. This worker is the other half — wall-clock ticks
that ADVANCE today's close through the same seam, opt-in only.

One pin per acceptance criterion:

* the default config is ≥4 session-anchored ticks (one after the open, one
  after the 16:00 close) and `next_tick` walks the day, the weekend and the
  last-fired bookkeeping correctly;
* a yfinance close dated today IS advanced by a later tick — a naive
  scheduler would be four no-ops after the first tick (the two guards this
  had to change: `ensure_recent_price`'s today-check and `_cache_closes`'
  insert-only rule);
* a MANUAL close dated today is never modified (models/prices.py: human data
  wins permanently);
* weekend / pre-open / post-close ticks make ZERO provider calls;
* a privacy-mode OWNER's symbols are never requested while other tenants
  refresh — per-user, unlike the shared-cache FX/benchmark workers (docs/11);
* dead symbols are not probed (M141);
* one symbol's provider failure does not abort the tick;
* a tick whose fetch publishes NO provider-arrival stamp moves nothing —
  the refusal half of the 737f214 fetch-evidence gate: neither the advance
  nor the no-op stamp refresh may run without `fetched_at is not None`
  (`_cache_closes`, docs/07 §Fetch provenance);
* the request paths — positions fan-out, `compute_positions`,
  `security_overview` — still never re-fetch a close dated today.
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import app.api.portfolio as portfolio
import app.services.market_data as market_data
import app.services.price_refresh as price_refresh
import app.services.valuation as valuation
from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.accounts import Account, AccountType
from app.models.prices import Price, PriceSource
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.models.watchlist import WatchlistItem

API = "/api/v1"
ET = ZoneInfo("America/New_York")
D = Decimal

#: A real Monday, so the session gate is unambiguous (m117's pinned weekday).
MONDAY = date(2026, 10, 5)
MIDDAY = datetime(2026, 10, 5, 10, 0, tzinfo=ET)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _user(session, *, privacy_mode: bool = False) -> User:
    user = User(
        # uuid, never `id(object())`: CPython hands the SAME id to back-to-back
        # temporary objects, so two `_user()` calls in one test collide on
        # `uq_users_email` (IntegrityError) depending on how the heap happens
        # to look — which is how this file went red on top of the fetch-stamp
        # regression it was already carrying.
        email=f"m187-{uuid.uuid4().hex}@example.com",
        hashed_password="x",
        privacy_mode=privacy_mode,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _holding(session, user: User, symbol: str) -> Security:
    """A security with a committed trade — held, therefore in the sweep."""
    account = Account(user_id=user.id, name=f"TFSA {symbol}", type=AccountType.tfsa, currency="CAD")
    security = Security(
        user_id=user.id,
        symbol=symbol,
        name=f"{symbol} Inc.",
        kind=SecurityKind.stock,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(account)
    session.add(security)
    session.commit()
    session.refresh(account)
    session.refresh(security)
    session.add(
        Trade(
            user_id=user.id,
            account_id=account.id,
            security_id=security.id,
            date=date(2026, 1, 5),
            action=TradeAction.buy,
            quantity=Decimal("10"),
            price_native=Decimal("50"),
            currency="CAD",
        )
    )
    session.commit()
    return security


def _quote(
    session,
    security: Security,
    day: date,
    value: str,
    source: PriceSource = PriceSource.yfinance,
) -> None:
    session.add(
        Price(
            user_id=security.user_id,
            security_id=security.id,
            date=day,
            price=D(value),
            source=source,
        )
    )
    session.commit()


def _rows(session, security: Security) -> list[Price]:
    return session.exec(select(Price).where(Price.security_id == security.id)).all()


def _stub_close_fetch(
    monkeypatch: pytest.MonkeyPatch, quotes=None, fail=(), *, stamp: bool = True
) -> list[tuple]:
    """Replace the yfinance latest-close seam with a frozen table; record calls.

    Keyed by the SYMBOL the seam actually receives, and returning a bar dated
    the ``today`` the caller passed — the shape `fetch_latest_close` answers
    with, so `ensure_recent_price`'s store path is exercised for real.

    Answering that shape means BOTH halves of it. The real seam publishes the
    provider-response arrival instant the moment it answers (Option A,
    t_bd5ec95f) and `_cache_closes` will not ADVANCE today's row without it —
    the stamp is what keeps a moved price from carrying an older fetch time
    than the value it now holds. A double that returned the value but no stamp
    would book "no evidence" and silently skip the very advance these tests
    pin (`price refresh: 0 advanced (none)`), so the default (`stamp=True`)
    publishes the same context the real fetch site stamps:
    `test_price_age_provenance.
    test_ensure_recent_price_plumbs_the_provider_arrival_instant`'s pattern.
    A symbol in ``fail`` raises before it stamps, exactly like a provider that
    never answered.

    ``stamp=False`` is the deliberate exception — the REFUSAL-direction
    double: the seam answers with a value but publishes no arrival evidence,
    which is exactly the shape `_cache_closes`' ``fetched_at is not None``
    gate must refuse (737f214, docs/07 §Fetch provenance). Keep it, or the
    suite loses the only signal that gate exists.
    """
    quotes = quotes or {}
    calls: list[tuple] = []

    def fake(symbol: str, today: date | None = None):
        calls.append((symbol, today))
        if symbol in fail:
            raise RuntimeError(f"{symbol} is down")
        if stamp:
            market_data._last_fetch_at.set(datetime.now(UTC))
        value = quotes.get(symbol)
        return None if value is None else (today or date.today(), D(value))

    monkeypatch.setattr(market_data, "fetch_latest_close", fake)
    return calls


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------


def test_the_default_schedule_is_at_least_four_session_anchored_ticks() -> None:
    """≥4 ticks, one at/after the open, one at/after the 16:00 close —
    asserted on the parsed default, not by reading the string."""
    times = price_refresh.tick_times(get_settings().price_refresh_times)
    assert len(times) >= 4
    assert min(times) >= time(9, 30), "nothing may tick before the open: no bar exists yet"
    assert max(times) >= time(16, 0), "the final tick must land after the 16:00 close"
    assert max(times) < time(20, 0), "…and stay inside the market-hours window"
    for tick in times:
        candidate = datetime(
            MONDAY.year, MONDAY.month, MONDAY.day, tick.hour, tick.minute, tzinfo=ET
        )
        assert price_refresh.markets_are_open(candidate), f"{tick} is outside the session gate"


def test_a_malformed_tick_is_dropped_but_an_empty_schedule_is_loud() -> None:
    """A typo must not kill the worker; a schedule with NOTHING valid must."""
    assert price_refresh.tick_times("09:35, banana, 16:15") == [time(9, 35), time(16, 15)]
    with pytest.raises(ValueError):
        price_refresh.tick_times("nope")


def test_the_loop_walks_the_trading_day_then_to_the_next() -> None:
    times = price_refresh.tick_times("09:35,12:00,15:00,16:15")
    now = datetime(2026, 10, 5, 8, 0, tzinfo=ET)  # Monday, pre-open
    walk = []
    last_fired = None
    for _ in range(6):
        target = price_refresh.next_tick(now, times, last_fired=last_fired)
        walk.append(target)
        now = target + timedelta(seconds=5)  # the tick fired; the loop wakes again
        last_fired = target
    assert walk[:4] == [
        datetime(2026, 10, 5, 9, 35, tzinfo=ET),
        datetime(2026, 10, 5, 12, 0, tzinfo=ET),
        datetime(2026, 10, 5, 15, 0, tzinfo=ET),
        datetime(2026, 10, 5, 16, 15, tzinfo=ET),
    ]
    # After the last tick of Monday the schedule rolls to Tuesday — never the
    # same tick twice (last_fired) and never a missed slot re-fired.
    assert walk[4] == datetime(2026, 10, 6, 9, 35, tzinfo=ET)
    assert walk[5] == datetime(2026, 10, 6, 12, 0, tzinfo=ET)


def test_the_schedule_skips_weekends_and_startups_past_the_last_tick() -> None:
    times = price_refresh.tick_times("09:35,12:00,15:00,16:15")
    # Friday evening → Monday's first tick: the weekend holds no session, so
    # the loop sleeps straight through instead of waking for four no-ops.
    friday = datetime(2026, 10, 2, 16, 20, tzinfo=ET)
    assert price_refresh.next_tick(friday, times) == datetime(2026, 10, 5, 9, 35, tzinfo=ET)
    # Saturday morning lands on Monday too — and raises nothing.
    saturday = datetime(2026, 10, 3, 10, 0, tzinfo=ET)
    assert price_refresh.next_tick(saturday, times) == datetime(2026, 10, 5, 9, 35, tzinfo=ET)
    # A startup past the last tick of the day waits for tomorrow's first.
    assert price_refresh.next_tick(datetime(2026, 10, 5, 16, 40, tzinfo=ET), times) == (
        datetime(2026, 10, 6, 9, 35, tzinfo=ET)
    )
    # The just-fired tick is ineligible even inside its own minute.
    fired = datetime(2026, 10, 5, 12, 0, tzinfo=ET)
    assert price_refresh.next_tick(fired + timedelta(seconds=20), times, last_fired=fired) == (
        datetime(2026, 10, 5, 15, 0, tzinfo=ET)
    )


def test_the_suite_keeps_the_worker_off() -> None:
    """conftest pins PRICE_REFRESH_ENABLED=false so fixtures stay deterministic."""
    assert get_settings().price_refresh_enabled is False
    assert price_refresh.start_worker() is None


# ---------------------------------------------------------------------------
# The feature actually works
# ---------------------------------------------------------------------------


def test_a_yfinance_close_dated_today_is_advanced_by_a_later_tick(session, monkeypatch) -> None:
    user = _user(session)
    security = _holding(session, user, "ADVND")
    _quote(session, security, MONDAY, "5.00")
    calls = _stub_close_fetch(monkeypatch, quotes={"ADVND": "6.25"})

    stats = price_refresh.run_once(session, now=datetime(2026, 10, 5, 15, 0, tzinfo=ET))

    row = _rows(session, security).pop()
    assert (row.price, row.source) == (D("6.25"), PriceSource.yfinance)
    assert stats.refreshed == ["ADVND"]
    assert stats.updated == ["ADVND"]
    assert stats.skipped is None
    assert calls == [("ADVND", MONDAY)]

    # A THIRD tick the same day advances it again — the schedule is not four
    # no-ops after the first tick (the two guards this card had to change).
    _stub_close_fetch(monkeypatch, quotes={"ADVND": "6.40"})
    later = price_refresh.run_once(session, now=datetime(2026, 10, 5, 16, 15, tzinfo=ET))
    assert _rows(session, security).pop().price == D("6.40")
    assert later.updated == ["ADVND"]


def test_a_manual_close_dated_today_is_never_touched_by_any_tick(session, monkeypatch) -> None:
    """models/prices.py: human data overwrites machine data, permanently —
    the invariant most at risk from an intraday overwrite."""
    user = _user(session)
    security = _holding(session, user, "MANUL")
    _quote(session, security, MONDAY, "5.00", source=PriceSource.manual)
    calls = _stub_close_fetch(monkeypatch, quotes={"MANUL": "6.25"})

    stats = price_refresh.run_once(session, now=datetime(2026, 10, 5, 16, 15, tzinfo=ET))

    row = _rows(session, security).pop()
    assert (row.price, row.source) == (D("5.00"), PriceSource.manual)
    assert calls == [("MANUL", MONDAY)]  # the fetch DID happen; the human row still won
    assert stats.refreshed == ["MANUL"]
    assert stats.updated == []


def test_a_tick_whose_fetch_publishes_no_arrival_stamp_never_moves_the_price(
    session, monkeypatch
) -> None:
    """The refusal half of the 737f214 fetch-evidence gate (docs/07 §Fetch
    provenance): `_cache_closes` may ADVANCE an existing row only when
    ``fetched_at is not None`` — "a price may not move while its fetch
    evidence stays stale". The seam here ANSWERS with a higher close but
    publishes no provider-arrival instant, so today's row must hold its old
    value and the tick must record nothing as updated.

    This is the signal the suite lost when the m187 repair made the stub
    publish the stamp: without this direction the gate was mutation-invisible
    (deleting both conditions left the reachable surface green)."""
    user = _user(session)
    security = _holding(session, user, "NOSTMP")
    _quote(session, security, MONDAY, "5.00")
    calls = _stub_close_fetch(monkeypatch, quotes={"NOSTMP": "6.25"}, stamp=False)

    stats = price_refresh.run_once(session, now=datetime(2026, 10, 5, 15, 0, tzinfo=ET))

    assert calls == [("NOSTMP", MONDAY)]  # the provider seam WAS reached…
    assert stats.refreshed == ["NOSTMP"]
    assert stats.failed == []
    assert stats.updated == []  # …and moved nothing (737f214: no evidence)
    row = _rows(session, security).pop()
    assert row.price == D("5.00")
    assert row.fetched_at is None  # no arrival instant was ever published


def test_a_no_op_tick_without_evidence_keeps_the_displayed_rows_stamp(session, monkeypatch) -> None:
    """The gate's second branch: with no arrival evidence the no-op re-fetch
    may not touch the displayed row's ``fetched_at`` either — a stale stamp
    survives an evidence-less tick, because only a provider that really
    answered refreshes evidence (docs/07 §Fetch provenance)."""
    user = _user(session)
    security = _holding(session, user, "KEEPS")
    _quote(session, security, MONDAY, "5.00")
    row = _rows(session, security).pop()
    earlier = datetime(2026, 10, 5, 13, 30, tzinfo=UTC)
    row.fetched_at = earlier
    session.commit()
    # Same value the row already holds: only the stamp-refresh branch can fire.
    calls = _stub_close_fetch(monkeypatch, quotes={"KEEPS": "5.00"}, stamp=False)

    stats = price_refresh.run_once(session, now=datetime(2026, 10, 5, 15, 0, tzinfo=ET))

    assert calls == [("KEEPS", MONDAY)]
    assert stats.updated == []
    session.refresh(row)
    assert row.price == D("5.00")
    assert row.fetched_at == earlier  # an evidence-less tick must not clear it


@pytest.mark.parametrize(
    "iso",
    [
        "2026-10-03T12:00",  # Saturday — no session at all
        "2026-10-05T09:00",  # Monday, pre-open: no bar dated today exists yet
        "2026-10-05T20:30",  # Monday, past the window
    ],
)
def test_a_tick_outside_the_session_makes_zero_provider_calls(
    session, monkeypatch, iso: str
) -> None:
    user = _user(session)
    security = _holding(session, user, "CLOSED")
    _quote(session, security, MONDAY, "5.00")
    calls = _stub_close_fetch(monkeypatch, quotes={"CLOSED": "9.99"})

    stats = price_refresh.run_once(session, now=datetime.fromisoformat(iso).replace(tzinfo=ET))

    assert stats.skipped == "markets_closed"
    assert calls == []
    assert stats.refreshed == []
    assert _rows(session, security).pop().price == D("5.00")


def test_a_privacy_mode_owners_symbols_are_never_requested(session, monkeypatch) -> None:
    """Per-user semantics (docs/11): `prices` carries `user_id`, so only the
    privacy-mode OWNER's securities are skipped — everyone else refreshes."""
    private = _user(session, privacy_mode=True)
    open_user = _user(session)
    hidden = _holding(session, private, "PRIVAT")
    visible = _holding(session, open_user, "PUBLIK")
    _quote(session, hidden, MONDAY, "3.00")
    _quote(session, visible, MONDAY, "4.00")
    # The privacy tenant WATCHES the other tenant's security: the gate keys on
    # the row's OWNER, so the watch adds nothing to the block — and the
    # private tenant's own row stays untouched either way.
    session.add(WatchlistItem(user_id=private.id, security_id=visible.id))
    session.commit()
    calls = _stub_close_fetch(monkeypatch, quotes={"PRIVAT": "9.99", "PUBLIK": "4.40"})

    stats = price_refresh.run_once(session, now=MIDDAY)

    assert calls == [("PUBLIK", MONDAY)]  # PRIVAT's symbol never leaves the machine
    assert stats.privacy_skipped == ["PRIVAT"]
    assert stats.updated == ["PUBLIK"]
    assert _rows(session, hidden).pop().price == D("3.00")
    assert _rows(session, visible).pop().price == D("4.40")


def test_dead_symbols_are_not_probed(session, monkeypatch) -> None:
    """M141: `force`/`intraday_update` are freshness knobs, never gate bypasses."""
    user = _user(session)
    dead = _holding(session, user, "DEADA")
    dead.dead_symbol = True
    session.commit()
    calls = _stub_close_fetch(monkeypatch, quotes={"DEADA": "9.99"})

    stats = price_refresh.run_once(session, now=MIDDAY)

    assert calls == []
    assert stats.dead_skipped == ["DEADA"]
    assert stats.refreshed == []


def test_one_symbols_provider_failure_does_not_abort_the_tick(session, monkeypatch) -> None:
    user = _user(session)
    broken = _holding(session, user, "BROKN")
    healthy = _holding(session, user, "HEALT")
    _quote(session, broken, MONDAY, "1.00")
    _quote(session, healthy, MONDAY, "2.00")
    _stub_close_fetch(monkeypatch, quotes={"BROKN": "1.10", "HEALT": "2.20"}, fail={"BROKN"})

    stats = price_refresh.run_once(session, now=MIDDAY)

    assert set(stats.refreshed) == {"BROKN", "HEALT"}  # both were ATTEMPTED
    assert stats.failed == ["BROKN"]
    assert stats.updated == ["HEALT"]
    assert _rows(session, broken).pop().price == D("1.00")  # the failure degrades to cache
    assert _rows(session, healthy).pop().price == D("2.20")


# ---------------------------------------------------------------------------
# The request paths are unchanged
# ---------------------------------------------------------------------------


def test_the_default_seam_still_stops_at_a_close_dated_today(session, monkeypatch) -> None:
    """The invariant the once-per-day rule exists for, pinned on BOTH request
    shapes, with a positive control that only the scheduler's opt-in fetches."""
    user = _user(session)
    security = _holding(session, user, "FROZEN")
    _quote(session, security, MONDAY, "5.00")

    def boom(*args, **kwargs):
        raise AssertionError("a request path must never re-fetch a close dated today")

    monkeypatch.setattr(market_data, "fetch_latest_close", boom)

    plain = market_data.ensure_recent_price(session, security, MONDAY)
    forced = market_data.ensure_recent_price(session, security, MONDAY, force=True)
    assert [row.price for row in (plain, forced)] == [D("5.00"), D("5.00")]

    # Positive control: the scheduler's opt-in DOES reach the seam.
    calls = _stub_close_fetch(monkeypatch, quotes={"FROZEN": "5.50"})
    advanced = market_data.ensure_recent_price(
        session, security, MONDAY, force=True, intraday_update=True
    )
    assert advanced.price == D("5.50")
    assert calls == [("FROZEN", MONDAY)]


def test_the_positions_fan_out_never_asks_for_an_intraday_update(
    client: TestClient, session, monkeypatch
) -> None:
    """`portfolio.py`'s fan-out must not gain the scheduler's opt-in — a page
    load still costs at most one fetch per security per calendar day."""
    user = _user(session)
    account = Account(user_id=user.id, name="TFSA", type=AccountType.tfsa, currency="CAD")
    security = Security(
        user_id=user.id,
        symbol="FANOUT",
        name="Fanout Inc.",
        kind=SecurityKind.stock,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(account)
    session.add(security)
    session.commit()
    session.refresh(account)
    session.refresh(security)
    session.add(
        Trade(
            user_id=user.id,
            account_id=account.id,
            security_id=security.id,
            date=date(2026, 1, 5),
            action=TradeAction.buy,
            quantity=Decimal("10"),
            price_native=Decimal("50"),
            currency="CAD",
        )
    )
    _quote(session, security, date.today(), "50.00")
    monkeypatch.setattr(portfolio, "_markets_are_open", lambda: True)
    seen: list[dict] = []
    monkeypatch.setattr(
        market_data,
        "ensure_recent_price",
        lambda _s, _sec, _today, **kwargs: seen.append(kwargs),
    )

    response = client.get(
        f"{API}/portfolio/positions",
        headers={"Authorization": f"Bearer {create_access_token(user.id)}"},
    )

    assert response.status_code == 200, response.text
    assert seen == [{"force": True}], "the fan-out must not gain the scheduler's opt-in"


def test_security_overview_does_not_refetch_a_close_dated_today(
    client: TestClient, session, monkeypatch
) -> None:
    user = _user(session)
    security = _holding(session, user, "OVERVW")
    _quote(session, security, date.today(), "50.00")

    def boom(*args, **kwargs):
        raise AssertionError("security_overview must not re-fetch a close dated today")

    monkeypatch.setattr(market_data, "fetch_latest_close", boom)

    response = client.get(
        f"{API}/securities/{security.id}/overview",
        headers={"Authorization": f"Bearer {create_access_token(user.id)}"},
    )

    assert response.status_code == 200, response.text


def test_compute_positions_is_cache_only_for_a_close_dated_today(session, monkeypatch) -> None:
    user = _user(session)
    security = _holding(session, user, "CACHEONLY")
    today = date.today()
    _quote(session, security, today, "50.00")

    def boom(*args, **kwargs):
        raise AssertionError("compute_positions must stay cache-only (M184)")

    monkeypatch.setattr(market_data, "fetch_latest_close", boom)

    items = valuation.compute_positions(session, user.id, today=today)

    assert [row["symbol"] for row in items] == ["CACHEONLY"]
    assert [row["price"] for row in items] == [D("50")]  # Numeric(20,8) round-trip

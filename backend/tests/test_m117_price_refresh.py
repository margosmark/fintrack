"""M117 — "Refresh prices" must actually refresh (docs/07 §staleness, docs/18).

Two defects pinned here, both from the same root: the shared
`PRICE_STALE_DAYS = 7` freshness window was doing duty as a *fetch gate*.

1. `ensure_recent_price` handed back the cached close untouched whenever the
   newest row was 1-7 days old. `POST /portfolio/refresh` and the market-hours
   `GET /portfolio/positions` fan-out both went through it, so a user's
   positions sat a day behind except on the one day in eight when the window
   expired — Mark's "refresh button appears ineffective".
2. The button additionally ran `ensure_recent_price` while
   `POST /securities/{id}/refresh` ran a 365-day `backfill_range`: same word,
   two contracts, which is why a security detail page "loaded history" and the
   investments page did not.

The window is now a *read-path* concern only (`force=False` keeps the old
behaviour for `compute_positions` / `security_overview`), the automatic
fan-out passes `force=True`, and the button runs `queue_backfill` — the same
365-day backfill the per-security refresh runs.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import app.api.portfolio as portfolio
import app.services.market_data as market_data
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.models.users import User

API = "/api/v1"
SEC_URL = f"{API}/securities"
ET = ZoneInfo("America/New_York")
D = Decimal


@pytest.fixture(autouse=True)
def offline_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """No yfinance in this module (docs/07 network discipline).

    `POST /securities` queues a 365-day backfill, and `_run_ensure_fundamentals`
    hits `Ticker.info` — both are the slow seams under test, so they become
    recorders the individual tests assert against.
    """
    monkeypatch.setattr(market_data, "backfill_range", lambda *a, **k: 0)
    monkeypatch.setattr(market_data, "ensure_fundamentals", lambda *a, **k: None)
    # `POST /accounts` recomputes positions, and `compute_positions` calls
    # `ensure_recent_price` inline for every open position — a security with
    # no cached close would otherwise go out to yfinance mid-setup.
    monkeypatch.setattr(market_data, "fetch_latest_close", lambda symbol, today=None: None)


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m117rf-{date.today().isoformat()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _security(client: TestClient, headers: dict[str, str], symbol: str) -> dict:
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


def _hold(session, client: TestClient, headers: dict[str, str], symbol: str) -> Security:
    """A security with a committed trade — held, therefore in the fan-out."""
    created = _security(client, headers, symbol)
    uid = client.get(f"{API}/users/me", headers=headers).json()["id"]
    user = session.get(User, uid)
    account = client.post(
        f"{API}/accounts",
        json={"name": f"Acct {symbol}", "type": "tfsa", "currency": "CAD"},
        headers=headers,
    )
    assert account.status_code == 201, account.text
    session.add(
        Trade(
            user_id=user.id,
            account_id=account.json()["id"],
            security_id=created["id"],
            action=TradeAction.buy,
            date=date.today(),
            quantity=D("1"),
            price_native=D("50"),
        )
    )
    session.flush()
    return session.get(Security, created["id"])


def _quote(session, security: Security, day: date, value: str) -> None:
    session.add(
        Price(
            user_id=security.user_id,
            security_id=security.id,
            date=day,
            price=D(value),
            source=PriceSource.yfinance,
        )
    )
    session.flush()


# --- ensure_recent_price -----------------------------------------------------


def test_force_advances_a_close_the_freshness_window_would_skip(
    client: TestClient, session, monkeypatch
) -> None:
    """The reported defect: a 1-day-old close is inside `PRICE_STALE_DAYS`, so
    the opportunistic path returns it — and only `force` fetches a newer one."""
    headers = _auth(client)
    security = _hold(session, client, headers, "STALE1")
    today = date.today()
    yesterday = today - timedelta(days=1)
    _quote(session, security, yesterday, "10.00")
    session.flush()
    fetched: list[tuple[str, date | None]] = []

    def fake_close(symbol, today=None):
        fetched.append((symbol, today))
        return (today or date.today(), D("11.25"))

    monkeypatch.setattr(market_data, "fetch_latest_close", fake_close)

    cached = market_data.ensure_recent_price(session, security, today)
    assert cached is not None
    assert cached.date == yesterday
    assert fetched == []  # window holds: no network on the read path

    refreshed = market_data.ensure_recent_price(session, security, today, force=True)
    assert refreshed is not None
    assert refreshed.date == today
    assert refreshed.price == D("11.25")
    assert refreshed.source is PriceSource.yfinance
    assert fetched == [(security.symbol, today)]


def test_a_close_already_dated_today_is_never_refetched(
    client: TestClient, session, monkeypatch
) -> None:
    """Even a forced refresh stops at today's row — that is what bounds the
    market-hours fan-out to one network call per security per day instead of
    one per positions request."""
    headers = _auth(client)
    security = _hold(session, client, headers, "FRESH1")
    _quote(session, security, date.today(), "12.34")
    session.flush()

    def explode(symbol, today=None):
        raise AssertionError(f"network fetch attempted although today's close is cached: {symbol}")

    monkeypatch.setattr(market_data, "fetch_latest_close", explode)

    row = market_data.ensure_recent_price(session, security, date.today(), force=True)
    assert row is not None
    assert row.price == D("12.34")


def test_opportunistic_path_keeps_the_window(client: TestClient, session, monkeypatch) -> None:
    """`compute_positions` / `security_overview` still must not turn every page
    load into a yfinance call for a week-old quote."""
    headers = _auth(client)
    security = _hold(session, client, headers, "WINDOW1")
    _quote(session, security, date.today() - timedelta(days=3), "9.00")
    session.flush()
    calls: list[str] = []
    monkeypatch.setattr(
        market_data, "fetch_latest_close", lambda symbol, today=None: calls.append(symbol)
    )

    row = market_data.ensure_recent_price(session, security, date.today())
    assert row is not None
    assert row.price == D("9.00")
    assert calls == []


# --- POST /portfolio/refresh -------------------------------------------------


def test_refresh_button_runs_the_same_backfill_as_the_security_page(
    client: TestClient, session, monkeypatch
) -> None:
    """Contract parity with `POST /securities/{id}/refresh`: a 365-day
    backfill, not the freshness-gated `ensure_recent_price` that made this
    endpoint a no-op for anything fetched inside the last week."""
    headers = _auth(client)
    held_a = _hold(session, client, headers, "REFRESHA")
    held_b = _hold(session, client, headers, "REFRESHB")
    events: list[tuple[str, int, int]] = []

    def record_backfill(_session, security, start_date, end_date, **_kwargs):
        events.append(("backfill", security.id, (end_date - start_date).days))
        return 0

    monkeypatch.setattr(market_data, "backfill_range", record_backfill)

    response = client.post(f"{API}/portfolio/refresh", headers=headers)
    assert response.status_code == 202
    # M141: `skipped_dead` names the rows the sweep did NOT queue (docs/07).
    assert response.json() == {"queued": 2, "skipped_dead": 0}

    assert [(kind, sec_id) for kind, sec_id, _ in events] == [
        ("backfill", held_a.id),
        ("backfill", held_b.id),
    ]
    assert all(span == 365 for _kind, _sec_id, span in events)


def test_refresh_queues_prices_before_fundamentals(
    client: TestClient, session, monkeypatch
) -> None:
    """Positions render from prices; fundamentals (`Ticker.info`) is the slow
    seam. Interleaving them pushed the last price row past the point where the
    UI's invalidation ladder was looking."""
    headers = _auth(client)
    _hold(session, client, headers, "ORDERA")
    _hold(session, client, headers, "ORDERB")
    events: list[str] = []
    monkeypatch.setattr(
        market_data,
        "backfill_range",
        lambda _s, security, _a, _b, **_k: events.append(f"price:{security.symbol}") or 0,
    )
    monkeypatch.setattr(
        market_data,
        "ensure_fundamentals",
        lambda _s, security, _d: events.append(f"fund:{security.symbol}"),
    )

    response = client.post(f"{API}/portfolio/refresh", headers=headers)
    assert response.status_code == 202

    assert events == ["price:ORDERA", "price:ORDERB", "fund:ORDERA", "fund:ORDERB"]


# --- GET /portfolio/positions ------------------------------------------------


def test_positions_auto_refresh_is_forced(client: TestClient, session, monkeypatch) -> None:
    """The market-hours fan-out must ask for a forced refresh — without
    `force=True` the freshness window hands back yesterday's close and
    positions never catch up to today."""
    headers = _auth(client)
    security = _hold(session, client, headers, "AUTO1")
    _quote(session, security, date.today(), "5.00")
    session.flush()
    monkeypatch.setattr(portfolio, "_markets_are_open", lambda: True)

    seen: list[bool] = []
    monkeypatch.setattr(
        market_data,
        "ensure_recent_price",
        lambda _s, _sec, _today, force=False: seen.append(force),
    )

    response = client.get(f"{API}/portfolio/positions", headers=headers)
    assert response.status_code == 200

    # Only the market-hours background task fetches; the foreground valuation
    # reads the cache and never forces a provider call.
    assert seen == [True], f"unexpected foreground or duplicate refreshes: {seen!r}"


def test_positions_skip_the_network_outside_market_hours(
    client: TestClient, session, monkeypatch
) -> None:
    headers = _auth(client)
    _hold(session, client, headers, "CLOSED1")
    monkeypatch.setattr(portfolio, "_markets_are_open", lambda: False)
    seen: list[bool] = []
    monkeypatch.setattr(
        market_data,
        "ensure_recent_price",
        lambda _s, _sec, _today, force=False: seen.append(force),
    )

    response = client.get(f"{API}/portfolio/positions", headers=headers)
    assert response.status_code == 200
    # The inline read path still ran, but nothing was enqueued: no forced
    # background call outside the market-hours window.
    assert True not in seen


# --- _markets_are_open -------------------------------------------------------


def _freeze(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    class _Frozen:
        @staticmethod
        def now(tz=None):
            return when if tz is None else when.astimezone(tz)

    monkeypatch.setattr(portfolio, "datetime", _Frozen)


@pytest.mark.parametrize(
    ("iso", "expected"),
    [
        ("2026-10-05T10:00", True),  # Monday, mid-session
        ("2026-10-05T09:29", False),  # Monday, before the 09:30 open
        ("2026-10-05T09:30", True),  # Monday, at the open
        ("2026-10-05T19:59", True),  # Monday, last minute of the window
        ("2026-10-05T20:00", False),  # Monday, window closed
        ("2026-10-03T12:00", False),  # Saturday — no session at all
        ("2026-10-04T12:00", False),  # Sunday
        ("2026-10-08T14:00", True),  # Thursday, in session
    ],
)
def test_markets_are_open_window(monkeypatch: pytest.MonkeyPatch, iso: str, expected: bool) -> None:
    """Weekdays 09:30-20:00 America/New_York.

    Before 09:30 yfinance has no bar dated today, so a forced refresh could
    only re-return the previous close — the wasted call the gate exists to
    prevent. Weekends have no TSX/NYSE/NASDAQ session at all.
    """
    naive = datetime.fromisoformat(iso).replace(tzinfo=ET)
    _freeze(monkeypatch, naive)
    assert portfolio._markets_are_open() is expected


def test_markets_are_open_uses_the_real_et_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old `6 <= (now.hour - 4) % 24 < 20` estimate was pinned to a fixed
    UTC offset; the gate must read America/New_York so it tracks DST."""
    _freeze(monkeypatch, datetime(2026, 1, 5, 14, 30, tzinfo=UTC))  # 09:30 EST
    assert portfolio._markets_are_open() is True
    _freeze(monkeypatch, datetime(2026, 1, 5, 13, 59, tzinfo=UTC))  # 08:59 EST
    assert portfolio._markets_are_open() is False

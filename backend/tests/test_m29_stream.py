"""M29 regression: SSE stream cache-only contract + benchmarks Decimal math.

docs/29 F5/F5b/F6:
- The stream reads the newest usable cached ``Price`` row on or before today per
  requested security in one DB query — ``fetch_latest_close``/``fetch_history``
  (network) are never reached, future-dated rows are excluded, and each payload
  element serializes the close as a Decimal string.
- A privacy-mode user's stream closes immediately with no events.
- Benchmarks sums/percentages are Decimal; ``portfolio_start`` /
  ``portfolio_end`` / ``portfolio_return_pct`` serialize as strings, and the
  benchmark side is a real cached series since M173 (docs/28 decision 5) — with
  NO series cached, ``benchmark_return_pct`` and ``excess_return_pct`` are
  ``null`` (never a numeric 0 that reads as alpha) while ``benchmark_points``
  counts the (empty) series truthfully.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from unittest.mock import MagicMock

from sqlmodel import select

import app.api._price_stream as price_stream_mod
import app.api.deps as deps_mod
import app.services.market_data as market_data
from app.api._price_stream import POLL_INTERVAL_SECONDS, price_stream_generator
from app.core.security import create_access_token
from app.models.accounts import Account, AccountType
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.services import benchmarks


async def _collect(gen, max_events=None):
    out = []
    try:
        while max_events is None or len(out) < max_events:
            out.append(await gen.__anext__())
    except StopAsyncIteration:
        pass
    return out


def _user(session) -> User:
    user = User(email=f"{uuid.uuid4()}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _security(session, user_id: int, symbol: str = "VEQT") -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind="etf",
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


# ---------------------------------------------------------------------------
# (a) Cache-only payload: newest usable Price per security (date <= today),
#     Decimal-as-string price, user-scoped — and zero market_data network calls.
# ---------------------------------------------------------------------------


def test_stream_payload_cache_only_decimal_strings(session, monkeypatch):
    today = date.today()
    user = _user(session)
    assert user.id is not None
    other = _user(session)
    assert other.id is not None
    security = _security(session, user.id)
    assert security.id is not None
    session.add(
        Price(
            user_id=user.id,
            security_id=security.id,
            date=today - timedelta(days=1),
            price=Decimal("190.00"),
            source=PriceSource.yfinance,
        )
    )
    stale = today - timedelta(days=9)
    session.add(
        Price(
            user_id=user.id,
            security_id=security.id,
            date=stale,
            price=Decimal("188.00"),
            source=PriceSource.manual,
        )
    )
    session.add(
        Price(
            user_id=user.id,
            security_id=security.id,
            date=today,
            price=Decimal("192.45"),
            source=PriceSource.yfinance,
        )
    )
    foreign = _security(session, other.id, symbol="XIU")
    assert foreign.id is not None
    session.add(
        Price(
            user_id=other.id,
            security_id=foreign.id,
            date=today,
            price=Decimal("30.00"),
            source=PriceSource.yfinance,
        )
    )
    session.commit()
    newest = session.exec(
        select(Price).where(Price.security_id == security.id).order_by(Price.date.desc())
    ).first()
    assert newest is not None and newest.date == today
    newest_price = str(newest.price)
    newest_date = newest.date
    newest_source = newest.source.value
    session.add(
        Price(
            user_id=user.id,
            security_id=security.id,
            date=today + timedelta(days=1),
            price=Decimal("999.00"),
            source=PriceSource.manual,
        )
    )
    session.commit()

    # Prove the stream stays cache-only: both network entry points are replaced
    # with spies that must never be called.
    monkeypatch.setattr(price_stream_mod, "SQLModelSession", lambda *args, **kwargs: session)
    fetch_latest = MagicMock()
    fetch_history = MagicMock()
    monkeypatch.setattr(market_data, "fetch_latest_close", fetch_latest)
    monkeypatch.setattr(market_data, "fetch_history", fetch_history)

    gen = price_stream_generator([security.id, foreign.id], user.id)
    events = asyncio.run(_collect(gen, max_events=1))

    assert len(events) == 1
    payload = json.loads(events[0].removeprefix("data: "))
    # Newest usable close only, own rows only, future-dated rows excluded;
    # Decimal serialized as string.
    assert payload == [
        {
            "security_id": security.id,
            "symbol": "VEQT",
            "price": newest_price,
            "date": newest_date.isoformat(),
            "source": newest_source,
            "age_days": 0,
            "stale": False,
        }
    ]
    for row in payload:
        assert set(row) == {
            "security_id",
            "symbol",
            "price",
            "date",
            "source",
            "age_days",
            "stale",
        }
        assert isinstance(row["price"], str)
        Decimal(row["price"])  # must parse as a Decimal string, not a float
    fetch_latest.assert_not_called()
    fetch_history.assert_not_called()


def test_poll_interval_unchanged():
    assert POLL_INTERVAL_SECONDS == 5


# ---------------------------------------------------------------------------
# (b) Privacy mode: the stream closes immediately, no events, no DB/network.
# ---------------------------------------------------------------------------


def test_privacy_mode_user_stream_closes_immediately(session, monkeypatch):
    today = date.today()
    user = _user(session)
    user.privacy_mode = True
    session.add(user)
    session.commit()
    security = _security(session, user.id)
    session.add(
        Price(
            user_id=user.id,
            security_id=security.id,
            date=today,
            price=Decimal("42.00"),
            source=PriceSource.manual,
        )
    )
    session.commit()

    sessions_opened = []

    def spy_session(*args, **kwargs):
        sessions_opened.append(1)
        return session

    monkeypatch.setattr(price_stream_mod, "SQLModelSession", spy_session)
    monkeypatch.setattr(market_data, "fetch_latest_close", MagicMock())
    monkeypatch.setattr(market_data, "fetch_history", MagicMock())

    market_data.bind_privacy_lock(True)
    try:
        gen = price_stream_generator([security.id], user.id)
        events = asyncio.run(_collect(gen))
    finally:
        market_data.bind_privacy_lock(False)

    assert events == []
    assert sessions_opened == []  # closed before any DB access
    market_data.fetch_latest_close.assert_not_called()  # type: ignore[attr-defined]
    market_data.fetch_history.assert_not_called()  # type: ignore[attr-defined]


def test_privacy_mode_endpoint_closes_stream_with_no_events(client, session):
    today = date.today()
    user = _user(session)
    user.privacy_mode = True
    session.add(user)
    session.commit()
    security = _security(session, user.id)
    session.add(
        Price(
            user_id=user.id,
            security_id=security.id,
            date=today,
            price=Decimal("42.00"),
            source=PriceSource.manual,
        )
    )
    session.commit()

    headers = {"Authorization": f"Bearer {create_access_token(user.id)}"}
    with client.stream(
        "GET",
        f"/api/v1/securities/stream/prices?security_ids={security.id}",
        headers=headers,
    ) as response:
        assert response.status_code == 200
        chunks = "".join(response.iter_text())
    assert "data:" not in chunks  # closed immediately, zero events


def test_stream_rejects_non_integer_security_ids_with_validation_error(client, session):
    user = _user(session)
    assert user.id is not None
    headers = {"Authorization": f"Bearer {create_access_token(user.id)}"}

    response = client.get(
        "/api/v1/securities/stream/prices",
        params={"security_ids": "1,nope"},
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"


def test_query_token_auth_releases_db_session_before_streaming(monkeypatch):
    """The SSE auth dependency must not pin a pooled connection for the
    stream's lifetime. FastAPI tears down yield-dependencies only after the
    long-lived response finishes, so the dependency closes the session itself.
    """
    db = MagicMock()
    user = User(id=42, email="sse@example.com", hashed_password="x")
    db.get.return_value = user
    decode = MagicMock(return_value=user.id)
    bind_fx = MagicMock()
    bind_market_data = MagicMock()
    monkeypatch.setattr(deps_mod, "_decode_user_id", decode)
    monkeypatch.setattr(deps_mod.fx_service, "bind_privacy_lock", bind_fx)
    monkeypatch.setattr(deps_mod.market_data_service, "bind_privacy_lock", bind_market_data)

    resolved = asyncio.run(
        deps_mod.get_current_user_with_query_token(
            credentials=None,
            token="query-token",
            db=db,
        )
    )

    assert resolved is user
    decode.assert_called_once_with(None, "query-token")
    bind_fx.assert_called_once_with(user.privacy_mode)
    bind_market_data.assert_called_once_with(user.privacy_mode)
    db.close.assert_called_once_with()


# ---------------------------------------------------------------------------
# (d) Benchmarks: Decimal sums, string serialization, numeric 0 placeholder.
# ---------------------------------------------------------------------------


def _rrsp_holding(session, quantity: Decimal, buy_price: Decimal):
    """User + RRSP account + one security with a single buy trade."""
    user = _user(session)
    account = Account(user_id=user.id, name="RRSP", type=AccountType.rrsp, currency="CAD")
    security = _security(session, user.id)
    session.add(account)
    session.commit()
    session.refresh(account)
    session.add(
        Trade(
            user_id=user.id,
            account_id=account.id,
            security_id=security.id,
            action=TradeAction.buy,
            date=date(2025, 1, 2),
            quantity=quantity,
            price_native=buy_price,
        )
    )
    session.commit()
    return user, account, security


def _seed_prices(session, user, security, prices: dict[date, Decimal]) -> None:
    """Benchmark comparison is cache-only; seed the required closes directly."""
    session.add_all(
        [
            Price(
                user_id=user.id,
                security_id=security.id,
                date=day,
                price=price,
                source=PriceSource.manual,
            )
            for day, price in prices.items()
        ]
    )
    session.flush()


def test_benchmark_decimal_math_and_string_serialization(session):
    user, _, security = _rrsp_holding(session, Decimal("10"), Decimal("100"))
    _seed_prices(
        session,
        user,
        security,
        {date(2025, 1, 5): Decimal("100"), date(2025, 2, 5): Decimal("110")},
    )

    result = benchmarks.compute_benchmark_comparison(
        session, user.id, date(2025, 1, 5), date(2025, 2, 5)
    )

    start = Decimal("1000.00")  # 10 x 100
    end = Decimal("1100.00")  # 10 x 110
    expected_return = ((end - start) / start * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )
    assert result["portfolio_start"] == str(start)
    assert result["portfolio_end"] == str(end)
    assert result["portfolio_return_pct"] == str(expected_return)
    # M173 (docs/28 decision 5): no series is cached here, so the benchmark
    # return is NULL — never the numeric-0 placeholder that M65 had to gate —
    # and the excess is null with it, so the two can never disagree.
    assert result["benchmark_return_pct"] is None
    assert result["excess_return_pct"] is None
    # M65 / m61 #7: benchmark_points gates the UI's EXCESS cell; with no
    # benchmark series it is 0 — never a value the panel could read as alpha.
    assert result["benchmark_points"] == 0


def test_benchmark_zero_start_serializes_strings(session):
    user = _user(session)  # user with no positions at all
    result = benchmarks.compute_benchmark_comparison(
        session, user.id, date(2025, 1, 1), date(2025, 2, 1)
    )
    assert result["portfolio_start"] == "0"
    assert result["portfolio_end"] == "0"
    assert result["portfolio_return_pct"] == "0.00"
    assert result["benchmark_return_pct"] is None
    assert result["benchmark_points"] == 0


def test_benchmark_wire_response_serializes_strings(client, session):
    user, _, security = _rrsp_holding(session, Decimal("2"), Decimal("50"))
    _seed_prices(
        session,
        user,
        security,
        {date(2025, 1, 5): Decimal("50"), date(2025, 2, 5): Decimal("55")},
    )

    token = create_access_token(user.id)
    response = client.get(
        "/api/v1/portfolio/benchmarks",
        params={"start_date": "2025-01-05", "end_date": "2025-02-05"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["portfolio_start"] == "100.00"
    assert body["portfolio_end"] == "110.00"
    assert body["benchmark_points"] == 0
    assert body["portfolio_return_pct"] == "10.00"
    assert body["benchmark_return_pct"] is None
    assert body["benchmark_symbol"] == "SPY"

"""market_data service + prices upsert contract (w2-contract §BackendMarkets 3/5).

Network edge (yfinance) is monkeypatched — no external HTTP in CI.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

import app.services.market_data as market_data
from app.models.accounts import Account, AccountType
from app.models.prices import Price, PriceSource
from app.models.securities import QuoteOutcome, Security
from app.models.users import User

PRICES_URL = "/api/v1/prices"


def _auth(client: TestClient) -> dict[str, str]:
    email = f"{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _security(session: Session, user_id: int, symbol: str = "AAPL") -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} Inc.",
        kind="stock",
        listing_currency="USD",
        exchange="NMS",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _me_id(client: TestClient, headers: dict[str, str]) -> int:
    return client.get("/api/v1/users/me", headers=headers).json()["id"]


def test_backfill_inserts_only_missing_and_never_touches_manual(
    client: TestClient, session: Session, monkeypatch
) -> None:
    user_id = _me_id(client, _auth(client))
    security = _security(session, user_id)
    today = date.today()
    manual_day = today - timedelta(days=3)
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=manual_day,
            price=Decimal("999.00"),
            source=PriceSource.manual,
        )
    )
    session.commit()

    closes = {
        manual_day - timedelta(days=offset): Decimal(str(100 + offset))
        for offset in range(3)  # covers manual_day too — must be skipped there
    }
    monkeypatch.setattr(market_data, "fetch_history", lambda symbol, s, e: closes)

    inserted = market_data.backfill_range(session, security, today - timedelta(days=3), today)

    assert inserted == 2  # only the two dates lacking rows
    rows = list(session.exec(select(Price).where(Price.security_id == security.id)))
    by_date = {row.date: row for row in rows}
    assert len(rows) == 3
    assert Decimal(str(by_date[manual_day].price)) == Decimal("999.00")  # manual untouched
    assert by_date[manual_day].source is PriceSource.manual
    yf_rows = [row for day, row in by_date.items() if day != manual_day]
    assert all(row.source is PriceSource.yfinance for row in yf_rows)


def test_fetch_history_degrades_to_empty_on_failure(monkeypatch) -> None:
    class ExplodingTicker:
        def __init__(self, symbol: str) -> None:
            pass

        def history(self, **kwargs):
            raise RuntimeError("yfinance is down")

    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", ExplodingTicker)
    monkeypatch.setattr(market_data.time, "sleep", lambda seconds: None)
    # M142: both candidates miss -> the search fallback runs next, so pin it
    # too (no external HTTP in CI) — no hit means the empty answer stands.
    monkeypatch.setattr(market_data, "_search_equity_quotes", lambda query: [])

    today = date.today()
    assert market_data.fetch_history("AAPL", today - timedelta(days=5), today) == {}


def test_fetch_history_retries_canadian_suffix(monkeypatch) -> None:
    """Bare CDN tickers (e.g. VEQT without `.TO`) resolve via suffix retry."""

    class _Idx:
        def __init__(self, d: date) -> None:
            self._d = d

        def date(self) -> date:
            return self._d

    class _Frame:
        empty = False

        def iterrows(self):
            yield _Idx(date(2026, 8, 27)), {"Close": 62.49}

    class _Empty:
        empty = True

    class _SuffixTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **kwargs):
            if self.symbol == "VEQT.TO":
                return _Frame()
            return _Empty()

    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", _SuffixTicker)
    monkeypatch.setattr(market_data.time, "sleep", lambda seconds: None)

    start = date(2026, 8, 1)
    end = date(2026, 8, 27)
    closes = market_data.fetch_history("VEQT", start, end)
    assert closes == {date(2026, 8, 27): Decimal("62.49")}


def test_ensure_recent_price_missing_row_fetches_latest_close(
    client: TestClient, session: Session, monkeypatch
) -> None:
    user_id = _me_id(client, _auth(client))
    security = _security(session, user_id)
    today = date.today()

    monkeypatch.setattr(
        market_data,
        "fetch_latest_close",
        lambda symbol, today=None: (today or date.today(), Decimal("424.20")),
    )

    price = market_data.ensure_recent_price(session, security, today)
    assert price is not None
    assert price.date == today
    assert price.source is PriceSource.yfinance
    assert Decimal(str(price.price)) == Decimal("424.20")


def test_price_upsert_overwrite_semantics(client: TestClient, session: Session) -> None:
    headers = _auth(client)
    security = _security(session, _me_id(client, headers))
    day = date.today().isoformat()

    created = client.post(
        PRICES_URL,
        json={"security_id": security.id, "date": day, "price": "100.50"},
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["source"] == "manual"

    # Same (security_id, date): human overwrite keeps ONE row, source=manual.
    overwritten = client.post(
        PRICES_URL,
        json={"security_id": security.id, "date": day, "price": "101.25"},
        headers=headers,
    )
    assert overwritten.status_code == 200
    assert Decimal(str(overwritten.json()["price"])) == Decimal("101.25")
    assert overwritten.json()["source"] == "manual"
    assert overwritten.json()["id"] == created.json()["id"]

    rows = list(session.exec(select(Price).where(Price.security_id == security.id)))
    assert len(rows) == 1
    assert Decimal(str(rows[0].price)) == Decimal("101.25")

    listed = client.get(PRICES_URL, params={"security_id": security.id}, headers=headers)
    assert listed.status_code == 200
    page = listed.json()
    assert set(page) == {"items", "total", "limit", "offset"}
    assert page["total"] == 1

    patched = client.patch(
        f"{PRICES_URL}/{created.json()['id']}", json={"price": "102.00"}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["source"] == "manual"  # human edit stays human


def test_price_rejects_foreign_security(client: TestClient, session: Session) -> None:
    attacker = _auth(client)
    owner = _auth(client)
    foreign_security = _security(session, _me_id(client, owner))

    response = client.post(
        PRICES_URL,
        json={"security_id": foreign_security.id, "date": date.today().isoformat(), "price": "10"},
        headers=attacker,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_manual_account_fixture_smoke(session: Session) -> None:
    """Guard: accounts model usable from session seeding (portfolio tests rely on it)."""
    email = f"{datetime.now(UTC).timestamp()}@example.com"
    user = User(email=email, hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    account = Account(
        user_id=user.id,
        name="Test",
        type=AccountType.brokerage_non_registered,
        currency="CAD",
        taxable=True,
    )
    session.add(account)
    session.commit()
    assert account.id is not None


def test_fetch_dividend_history_returns_decimal_amounts(monkeypatch) -> None:
    """Per-share dividends serialize as exact 6dp Decimal strings, never floats.

    The ticker seam is injected so the test needs no network.
    """
    import datetime as _dt

    class _Ts:
        def __init__(self, d: _dt.date) -> None:
            self._d = d

        def date(self) -> _dt.date:
            return self._d

    class _FakeSeries:
        @property
        def empty(self) -> bool:
            return False

        def items(self):
            # yfinance yields a DatetimeIndex sorted ascending; the code
            # reverses to newest-first, so emit oldest-first here.
            return iter(
                [
                    (_Ts(_dt.date(2024, 1, 1)), 0.123456789),
                    (_Ts(_dt.date(2024, 1, 15)), 1.5),
                ]
            )

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def dividends(self) -> _FakeSeries:
            return _FakeSeries()

    monkeypatch.setattr(market_data, "_ticker", lambda symbol: _FakeTicker(symbol))

    result = market_data.fetch_dividend_history("AAPL")
    assert result is not None
    # Newest first.
    assert result[0]["date"] == "2024-01-15"
    assert result[1]["date"] == "2024-01-01"
    # Exact decimal strings, fixed 6dp, no float artifacts.
    assert result[0]["amount"] == "1.500000"
    assert result[1]["amount"] == "0.123457"
    assert Decimal(result[0]["amount"]) == Decimal("1.500000")
    assert Decimal(result[1]["amount"]) == Decimal("0.123457")


def test_empty_dividend_frame_needs_a_quote_before_it_is_success(monkeypatch) -> None:
    """Yahoo's quote-not-found shape must not become a destructive empty refresh."""
    from yfinance.exceptions import YFTzMissingError

    class _EmptyDividends:
        empty = True

    class _MissingTicker:
        @property
        def dividends(self) -> _EmptyDividends:
            return _EmptyDividends()

        def history(self, **_kwargs):
            raise YFTzMissingError("$NO-DIVS: possibly delisted; no timezone found")

    monkeypatch.setattr(market_data, "_ticker", lambda symbol: _MissingTicker())
    monkeypatch.setattr(market_data, "_search_equity_quotes", lambda query: [])

    assert market_data.fetch_dividend_history("NO-DIVS") is None
    outcome, reason = market_data.last_quote_verdict()
    assert outcome is QuoteOutcome.not_found
    assert reason is not None and "delisted" in reason

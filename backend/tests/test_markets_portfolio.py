"""portfolio valuation contract (w2-contract §BackendMarkets 6-8, docs/10 M4 smoke).

Network edges (yfinance, BoC Valet) are monkeypatched — no external HTTP in CI.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

import app.services.fx as fx_service
import app.services.market_data as market_data
from app.models.acb_pools import AcbPool
from app.models.accounts import Account, AccountType
from app.models.fx_rates import FxRate, FxSource
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.tax.acb import rebuild_user_pools

PORTFOLIO_URL = "/api/v1/portfolio"


def _today():
    """The app values positions against the UTC calendar date; seeds must match."""
    return datetime.now(UTC).date()


def _auth(client: TestClient) -> dict[str, str]:
    email = f"{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _account(session: Session, user_id: int, currency: str) -> Account:
    account = Account(
        user_id=user_id,
        name=f"Brokerage {currency}",
        type=AccountType.brokerage_non_registered,
        currency=currency,
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _security(
    session: Session,
    user_id: int,
    symbol: str,
    listing_currency: str = "USD",
) -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind="stock",
        listing_currency=listing_currency,
        exchange="NMS" if listing_currency == "USD" else "TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _trade(
    session: Session,
    user_id: int,
    account: Account,
    security: Security,
    action: TradeAction,
    quantity: str,
    price: str,
    day: date | None = None,
    commission: str = "0",
) -> None:
    session.add(
        Trade(
            user_id=user_id,
            account_id=account.id,
            security_id=security.id,
            action=action,
            date=day or _today(),
            quantity=Decimal(quantity),
            price_native=Decimal(price),
            commission=Decimal(commission),
        )
    )
    session.commit()


def _registered_account(session: Session, user_id: int, name: str) -> Account:
    account = Account(
        user_id=user_id,
        name=name,
        type=AccountType.tfsa,
        currency="CAD",
        taxable=False,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _no_network(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("network fetch attempted although data is cached")

    monkeypatch.setattr(market_data, "fetch_latest_close", forbidden)
    monkeypatch.setattr(market_data, "fetch_history", forbidden)
    monkeypatch.setattr(fx_service, "fetch_valet_series", forbidden)


def _price(session: Session, user_id: int, security: Security, day: date, value: str) -> None:
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=day,
            price=Decimal(value),
            source=PriceSource.manual,
        )
    )


def test_positions_cad_and_usd_buys_docs10_smoke(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """docs/10 M4 smoke: CAD buy + USD buy of ONE security -> signed qty +
    both native (USD-listed) and CAD values correct."""
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]

    def no_network(*args, **kwargs):
        raise AssertionError("network fetch attempted although price + fx are cached")

    monkeypatch.setattr(market_data, "fetch_latest_close", no_network)
    monkeypatch.setattr(fx_service, "fetch_valet_series", no_network)

    cad_account = _account(session, user_id, "CAD")
    usd_account = _account(session, user_id, "USD")
    aapl = _security(session, user_id, "AAPL", listing_currency="USD")

    today = _today()
    _trade(session, user_id, cad_account, aapl, TradeAction.buy, "2", "100.00")  # CAD leg
    _trade(session, user_id, usd_account, aapl, TradeAction.buy, "3", "150.00")  # USD leg
    _trade(session, user_id, usd_account, aapl, TradeAction.sell, "-1", "160.00")

    session.add(
        Price(
            user_id=user_id,
            security_id=aapl.id,
            date=today,
            price=Decimal("200"),
            source=PriceSource.manual,
        )
    )
    session.add(
        FxRate(base="USD", quote="CAD", rate=Decimal("1.25"), date=today, source=FxSource.boc_valet)
    )
    session.commit()

    response = client.get(f"{PORTFOLIO_URL}/positions", headers=headers)

    assert response.status_code == 200
    positions = response.json()["items"]
    # docs/08: positions are PER (account, security) -> two accounts, two rows.
    assert len(positions) == 2
    by_account = {p["account_name"]: p for p in positions}
    cad_row = by_account["Brokerage CAD"]
    usd_row = by_account["Brokerage USD"]
    # Signed sums: CAD leg bought 2; USD leg bought 3, sold 1 -> net 2.
    assert Decimal(str(cad_row["quantity"])) == Decimal("2")
    assert Decimal(str(usd_row["quantity"])) == Decimal("2")
    assert sum(Decimal(str(p["quantity"])) for p in positions) == Decimal("4")

    for position in (cad_row, usd_row):
        assert position["symbol"] == "AAPL"
        assert position["listing_currency"] == "USD"
        assert position["price_source"] == "manual"
        assert position["price_age_days"] in (0, 1)
        assert position["price_stale"] is False
        # w12 fields ride along on every row: single seeded close -> a
        # one-point spark; book_cost_cad key present (pool cache not rebuilt
        # for these direct inserts, so the value is null here).
        assert len(position["spark"]) == 1
        assert "book_cost_cad" in position
    # Combined view matches the docs/10 smoke numbers.
    assert sum(Decimal(str(p["market_value_native"])) for p in positions) == Decimal("800.00")
    assert sum(Decimal(str(p["market_value_cad"])) for p in positions) == Decimal("1000.00")


def test_exposure_grouped_by_listing_currency_sorted_desc(
    client: TestClient, session: Session, monkeypatch
) -> None:
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _account(session, user_id, "CAD")
    aapl = _security(session, user_id, "AAPL", listing_currency="USD")
    ry = _security(session, user_id, "RY", listing_currency="CAD")
    _trade(session, user_id, account, aapl, TradeAction.buy, "2", "100.00")
    _trade(session, user_id, account, ry, TradeAction.buy, "10", "150.00")

    today = _today()
    session.add(
        Price(
            user_id=user_id,
            security_id=aapl.id,
            date=today,
            price=Decimal("400"),
            source=PriceSource.manual,
        )
    )  # USD 800
    session.add(
        Price(
            user_id=user_id,
            security_id=ry.id,
            date=today,
            price=Decimal("150"),
            source=PriceSource.manual,
        )
    )  # CAD 1500
    session.commit()

    from unittest.mock import patch

    with (
        patch.object(market_data, "fetch_latest_close"),
        patch.object(fx_service, "fetch_valet_series"),
    ):
        response = client.get(f"{PORTFOLIO_URL}/exposure", headers=headers)

    assert response.status_code == 200
    rows = response.json()["items"]
    assert [row["listing_currency"] for row in rows] == ["CAD", "USD"]  # desc by value
    assert Decimal(str(rows[0]["total_market_value_native"])) == Decimal("1500.00")
    assert rows[0]["holdings_count"] == 1
    assert Decimal(str(rows[1]["total_market_value_native"])) == Decimal("800.00")


def test_staleness_boundary_exact_7d_fresh_8d_stale(
    client: TestClient, session: Session, monkeypatch
) -> None:
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _account(session, user_id, "USD")
    security = _security(session, user_id, "MSFT")
    _trade(session, user_id, account, security, TradeAction.buy, "1", "300.00")
    today = _today()

    def refuse(*args, **kwargs):
        raise AssertionError("refresh attempted at or inside the 7-day boundary")

    # Exactly 7 days old: NOT stale (stale iff age_days > 7) and no refresh.
    boundary_row = Price(
        user_id=user_id,
        security_id=security.id,
        date=today - timedelta(days=7),
        price=Decimal("300"),
        source=PriceSource.manual,
    )
    session.add(boundary_row)
    session.commit()
    monkeypatch.setattr(market_data, "fetch_latest_close", refuse)

    fresh = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()["items"][0]
    assert fresh["price_age_days"] == 7
    assert fresh["price_stale"] is False
    # t_bd5ec95f (Option A): the fetch timestamp may only claim a REAL fetch.
    # This row was seeded by hand — no provider stamped it — so the key is
    # present and NULL, and market-date age stays the only freshness fact.
    assert fresh["price_fetched_at"] is None

    # One day older: stale flag flips on; failed refresh degrades to the cached row.
    boundary_row.date = today - timedelta(days=8)
    session.add(boundary_row)
    session.commit()
    monkeypatch.setattr(market_data, "fetch_latest_close", lambda *a, **k: None)

    stale = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()["items"][0]
    assert stale["price_age_days"] == 8
    assert stale["price_stale"] is True
    assert Decimal(str(stale["price"])) == Decimal("300")  # degrade keeps cached close


def test_zero_quantity_position_is_skipped(client: TestClient, session: Session) -> None:
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _account(session, user_id, "CAD")
    security = _security(session, user_id, "BMO", listing_currency="CAD")
    _trade(session, user_id, account, security, TradeAction.buy, "5", "100.00")
    _trade(session, user_id, account, security, TradeAction.sell, "-5", "110.00")

    positions = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()["items"]
    assert positions == []


def test_missing_fx_rate_degrades_market_value_cad_to_none(
    client: TestClient, session: Session, monkeypatch
) -> None:
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _account(session, user_id, "EUR")
    security = _security(session, user_id, "SAP", listing_currency="EUR")
    _trade(session, user_id, account, security, TradeAction.buy, "4", "90.00")
    today = _today()
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=today,
            price=Decimal("100"),
            source=PriceSource.manual,
        )
    )
    session.commit()

    def exhausted(*args, **kwargs):
        raise fx_service.ValetUnavailable("offline")

    monkeypatch.setattr(fx_service, "fetch_valet_series", exhausted)

    response = client.get(f"{PORTFOLIO_URL}/positions", headers=headers)

    assert response.status_code == 200  # never a 5xx for a missing rate
    position = response.json()["items"][0]
    assert Decimal(str(position["market_value_native"])) == Decimal("400.00")
    assert position["market_value_cad"] is None


def test_cad_listed_position_needs_no_fx_row(client: TestClient, session: Session) -> None:
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _account(session, user_id, "CAD")
    security = _security(session, user_id, "RY", listing_currency="CAD")
    _trade(session, user_id, account, security, TradeAction.buy, "10", "150.00")
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=_today(),
            price=Decimal("155"),
            source=PriceSource.manual,
        )
    )
    session.commit()

    position = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()["items"][0]
    assert Decimal(str(position["market_value_cad"])) == Decimal("1550.00")


def test_nonregistered_book_cost_equals_pool_after_partial_sell(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """w12 pin 3: non-registered book_cost_cad == acb_pools.total_acb_cad.

    Hand check (CAD-listed, no fx): buy 100 @ $10 -> ACB 1000.00;
    buy 100 @ $20 -> 3000.00 over 200 sh; sell 50 allocates
    3000.00 x 50/200 = 750.00 -> pool ACB 2250.00 over 150 sh.
    """
    _no_network(monkeypatch)
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _account(session, user_id, "CAD")
    sec = _security(session, user_id, "BNS", listing_currency="CAD")
    today = _today()
    _trade(session, user_id, account, sec, TradeAction.buy, "100", "10", today - timedelta(days=9))
    _trade(session, user_id, account, sec, TradeAction.buy, "100", "20", today - timedelta(days=8))
    _trade(session, user_id, account, sec, TradeAction.sell, "-50", "25", today - timedelta(days=7))
    _price(session, user_id, sec, today - timedelta(days=1), "30")
    session.commit()
    rebuild_user_pools(session, user_id)

    position = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()["items"][0]
    pool = session.exec(select(AcbPool).where(AcbPool.security_id == sec.id)).one()
    assert Decimal(str(pool.total_acb_cad)) == Decimal("2250.00")
    assert position["book_cost_cad"] is not None
    assert Decimal(str(position["book_cost_cad"])) == pool.total_acb_cad == Decimal("2250.00")
    assert Decimal(str(position["quantity"])) == Decimal("150")


def test_registered_replay_buy_sell_split_roc(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """w12 pin 3: registered replay matches the hand calculation.

    Hand check (CAD-listed): buy 100 @ $10 -> ACB 1000.00; sell 40 removes
    1000.00 x 40/100 = 400.00 -> 600.00 over 60 sh; 2-for-1 split keeps ACB
    at 600.00 and sets qty to 120; ROC of $150 reduces to 450.00.
    """
    _no_network(monkeypatch)
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _registered_account(session, user_id, "WS TFSA")
    sec = _security(session, user_id, "TD", listing_currency="CAD")
    today = _today()
    _trade(session, user_id, account, sec, TradeAction.buy, "100", "10", today - timedelta(days=6))
    _trade(session, user_id, account, sec, TradeAction.sell, "-40", "12", today - timedelta(days=5))
    _trade(
        session,
        user_id,
        account,
        sec,
        TradeAction.corporate_action_adjustment,
        "120",
        "0",
        today - timedelta(days=4),
    )
    _trade(
        session,
        user_id,
        account,
        sec,
        TradeAction.return_of_capital,
        "0",
        "150",
        today - timedelta(days=3),
    )
    _price(session, user_id, sec, today - timedelta(days=1), "5")
    session.commit()

    position = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()["items"][0]
    assert Decimal(str(position["quantity"])) == Decimal("120")
    assert Decimal(str(position["book_cost_cad"])) == Decimal("450.00")
    # Replay is pure math: registered accounts never persist pools.
    assert session.exec(select(AcbPool)).all() == []


def test_registered_replay_buy_commission_and_roc_floor(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """Commission capitalizes into the basis; ROC below zero floors at 0."""
    _no_network(monkeypatch)
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _registered_account(session, user_id, "NB LIRA")
    sec = _security(session, user_id, "ENB", listing_currency="CAD")
    today = _today()
    # buy 10 @ $20 + $9.95 commission -> ACB 209.95
    _trade(
        session,
        user_id,
        account,
        sec,
        TradeAction.buy,
        "10",
        "20",
        today - timedelta(days=2),
        commission="9.95",
    )
    # ROC of $500 exceeds the basis -> floor at zero.
    _trade(
        session,
        user_id,
        account,
        sec,
        TradeAction.return_of_capital,
        "0",
        "500",
        today - timedelta(days=1),
    )
    _price(session, user_id, sec, today - timedelta(days=1), "21")
    session.commit()

    position = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()["items"][0]
    assert Decimal(str(position["quantity"])) == Decimal("10")
    assert Decimal(str(position["book_cost_cad"])) == Decimal("0.00")


def test_spark_last_30_ascending_manual_wins(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """w12 pin 3: spark is <= 30 closes ascending by date; manual rows win."""
    _no_network(monkeypatch)
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _registered_account(session, user_id, "WS TFSA spark")
    sec = _security(session, user_id, "SHOP", listing_currency="CAD")
    today = _today()
    _trade(session, user_id, account, sec, TradeAction.buy, "1", "100", today - timedelta(days=40))
    for offset in range(35):
        day = today - timedelta(days=35 - offset)
        _price(session, user_id, sec, day, str(50 + offset))
    session.commit()
    # Manual wins over yfinance on the SAME date: rewrite the newest row.
    newest = session.exec(
        select(Price).where(Price.security_id == sec.id).order_by(Price.date.desc())
    ).first()
    newest.source = PriceSource.manual
    newest.price = Decimal("999")
    session.add(newest)
    session.commit()

    position = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()["items"][0]
    spark = position["spark"]
    assert len(spark) == 30
    values = [Decimal(v) for v in spark]
    assert values == sorted(values)  # ascending by date
    assert values[-1] == Decimal("999")  # manual value won
    assert values[0] == Decimal("55")  # oldest of the LAST 30 days


def test_zero_price_security_spark_empty_book_cost_still_computed(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """w12 pin 3: no prices -> spark [], yet book cost still replays."""
    _no_network(monkeypatch)
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _registered_account(session, user_id, "WS TFSA noprices")
    sec = _security(session, user_id, "ZZZP", listing_currency="CAD")
    # No price rows at all -> valuation legitimately tries its fetch seam;
    # make it return nothing so the endpoint degrades instead of erroring.
    monkeypatch.setattr(market_data, "fetch_latest_close", lambda *a, **k: None)
    _trade(session, user_id, account, sec, TradeAction.buy, "5", "12.34", _today())
    session.commit()

    position = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()["items"][0]
    assert position["spark"] == []
    assert position["price"] is None
    assert Decimal(str(position["book_cost_cad"])) == Decimal("61.70")

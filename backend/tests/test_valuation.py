"""Portfolio valuation: compute_exposure per-currency native bucketing (scout M5).

Pure service contract — exercised directly, not via the endpoint. Mirrors the
offline discipline of test_markets_portfolio (docs/07): every input is a cached
row; any fetch attempt is forbidden by the autouse fixture.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.acb_pools import AcbPool
from app.models.accounts import Account, AccountType
from app.models.fx_rates import FxRate, FxSource
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.services import fx as fx_service
from app.services import market_data as market_data_service
from app.services.valuation import compute_exposure, compute_positions


def _today() -> date:
    return datetime.now(UTC).date()


def _auth(client: TestClient) -> dict[str, str]:
    email = f"val-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "password123",
            "display_name": "Val",
            "currency": "CAD",
        },
    )
    body = client.post(
        "/api/v1/auth/login",
        json={
            "email": email,
            "password": "password123",
            "display_name": "Val",
            "currency": "CAD",
        },
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _account(session: Session, user_id: int) -> Account:
    account = Account(
        user_id=user_id,
        name="Brokerage",
        type=AccountType.brokerage_non_registered,
        currency="CAD",
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _security(session: Session, user_id: int, symbol: str, listing_currency: str) -> Security:
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


def _buy(
    session: Session,
    user_id: int,
    account: Account,
    security: Security,
    qty: str,
    price: str,
    commission: str = "0",
) -> None:
    """Buy trade + a fresh manual close so market_value_native is computable."""
    today = _today()
    session.add(
        Trade(
            user_id=user_id,
            account_id=account.id,
            security_id=security.id,
            action=TradeAction.buy,
            date=today,
            quantity=Decimal(qty),
            price_native=Decimal(price),
            commission=Decimal(commission),
        )
    )
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=today,
            price=Decimal(price),
            source=PriceSource.manual,
        )
    )
    session.commit()


def _sell(
    session: Session, user_id: int, account: Account, security: Security, qty: str, price: str
) -> None:
    """Sell trade, stored NEGATIVE per the docs/02 signed-quantity convention."""
    session.add(
        Trade(
            user_id=user_id,
            account_id=account.id,
            security_id=security.id,
            action=TradeAction.sell,
            date=_today(),
            quantity=-Decimal(qty),
            price_native=Decimal(price),
            commission=Decimal("0"),
        )
    )
    session.commit()


def _fx(session: Session, base: str, quote: str, rate: str, day: date) -> None:
    session.add(
        FxRate(base=base, quote=quote, date=day, rate=Decimal(rate), source=FxSource.manual)
    )
    session.commit()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("network fetch attempted although data is cached")

    monkeypatch.setattr(market_data_service, "fetch_latest_close", forbidden)
    monkeypatch.setattr(market_data_service, "fetch_history", forbidden)
    monkeypatch.setattr(fx_service, "fetch_valet_series", forbidden)


def test_registered_position_after_sell_replays_reduced_book_cost(
    client: TestClient, session: Session
) -> None:
    """A registered sell shrinks the replayed holding and its average cost.

    CAD-listed (fx = 1): buy 10 @ $20 (+$9.95 commission) -> 10 sh / 209.95;
    sell 4 -> removes 209.95 x 4/10 = 83.98 -> 6 sh / 125.97.
    """
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    registered = Account(
        user_id=user_id,
        name="RRSP",
        type=AccountType.rrsp,
        currency="CAD",
        taxable=False,
    )
    session.add(registered)
    session.commit()
    session.refresh(registered)
    ry = _security(session, user_id, "RY", "CAD")
    _buy(session, user_id, registered, ry, "10", "20.00", commission="9.95")
    _sell(session, user_id, registered, ry, "4", "25.00")
    _fx(session, "USD", "CAD", "1.30", _today())  # never fetched; keeps the path cache-only

    positions = compute_positions(session, user_id, account_id=registered.id)

    assert len(positions) == 1
    position = positions[0]
    assert position["quantity"] == Decimal("6.000000")
    assert position["book_cost_cad"] == Decimal("125.97")
    # Registered accounts are still excluded from the persisted ACB cache.
    assert session.exec(select(AcbPool)).all() == []


def test_compute_exposure_groups_by_listing_currency_with_cad_usd_split(
    client: TestClient, session: Session
) -> None:
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _account(session, user_id)

    ry = _security(session, user_id, "RY", "CAD")
    aapl = _security(session, user_id, "AAPL", "USD")
    msft = _security(session, user_id, "MSFT", "USD")
    _buy(session, user_id, account, ry, "10", "150.00")  # CAD 1500.00
    _buy(session, user_id, account, aapl, "2", "100.00")  # USD 200.00
    _buy(session, user_id, account, msft, "5", "50.00")  # USD 250.00
    _fx(session, "USD", "CAD", "1.30", _today())  # cached so no fetch

    rows = compute_exposure(session, user_id)

    # Sorted desc by native value: CAD 1500 > USD 450.
    assert [r["listing_currency"] for r in rows] == ["CAD", "USD"]
    cad = next(r for r in rows if r["listing_currency"] == "CAD")
    usd = next(r for r in rows if r["listing_currency"] == "USD")
    assert cad["total_market_value_native"] == Decimal("1500.00")
    assert cad["holdings_count"] == 1
    assert usd["total_market_value_native"] == Decimal("450.00")
    assert usd["holdings_count"] == 2


@pytest.mark.parametrize("held_count", [1, 10, 50])
def test_m184_positions_query_count_is_batched_by_security_count(
    client: TestClient, session: Session, held_count: int
) -> None:
    """Latest close/history/FX reads stay constant as holdings scale 1→10→50."""
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _account(session, user_id)
    for idx in range(held_count):
        sec = _security(session, user_id, f"BATCH{held_count}_{idx}", "USD")
        _buy(session, user_id, account, sec, "2", "10.005")
    _fx(session, "USD", "CAD", "1.30", _today())

    engine = session.get_bind()
    statements: list[str] = []

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        rows = compute_positions(session, user_id, today=_today())
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert len(rows) == held_count
    assert all(row["quantity"] == Decimal("2") for row in rows)
    assert all(row["market_value_native"] == Decimal("20.01") for row in rows)
    assert len(statements) <= 5


def test_m184_repeated_security_across_accounts_does_not_repeat_fx_or_price_reads(
    client: TestClient, session: Session
) -> None:
    """Ten account/security rows share one price and one repeated USD/CAD pair."""
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    security = _security(session, user_id, "SHAREDUSD", "USD")
    today = _today()
    accounts = [
        Account(
            user_id=user_id,
            name=f"Brokerage {index}",
            type=AccountType.brokerage_non_registered,
            currency="CAD",
            taxable=True,
        )
        for index in range(10)
    ]
    session.add_all(accounts)
    session.flush()
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=today,
            price=Decimal("10.005"),
            source=PriceSource.manual,
        )
    )
    session.add_all(
        [
            Trade(
                user_id=user_id,
                account_id=account.id,
                security_id=security.id,
                action=TradeAction.buy,
                date=today,
                quantity=Decimal("2"),
                price_native=Decimal("10.005"),
                commission=Decimal("0"),
                currency="USD",
            )
            for account in accounts
        ]
    )
    session.add(
        FxRate(base="USD", quote="CAD", date=today, rate=Decimal("1.30"), source=FxSource.manual)
    )
    session.commit()

    statements: list[str] = []
    engine = session.get_bind()

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        rows = compute_positions(session, user_id, today=today)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert len(rows) == 10
    assert {row["symbol"] for row in rows} == {"SHAREDUSD"}
    assert all(row["market_value_native"] == Decimal("20.01") for row in rows)
    assert all(row["market_value_cad"] == Decimal("26.01") for row in rows)
    assert len(statements) <= 5


def test_m184_registered_trade_date_fx_is_preloaded_for_repeated_account_security_pairs(
    client: TestClient, session: Session
) -> None:
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    today = _today()
    security = _security(session, user_id, "SHARED_REGISTERED", "USD")
    accounts = [
        Account(
            user_id=user_id,
            name=f"RRSP {index}",
            type=AccountType.rrsp,
            currency="CAD",
            taxable=False,
        )
        for index in range(50)
    ]
    session.add_all(accounts)
    session.flush()
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=today,
            price=Decimal("10.005"),
            source=PriceSource.manual,
        )
    )
    session.add_all(
        [
            Trade(
                user_id=user_id,
                account_id=account.id,
                security_id=security.id,
                action=TradeAction.buy,
                date=today,
                quantity=Decimal("2"),
                price_native=Decimal("10.005"),
                commission=Decimal("0"),
                currency="USD",
            )
            for account in accounts
        ]
    )
    _fx(session, "USD", "CAD", "1.30", today)

    statements: list[str] = []
    engine = session.get_bind()

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        rows = compute_positions(session, user_id, today=today)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert len(rows) == 50
    assert all(row["market_value_cad"] == Decimal("26.01") for row in rows)
    assert all(row["book_cost_cad"] == Decimal("26.01") for row in rows)
    assert len(statements) <= 7


def test_m184_price_history_read_is_bounded_and_keeps_spark_and_previous_close(
    client: TestClient, session: Session
) -> None:
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    account = _account(session, user_id)
    security = _security(session, user_id, "BOUNDED", "CAD")
    today = _today()
    _buy(session, user_id, account, security, "1", "50.00")
    session.add_all(
        [
            Price(
                user_id=user_id,
                security_id=security.id,
                date=today - timedelta(days=days_ago),
                price=Decimal("40") + Decimal(days_ago) / Decimal("100"),
                source=PriceSource.manual,
            )
            for days_ago in range(1, 41)
        ]
    )
    session.commit()

    row = compute_positions(session, user_id, today=today)[0]

    assert len(row["spark"]) == 30
    assert row["spark"][-1] == "50.00000000"
    assert Decimal(row["previous_close_native"]) == Decimal("40.01")

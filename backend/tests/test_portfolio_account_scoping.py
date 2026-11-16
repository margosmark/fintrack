"""Per-account portfolio scoping (investment-account-detail-plan).

Every portfolio endpoint accepts an OPTIONAL ``account_id`` query param that
restricts results to a single account. Callers that omit it keep today's
all-account behavior. Tenancy holds: another user's ``account_id`` yields
zero rows. These tests pin the new contract end-to-end through the API.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models.accounts import Account, AccountType
from app.models.dividend_history import DividendHistory
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import Trade, TradeAction

PORTFOLIO_URL = "/api/v1/portfolio"
TRADES_URL = "/api/v1/trades"


def _today() -> date:
    return datetime.now(UTC).date()


def _auth(client: TestClient) -> dict[str, str]:
    email = f"{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "password123"},
    )
    body = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "password123"},
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _account(session: Session, user_id: int, currency: str, type_: AccountType) -> Account:
    account = Account(
        user_id=user_id,
        name=f"{type_.value} {currency}",
        type=type_,
        currency=currency,
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _security(
    session: Session, user_id: int, symbol: str, listing_currency: str = "CAD"
) -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind="stock",
        listing_currency=listing_currency,
        exchange="TSX" if listing_currency == "CAD" else "NMS",
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
            commission=Decimal("0"),
        )
    )
    session.commit()


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
    session.commit()


def _seed_two_accounts(client: TestClient, session: Session):
    """One user, two distinct CAD investment accounts + one empty bank account."""
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    today = _today()

    acct_a = _account(session, user_id, "CAD", AccountType.brokerage_non_registered)
    acct_b = _account(session, user_id, "CAD", AccountType.tfsa)
    bank = _account(session, user_id, "CAD", AccountType.chequing)

    sec_a = _security(session, user_id, "AAA", listing_currency="CAD")
    sec_b = _security(session, user_id, "BBB", listing_currency="CAD")

    # Buys predate the 2024-03-01 dividend so point-in-time holding-window
    # logic suggests them; the test verifies per-account scoping, not dates.
    _trade(session, user_id, acct_a, sec_a, TradeAction.buy, "10", "50.00", day=date(2024, 1, 15))
    _trade(session, user_id, acct_b, sec_b, TradeAction.buy, "5", "20.00", day=date(2024, 1, 15))
    _price(session, user_id, sec_a, today, "50.00")
    _price(session, user_id, sec_b, today, "20.00")
    session.commit()
    return headers, acct_a, acct_b, bank


def test_positions_scoped_to_single_account(client: TestClient, session: Session) -> None:
    headers, acct_a, acct_b, bank = _seed_two_accounts(client, session)

    # Account A sees only its own position.
    resp_a = client.get(
        f"{PORTFOLIO_URL}/positions", params={"account_id": acct_a.id}, headers=headers
    )
    assert resp_a.status_code == 200
    body_a = resp_a.json()
    assert body_a["total"] == 1
    assert {p["account_id"] for p in body_a["items"]} == {acct_a.id}
    assert body_a["items"][0]["symbol"] == "AAA"

    # Account B sees only its own position.
    resp_b = client.get(
        f"{PORTFOLIO_URL}/positions", params={"account_id": acct_b.id}, headers=headers
    )
    assert resp_b.status_code == 200
    body_b = resp_b.json()
    assert body_b["total"] == 1
    assert {p["account_id"] for p in body_b["items"]} == {acct_b.id}

    # An account with no trades returns an empty page.
    resp_empty = client.get(
        f"{PORTFOLIO_URL}/positions", params={"account_id": bank.id}, headers=headers
    )
    assert resp_empty.status_code == 200
    body_empty = resp_empty.json()
    assert body_empty["total"] == 0
    assert body_empty["items"] == []


def test_exposure_scoped_to_single_account(client: TestClient, session: Session) -> None:
    headers, acct_a, acct_b, _ = _seed_two_accounts(client, session)

    resp_a = client.get(
        f"{PORTFOLIO_URL}/exposure", params={"account_id": acct_a.id}, headers=headers
    )
    assert resp_a.status_code == 200
    body_a = resp_a.json()
    # CAD-listed, single currency bucket, total native value = 10 * 50 = 500.
    assert body_a["total"] == 1
    assert Decimal(str(body_a["items"][0]["total_market_value_native"])) == Decimal("500.00")

    resp_b = client.get(
        f"{PORTFOLIO_URL}/exposure", params={"account_id": acct_b.id}, headers=headers
    )
    assert resp_b.status_code == 200
    body_b = resp_b.json()
    assert body_b["total"] == 1
    assert Decimal(str(body_b["items"][0]["total_market_value_native"])) == Decimal("100.00")


def test_breakdown_scoped_to_single_account(client: TestClient, session: Session) -> None:
    headers, acct_a, acct_b, _ = _seed_two_accounts(client, session)

    resp_a = client.get(
        f"{PORTFOLIO_URL}/breakdown",
        params={"dimension": "sector", "account_id": acct_a.id},
        headers=headers,
    )
    assert resp_a.status_code == 200
    body_a = resp_a.json()
    assert body_a["dimension"] == "sector"
    # A alone: total_cad == 500 (CAD-listed, no fx).
    assert Decimal(str(body_a["total_cad"])) == Decimal("500.00")

    resp_b = client.get(
        f"{PORTFOLIO_URL}/breakdown",
        params={"dimension": "sector", "account_id": acct_b.id},
        headers=headers,
    )
    assert resp_b.status_code == 200
    body_b = resp_b.json()
    assert Decimal(str(body_b["total_cad"])) == Decimal("100.00")


def test_dividend_suggestions_scoped_to_single_account(
    client: TestClient, session: Session
) -> None:
    headers, acct_a, acct_b, _ = _seed_two_accounts(client, session)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    for symbol in ("AAA", "BBB"):
        security = session.exec(
            select(Security).where(Security.user_id == user_id, Security.symbol == symbol)
        ).one()
        session.add(
            DividendHistory(
                user_id=user_id,
                security_id=security.id,
                ex_date=date(2024, 3, 1),
                amount=Decimal("1.25"),
                source="yfinance",
            )
        )
    session.commit()

    resp_a = client.get(
        f"{TRADES_URL}/dividend-suggestions",
        params={"account_id": acct_a.id},
        headers=headers,
    )
    assert resp_a.status_code == 200
    items_a = resp_a.json()
    assert len(items_a) == 1
    assert items_a[0]["account_id"] == acct_a.id
    assert items_a[0]["symbol"] == "AAA"

    resp_b = client.get(
        f"{TRADES_URL}/dividend-suggestions",
        params={"account_id": acct_b.id},
        headers=headers,
    )
    assert resp_b.status_code == 200
    items_b = resp_b.json()
    assert len(items_b) == 1
    assert items_b[0]["account_id"] == acct_b.id
    assert items_b[0]["symbol"] == "BBB"

    # No account filter still returns both (backward compatible).
    resp_all = client.get(f"{TRADES_URL}/dividend-suggestions", headers=headers)
    assert resp_all.status_code == 200
    assert len(resp_all.json()) == 2

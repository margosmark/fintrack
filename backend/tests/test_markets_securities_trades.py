"""securities + trades contract (w2-contract §BackendMarkets 2, docs/05, docs/02).

Network edge (yfinance lookup) is monkeypatched — no external HTTP in CI.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

import app.services.market_data as market_data
from app.models.accounts import Account, AccountType
from app.models.dividend_history import (
    DividendHistory,
    DividendHistoryRefresh,
    DividendHistoryState,
)
from app.models.securities import Security
from app.models.users import User

SEC_URL = "/api/v1/securities"
TRADES_URL = "/api/v1/trades"


def _auth(client: TestClient) -> dict[str, str]:
    email = f"{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _account(session: Session, user: User, currency: str = "CAD") -> Account:
    account = Account(
        user_id=user.id,
        name=f"Brokerage {currency}",
        type=AccountType.brokerage_non_registered,
        currency=currency,
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def test_lookup_success_shape(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        market_data,
        "resolve_symbol",
        lambda symbol: {
            "match": "direct",
            "result": {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "kind": "stock",
                "listing_currency": "USD",
                "exchange": "NMS",
                "exchange_display": None,
            },
            "candidates": [],
        },
    )
    response = client.get(f"{SEC_URL}/lookup", params={"symbol": "aapl"}, headers=_auth(client))
    assert response.status_code == 200
    assert response.json() == {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "kind": "stock",
        "listing_currency": "USD",
        "exchange": "NMS",
        "exchange_display": None,
        "match": "direct",
        "candidates": [],
    }


def test_lookup_failure_degrades_to_404_lookup_unavailable(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        market_data,
        "resolve_symbol",
        lambda symbol: {"match": "none", "result": None, "candidates": []},
    )
    response = client.get(f"{SEC_URL}/lookup", params={"symbol": "NOPE"}, headers=_auth(client))
    assert response.status_code == 404
    assert response.json()["code"] == "lookup_unavailable"


def test_lookup_symbol_unit_maps_kind_and_currency(monkeypatch) -> None:
    class FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def info(self) -> dict:
            if self.symbol == "RY.TO":
                return {"quoteType": "ETF", "shortName": "RBC Etf", "exchange": "TSX"}
            if self.symbol == "BROKEN":
                raise RuntimeError("yfinance died")
            return {}  # empty payload degrades like a failure

    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
    # M142: the search fallback only runs when BOTH candidates miss, so a unit
    # test over the quote probes must pin it too (no external HTTP in CI).
    monkeypatch.setattr(market_data, "_search_equity_quotes", lambda query: [])

    ry = market_data.lookup_symbol("ry.to")
    assert ry == {
        "symbol": "RY.TO",
        "name": "RBC Etf",
        "kind": "etf",
        "listing_currency": "CAD",  # .TO -> CAD heuristic (w2-contract §2)
        "exchange": "TSX",
        "exchange_display": None,
    }
    assert market_data.lookup_symbol("BROKEN") is None
    assert market_data.lookup_symbol("UNKNOWN") is None


def test_security_crud_pagination_and_duplicate(client: TestClient, session: Session) -> None:
    headers = _auth(client)
    created = client.post(
        SEC_URL,
        json={
            "symbol": "aapl",
            "name": "Apple Inc.",
            "kind": "stock",
            "listing_currency": "usd",
            "exchange": "NMS",
        },
        headers=headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["symbol"] == "AAPL"  # normalized upper
    assert body["listing_currency"] == "USD"
    security_id = body["id"]

    duplicate = client.post(
        SEC_URL,
        json={
            "symbol": "AAPL",
            "name": "Apple again",
            "kind": "stock",
            "listing_currency": "USD",
            "exchange": "NMS",
        },
        headers=headers,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "duplicate_security"

    # Same symbol on another exchange is a distinct security.
    other_exchange = client.post(
        SEC_URL,
        json={
            "symbol": "AAPL",
            "name": "Apple NEO",
            "kind": "etf",
            "listing_currency": "CAD",
            "exchange": "NEO",
        },
        headers=headers,
    )
    assert other_exchange.status_code == 201

    listed = client.get(SEC_URL, params={"limit": 1, "offset": 0}, headers=headers)
    assert listed.status_code == 200
    page = listed.json()
    assert set(page) == {"items", "total", "limit", "offset"}
    assert page["total"] == 2 and len(page["items"]) == 1 and page["limit"] == 1

    patched = client.patch(
        f"{SEC_URL}/{security_id}", json={"name": "Apple Inc. (updated)"}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Apple Inc. (updated)"

    fetched = client.get(f"{SEC_URL}/{security_id}", headers=headers)
    assert fetched.status_code == 200

    deleted = client.delete(f"{SEC_URL}/{security_id}", headers=headers)
    assert deleted.status_code == 204
    assert client.get(f"{SEC_URL}/{security_id}", headers=headers).status_code == 404


def test_delete_security_in_use_conflicts(client: TestClient, session: Session) -> None:
    headers = _auth(client)
    me = client.get("/api/v1/users/me", headers=headers).json()
    user = session.get(User, me["id"])
    account = _account(session, user)

    security = Security(
        user_id=user.id,
        symbol="XIU",
        name="iShares S&P/TSX 60",
        kind="etf",
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)

    from app.models.trades import Trade, TradeAction

    session.add(
        Trade(
            user_id=user.id,
            account_id=account.id,
            security_id=security.id,
            action=TradeAction.buy,
            date=date.today(),
            quantity=Decimal("10"),
            price_native=Decimal("30"),
        )
    )
    session.commit()

    response = client.delete(f"{SEC_URL}/{security.id}", headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "security_in_use"


def test_trade_signed_quantity_convention(client: TestClient, session: Session) -> None:
    headers = _auth(client)
    me = client.get("/api/v1/users/me", headers=headers).json()
    user = session.get(User, me["id"])
    account = _account(session, user)
    security = Security(
        user_id=user.id,
        symbol="TD",
        name="Toronto-Dominion Bank",
        kind="stock",
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    today = date.today().isoformat()

    def post_trade(action: str, quantity: str, price: str, commission: str = "0") -> dict:
        response = client.post(
            TRADES_URL,
            json={
                "account_id": account.id,
                "security_id": security.id,
                "action": action,
                "date": today,
                "quantity": quantity,
                "price_native": price,
                "commission": commission,
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        return response.json()

    # buy stores POSITIVE even when the client sends a signed negative.
    buy = post_trade("buy", "-5", "80.00", commission="4.95")
    assert Decimal(str(buy["quantity"])) == Decimal("5.000000")
    assert Decimal(str(buy["commission"])) == Decimal("4.95")

    # sell stores NEGATIVE even when the client sends a positive.
    sell = post_trade("sell", "5", "82.00")
    assert Decimal(str(sell["quantity"])) == Decimal("-5.000000")

    # drip stores POSITIVE; cash-only actions carry quantity=0 per docs/02.
    drip = post_trade("drip", "0.25", "81.00")
    assert Decimal(str(drip["quantity"])) > 0

    dividend = post_trade("dividend", "3", "12.50", commission="1.00")
    assert Decimal(str(dividend["quantity"])) == Decimal("0")
    assert Decimal(str(dividend["commission"])) == Decimal("0")  # commission only on buy/sell/drip

    # corporate_action_adjustment accepted only in docs/06 forms:
    # quantity>0 = resulting share count; quantity=0 = signed CAD ACB delta.
    # Negative quantities are rejected 422 invalid_corporate_action by the
    # M5 pool rebuild that runs inside the trades write path.
    corp = client.post(
        TRADES_URL,
        json={
            "account_id": account.id,
            "security_id": security.id,
            "action": "corporate_action_adjustment",
            "date": today,
            "quantity": "5",
            "price_native": "0",
        },
        headers=headers,
    )
    assert corp.status_code == 201
    assert Decimal(str(corp.json()["quantity"])) == Decimal("5.000000")


def test_patch_renormalizes_action_with_legal_pool(client: TestClient, session: Session) -> None:
    """Flipping a buy to a sell re-normalizes the sign AND must keep the
    replayed pool legal — the docs/02 rebuild runs inside the write path."""
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    owner = session.get(User, user_id)
    account = _account(session, owner)
    security = Security(
        user_id=owner.id,
        symbol="BMO",
        name="BMO",
        kind="stock",
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    today = date.today().isoformat()
    client.post(
        TRADES_URL,
        json={
            "account_id": account.id,
            "security_id": security.id,
            "action": "buy",
            "date": today,
            "quantity": "10",
            "price_native": "50.00",
            "commission": "0",
        },
        headers=headers,
    ).json()
    # A second lot; flipping ROW2 to a sell replays legally
    # (buy 10 first, then sell 10 — pool never goes intermediate-negative).
    buy2 = client.post(
        TRADES_URL,
        json={
            "account_id": account.id,
            "security_id": security.id,
            "action": "buy",
            "date": today,
            "quantity": "10",
            "price_native": "52.00",
            "commission": "0",
        },
        headers=headers,
    ).json()
    flipped = client.patch(
        f"{TRADES_URL}/{buy2['id']}",
        json={"action": "sell", "quantity": "-10"},
        headers=headers,
    )
    assert flipped.status_code == 200
    assert Decimal(str(flipped.json()["quantity"])) == Decimal("-10.000000")


def test_trade_rejects_foreign_account_and_security(client: TestClient, session: Session) -> None:
    attacker_headers = _auth(client)
    owner_headers = _auth(client)

    owner_email_body = client.get("/api/v1/users/me", headers=owner_headers).json()
    owner = session.get(User, owner_email_body["id"])
    account = _account(session, owner)
    security = Security(
        user_id=owner.id,
        symbol="ENB",
        name="Enbridge",
        kind="stock",
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)

    foreign_account = client.post(
        TRADES_URL,
        json={
            "account_id": account.id,
            "security_id": security.id,
            "action": "buy",
            "date": date.today().isoformat(),
            "quantity": "1",
            "price_native": "50",
        },
        headers=attacker_headers,
    )
    assert foreign_account.status_code == 404
    assert foreign_account.json()["code"] == "not_found"

    # A trade created by the owner is invisible to the other tenant.
    mine = client.post(
        TRADES_URL,
        json={
            "account_id": account.id,
            "security_id": security.id,
            "action": "buy",
            "date": date.today().isoformat(),
            "quantity": "1",
            "price_native": "50",
        },
        headers=owner_headers,
    )
    assert mine.status_code == 201
    assert (
        client.get(f"{TRADES_URL}/{mine.json()['id']}", headers=attacker_headers).status_code == 404
    )

    # Foreign security id also rejected for the attacker's own account.
    attackers_account = _account(
        session,
        session.get(User, client.get("/api/v1/users/me", headers=attacker_headers).json()["id"]),
    )
    foreign_security = client.post(
        TRADES_URL,
        json={
            "account_id": attackers_account.id,
            "security_id": security.id,
            "action": "buy",
            "date": date.today().isoformat(),
            "quantity": "1",
            "price_native": "50",
        },
        headers=attacker_headers,
    )
    assert foreign_security.status_code == 404


# --- Dividend suggestions + recording -----------------------------------------


def test_dividend_suggestions_returns_pending(client: TestClient, session: Session) -> None:
    """Suggestions include unrecorded dividends and exclude already-recorded ones."""
    headers = _auth(client)
    me = client.get("/api/v1/users/me", headers=headers).json()
    user = session.get(User, me["id"])
    account = _account(session, user)

    # Create a security with a buy trade (establishes a position)
    sec_resp = client.post(
        SEC_URL,
        json={
            "symbol": "RY",
            "name": "Royal Bank",
            "kind": "stock",
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
        headers=headers,
    )
    assert sec_resp.status_code == 201
    security_id = sec_resp.json()["id"]

    buy_resp = client.post(
        TRADES_URL,
        json={
            "account_id": account.id,
            "security_id": security_id,
            "action": "buy",
            "date": "2024-01-15",
            "quantity": "100",
            "price_native": "130.00",
        },
        headers=headers,
    )
    assert buy_resp.status_code == 201

    _seed_dividend_history(
        session,
        security_id,
        [
            {"date": "2024-03-01", "amount": "1.38"},
            {"date": "2024-06-01", "amount": "1.38"},
        ],
    )

    suggestions = client.get(f"{TRADES_URL}/dividend-suggestions", headers=headers)
    assert suggestions.status_code == 200
    assert '"pending":1' in suggestions.headers["X-Dividend-History-State"]
    items = suggestions.json()
    assert len(items) == 2
    assert items[0]["symbol"] == "RY"
    assert Decimal(items[0]["amount"]) == Decimal("1.38")
    assert items[0]["account_id"] == account.id
    assert float(items[0]["quantity"]) == 100
    assert float(items[0]["gross_amount"]) == 138

    # Record one dividend, then suggestions should only return the other
    record_resp = client.post(
        f"{TRADES_URL}/record-dividend",
        json={
            "account_id": account.id,
            "security_id": security_id,
            "date": "2024-03-01",
            "gross_amount": "138.00",
        },
        headers=headers,
    )
    assert record_resp.status_code == 201

    suggestions2 = client.get(f"{TRADES_URL}/dividend-suggestions", headers=headers)
    assert suggestions2.status_code == 200
    items2 = suggestions2.json()
    assert len(items2) == 1
    assert items2[0]["date"] == "2024-06-01"


def test_record_dividend_creates_trade_and_transaction(
    client: TestClient, session: Session
) -> None:
    """Recording a dividend creates a trade (quantity=0) and a ledger transaction."""
    headers = _auth(client)
    me = client.get("/api/v1/users/me", headers=headers).json()
    user = session.get(User, me["id"])
    account = _account(session, user)

    sec_resp = client.post(
        SEC_URL,
        json={
            "symbol": "VTI",
            "name": "Vanguard Total Stock",
            "kind": "etf",
            "listing_currency": "USD",
            "exchange": "NMS",
        },
        headers=headers,
    )
    assert sec_resp.status_code == 201
    security_id = sec_resp.json()["id"]

    resp = client.post(
        f"{TRADES_URL}/record-dividend",
        json={
            "account_id": account.id,
            "security_id": security_id,
            "date": "2024-06-15",
            "gross_amount": "50.00",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "trade_id" in body
    assert body["drip_trade_id"] is None
    assert "transaction_id" in body

    # Verify the trade
    trade = client.get(f"{TRADES_URL}/{body['trade_id']}", headers=headers).json()
    assert trade["action"] == "dividend"
    assert float(trade["quantity"]) == 0
    assert float(trade["price_native"]) == 50
    assert trade["date"] == "2024-06-15"

    # Verify the transaction exists in the ledger
    txns = client.get("/api/v1/transactions", params={"account_id": account.id}, headers=headers)
    assert txns.status_code == 200
    items = txns.json()["items"]
    assert any(t["id"] == body["transaction_id"] for t in items)


def test_record_dividend_with_drip_creates_two_trades(client: TestClient, session: Session) -> None:
    """DRIP=True creates a dividend trade plus a fractional buy trade."""
    headers = _auth(client)
    me = client.get("/api/v1/users/me", headers=headers).json()
    user = session.get(User, me["id"])
    account = _account(session, user)

    sec_resp = client.post(
        SEC_URL,
        json={
            "symbol": "DRIP",
            "name": "DRIP Test",
            "kind": "stock",
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
        headers=headers,
    )
    assert sec_resp.status_code == 201
    security_id = sec_resp.json()["id"]

    resp = client.post(
        f"{TRADES_URL}/record-dividend",
        json={
            "account_id": account.id,
            "security_id": security_id,
            "date": "2024-09-01",
            "gross_amount": "100.00",
            "drip": True,
            "drip_price": "25.00",
            "drip_commission": "0",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["drip_trade_id"] is not None

    # Verify dividend trade
    div_trade = client.get(f"{TRADES_URL}/{body['trade_id']}", headers=headers).json()
    assert div_trade["action"] == "dividend"
    assert float(div_trade["quantity"]) == 0
    assert float(div_trade["price_native"]) == 100

    # Verify DRIP buy trade: quantity = 100 / 25 = 4 shares
    drip_trade = client.get(f"{TRADES_URL}/{body['drip_trade_id']}", headers=headers).json()
    assert drip_trade["action"] == "drip"
    assert float(drip_trade["quantity"]) == 4.0
    assert float(drip_trade["price_native"]) == 25


def test_record_dividend_drip_whole_shares(client: TestClient, session: Session) -> None:
    """DRIP=True + fractional=False truncates to whole shares."""
    headers = _auth(client)
    me = client.get("/api/v1/users/me", headers=headers).json()
    user = session.get(User, me["id"])
    account = _account(session, user)

    sec_resp = client.post(
        SEC_URL,
        json={
            "symbol": "WSHP",
            "name": "Whole Share Test",
            "kind": "stock",
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
        headers=headers,
    )
    assert sec_resp.status_code == 201
    security_id = sec_resp.json()["id"]

    # gross=100, price=30 → fractional would give 3.333 shares, whole gives 3
    resp = client.post(
        f"{TRADES_URL}/record-dividend",
        json={
            "account_id": account.id,
            "security_id": security_id,
            "date": "2024-12-01",
            "gross_amount": "100.00",
            "drip": True,
            "drip_price": "30.00",
            "drip_commission": "0",
            "fractional": False,
        },
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["drip_trade_id"] is not None

    drip_trade = client.get(f"{TRADES_URL}/{body['drip_trade_id']}", headers=headers).json()
    assert drip_trade["action"] == "drip"
    assert float(drip_trade["quantity"]) == 3  # truncated from 3.333
    assert float(drip_trade["price_native"]) == 30


def test_record_dividend_validates_investment_account(client: TestClient, session: Session) -> None:
    """Non-investment account type returns 422."""
    headers = _auth(client)
    me = client.get("/api/v1/users/me", headers=headers).json()
    user = session.get(User, me["id"])

    from app.models.accounts import AccountType

    chequing = Account(user_id=user.id, name="Chequing", type=AccountType.chequing, currency="CAD")
    session.add(chequing)
    session.commit()
    session.refresh(chequing)

    sec_resp = client.post(
        SEC_URL,
        json={
            "symbol": "TEST",
            "name": "Test",
            "kind": "stock",
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
        headers=headers,
    )
    assert sec_resp.status_code == 201
    security_id = sec_resp.json()["id"]

    resp = client.post(
        f"{TRADES_URL}/record-dividend",
        json={
            "account_id": chequing.id,
            "security_id": security_id,
            "date": "2024-01-01",
            "gross_amount": "10.00",
        },
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "account_not_investment"


def test_record_dividend_validates_drip_price(client: TestClient, session: Session) -> None:
    """DRIP=True without drip_price returns 422."""
    headers = _auth(client)
    me = client.get("/api/v1/users/me", headers=headers).json()
    user = session.get(User, me["id"])
    account = _account(session, user)

    sec_resp = client.post(
        SEC_URL,
        json={
            "symbol": "DPRC",
            "name": "DRIP Price Test",
            "kind": "stock",
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
        headers=headers,
    )
    assert sec_resp.status_code == 201
    security_id = sec_resp.json()["id"]

    # drip=True but no drip_price
    resp = client.post(
        f"{TRADES_URL}/record-dividend",
        json={
            "account_id": account.id,
            "security_id": security_id,
            "date": "2024-01-01",
            "gross_amount": "10.00",
            "drip": True,
        },
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_drip_price"


def _seed_holding(
    client: TestClient, session: Session, symbol: str, currency: str = "CAD"
) -> tuple[dict[str, str], int, int]:
    """Create a fresh user, investment account, and security; return auth headers."""
    headers = _auth(client)
    me = client.get("/api/v1/users/me", headers=headers).json()
    user = session.get(User, me["id"])
    account = _account(session, user, currency=currency)
    sec_resp = client.post(
        SEC_URL,
        json={
            "symbol": symbol,
            "name": f"{symbol} Fund",
            "kind": "etf",
            "listing_currency": currency,
            "exchange": "TSX",
        },
        headers=headers,
    )
    assert sec_resp.status_code == 201
    return headers, account.id, sec_resp.json()["id"]


def _post_holding_trade(
    client: TestClient,
    headers: dict[str, str],
    account_id: int,
    security_id: int,
    action: str,
    qty: str,
    dt: str,
) -> None:
    resp = client.post(
        TRADES_URL,
        json={
            "account_id": account_id,
            "security_id": security_id,
            "action": action,
            "date": dt,
            "quantity": qty,
            "price_native": "1.00",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text


def _seed_dividend_history(
    session: Session, security_id: int, entries: list[dict[str, str]]
) -> None:
    security = session.get(Security, security_id)
    assert security is not None
    for provider_order, entry in enumerate(entries):
        session.add(
            DividendHistory(
                user_id=security.user_id,
                security_id=security_id,
                ex_date=date.fromisoformat(entry["date"]),
                amount=Decimal(entry["amount"]),
                provider_order=provider_order,
                source="yfinance",
            )
        )
    session.commit()


def test_dividend_suggestions_prebuy_excl(client: TestClient, session: Session) -> None:
    """Dividends dated before the user's first buy are not suggested (holding window)."""
    headers, account_id, security_id = _seed_holding(client, session, "VCN")
    _post_holding_trade(client, headers, account_id, security_id, "buy", "10", "2023-01-01")
    _seed_dividend_history(
        session,
        security_id,
        [
            {"date": "2022-06-01", "amount": "1.00"},
            {"date": "2023-06-01", "amount": "1.00"},
            {"date": "2024-06-01", "amount": "1.00"},
        ],
    )

    items = client.get(f"{TRADES_URL}/dividend-suggestions", headers=headers).json()
    assert len(items) == 2
    by_date = {it["date"]: it for it in items}
    assert "2022-06-01" not in by_date
    assert by_date["2023-06-01"]["quantity"] == "10.00"
    assert by_date["2023-06-01"]["gross_amount"] == "10.00"
    assert by_date["2024-06-01"]["quantity"] == "10.00"
    assert by_date["2024-06-01"]["gross_amount"] == "10.00"


def test_dividend_suggestions_holding_qty(client: TestClient, session: Session) -> None:
    """Suggested quantity reflects shares held on the ex-date, not the current total."""
    headers, account_id, security_id = _seed_holding(client, session, "VCN")
    _post_holding_trade(client, headers, account_id, security_id, "buy", "10", "2023-01-01")
    _post_holding_trade(client, headers, account_id, security_id, "sell", "5", "2024-01-01")
    _seed_dividend_history(
        session,
        security_id,
        [
            {"date": "2022-06-01", "amount": "1.00"},
            {"date": "2023-06-01", "amount": "1.00"},
            {"date": "2024-06-01", "amount": "1.00"},
        ],
    )

    items = client.get(f"{TRADES_URL}/dividend-suggestions", headers=headers).json()
    assert len(items) == 2
    by_date = {it["date"]: it for it in items}
    assert by_date["2023-06-01"]["quantity"] == "10.00"  # held 10 before the sell
    assert by_date["2023-06-01"]["gross_amount"] == "10.00"
    assert by_date["2024-06-01"]["quantity"] == "5.00"  # held 5 after the sell
    assert by_date["2024-06-01"]["gross_amount"] == "5.00"


def test_dividend_suggestions_include_all_trades_on_the_dividend_date(
    client: TestClient, session: Session
) -> None:
    headers, account_id, security_id = _seed_holding(client, session, "VCN")
    _post_holding_trade(client, headers, account_id, security_id, "buy", "10", "2024-06-01")
    _post_holding_trade(client, headers, account_id, security_id, "sell", "3", "2024-06-01")
    _seed_dividend_history(
        session,
        security_id,
        [{"date": "2024-06-01", "amount": "0.25"}],
    )

    response = client.get(f"{TRADES_URL}/dividend-suggestions", headers=headers)

    assert response.status_code == 200
    assert response.json() == [
        {
            "security_id": security_id,
            "account_id": account_id,
            "symbol": "VCN",
            "date": "2024-06-01",
            "amount": "0.25",
            "quantity": "7.00",
            "gross_amount": "1.75",
            "currency": "CAD",
        }
    ]


def test_dividend_suggestions_closed_none(client: TestClient, session: Session) -> None:
    """A fully-closed position yields no suggestions even if a dividend falls in-range."""
    headers, account_id, security_id = _seed_holding(client, session, "VCN")
    _post_holding_trade(client, headers, account_id, security_id, "buy", "10", "2023-01-01")
    _post_holding_trade(client, headers, account_id, security_id, "sell", "10", "2023-12-01")
    _seed_dividend_history(session, security_id, [{"date": "2023-06-01", "amount": "1.00"}])

    items = client.get(f"{TRADES_URL}/dividend-suggestions", headers=headers).json()
    assert items == []


def test_dividend_suggestions_perdate_amt(client: TestClient, session: Session) -> None:
    """Each date uses its own per-share amount (regression of the old first-entry capture)."""
    headers, account_id, security_id = _seed_holding(client, session, "VCN")
    _post_holding_trade(client, headers, account_id, security_id, "buy", "10", "2023-01-01")
    _seed_dividend_history(
        session,
        security_id,
        [{"date": "2023-06-01", "amount": "1.00"}, {"date": "2024-06-01", "amount": "2.00"}],
    )

    items = client.get(f"{TRADES_URL}/dividend-suggestions", headers=headers).json()
    assert len(items) == 2
    by_date = {it["date"]: it for it in items}
    assert by_date["2023-06-01"]["gross_amount"] == "10.00"  # 1.00 * 10
    assert by_date["2024-06-01"]["gross_amount"] == "20.00"  # 2.00 * 10


def test_dividend_suggestion_history_preserves_provider_order_and_duplicate_dates(
    client: TestClient, session: Session
) -> None:
    headers, account_id, security_id = _seed_holding(client, session, "VCN")
    _post_holding_trade(client, headers, account_id, security_id, "buy", "10", "2023-01-01")
    _seed_dividend_history(
        session,
        security_id,
        [
            {"date": "2024-06-01", "amount": "2.00"},
            {"date": "2023-06-01", "amount": "1.00"},
            {"date": "2023-06-01", "amount": "0.50"},
        ],
    )

    response = client.get(f"{TRADES_URL}/dividend-suggestions", headers=headers)

    assert response.status_code == 200
    assert [(row["date"], Decimal(row["amount"])) for row in response.json()] == [
        ("2024-06-01", Decimal("2.00")),
        ("2023-06-01", Decimal("1.00")),
        ("2023-06-01", Decimal("0.50")),
    ]


def test_dividend_suggestions_do_not_queue_foreign_security_ids(
    client: TestClient, session: Session
) -> None:
    headers, account_id, _ = _seed_holding(client, session, "OWNED")
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    foreign_user = User(
        email=f"foreign-{datetime.now(UTC).timestamp()}@example.com",
        hashed_password="x",
    )
    session.add(foreign_user)
    session.commit()
    session.refresh(foreign_user)
    foreign_security = Security(
        user_id=foreign_user.id,
        symbol="PRIVATE",
        name="Foreign security",
        kind="stock",
        listing_currency="USD",
        exchange="NMS",
    )
    session.add(foreign_security)
    session.commit()
    session.refresh(foreign_security)
    from app.models.trades import Trade, TradeAction

    session.add(
        Trade(
            user_id=user_id,
            account_id=account_id,
            security_id=foreign_security.id,
            action=TradeAction.buy,
            date=date(2025, 1, 1),
            quantity=Decimal("1"),
            price_native=Decimal("10"),
            commission=Decimal("0"),
            currency="USD",
        )
    )
    session.add(
        DividendHistory(
            user_id=foreign_user.id,
            security_id=foreign_security.id,
            ex_date=date(2025, 2, 1),
            amount=Decimal("1"),
        )
    )
    session.commit()

    response = client.get(f"{TRADES_URL}/dividend-suggestions", headers=headers)

    assert response.status_code == 200
    assert response.json() == []
    state = json.loads(response.headers["X-Dividend-History-State"])
    assert state["pending"] == state["unavailable"] == state["empty"] == state["stale"] == 0
    assert (
        session.exec(
            select(DividendHistoryRefresh).where(
                DividendHistoryRefresh.user_id == user_id,
                DividendHistoryRefresh.security_id == foreign_security.id,
            )
        ).first()
        is None
    )


def test_dividend_suggestions_exposes_successful_empty_history_state(
    client: TestClient, session: Session
) -> None:
    headers, account_id, security_id = _seed_holding(client, session, "VCN")
    _post_holding_trade(client, headers, account_id, security_id, "buy", "10", "2023-01-01")
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    session.add(
        DividendHistoryRefresh(
            user_id=user_id,
            security_id=security_id,
            last_success_at=datetime.now(UTC).replace(tzinfo=None),
            state=DividendHistoryState.empty,
        )
    )
    session.commit()

    response = client.get(f"{TRADES_URL}/dividend-suggestions", headers=headers)

    assert response.status_code == 200
    assert response.json() == []
    state = json.loads(response.headers["X-Dividend-History-State"])
    assert state["pending"] == 0
    assert state["unavailable"] == 0
    assert state["empty"] == 1
    assert state["stale"] == 0
    assert state["as_of"].endswith("Z")

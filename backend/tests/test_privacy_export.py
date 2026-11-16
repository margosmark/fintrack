"""GET /export/full — whole-tenant snapshot (docs/11 step 2, w6-contract pin 3).

Covers w6-contract BackendPrivacy pins 3-4: every domain table present and
caller-scoped, fx_rates unfiltered, refresh_tokens excluded, hashed_password /
llm_api_key stripped, PKs/FKs preserved, Content-Disposition filename pinned,
and zero LLM/market-data dependency (network is rigged to explode).

Key-set expectations are REGISTRY-DRIVEN (audit M13): derived from
`SQLModel.metadata`, never a hand-pinned literal list — a pinned list is what
silently dropped the 14 post-M7 tables (budgets, goals, debts, tags,
etf_holdings, fundamentals, ...). tests/test_export_registry.py pins the
exclusion set itself and proves caller scoping.
"""

import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import SQLModel, select

from app.api.export import EXCLUDED_TABLES
from app.models import (
    Account,
    AccountType,
    FxRate,
    FxSource,
    Institution,
    Price,
    PriceSource,
    RefreshToken,
    Security,
    SecurityKind,
    Trade,
    TradeAction,
    Transaction,
    User,
)

EXPORT_URL = "/api/v1/export/full"

# Every table the ORM maps is in the snapshot except the reviewed exclusion
# list (pinned by tests/test_export_registry.py, which is what keeps THIS
# derivation honest — deriving it from the module avoids a second list that
# would drift whenever the exclusion set grows, as it did for M56).
EXPECTED_KEYS: set[str] = set(SQLModel.metadata.tables) - set(EXCLUDED_TABLES)


def _register(client: TestClient) -> tuple[dict[str, str], str]:
    email = f"{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "secret123"})
    body = client.post("/api/v1/auth/login", json={"email": email, "password": "secret123"})
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}, email


def _forbid_network(monkeypatch: Any) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Export must not touch LLM or market-data services")

    monkeypatch.setattr("app.services.fx.requests.get", explode)
    empty_frame = SimpleNamespace(empty=True)
    fake_yf = SimpleNamespace(
        Ticker=lambda symbol: SimpleNamespace(info={}, history=lambda **kwargs: empty_frame)
    )
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    # M142: the name→ticker search fallback is the market-data module's other
    # network seam (docs/07) — the export must never reach it either.
    monkeypatch.setattr("app.services.market_data._search_equity_quotes", lambda query: [])


def _transaction(
    user_id: int, account_id: int, day: date, *, raw_description: str, dedup_hash: str
) -> Transaction:
    return Transaction(
        user_id=user_id,
        account_id=account_id,
        date=day,
        raw_description=raw_description,
        normalized_payee=raw_description.lower(),
        amount_native=Decimal("-4.50"),
        currency="CAD",
        cad_amount=Decimal("-4.50"),
        dedup_hash=dedup_hash,
    )


def _seed(session: Session, email_a: str, email_b: str) -> dict[str, int]:
    """Two tenants' data + a shared fx rate + a refresh token for the caller."""
    user_a = session.exec(select(User).where(User.email == email_a)).one()
    user_b = session.exec(select(User).where(User.email == email_b)).one()
    today = datetime.now(UTC).date()

    institution_a = Institution(user_id=user_a.id, name="RBC")
    account_a = Account(
        user_id=user_a.id, name="Chequing", type=AccountType.chequing, currency="CAD"
    )
    security_a = Security(
        user_id=user_a.id,
        symbol="XIC",
        name="iShares Core S&P/TSX Capped Composite",
        kind=SecurityKind.etf,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add_all([institution_a, account_a, security_a])
    session.flush()

    transaction_a = _transaction(
        user_a.id, account_a.id, today, raw_description="CAFE MOCHA", dedup_hash="hash-a"
    )
    trade_a = Trade(
        user_id=user_a.id,
        account_id=account_a.id,
        security_id=security_a.id,
        action=TradeAction.buy,
        date=today,
        quantity=Decimal("10"),
        price_native=Decimal("30"),
    )
    price_a = Price(
        user_id=user_a.id,
        security_id=security_a.id,
        date=today,
        price=Decimal("30"),
        source=PriceSource.manual,
    )
    shared_fx = FxRate(
        base="EUR", quote="CAD", rate=Decimal("1.47"), date=today, source=FxSource.boc_valet
    )
    refresh_a = RefreshToken(
        user_id=user_a.id,
        token_hash="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    # Tenant B's rows must never leak into A's snapshot.
    institution_b = Institution(user_id=user_b.id, name="TD")
    account_b = Account(user_id=user_b.id, name="Savings", type=AccountType.savings, currency="CAD")
    session.add_all([institution_b, account_b])
    session.flush()
    transaction_b = _transaction(
        user_b.id, account_b.id, today, raw_description="GROCER", dedup_hash="hash-b"
    )

    session.add_all([transaction_a, trade_a, price_a, shared_fx, refresh_a, transaction_b])
    session.commit()
    return {
        "user_a": user_a.id,
        "institution_a": institution_a.id,
        "account_a": account_a.id,
        "security_a": security_a.id,
        "transaction_a": transaction_a.id,
        "trade_a": trade_a.id,
        "price_a": price_a.id,
        "fx_id": shared_fx.id,
        "transaction_b": transaction_b.id,
        "institution_b": institution_b.id,
    }


def test_export_full_snapshot_shape_scope_and_secrets(
    client: TestClient, session: Session, monkeypatch: Any
) -> None:
    _forbid_network(monkeypatch)
    headers_a, email_a = _register(client)
    _headers_b, email_b = _register(client)
    ids = _seed(session, email_a, email_b)

    response = client.get(EXPORT_URL, headers=headers_a)
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == (
        f'attachment; filename="fin-tracker-export-{datetime.now(UTC).date().isoformat()}.json"'
    )

    snap = response.json()
    assert set(snap) == EXPECTED_KEYS
    assert "refresh_tokens" not in snap

    # Caller's own row only, secrets stripped, preferences kept.
    assert len(snap["users"]) == 1
    me = snap["users"][0]
    assert me["email"] == email_a
    assert me["id"] == ids["user_a"]
    assert "hashed_password" not in me
    assert "llm_api_key" not in me
    assert "privacy_mode" in me and "llm_provider" in me

    # PK/FK preservation on the caller's rows.
    tx = next(row for row in snap["transactions"] if row["id"] == ids["transaction_a"])
    assert tx["account_id"] == ids["account_a"]
    assert tx["user_id"] == ids["user_a"]
    trade = next(row for row in snap["trades"] if row["id"] == ids["trade_a"])
    assert trade["security_id"] == ids["security_a"]
    assert Decimal(str(trade["quantity"])) == Decimal("10")  # Decimal precision survives
    assert [row["id"] for row in snap["securities"]] == [ids["security_a"]]
    assert [row["id"] for row in snap["prices"]] == [ids["price_a"]]
    assert [row["id"] for row in snap["institutions"]] == [ids["institution_a"]]
    assert [row["id"] for row in snap["accounts"]] == [ids["account_a"]]

    # Tenant B's data excluded everywhere.
    assert ids["transaction_b"] not in {row["id"] for row in snap["transactions"]}
    assert ids["institution_b"] not in {row["id"] for row in snap["institutions"]}
    assert all(row["email"] != email_b for row in snap["users"])

    # fx_rates exported UNFILTERED (shared reference cache).
    assert ids["fx_id"] in {row["id"] for row in snap["fx_rates"]}
    # No auth secret anywhere in the payload (refresh-token hashes included).
    assert all("token_hash" not in row for table in snap.values() for row in table)


def test_export_full_works_under_privacy_mode_and_without_external_services(
    client: TestClient, session: Session, monkeypatch: Any
) -> None:
    _forbid_network(monkeypatch)
    headers, email = _register(client)
    _, email_b = _register(client)  # second tenant keeps the scoping seed valid
    _seed(session, email, email_b)
    toggled = client.patch("/api/v1/users/me", json={"privacy_mode": True}, headers=headers)
    assert toggled.status_code == 200, toggled.text

    response = client.get(EXPORT_URL, headers=headers)
    assert response.status_code == 200, response.text
    snap = response.json()
    assert set(snap) == EXPECTED_KEYS
    assert len(snap["users"]) == 1
    assert snap["users"][0]["privacy_mode"] is True


def test_export_requires_auth(client: TestClient) -> None:
    response = client.get(EXPORT_URL)
    assert response.status_code == 401

"""M9 accounts tests — enum widening, derived taxable, cash_balance (docs/14 C1/C2)."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.accounts import Account
from app.models.fx_rates import FxRate, FxSource
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.models.transactions import Transaction
from app.models.users import User
from app.services import fx as fx_service

ALL_TYPES = (
    "chequing",
    "savings",
    "credit_card",
    "line_of_credit",
    "brokerage_non_registered",
    "rrsp",
    "tfsa",
    "fhsa",
    "resp",
    "lira",
    "rdsp",
)


def _register(client: TestClient, email: str) -> dict:
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_account(client: TestClient, token: str, **overrides) -> dict:
    payload = {"name": "Main", "type": "chequing", "currency": "CAD", **overrides}
    response = client.post("/api/v1/accounts", json=payload, headers=_headers(token))
    assert response.status_code == 201
    return response.json()


# --- pin 1: widened enum ----------------------------------------------------


@pytest.mark.parametrize("account_type", ["lira", "rdsp"])
def test_widened_types_accepted_through_post(client: TestClient, account_type: str) -> None:
    auth = _register(client, f"{account_type}@example.com")
    body = _create_account(client, auth["access_token"], name="Plan", type=account_type)
    assert body["type"] == account_type
    assert body["taxable"] is False


def test_type_check_constraint_covers_all_eleven_values(session: Session) -> None:
    row = session.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'accounts'::regclass AND conname = 'ck_accounts_type'"
        )
    ).one()
    definition = row[0]
    for value in ALL_TYPES:
        assert f"'{value}'" in definition


def _user_id(session: Session, email: str) -> int:
    return session.exec(select(User).where(User.email == email)).one().id


def _constraint_is_active(session: Session) -> bool:
    return (
        session.execute(
            text(
                "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_accounts_type' "
                "AND conrelid = 'accounts'::regclass"
            )
        ).scalar_one()
        == 1
    )


def test_unknown_type_rejected_by_database_constraint(session: Session, client: TestClient) -> None:
    _register(client, "constraint@example.com")
    user_id = _user_id(session, "constraint@example.com")
    assert _constraint_is_active(session)
    with pytest.raises(IntegrityError):
        session.add(
            Account(user_id=user_id, name="Bad", type="isa", currency="CAD")  # type: ignore[arg-type]
        )
        session.flush()
    session.rollback()


# --- pin 3: server-derived taxable -----------------------------------------


def test_taxable_derived_on_create(client: TestClient) -> None:
    auth = _register(client, "taxable-create@example.com")
    token = auth["access_token"]
    taxable = _create_account(client, token, type="brokerage_non_registered")
    registered = _create_account(client, token, name="TFSA", type="tfsa")
    assert taxable["taxable"] is True
    assert registered["taxable"] is False


def test_client_sent_taxable_rejected_in_create_and_update(client: TestClient) -> None:
    auth = _register(client, "taxable-inert@example.com")
    token = auth["access_token"]
    rejected = client.post(
        "/api/v1/accounts",
        json={"name": "X", "type": "chequing", "currency": "CAD", "taxable": True},
        headers=_headers(token),
    )
    assert rejected.status_code == 422
    account = _create_account(client, token)
    patched = client.patch(
        f"/api/v1/accounts/{account['id']}",
        json={"taxable": True},
        headers=_headers(token),
    )
    assert patched.status_code == 422


def test_taxable_flips_on_type_change_patch(client: TestClient) -> None:
    auth = _register(client, "taxable-flip@example.com")
    token = auth["access_token"]
    account = _create_account(client, token, name="Chequing", type="chequing")
    assert account["taxable"] is False

    flipped = client.patch(
        f"/api/v1/accounts/{account['id']}",
        json={"type": "brokerage_non_registered"},
        headers=_headers(token),
    )
    assert flipped.status_code == 200
    assert flipped.json()["taxable"] is True

    back = client.patch(
        f"/api/v1/accounts/{account['id']}",
        json={"type": "lira"},
        headers=_headers(token),
    )
    assert back.status_code == 200
    assert back.json()["taxable"] is False


# --- pin 4: cash_balance -----------------------------------------------------


def _add_batch(
    session: Session,
    user_id: int,
    account_id: int,
    status: ImportBatchStatus,
    *amounts: str,
) -> ImportBatch:
    batch = ImportBatch(
        user_id=user_id,
        account_id=account_id,
        institution="Test Bank",
        file_name=f"{status.value}.csv",
        status=status,
    )
    session.add(batch)
    session.flush()
    for index, amount in enumerate(amounts):
        session.add(
            Transaction(
                user_id=user_id,
                account_id=account_id,
                date=date(2026, 8, 1),
                raw_description=f"tx {index}",
                normalized_payee=f"Payee {index}",
                amount_native=Decimal(amount),
                currency="CAD",
                dedup_hash=f"{status.value}-{index}-{amount}",
                import_batch_id=batch.id,
            )
        )
    session.flush()
    return batch


def test_cash_balance_counts_committed_only(session: Session, client: TestClient) -> None:
    auth = _register(client, "cash-committed@example.com")
    token = auth["access_token"]
    account = _create_account(client, token, name="Chequing")
    account_id = account["id"]
    user_id = _user_id(session, "cash-committed@example.com")

    _add_batch(session, user_id, account_id, ImportBatchStatus.committed, "100.00", "-30.25")
    _add_batch(session, user_id, account_id, ImportBatchStatus.pending_review, "999.99")
    _add_batch(session, user_id, account_id, ImportBatchStatus.discarded, "-500.00")

    listed = client.get("/api/v1/accounts", headers=_headers(token)).json()["items"][0]
    single = client.get(f"/api/v1/accounts/{account_id}", headers=_headers(token)).json()
    assert listed["cash_balance"] == "69.75"
    assert single["cash_balance"] == "69.75"


def test_cash_balance_empty_account_is_zero_string(client: TestClient) -> None:
    auth = _register(client, "cash-empty@example.com")
    account = _create_account(client, auth["access_token"])
    assert account["cash_balance"] == "0.00"


def test_cash_balance_is_native_regardless_of_cad_columns(
    session: Session, client: TestClient
) -> None:
    auth = _register(client, "cash-usd@example.com")
    token = auth["access_token"]
    account = _create_account(client, token, name="USD Chequing", currency="USD")
    user_id = _user_id(session, "cash-usd@example.com")

    batch = _add_batch(session, user_id, account["id"], ImportBatchStatus.committed, "10.50")
    session.add(
        Transaction(
            user_id=user_id,
            account_id=account["id"],
            date=date(2026, 8, 2),
            raw_description="usd tx",
            normalized_payee="Payee",
            amount_native=Decimal("20.25"),
            currency="USD",
            cad_amount=Decimal("13.37"),
            dedup_hash="usd-extra",
            import_batch_id=batch.id,
        )
    )
    session.flush()

    body = client.get(f"/api/v1/accounts/{account['id']}", headers=_headers(token)).json()
    # Native USD sum, not the CAD-converted column.
    assert body["cash_balance"] == "30.75"


# --- pin 5: investment_value + total_value (nav total for investment accounts) ---


def _seed_security_price(
    session: Session,
    user_id: int,
    account_id: int,
    symbol: str,
    listing_currency: str,
    qty: str,
    price: str,
    today: date,
) -> None:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind="stock",
        listing_currency=listing_currency,
        exchange="NMS" if listing_currency == "USD" else "TSX",
    )
    session.add(security)
    session.flush()
    session.add(
        Trade(
            user_id=user_id,
            account_id=account_id,
            security_id=security.id,
            action=TradeAction.buy,
            date=today,
            quantity=Decimal(qty),
            price_native=Decimal(price),
            commission=Decimal("0"),
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
    session.flush()


def _contribute_cash(
    session: Session,
    user_id: int,
    account_id: int,
    symbol: str,
    amount: str,
    listing_currency: str = "CAD",
) -> None:
    """Fund an investment account with a CASH-security contribution TRADE.

    M145: an investment account's money lives in TRADES, so a buy draws its
    cash from earlier contribution trades (docs/05 chain) — not from the
    transactions ledger, which an investment account does not use. Seeding the
    contribution keeps `cash_balance` the honest net of money in minus the buy.
    """
    cash = Security(
        user_id=user_id,
        symbol=symbol,
        name="Cash",
        kind="stock",
        listing_currency=listing_currency,
        exchange="TSX" if listing_currency == "CAD" else "ARCA",
    )
    session.add(cash)
    session.flush()
    session.add(
        Trade(
            user_id=user_id,
            account_id=account_id,
            security_id=cash.id,
            action=TradeAction.contribution,
            date=date(2026, 8, 1),
            quantity=Decimal("0"),
            price_native=Decimal(amount),
            commission=Decimal("0"),
        )
    )
    session.flush()


def test_investment_total_cad_account(client: TestClient, session: Session) -> None:
    auth = _register(client, "inv-cad@example.com")
    token = auth["access_token"]
    account = _create_account(
        client, token, name="Brokerage", type="brokerage_non_registered", currency="CAD"
    )
    account_id = account["id"]
    user_id = _user_id(session, "inv-cad@example.com")
    today = date.today()

    # +50 committed cash (native account cash) and a 10-unit CAD-listed
    # holding priced at 100, bought with a 1,000 CASH-security contribution so
    # the buy is exactly funded: the trade chain (1,000 in - 1,000 buy) nets
    # to zero and M145's cash_balance still reports the ledger's 50.00.
    _add_batch(session, user_id, account_id, ImportBatchStatus.committed, "50.00")
    _contribute_cash(session, user_id, account_id, "CASH", "1000.00")
    _seed_security_price(session, user_id, account_id, "RY", "CAD", "10", "100.00", today)
    session.commit()

    listed = client.get("/api/v1/accounts", headers=_headers(token)).json()["items"]
    single = client.get(f"/api/v1/accounts/{account_id}", headers=_headers(token)).json()
    assert listed[0]["cash_balance"] == "50.00"
    assert listed[0]["investment_value"] == "1000.00"
    assert listed[0]["total_value"] == "1050.00"
    assert single["cash_balance"] == "50.00"
    assert single["investment_value"] == "1000.00"
    assert single["total_value"] == "1050.00"


def test_investment_total_fx_converts_to_account_currency(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _forbid_network(*_a, **_k):
        raise AssertionError("network fetch attempted although data is cached")

    monkeypatch.setattr(fx_service, "fetch_valet_series", _forbid_network)

    auth = _register(client, "inv-usd@example.com")
    token = auth["access_token"]
    today = date.today()

    # USD→CAD rate must exist BEFORE account creation: BoC publishes account→CAD
    # (e.g. USD→CAD), never CAD→account. create_account computes totals eagerly
    # and converts the CAD-denominated holdings into the account's native currency
    # by dividing by this rate. 1000 CAD / 1.3333333333 ≈ 750.00 USD.
    session.add(
        FxRate(
            base="USD",
            quote="CAD",
            date=today,
            rate=Decimal("1.3333333333"),
            source=FxSource.manual,
        )
    )
    session.flush()

    account = _create_account(
        client, token, name="USD Brokerage", type="brokerage_non_registered", currency="USD"
    )
    account_id = account["id"]
    user_id = _user_id(session, "inv-usd@example.com")
    # Mirror the CAD sibling: +50 committed cash plus a 1,000 USD CASH-security
    # contribution. The buy row carries no per-row currency, so it reads in the
    # ACCOUNT's currency (USD 1,000 — M132 legacy rule); contribution and buy
    # cancel and the native `cash_balance` still reports the ledger's 50.00 USD.
    _add_batch(session, user_id, account_id, ImportBatchStatus.committed, "50.00")
    _contribute_cash(session, user_id, account_id, "CASHUSD", "1000.00", listing_currency="USD")

    # CAD-listed holding worth 1000 CAD; CAD→USD rate 0.75 → 750.00 USD.
    # CAD-listed holding worth 1000 CAD; USD→CAD rate 1.3333333333 → 750.00 USD.
    _seed_security_price(session, user_id, account_id, "RY", "CAD", "10", "100.00", today)
    session.commit()

    single = client.get(f"/api/v1/accounts/{account_id}", headers=_headers(token)).json()
    assert single["cash_balance"] == "50.00"
    assert single["investment_value"] == "750.00"
    assert single["total_value"] == "800.00"

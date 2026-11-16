"""M145 — the Detail card's `cash_balance` and the value chart read ONE source.

The bug: two components read cash from two different tables.

  * The chart (`services/account_value.py`, M118) reads the CASH-security
    TRADE rows — `_trade_cash_effect` over `trades`, M132's per-row FX.
  * The Detail card (`api/accounts.py::_committed_cash_subquery` → the
    `cash_balance` field) read the TRANSACTIONS table. An investment account
    records its money as TRADES, not transactions, so the card computed
    `$0.00` forever while the chart read the truth.

Real account 459 (anonymized TFSA CAD, verified against the deployed DB): ZERO
transactions, 32 trades, `starting_balance NULL` — the chart reads $3,486.12,
the card read $0.00. Every investment account whose cash lives only as
CASH-security trades showed `$0.00`.

The fix (docs/08 §cash_balance): for `kind=investment` the card reads the
SAME chain the chart draws — `services/account_value.account_cash` — so the
two cannot disagree by construction. Cash accounts keep the ledger subquery
(tested unchanged in `test_accounts_m9.py`). `starting_balance` is inside the
trade chain exactly once; an investment account's contributions ARE its
starting position, so the ledger's `+ sb` must not be applied again.

Every figure below is arithmetic done by hand from the seed, never captured
from a run. Each API-facing test registers its user FIRST and seeds account +
trades under that one user, so `account.user_id` and `trade.user_id` agree
(the chain is scoped to `account.user_id`).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select
from test_tax_acb import add_trade, make_account, make_security, seed_fx

from app.models.accounts import INVESTMENT_ACCOUNT_TYPES, Account, AccountType
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.securities import Security, SecurityKind
from app.models.trades import TradeAction as TA
from app.models.transactions import Transaction
from app.models.users import User
from app.services.account_value import account_cash, account_value_history, accounts_cash

D = Decimal
# The valuation date every read below uses. The CAD-only fixtures need no fx
# rate at all; the one USD leg seeds its rate at 2026-06-14 — inside the M115
# <=7d carry-forward window of this date, so no leg is honestly refused.
TODAY = date(2026, 6, 15)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client: TestClient, email: str) -> str:
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse"})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": "correct-horse"})
    assert response.status_code == 200
    return response.json()["access_token"]


def _api_user(session: Session, email: str) -> User:
    return session.exec(select(User).where(User.email == email)).one()


def _cash_security(session: Session, user: User) -> Security:
    security = Security(
        user_id=user.id,
        symbol="CASH",
        name="Cash",
        kind=SecurityKind.stock,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _seed_459_shape(session: Session, client: TestClient, email: str) -> tuple[str, Account]:
    """The real 459 shape: contributions + buys as CASH-security TRADES, no txns.

    Hand-computed cash (docs/05 chain, all CAD, no fx needed):

        contribution  +40,000.00
        contribution  +16,500.00
        buy           -18,000.00   (100 @ 180.00)
        contribution  +1,582.40
        ─────────────────────────
        cash           40,082.40

    `starting_balance` is NULL (real 459's value), so nothing is double-added.
    """
    token = _register(client, email)
    user = _api_user(session, email)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    account.starting_balance = None
    session.add(account)
    session.commit()
    session.refresh(account)
    cash = _cash_security(session, user)
    equity = make_security(session, user, "RY", "CAD")
    add_trade(session, user, account, cash, TA.contribution, "2025-01-05", "0", "40000.00")
    add_trade(session, user, account, cash, TA.contribution, "2025-03-10", "0", "16500.00")
    add_trade(session, user, account, equity, TA.buy, "2025-06-01", "100", "180.00")
    add_trade(session, user, account, cash, TA.contribution, "2026-01-15", "0", "1582.40")
    return token, account


def test_investment_cash_only_trades_no_transactions_card_matches_chart(
    session: Session, client: TestClient
) -> None:
    """The 459 fixture: an investment account with only CASH trades and ZERO
    transactions reports the trade-chain cash, not $0.00."""
    token, account = _seed_459_shape(session, client, "m145-459@example.com")

    response = client.get(f"/api/v1/accounts/{account.id}", headers=_headers(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cash_balance"] == "40082.40"
    assert body["starting_balance"] == "0.00"
    # `investment_value` is network-priced (yfinance) and NOT asserted here —
    # this test pins the CASH source, which is M145's subject.
    # The service function the card now reads agrees with the card.
    assert account_cash(session, account, TODAY) == D("40082.40")


def test_cash_account_detail_still_reads_transactions(session: Session, client: TestClient) -> None:
    """Cash accounts are UNCHANGED: the ledger subquery, not the trade chain."""
    token = _register(client, "m145-cash@example.com")
    user = _api_user(session, "m145-cash@example.com")
    response = client.post(
        "/api/v1/accounts",
        json={"name": "Chequing", "type": "chequing", "currency": "CAD"},
        headers=_headers(token),
    )
    assert response.status_code == 201
    account_id = response.json()["id"]
    batch = ImportBatch(
        user_id=user.id,
        account_id=account_id,
        institution="Bank",
        file_name="x.csv",
        status=ImportBatchStatus.committed,
    )
    session.add(batch)
    session.flush()
    session.add(
        Transaction(
            user_id=user.id,
            account_id=account_id,
            date=date(2026, 1, 5),
            raw_description="pay",
            normalized_payee="Employer",
            amount_native=D("1200.00"),
            currency="CAD",
            dedup_hash="m145-cash-1",
            import_batch_id=batch.id,
        )
    )
    session.add(
        Transaction(
            user_id=user.id,
            account_id=account_id,
            date=date(2026, 2, 5),
            raw_description="rent",
            normalized_payee="Landlord",
            amount_native=D("-700.00"),
            currency="CAD",
            dedup_hash="m145-cash-2",
            import_batch_id=batch.id,
        )
    )
    session.commit()

    body = client.get(f"/api/v1/accounts/{account_id}", headers=_headers(token)).json()
    assert body["cash_balance"] == "500.00"
    assert body["total_value"] == "500.00"


def test_value_history_last_cash_point_equals_detail_card_cash(
    session: Session, client: TestClient
) -> None:
    """The anti-fabrication cross-surface pin: the card's cash STRING equals
    the latest point of the value-history's cash line, to the cent."""
    token, account = _seed_459_shape(session, client, "m145-cross@example.com")

    history = account_value_history(session, account, date(2025, 1, 1), TODAY, "day", D("1000"))
    last_cash = history["buckets"][-1]["cash_cad"]
    assert last_cash == D("40082.40")

    body = client.get(f"/api/v1/accounts/{account.id}", headers=_headers(token)).json()
    assert D(body["cash_balance"]) == last_cash


def test_fx_usd_cash_trade_converts_at_valuation_rate(session: Session) -> None:
    """A USD CASH-security trade inside a CAD account converts at the cached
    USD→CAD rate (M132's per-row-currency legs), matching the chart exactly."""
    user = _make_user(session, "m145-fx@example.com")
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    cash = _cash_security(session, user)
    add_trade(session, user, account, cash, TA.contribution, "2026-06-01", "0", "1000.00")
    usd_cash = Security(
        user_id=user.id,
        symbol="CASHUSD",
        name="USD Cash",
        kind=SecurityKind.stock,
        listing_currency="USD",
        exchange="ARCA",
    )
    session.add(usd_cash)
    session.commit()
    session.refresh(usd_cash)
    usd_row = add_trade(
        session, user, account, usd_cash, TA.contribution, "2026-06-05", "0", "500.00"
    )
    usd_row.currency = "USD"
    session.add(usd_row)
    session.commit()
    seed_fx(session, "USD", "CAD", "2026-06-14", "1.4000")

    # CAD 1,000.00 + USD 500.00 x 1.4000 = 1,000.00 + 700.00 = 1,700.00 CAD
    cash_cad = account_cash(session, account, TODAY)
    assert cash_cad == D("1700.00")
    history = account_value_history(session, account, date(2026, 1, 1), TODAY, "day", D("1000"))
    assert history["buckets"][-1]["cash_cad"] == cash_cad


def _make_user(session: Session, email: str) -> User:
    user = User(email=email, hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_starting_balance_counts_once_and_batched_map_matches(
    session: Session, client: TestClient
) -> None:
    """`starting_balance` is inside the trade chain ONCE: an investment
    account does not double-add it, and the batched `/accounts`-list map is
    byte-identical to the per-account read."""
    token = _register(client, "m145-sb@example.com")
    user = _api_user(session, "m145-sb@example.com")
    account = make_account(session, user, "RRSP", AccountType.rrsp, taxable=False)
    account.starting_balance = D("5000.00")
    session.add(account)
    session.commit()
    session.refresh(account)
    cash = _cash_security(session, user)
    add_trade(session, user, account, cash, TA.contribution, "2026-02-01", "0", "1000.00")

    # 5,000 (starting) + 1,000 (trade) = 6,000 — NOT 11,000 (double-added sb).
    assert account_cash(session, account, TODAY) == D("6000.00")

    cash_map = accounts_cash(session, user.id, [account], TODAY)
    assert cash_map[account.id] == account_cash(session, account, TODAY)

    body = client.get(f"/api/v1/accounts/{account.id}", headers=_headers(token)).json()
    assert body["cash_balance"] == "6000.00"
    assert body["starting_balance"] == "5000.00"


def test_investment_account_kinds_covered_by_the_fix() -> None:
    """The fix is keyed on KIND, so every investment type is in scope and no
    cash type is."""
    for kind in (AccountType.tfsa, AccountType.rrsp, AccountType.brokerage_non_registered):
        assert kind in INVESTMENT_ACCOUNT_TYPES
    for kind in (AccountType.chequing, AccountType.savings, AccountType.credit_card):
        assert kind not in INVESTMENT_ACCOUNT_TYPES

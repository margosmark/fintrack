"""M29 delete-path FK guards (docs/29 F3, F8).

Covers: the docs/20 user cascade (junctions, budgets, bills, goals, debts,
reconciliation, MCP tokens), account_in_use pre-checks, security_in_use
pre-checks + watchlist clearing, category budget/bill guards, and the
transaction delete cleanup of reconciliation/tag junction rows.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlmodel import func, select

from app.models.acb_pools import AcbPool
from app.models.accounts import Account, AccountType
from app.models.bill_reminders import BillReminder
from app.models.budgets import Budget
from app.models.categories import Category
from app.models.debts import Debt
from app.models.dividend_history import DividendHistory, DividendHistoryRefresh
from app.models.etf_holdings import EtfAssetClass, EtfHolding, EtfHoldingSource
from app.models.fundamentals import Fundamentals, FundamentalsSource
from app.models.goals import Goal
from app.models.import_batches import ImportBatch
from app.models.institutions import Institution
from app.models.learned_payees import LearnedPayee
from app.models.llm_suggestions import LlmSuggestion
from app.models.mcp_tokens import McpToken, hash_mcp_token
from app.models.notifications import (
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationInboxRead,
    NotificationSetting,
)
from app.models.prices import Price, PriceSource
from app.models.reconciliation import ReconciliationItem
from app.models.rule_tags import RuleTag
from app.models.rules import Rule, RuleMatchType
from app.models.securities import Security, SecurityKind
from app.models.spending_alerts import SpendingAlert
from app.models.tags import Tag
from app.models.tax_projections import TaxProjection
from app.models.trades import Trade, TradeAction
from app.models.transaction_tags import TransactionTag
from app.models.transactions import Transaction
from app.models.users import User
from app.models.watchlist import WatchlistItem
from tests.test_auth import EMAIL, PASSWORD, _auth_headers, _register

DELETE_ME = "/api/v1/users/me"
ACCOUNTS = "/api/v1/accounts"
SECURITIES = "/api/v1/securities"
CATEGORIES = "/api/v1/categories"
TRANSACTIONS = "/api/v1/transactions"


def _user_id(session, email: str) -> int:
    return session.exec(select(User).where(User.email == email)).one().id


def _seed_full_user(session, user_id: int) -> None:
    """One row in every user-owned table plus both tag junctions, in
    dependency order — the exact delete-account cascade surface (docs/20)."""
    inst = Institution(user_id=user_id, name="RBC")
    session.add(inst)
    session.flush()

    cat = Category(user_id=user_id, name="Groceries")
    session.add(cat)
    session.flush()

    sec = Security(
        user_id=user_id,
        symbol="XEI",
        name="iShares XEI",
        kind=SecurityKind.etf,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(sec)
    session.flush()

    acct = Account(user_id=user_id, name="Joint", type=AccountType.chequing, currency="CAD")
    session.add(acct)
    session.flush()

    txn = Transaction(
        user_id=user_id,
        account_id=acct.id,
        date=date(2026, 1, 1),
        raw_description="WALMART",
        normalized_payee="walmart",
        amount_native=42.0,
        currency="CAD",
        dedup_hash="abc123",
    )
    session.add(txn)
    session.flush()

    session.add(
        Trade(
            user_id=user_id,
            account_id=acct.id,
            security_id=sec.id,
            action=TradeAction.buy,
            date=date(2026, 1, 2),
            quantity=10,
            price_native=30.0,
            commission=0,
        )
    )
    session.add(
        AcbPool(
            user_id=user_id,
            account_id=acct.id,
            security_id=sec.id,
            quantity=10,
            total_acb_cad=300,
        )
    )
    session.add(
        ImportBatch(user_id=user_id, account_id=acct.id, institution="rbc", file_name="q.csv")
    )
    session.flush()
    # The imported transaction pins the FK-safe cascade order: transactions
    # reference import_batches (no ON DELETE), so the batch must be deleted
    # AFTER its transaction rows.
    batch = session.exec(select(ImportBatch).where(ImportBatch.user_id == user_id)).one()
    txn.import_batch_id = batch.id
    session.add(txn)
    session.add(
        EtfHolding(
            user_id=user_id,
            security_id=sec.id,
            holding_name="Acme",
            asset_class=EtfAssetClass.other,
            weight_pct=10.0,
            source=EtfHoldingSource.manual,
        )
    )
    session.add(
        Fundamentals(
            user_id=user_id,
            security_id=sec.id,
            source=FundamentalsSource.manual,
            fetched_at=date(2026, 1, 3),
        )
    )
    session.add(
        Price(
            user_id=user_id,
            security_id=sec.id,
            date=date(2026, 1, 4),
            price=30.5,
            source=PriceSource.manual,
        )
    )
    session.add(WatchlistItem(user_id=user_id, security_id=sec.id))

    rule = Rule(
        user_id=user_id,
        match_type=RuleMatchType.contains,
        pattern="walmart",
        priority=1,
        category_id=cat.id,
    )
    session.add(rule)
    session.flush()

    tag = Tag(user_id=user_id, name="vacation")
    session.add(tag)
    session.flush()

    session.add(RuleTag(rule_id=rule.id, tag_id=tag.id))
    session.add(TransactionTag(transaction_id=txn.id, tag_id=tag.id))

    budget = Budget(user_id=user_id, category_id=cat.id, amount=Decimal("500.00"))
    session.add(budget)
    session.flush()

    session.add(
        SpendingAlert(
            user_id=user_id,
            budget_id=budget.id,
            alert_type="warning",
            threshold_pct=Decimal("80.00"),
            actual_pct=Decimal("50.00"),
            spent=Decimal("250.00"),
            budget_amount=Decimal("500.00"),
        )
    )
    session.add(TaxProjection(user_id=user_id, year=2026))
    session.add(NotificationSetting(user_id=user_id, alert_type="low_balance"))
    # M51 (docs/35): user_id-carrying, FK to users with no ON DELETE — without
    # `_USER_OWNED_MODELS` coverage this row would orphan and the account
    # delete would fail the FK, so seed it to keep the cascade claim honest.
    session.add(
        NotificationInboxRead(user_id=user_id, item_key="bill_due:1", read_at=datetime.now(UTC))
    )
    # M59 (docs/37): channel -> delivery -> attempt, likewise FK'd with no ON
    # DELETE; `_USER_OWNED_MODELS` lists the attempt BEFORE the delivery it
    # references, which is exactly the ordering this seed proves.
    stamp = datetime.now(UTC)
    channel = NotificationChannel(
        user_id=user_id, channel="webhook", enabled=True, target="https://example.invalid/hook"
    )
    session.add(channel)
    session.flush()
    delivery = NotificationDelivery(
        user_id=user_id,
        channel="webhook",
        alert_type="bill_due",
        item_key="bill_due:1",
        severity="warning",
        occurred_at=stamp,
        link="/bills",
        status="dead",
        attempts=1,
        next_attempt_at=stamp,
        dead_at=stamp,
    )
    session.add(delivery)
    session.flush()
    session.add(
        NotificationDeliveryAttempt(
            user_id=user_id,
            delivery_id=delivery.id,
            attempt=1,
            outcome="fatal",
            error_code="webhook_http_error",
            http_status=404,
            detail="sink rejected",
        )
    )
    session.add(
        BillReminder(
            user_id=user_id,
            name="Rent",
            category_id=cat.id,
            frequency="monthly",
            next_due_date="2026-02-01",
            match_term="Rent",
        )
    )
    session.add(
        Goal(
            user_id=user_id,
            name="Emergency fund",
            target_amount=Decimal("10000.00"),
            account_id=acct.id,
        )
    )
    session.add(
        Debt(
            user_id=user_id,
            account_id=acct.id,
            name="Mortgage",
            debt_type="mortgage",
        )
    )
    session.add(
        ReconciliationItem(
            user_id=user_id,
            account_id=acct.id,
            transaction_id=txn.id,
            statement_date="2026-01-01",
            statement_amount=Decimal("42.00"),
            status="matched",
        )
    )
    # mcp_tokens migration (0015) lacks server defaults for the timestamp
    # columns, so seed them explicitly here.
    now = datetime.now(UTC)
    session.add(
        McpToken(
            user_id=user_id,
            name="cli",
            token_hash=hash_mcp_token("tok"),
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()


# --- (a) user cascade -------------------------------------------------------


def test_delete_user_cascades_every_table(client, session) -> None:
    body = _register(client)
    uid = _user_id(session, EMAIL)
    _seed_full_user(session, uid)

    res = client.request(
        "DELETE",
        DELETE_ME,
        json={"current_password": PASSWORD},
        headers=_auth_headers(body["access_token"]),
    )
    assert res.status_code == 204

    # Zero rows left in every user_id-carrying table.
    for model in (
        LlmSuggestion,
        LearnedPayee,
        Trade,
        AcbPool,
        ImportBatch,
        Tag,
        Transaction,
        EtfHolding,
        Fundamentals,
        Price,
        WatchlistItem,
        Rule,
        SpendingAlert,
        Budget,
        BillReminder,
        TaxProjection,
        NotificationSetting,
        NotificationInboxRead,
        NotificationChannel,
        NotificationDelivery,
        NotificationDeliveryAttempt,
        Category,
        ReconciliationItem,
        Goal,
        Debt,
        McpToken,
        Account,
        Security,
        Institution,
    ):
        count = session.exec(
            select(func.count()).select_from(model).where(model.user_id == uid)
        ).first()
        assert count == 0, f"{model.__name__} still has rows for deleted user"

    # Junctions (no user_id) are gone too: no rows reference the user's
    # (deleted) rules or transactions.
    assert session.exec(select(func.count()).select_from(RuleTag)).first() == 0
    assert session.exec(select(func.count()).select_from(TransactionTag)).first() == 0
    assert session.exec(select(User).where(User.id == uid)).first() is None


# --- (b) account guards -----------------------------------------------------


def test_delete_account_with_trade_conflicts(client, session) -> None:
    token = _register(client)["access_token"]
    uid = _user_id(session, EMAIL)
    acct = Account(user_id=uid, name="TFSA", type=AccountType.chequing, currency="CAD")
    session.add(acct)
    session.flush()
    sec = Security(
        user_id=uid,
        symbol="XEI",
        name="iShares XEI",
        kind=SecurityKind.etf,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(sec)
    session.flush()
    session.add(
        Trade(
            user_id=uid,
            account_id=acct.id,
            security_id=sec.id,
            action=TradeAction.buy,
            date=date(2026, 1, 2),
            quantity=10,
            price_native=30.0,
            commission=0,
        )
    )
    session.commit()

    res = client.delete(f"{ACCOUNTS}/{acct.id}", headers=_auth_headers(token))
    assert res.status_code == 409
    assert res.json()["code"] == "account_in_use"


def test_delete_account_referenced_by_debt_conflicts(client, session) -> None:
    token = _register(client)["access_token"]
    uid = _user_id(session, EMAIL)
    acct = Account(user_id=uid, name="Chequing", type=AccountType.chequing, currency="CAD")
    session.add(acct)
    session.flush()
    session.add(Debt(user_id=uid, account_id=acct.id, name="Loan", debt_type="loan"))
    session.commit()

    res = client.delete(f"{ACCOUNTS}/{acct.id}", headers=_auth_headers(token))
    assert res.status_code == 409
    assert res.json()["code"] == "account_in_use"


def test_delete_account_missing_returns_envelope(client) -> None:
    token = _register(client)["access_token"]
    res = client.delete(f"{ACCOUNTS}/999999", headers=_auth_headers(token))
    assert res.status_code == 404
    assert res.json()["code"] == "not_found"


# --- (c) security guards ----------------------------------------------------


def test_delete_watched_security_clears_watchlist(client, session) -> None:
    token = _register(client)["access_token"]
    uid = _user_id(session, EMAIL)
    sec = Security(
        user_id=uid,
        symbol="XEI",
        name="iShares XEI",
        kind=SecurityKind.etf,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(sec)
    session.flush()
    session.add(WatchlistItem(user_id=uid, security_id=sec.id))
    session.commit()

    res = client.delete(f"{SECURITIES}/{sec.id}", headers=_auth_headers(token))
    assert res.status_code == 204
    assert session.exec(select(func.count()).select_from(WatchlistItem)).first() == 0


def test_delete_security_with_fundamentals_conflicts(client, session) -> None:
    token = _register(client)["access_token"]
    uid = _user_id(session, EMAIL)
    sec = Security(
        user_id=uid,
        symbol="XEI",
        name="iShares XEI",
        kind=SecurityKind.etf,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(sec)
    session.flush()
    session.add(
        Fundamentals(
            user_id=uid,
            security_id=sec.id,
            source=FundamentalsSource.manual,
            fetched_at=date(2026, 1, 3),
        )
    )
    session.commit()

    res = client.delete(f"{SECURITIES}/{sec.id}", headers=_auth_headers(token))
    assert res.status_code == 409
    assert res.json()["code"] == "security_in_use"


@pytest.mark.parametrize("reference", ["history", "refresh"])
def test_delete_security_with_dividend_history_conflicts(client, session, reference: str) -> None:
    """M184 child FKs have no database cascade, so the documented guard owns them."""
    token = _register(client)["access_token"]
    uid = _user_id(session, EMAIL)
    sec = Security(
        user_id=uid,
        symbol=f"DIV{reference.upper()}",
        name="Dividend history fixture",
        kind=SecurityKind.stock,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(sec)
    session.flush()
    if reference == "history":
        session.add(
            DividendHistory(
                user_id=uid,
                security_id=sec.id,
                ex_date=date(2026, 1, 3),
                amount=Decimal("0.25"),
            )
        )
    else:
        session.add(DividendHistoryRefresh(user_id=uid, security_id=sec.id))
    session.commit()

    res = client.delete(f"{SECURITIES}/{sec.id}", headers=_auth_headers(token))

    assert res.status_code == 409
    assert res.json()["code"] == "security_in_use"
    assert session.get(Security, sec.id) is not None


def test_delete_security_missing_returns_envelope(client) -> None:
    token = _register(client)["access_token"]
    res = client.delete(f"{SECURITIES}/999999", headers=_auth_headers(token))
    assert res.status_code == 404
    assert res.json()["code"] == "not_found"


# --- (d) category guards ----------------------------------------------------


def test_delete_category_referenced_by_budget_conflicts(client, session) -> None:
    token = _register(client)["access_token"]
    uid = _user_id(session, EMAIL)
    cat = Category(user_id=uid, name="Groceries")
    session.add(cat)
    session.flush()
    session.add(Budget(user_id=uid, category_id=cat.id, amount=Decimal("500.00")))
    session.commit()

    res = client.delete(f"{CATEGORIES}/{cat.id}", headers=_auth_headers(token))
    assert res.status_code == 409
    assert res.json()["code"] == "category_in_use"


def test_delete_category_referenced_by_bill_conflicts(client, session) -> None:
    token = _register(client)["access_token"]
    uid = _user_id(session, EMAIL)
    cat = Category(user_id=uid, name="Housing")
    session.add(cat)
    session.flush()
    session.add(
        BillReminder(
            user_id=uid,
            name="Rent",
            category_id=cat.id,
            frequency="monthly",
            next_due_date="2026-02-01",
            match_term="Rent",
        )
    )
    session.commit()

    res = client.delete(f"{CATEGORIES}/{cat.id}", headers=_auth_headers(token))
    assert res.status_code == 409
    assert res.json()["code"] == "category_in_use"


# --- (e) transaction delete cleanup -----------------------------------------


def test_delete_matched_transaction_removes_reconciliation_item(client, session) -> None:
    token = _register(client)["access_token"]
    uid = _user_id(session, EMAIL)
    acct = Account(user_id=uid, name="Chequing", type=AccountType.chequing, currency="CAD")
    session.add(acct)
    session.flush()
    txn = Transaction(
        user_id=uid,
        account_id=acct.id,
        date=date(2026, 1, 1),
        raw_description="WALMART",
        normalized_payee="walmart",
        amount_native=42.0,
        currency="CAD",
        dedup_hash="abc123",
    )
    session.add(txn)
    session.flush()
    tag = Tag(user_id=uid, name="vacation")
    session.add(tag)
    session.flush()
    session.add(TransactionTag(transaction_id=txn.id, tag_id=tag.id))
    session.add(
        ReconciliationItem(
            user_id=uid,
            account_id=acct.id,
            transaction_id=txn.id,
            statement_date="2026-01-01",
            statement_amount=Decimal("42.00"),
            status="matched",
        )
    )
    session.commit()

    res = client.delete(f"{TRANSACTIONS}/{txn.id}", headers=_auth_headers(token))
    assert res.status_code == 204
    assert session.exec(select(func.count()).select_from(ReconciliationItem)).first() == 0
    assert session.exec(select(func.count()).select_from(TransactionTag)).first() == 0

"""M29/M47 aggregate remediation regressions (docs/29 F1, F1b, F1c, F1d, F2, F7).

Proves the committed-only staging filter, starting-balance seeding, tax-alert
tenancy resolution, yearly-budget window, and the goals PATCH ownership guard.
"""

import hashlib
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.spending_alerts import _check_alerts
from app.models.acb_pools import AcbPool
from app.models.accounts import Account, AccountType
from app.models.budgets import Budget
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.models.transactions import Transaction
from app.services.insights import compute_insights
from app.services.networth_snapshot import networth_snapshot
from app.services.subscriptions import detect_subscriptions
from app.services.txn_filters import today_et
from tests.test_deep_common import (
    auth,
    create_account,
    get_category,
    register,
    seed_transaction,
)


def _today() -> date:
    """The clock, read at CALL time — never captured at import (t_435c2d14)."""
    return today_et()


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _months_back(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) - months
    return date(total // 12, total % 12 + 1, 1)


def _register(client: TestClient) -> tuple[dict, int]:
    body = register(client)
    return auth(body["access_token"]), body["user"]["id"]


def _pending_batch(session: Session, user_id: int, account_id: int) -> ImportBatch:
    batch = ImportBatch(
        user_id=user_id,
        account_id=account_id,
        institution="Test Bank",
        file_name="staging.csv",
        status=ImportBatchStatus.pending_review,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def _batch_tx(
    session: Session,
    user_id: int,
    account_id: int,
    batch_id: int,
    payee: str,
    amount: str,
    day: date,
    category_id: int | None = None,
) -> None:
    session.add(
        Transaction(
            user_id=user_id,
            account_id=account_id,
            import_batch_id=batch_id,
            date=day,
            raw_description=payee,
            normalized_payee=payee,
            amount_native=Decimal(amount),
            currency="CAD",
            dedup_hash=hashlib.sha256(f"{payee}-{day}-{amount}".encode()).hexdigest(),
            category_id=category_id,
        )
    )
    session.commit()


def _commit_batch(session: Session, batch: ImportBatch) -> None:
    batch.status = ImportBatchStatus.committed
    session.add(batch)
    session.commit()


# ---------------------------------------------------------------- budgets F1


def test_budget_spent_excludes_pending_review_batch(client: TestClient, session: Session) -> None:
    """Pending-review rows are invisible to budget spent until the batch commits."""
    headers, user_id = _register(client)
    acct = create_account(client, headers)
    cat_id = get_category(session, user_id, "groceries")
    batch = _pending_batch(session, user_id, acct)

    seed_transaction(
        session,
        user_id,
        acct,
        "Manual Loblaws",
        amount="-80.00",
        day=_today().replace(day=2),
        category_id=cat_id,
    )
    _batch_tx(
        session,
        user_id,
        acct,
        batch.id,
        "Import Metro",
        "-60.00",
        _today().replace(day=4),
        category_id=cat_id,
    )

    r = client.post(
        "/api/v1/budgets",
        headers=headers,
        json={"category_id": cat_id, "amount": 200, "period": "monthly"},
    )
    assert r.status_code == 201

    r = client.get("/api/v1/budgets", headers=headers)
    assert r.status_code == 200
    assert float(r.json()[0]["spent"]) == 80.00  # pending -60.00 excluded

    _commit_batch(session, batch)

    r = client.get("/api/v1/budgets", headers=headers)
    assert float(r.json()[0]["spent"]) == 140.00  # now counts


# ----------------------------------------------------------------- goals F1/F7


def test_goal_progress_excludes_pending_and_patch_rejects_foreign_account(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _register(client)
    acct = create_account(client, headers)
    batch = _pending_batch(session, user_id, acct)

    seed_transaction(
        session, user_id, acct, "Deposit", amount="1000.00", day=_today().replace(day=1)
    )
    _batch_tx(session, user_id, acct, batch.id, "Staged Deposit", "500.00", _today().replace(day=5))

    r = client.post(
        "/api/v1/goals",
        headers=headers,
        json={"name": "Emergency fund", "target_amount": 5000, "account_id": acct},
    )
    assert r.status_code == 201
    goal_id = r.json()["id"]

    r = client.get("/api/v1/goals", headers=headers)
    assert float(r.json()[0]["current_amount"]) == 1000.00  # pending excluded

    _commit_batch(session, batch)

    r = client.get("/api/v1/goals", headers=headers)
    assert float(r.json()[0]["current_amount"]) == 1500.00

    # PATCH with another user's account_id → 404 (docs/29 F7)
    other = register(client)
    other_headers = auth(other["access_token"])
    other_acct = create_account(client, other_headers, "Foreign Account")
    r = client.patch(f"/api/v1/goals/{goal_id}", headers=headers, json={"account_id": other_acct})
    assert r.status_code == 404
    assert r.json()["detail"] == "Account not found"


# ------------------------------------------------------------- insights F1/F1d


def test_insights_burn_rate_excludes_pending_review_batch(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    headers, user_id = _register(client)
    acct = create_account(client, headers)
    batch = _pending_batch(session, user_id, acct)

    seed_transaction(
        session, user_id, acct, "Committed Spend", "-900.00", day=frozen_today.replace(day=2)
    )
    _batch_tx(
        session, user_id, acct, batch.id, "Pending Spend", "-450.00", frozen_today.replace(day=6)
    )

    before = compute_insights(session, user_id)
    assert before["current_month_spending"] == Decimal("900.00")

    _commit_batch(session, batch)

    after = compute_insights(session, user_id)
    assert after["current_month_spending"] == Decimal("1350.00")
    assert after["daily_burn_rate"] > before["daily_burn_rate"]


# -------------------------------------------------------- networth F1/F1b (b)


def test_networth_snapshot_starting_balance_and_pending(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """Starting balance seeds each account; pending-review rows stay out."""
    headers, user_id = _register(client)
    acct_id = create_account(client, headers)
    account = session.get(Account, acct_id)
    account.starting_balance = Decimal("250.00")
    session.add(account)
    session.commit()
    batch = _pending_batch(session, user_id, acct_id)

    seed_transaction(
        session, user_id, acct_id, "Committed Buy", "-120.00", day=frozen_today.replace(day=3)
    )
    _batch_tx(
        session, user_id, acct_id, batch.id, "Pending Buy", "-80.00", frozen_today.replace(day=4)
    )

    snap = networth_snapshot(session, user_id, frozen_today)
    assert float(snap["accounts"][0]["balance"]) == 130.00  # 250 - 120
    assert float(snap["total_cash"]) == 130.00

    _commit_batch(session, batch)

    snap = networth_snapshot(session, user_id, frozen_today)
    assert float(snap["accounts"][0]["balance"]) == 50.00  # 250 - 120 - 80
    assert float(snap["total_cash"]) == 50.00


# ------------------------------------------------------ subscriptions F1/F10


def test_subscription_detection_excludes_pending_and_stale(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _register(client)
    acct = create_account(client, headers)
    batch = _pending_batch(session, user_id, acct)

    # Two staged occurrences of the same payee — a plausible subscription.
    _batch_tx(
        session, user_id, acct, batch.id, "NETFLIX.COM", "-16.99", _today() - timedelta(days=35)
    )
    _batch_tx(
        session, user_id, acct, batch.id, "NETFLIX.COM", "-16.99", _today() - timedelta(days=5)
    )
    # Committed payee pair from beyond the trailing 13-month window.
    stale = _months_back(_today(), 14)
    seed_transaction(session, user_id, acct, "GYM OLD", "-40.00", day=stale)
    seed_transaction(session, user_id, acct, "GYM OLD", "-40.00", day=stale + timedelta(days=30))

    assert detect_subscriptions(session, user_id) == []

    _commit_batch(session, batch)

    subs = detect_subscriptions(session, user_id)
    assert len(subs) == 1
    assert subs[0]["payee"].startswith("NETFLIX")


# ------------------------------------------------ spending alerts F1 / docs/26 (d)


def test_yearly_budget_alert_evaluates_year_window(client: TestClient, session: Session) -> None:
    """A yearly budget sums the whole calendar year — one month alone wouldn't fire."""
    if _today().month == 1:
        pytest.skip("no prior month exists inside the calendar year in January")
    headers, user_id = _register(client)
    acct = create_account(client, headers)
    cat_id = get_category(session, user_id, "travel")
    session.add(
        Budget(user_id=user_id, category_id=cat_id, amount=Decimal("100.00"), period="yearly")
    )
    session.commit()

    # Same calendar year, different month (prior month), plus a current-month tx.
    prior_month_day = _month_start(_today()) - timedelta(days=1)
    seed_transaction(
        session, user_id, acct, "Winter Trip", "-60.00", day=prior_month_day, category_id=cat_id
    )
    seed_transaction(
        session,
        user_id,
        acct,
        "Fall Trip",
        "-50.00",
        day=_today().replace(day=5),
        category_id=cat_id,
    )

    alerts = _check_alerts(session, user_id)
    assert len(alerts) == 1
    assert alerts[0].alert_type == "critical"  # 110% of the yearly amount
    assert float(alerts[0].spent) == 110.00

    # Daily dedup: a second pass same day creates nothing new.
    assert _check_alerts(session, user_id) == []


def test_monthly_budget_alert_uses_month_window(client: TestClient, session: Session) -> None:
    headers, user_id = _register(client)
    acct = create_account(client, headers)
    cat_id = get_category(session, user_id, "dining")
    session.add(
        Budget(user_id=user_id, category_id=cat_id, amount=Decimal("100.00"), period="monthly")
    )
    session.commit()

    prior_month_day = _month_start(_today()) - timedelta(days=1)
    seed_transaction(
        session, user_id, acct, "Old Dinner", "-60.00", day=prior_month_day, category_id=cat_id
    )
    seed_transaction(
        session,
        user_id,
        acct,
        "Current Dinner",
        "-50.00",
        day=_today().replace(day=5),
        category_id=cat_id,
    )

    assert _check_alerts(session, user_id) == []  # 50% — below the 80% warning


# ------------------------------------------------------------- tax alerts F2 (c)


def _seed_loss_position(
    session: Session, user_id: int, account: Account, symbol: str, buy_price: str, now_price: str
) -> None:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind="stock",
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.flush()
    session.add(
        Trade(
            user_id=user_id,
            account_id=account.id,
            security_id=security.id,
            action=TradeAction.buy,
            date=_today() - timedelta(days=90),
            quantity=Decimal("10"),
            price_native=Decimal(buy_price),
            commission=Decimal("0"),
        )
    )
    session.add(
        AcbPool(
            user_id=user_id,
            account_id=account.id,
            security_id=security.id,
            quantity=Decimal("10"),
            total_acb_cad=Decimal("10") * Decimal(buy_price),
        )
    )
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=_today(),
            price=Decimal(now_price),
            source=PriceSource.manual,
        )
    )
    session.commit()


def test_tax_alerts_fires_for_taxable_and_skips_registered(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _register(client)
    taxable = Account(
        user_id=user_id,
        name="Open Brokerage",
        type=AccountType.brokerage_non_registered,
        currency="CAD",
        taxable=True,
    )
    registered = Account(
        user_id=user_id, name="RRSP", type=AccountType.rrsp, currency="CAD", taxable=False
    )
    session.add(taxable)
    session.add(registered)
    session.commit()
    session.refresh(taxable)
    session.refresh(registered)

    _seed_loss_position(session, user_id, taxable, "LOSS.TO", "50.00", "30.00")  # loss 200
    _seed_loss_position(session, user_id, registered, "RRSPSYM.TO", "50.00", "30.00")  # loss 200

    r = client.get("/api/v1/taxes/alerts", headers=headers)
    assert r.status_code == 200
    alerts = r.json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["symbol"] == "LOSS.TO"
    assert alerts[0]["account_id"] == taxable.id
    assert float(alerts[0]["unrealized_loss_cad"]) == 200.00

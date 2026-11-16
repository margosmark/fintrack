"""Spending + cash-flow aggregation contract tests (docs/12, w6-contract pins 5-8).

Hand-calculated on small deterministic seeded datasets; Decimal equality is
exact and no network is touched (fx comes from seeded manual cache rows only).
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_tax_acb import _user, add_trade, make_account, make_security

from app.core.security import create_access_token
from app.models.accounts import Account, AccountType
from app.models.categories import Category
from app.models.fx_rates import FxRate, FxSource
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.trades import TradeAction as TA
from app.models.transactions import Transaction
from app.models.users import User

D = Decimal

RANGE = {"from": "2025-01-01", "to": "2025-03-31"}
JAN, FEB, MAR = "2025-01-01", "2025-02-01", "2025-03-01"


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _category(session: Session, user: User, name: str, parent: Category | None = None) -> Category:
    cat = Category(user_id=user.id, name=name, parent_id=parent.id if parent else None)
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def _batch(
    session: Session, user: User, account: Account, status: ImportBatchStatus
) -> ImportBatch:
    batch = ImportBatch(
        user_id=user.id,
        account_id=account.id,
        institution="Test Bank",
        file_name="stmt.csv",
        status=status,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def _txn(  # explicit seed rows read like the ledger they model
    session: Session,
    user: User,
    account: Account,
    batch: ImportBatch,
    day: str,
    amount_native: str,
    cad_amount: str | None,
    category: Category | None,
    currency: str = "CAD",
) -> Transaction:
    txn = Transaction(
        user_id=user.id,
        account_id=account.id,
        date=date.fromisoformat(day),
        raw_description=f"raw {day}",
        normalized_payee="Payee",
        amount_native=D(amount_native),
        currency=currency,
        cad_amount=D(cad_amount) if cad_amount is not None else None,
        category_id=category.id if category else None,
        import_batch_id=batch.id,
        dedup_hash=f"{account.id}-{day}-{amount_native}-{batch.id}",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _seed_transactions(session: Session) -> tuple[User, dict[str, int]]:
    """Dataset A — transactions only. Expected numbers derived by hand below."""
    user = _user(session)
    food = _category(session, user, "Food")
    groceries = _category(session, user, "Groceries", parent=food)
    dining = _category(session, user, "Dining", parent=food)

    chequing = make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    cc = make_account(session, user, "Card", AccountType.credit_card, taxable=False)
    usd = Account(user_id=user.id, name="USD Savings", type=AccountType.savings, currency="USD")
    session.add(usd)
    session.commit()
    session.refresh(usd)

    b_cheq = _batch(session, user, chequing, ImportBatchStatus.committed)
    b_cc = _batch(session, user, cc, ImportBatchStatus.committed)
    b_usd = _batch(session, user, usd, ImportBatchStatus.committed)
    b_pending = _batch(session, user, chequing, ImportBatchStatus.pending_review)

    # Jan spending: 50.00 Groceries + 10 USD → 13.60 precomputed CAD Groceries.
    _txn(session, user, chequing, b_cheq, "2025-01-05", "-50.00", "-50.00", groceries)
    _txn(session, user, usd, b_usd, "2025-01-10", "-10.00", "-13.60", groceries, currency="USD")
    # Unresolved CAD (cad_amount NULL) → excluded from every aggregate.
    _txn(session, user, usd, b_usd, "2025-01-12", "-5.00", None, groceries, currency="USD")
    # Pending batch → never counted.
    _txn(session, user, chequing, b_pending, "2025-01-07", "-999.00", "-999.00", dining)
    # Feb spending: 25.50 Dining + 12.25 Uncategorized. Income: +2000 salary.
    _txn(session, user, cc, b_cc, "2025-02-10", "-25.50", "-25.50", dining)
    _txn(session, user, cc, b_cc, "2025-02-14", "-12.25", "-12.25", None)
    _txn(session, user, chequing, b_cheq, "2025-02-20", "2000.00", "2000.00", None)

    return user, {"food": food.id, "groceries": groceries.id, "dining": dining.id}


# --- Spending series ---------------------------------------------------------


def test_spending_monthly_series_exact(client: TestClient, session: Session) -> None:
    user, cats = _seed_transactions(session)
    body = client.get("/api/v1/reports/spending", params=RANGE, headers=_headers(user)).json()

    assert (body["from"], body["to"], body["granularity"]) == ("2025-01-01", "2025-03-31", "month")
    items = [
        (i["bucket"], i["category_id"], i["category_name"], D(str(i["total_cad"])))
        for i in body["items"]
    ]
    assert items == [
        (JAN, cats["groceries"], "Groceries", D("63.60")),
        (FEB, cats["dining"], "Dining", D("25.50")),
        (FEB, None, "Uncategorized", D("12.25")),
        (MAR, None, "Uncategorized", D("0")),  # zero-filled quiet bucket
    ]
    assert D(str(body["total_cad"])) == D("101.35")


def test_spending_excludes_pending_and_unresolved(client: TestClient, session: Session) -> None:
    user, _ = _seed_transactions(session)
    body = client.get("/api/v1/reports/spending", params=RANGE, headers=_headers(user)).json()
    total = sum(D(str(i["total_cad"])) for i in body["items"])
    assert total == D("101.35")  # no 999 pending slice, no unresolved-USD slice


def test_spending_week_buckets_are_iso_mondays(client: TestClient, session: Session) -> None:
    user, _ = _seed_transactions(session)
    body = client.get(
        "/api/v1/reports/spending",
        params={"from": "2025-01-01", "to": "2025-01-21", "granularity": "week"},
        headers=_headers(user),
    ).json()
    assert [i["bucket"] for i in body["items"]] == [  # Jan 1 is a Wednesday
        "2024-12-30",
        "2025-01-06",
        "2025-01-13",
        "2025-01-20",
    ]


def test_spending_day_granularity_bucket_count(client: TestClient, session: Session) -> None:
    user, _ = _seed_transactions(session)
    body = client.get(
        "/api/v1/reports/spending", params={**RANGE, "granularity": "day"}, headers=_headers(user)
    ).json()
    buckets = {i["bucket"] for i in body["items"]}
    assert len(buckets) == 90  # Jan 31 + Feb 28 + Mar 31 — one entry per day
    assert all(len([i for i in body["items"] if i["bucket"] == b]) == 1 for b in buckets)


# --- Category breakdown ------------------------------------------------------


def test_category_breakdown_rolls_up_to_roots_descending(
    client: TestClient, session: Session
) -> None:
    user, cats = _seed_transactions(session)
    body = client.get(
        "/api/v1/reports/spending/categories", params=RANGE, headers=_headers(user)
    ).json()
    items = [(i["category_id"], i["name"], D(str(i["cad_total"]))) for i in body["items"]]
    # Children fold into Food: 63.60 + 25.50; descending sort.
    assert items == [(cats["food"], "Food", D("89.10")), (None, "Uncategorized", D("12.25"))]
    assert D(str(body["total_cad"])) == D("101.35")


# --- Cash flow ---------------------------------------------------------------


def test_cashflow_transactions_only(client: TestClient, session: Session) -> None:
    user, _ = _seed_transactions(session)
    body = client.get("/api/v1/reports/cashflow", params=RANGE, headers=_headers(user)).json()
    rows = [
        (b["bucket"], D(str(b["income_cad"])), D(str(b["expenses_cad"])), D(str(b["net_cad"])))
        for b in body["buckets"]
    ]
    assert rows == [
        (JAN, D("0"), D("63.60"), D("-63.60")),
        (FEB, D("2000"), D("37.75"), D("1962.25")),
        (MAR, D("0"), D("0"), D("0")),  # zero-filled quiet bucket
    ]


def test_cashflow_trade_actions_and_principal_exclusion(
    client: TestClient, session: Session
) -> None:
    """Inflow/outflow action sets count; buy/sell/drip principal does NOT;
    commissions on buy|sell|drip DO; transfers are share movements and excluded from cashflow."""
    user = _user(session)
    brokerage = make_account(
        session, user, "Brokerage", AccountType.brokerage_non_registered, taxable=True
    )
    usd_brk = Account(
        user_id=user.id,
        name="USD Brokerage",
        type=AccountType.brokerage_non_registered,
        currency="USD",
    )
    session.add(usd_brk)
    session.commit()
    session.refresh(usd_brk)
    chequing = make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    ry = make_security(session, user, "RY", "CAD")

    def trade(action, acct, day, qty, price, comm="0"):
        add_trade(session, user, acct, ry, action, day, qty, price, comm)

    trade(TA.dividend, brokerage, "2025-01-10", "0", "100.00")  # income 100
    trade(TA.buy, brokerage, "2025-01-15", "10", "25.00", "9.95")  # expense 9.95 only
    trade(TA.fee, usd_brk, "2025-02-01", "0", "8.00")  # 8 x 1.25 = 10
    trade(TA.drip, brokerage, "2025-02-15", "1", "24.00", "0.75")  # commission only
    trade(TA.corporate_action_adjustment, brokerage, "2025-02-20", "20", "0")
    trade(TA.contribution, brokerage, "2025-03-05", "0", "1000.00")  # income 1000
    trade(
        TA.transfer_out, chequing, "2025-03-10", "0", "500.00"
    )  # share movement, not a cash expense
    trade(
        TA.transfer_in, brokerage, "2025-03-11", "0", "500.00"
    )  # share movement, not a cash inflow
    session.add(
        FxRate(
            base="USD", quote="CAD", date=date(2025, 1, 1), rate=D("1.25"), source=FxSource.manual
        )
    )
    session.commit()

    body = client.get("/api/v1/reports/cashflow", params=RANGE, headers=_headers(user)).json()
    rows = [
        (b["bucket"], D(str(b["income_cad"])), D(str(b["expenses_cad"])), D(str(b["net_cad"])))
        for b in body["buckets"]
    ]
    assert rows == [
        (JAN, D("100.00"), D("9.95"), D("90.05")),
        (FEB, D("0"), D("10.75"), D("-10.75")),  # 10 fee + 0.75 drip commission
        (MAR, D("1000.00"), D("0.00"), D("1000.00")),
    ]


def test_cashflow_unresolved_fx_trade_excluded(client: TestClient, session: Session) -> None:
    user = _user(session)
    usd_brk = Account(
        user_id=user.id,
        name="USD Brokerage",
        type=AccountType.brokerage_non_registered,
        currency="USD",
    )
    session.add(usd_brk)
    session.commit()
    session.refresh(usd_brk)
    ry = make_security(session, user, "RY", "CAD")
    add_trade(session, user, usd_brk, ry, TA.fee, "2025-02-01", "0", "8.00")

    body = client.get("/api/v1/reports/cashflow", params=RANGE, headers=_headers(user)).json()
    feb = next(b for b in body["buckets"] if b["bucket"] == FEB)
    assert D(str(feb["expenses_cad"])) == D("0")  # no cached USD/CAD rate → excluded


# --- Validation & errors -----------------------------------------------------


def test_invalid_date_range_envelope(client: TestClient, session: Session) -> None:
    user = _user(session)
    resp = client.get(
        "/api/v1/reports/spending",
        params={"from": "2025-03-01", "to": "2025-01-01"},
        headers=_headers(user),
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_date_range"


def test_unknown_granularity_standard_validation_envelope(
    client: TestClient, session: Session
) -> None:
    user = _user(session)
    resp = client.get(
        "/api/v1/reports/cashflow",
        params={**RANGE, "granularity": "fortnight"},
        headers=_headers(user),
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


# --- CSV ---------------------------------------------------------------------


def test_spending_csv_header_pinned_and_totals_match(client: TestClient, session: Session) -> None:
    user, _ = _seed_transactions(session)
    json_body = client.get("/api/v1/reports/spending", params=RANGE, headers=_headers(user)).json()
    resp = client.get(
        "/api/v1/reports/spending", params={**RANGE, "format": "csv"}, headers=_headers(user)
    )
    assert resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.strip().splitlines()
    assert lines[0] == "bucket,category_id,category_name,total_cad"
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == len(json_body["items"])
    assert sum(D(r["total_cad"]) for r in rows) == D("101.35")


def test_categories_and_cashflow_csv_headers_pinned(client: TestClient, session: Session) -> None:
    user, _ = _seed_transactions(session)
    cats = client.get(
        "/api/v1/reports/spending/categories",
        params={**RANGE, "format": "csv"},
        headers=_headers(user),
    ).text.splitlines()[0]
    flow = client.get(
        "/api/v1/reports/cashflow", params={**RANGE, "format": "csv"}, headers=_headers(user)
    ).text.splitlines()[0]
    assert cats == "category_id,name,cad_total"
    assert flow == "bucket,income_cad,expenses_cad,net_cad"

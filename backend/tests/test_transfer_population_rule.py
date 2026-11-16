"""Transfer population rule: a transfer is money MOVED, never SPENT (docs/12).

Kilter's transfer detection adopted into docs/12 §Shared population rules — a
row counts as spending only when it is not a transfer, and it has exactly two
spellings, both of which this module exercises:

* `transfer_pair_id IS NOT NULL` — the pairing the detect/link/accept routes
  write (here: the -500 chequing leg and its +500 TFSA leg);
* booked to the tenant's SYSTEM `transfers` category — what someone sets by
  hand when there is no counterpart to pair (here: -250 to an outside account).

Every spend-shaped surface is checked against the same seed — spending
series, category breakdown, category-by-tag matrix, cash flow (BOTH sides, so a
pair cannot appear as income while vanishing from expense), the budget roll
and insights — plus the invariant that the Python row filter and the SQL
conditions used by the set-based readers always agree.

The seed deliberately puts a real +2000 salary beside the transfers: excluding
a transfer from income must not silently exclude real income, and a transfer
inflow must not be counted as a dollar earned.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select
from test_tax_acb import _user, make_account

from app.core.security import create_access_token
from app.models.accounts import AccountType
from app.models.categories import Category
from app.models.import_batches import ImportBatch
from app.models.transactions import Transaction
from app.models.users import User
from app.services.txn_filters import (
    countable_tx_conditions,
    is_transfer_row,
    today_et,
    transfer_exclusion_conditions,
    transfers_category_ids,
)

D = Decimal


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _month_window() -> tuple[str, str]:
    """The CURRENT month, read at call time (never captured at import).

    The budget roll is period-scoped, so seeding on a fixed literal date would
    read $0.00 spent on every run outside that month — the import-capture
    straddle docs/AGENTS §6 warns about, solved by re-deriving here.
    """
    first = today_et().replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _day() -> date:
    return today_et().replace(day=1)


def _seed(session: Session) -> dict:
    user = _user(session)
    groceries = Category(user_id=user.id, name="Groceries")
    transfers = Category(user_id=user.id, name="transfers", is_system=True)
    income = Category(user_id=user.id, name="Income")
    session.add_all([groceries, transfers, income])
    session.commit()
    session.refresh(groceries)
    session.refresh(transfers)
    session.refresh(income)

    chequing = make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    tfsa = make_account(session, user, "TFSA", AccountType.savings, taxable=False)
    day = _day()

    def txn(account, payee: str, amount: str, category, **overrides) -> Transaction:
        row = Transaction(
            user_id=user.id,
            account_id=account.id,
            date=day,
            raw_description=payee,
            normalized_payee=payee,
            amount_native=D(amount),
            currency="CAD",
            cad_amount=D(amount),
            category_id=category.id if category else None,
            dedup_hash=f"transfer-rule-{account.id}-{payee}-{amount}",
            **overrides,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row

    ordinary = txn(chequing, "Loblaws", "-100.00", groceries)
    pair_out = txn(chequing, "Transfer to TFSA", "-500.00", None)
    pair_in = txn(tfsa, "Transfer from Chequing", "500.00", None)
    # A pair is two rows pointing at each other; both exist now, so link them.
    pair_out.transfer_pair_id = pair_in.id
    pair_in.transfer_pair_id = pair_out.id
    session.add_all([pair_out, pair_in])
    session.commit()
    session.refresh(pair_out)
    session.refresh(pair_in)

    booked = txn(chequing, "Transfer to RBC Direct", "-250.00", transfers)
    salary = txn(chequing, "Payroll", "2000.00", income)
    waiting = txn(chequing, "Mystery Merchant", "-20.00", None)

    return {
        "user": user,
        "headers": _headers(user),
        "categories": {"groceries": groceries, "transfers": transfers, "income": income},
        "ordinary": ordinary,
        "pair_out": pair_out,
        "pair_in": pair_in,
        "booked": booked,
        "salary": salary,
        "waiting": waiting,
    }


# --- One population, every surface ------------------------------------------


def test_spending_series_drops_both_transfer_spellings(
    client: TestClient, session: Session
) -> None:
    seed = _seed(session)
    frm, to = _month_window()
    body = client.get(
        "/api/v1/reports/spending", params={"from": frm, "to": to}, headers=seed["headers"]
    ).json()

    # 100 Groceries + 20 still-waiting = 120. The paired legs (-500) and the
    # booked transfer (-250) are gone; the salary was never spend.
    assert D(str(body["total_cad"])) == D("120.00")
    by_name = {i["category_name"]: D(str(i["total_cad"])) for i in body["items"]}
    assert by_name.get("transfers") is None, "a transfers bucket must not rank as spending"
    assert D(str(by_name["Groceries"])) == D("100.00")

    categories = client.get(
        "/api/v1/reports/spending/categories",
        params={"from": frm, "to": to},
        headers=seed["headers"],
    ).json()
    assert D(str(categories["total_cad"])) == D("120.00")
    assert {i["name"] for i in categories["items"]} == {"Groceries", "Uncategorized"}


def test_cashflow_excludes_a_pair_from_both_sides(client: TestClient, session: Session) -> None:
    seed = _seed(session)
    frm, to = _month_window()
    body = client.get(
        "/api/v1/reports/cashflow", params={"from": frm, "to": to}, headers=seed["headers"]
    ).json()

    assert len(body["buckets"]) == 1  # one month of seed rows
    bucket = body["buckets"][0]
    assert D(str(bucket["income_cad"])) == D("2000.00")  # salary, not the 500 leg
    assert D(str(bucket["expenses_cad"])) == D("120.00")
    assert D(str(bucket["net_cad"])) == D("1880.00")


def test_category_tag_matrix_ranks_no_transfers_row(client: TestClient, session: Session) -> None:
    seed = _seed(session)
    frm, to = _month_window()
    body = client.get(
        "/api/v1/reports/category-tag-matrix",
        params={"from": frm, "to": to},
        headers=seed["headers"],
    ).json()

    row_ids = {r["category_id"] for r in body["rows"]}
    assert seed["categories"]["transfers"].id not in row_ids
    assert seed["categories"]["groceries"].id in row_ids


def test_budget_roll_never_counts_a_transfer(client: TestClient, session: Session) -> None:
    seed = _seed(session)
    headers = seed["headers"]
    groceries_id = seed["categories"]["groceries"].id
    transfers_id = seed["categories"]["transfers"].id

    for category_id in (groceries_id, transfers_id):
        created = client.post(
            "/api/v1/budgets",
            json={"category_id": category_id, "amount": 200.0, "period": "monthly"},
            headers=headers,
        )
        assert created.status_code in (200, 201), created.text

    spent = {
        b["category_id"]: D(str(b["spent"]))
        for b in client.get("/api/v1/budgets", headers=headers).json()
    }
    assert spent[groceries_id] == D("100.00")
    assert spent[transfers_id] == D("0.00"), "money that only moved must not read as spent"


def test_insights_month_total_excludes_transfers(client: TestClient, session: Session) -> None:
    seed = _seed(session)
    body = client.get("/api/v1/reports/insights", headers=seed["headers"]).json()
    # 100 + 20 outflow only: the salary bucket is all-inflow (income) and the
    # transfer buckets never exist.
    assert D(str(body["current_month_spending"])) == D("120.00")


# --- The two readers of the same rule ---------------------------------------


def test_python_row_filter_and_sql_conditions_always_agree(
    session: Session,
) -> None:
    """`is_transfer_row` (id list in hand) and `transfer_exclusion_conditions`
    (flag category resolved INSIDE the statement) are two spellings of ONE
    definition — if they ever disagree, a report built one way and its twin
    built the other way would count different money, so every row shape is
    crossed here.
    """
    seed = _seed(session)
    user_id = seed["user"].id
    transfer_ids = transfers_category_ids(session, user_id)
    assert transfer_ids == {seed["categories"]["transfers"].id}

    rows = session.exec(select(Transaction).where(Transaction.user_id == user_id)).all()
    assert len(rows) == 6

    python_kept = {row.id for row in rows if not is_transfer_row(row, transfer_ids)}
    # `committed_staging_conditions` reads `import_batches.status`, so the
    # caller owns that join (docs for `committed_tx_conditions`): skipping it
    # is a cartesian product, which with zero batches yields ZERO rows — the
    # silent-wrong half this test exists to catch.
    sql_kept = {
        row.id
        for row in session.exec(
            select(Transaction)
            .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
            .where(
                *countable_tx_conditions(user_id),
                *transfer_exclusion_conditions(user_id),
            )
        ).all()
    }

    assert python_kept == sql_kept
    # And the verdicts are the ones this fixture was built to have: both
    # spellings excluded, everything else kept.
    excluded = {row.id for row in rows} - python_kept
    assert excluded == {seed["pair_out"].id, seed["pair_in"].id, seed["booked"].id}
    assert seed["ordinary"].id in python_kept
    assert seed["waiting"].id in python_kept
    assert seed["salary"].id in python_kept


def test_a_row_booked_to_a_non_system_transfers_category_still_counts(
    session: Session,
) -> None:
    """The system category is the flag; the word alone is not.

    `Category.name` is only unique per tenant, and a tenant may well own their
    own category called `transfers` — reading the NAME on a non-system row
    would silently drop honest spending from every report.
    """
    seed = _seed(session)
    user_id = seed["user"].id
    own = Category(user_id=user_id, name="transfers", is_system=False)
    session.add(own)
    session.commit()
    session.refresh(own)

    day = _day()
    row = Transaction(
        user_id=user_id,
        account_id=seed["ordinary"].account_id,
        date=day,
        raw_description="My own transfers category",
        normalized_payee="My own transfers category",
        amount_native=D("-42.00"),
        currency="CAD",
        cad_amount=D("-42.00"),
        category_id=own.id,
        dedup_hash="own-transfers-category-42",
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    transfer_ids = transfers_category_ids(session, user_id)
    assert own.id not in transfer_ids  # only the SYSTEM row is the flag
    assert not is_transfer_row(row, transfer_ids)

"""Cash-flow Sankey contract tests (M77, docs/12 §Sankey cash-flow).

Every expected number below is derived by hand from the seeded ledger, not
from the implementation: the graph's job is to conserve to the cent, so the
assertions check conservation, ordering, and the exact split of a hand-built
dataset rather than "the code returns what the code returns".
"""

from __future__ import annotations

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
from app.models.prices import Price, PriceSource
from app.models.trades import TradeAction as TA
from app.models.transactions import Transaction
from app.models.users import User

D = Decimal

RANGE = {"from": "2025-01-01", "to": "2025-03-31"}


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


def _txn(
    session: Session,
    user: User,
    account: Account,
    batch: ImportBatch,
    day: str,
    amount: str,
    category: Category | None,
) -> Transaction:
    txn = Transaction(
        user_id=user.id,
        account_id=account.id,
        date=date.fromisoformat(day),
        raw_description=f"raw {day}",
        normalized_payee="Payee",
        amount_native=D(amount),
        currency="CAD",
        cad_amount=D(amount),
        category_id=category.id if category else None,
        import_batch_id=batch.id,
        dedup_hash=f"{account.id}-{day}-{amount}-{batch.id}",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _price(session: Session, user: User, security_id: int, day: str, price: str) -> None:
    session.add(
        Price(
            user_id=user.id,
            security_id=security_id,
            date=date.fromisoformat(day),
            price=D(price),
            source=PriceSource.manual,
        )
    )
    session.commit()


def _seed(session: Session) -> tuple[User, dict[str, object]]:
    """Hand-built dataset: 2 income sources, 3 expense categories, 1 buy."""
    user = _user(session)
    salary = _category(session, user, "Salary")
    food = _category(session, user, "Food")
    groceries = _category(session, user, "Groceries", parent=food)
    dining = _category(session, user, "Dining", parent=food)
    transport = _category(session, user, "Transport")

    chequing = make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    brokerage = make_account(
        session, user, "Brokerage", AccountType.brokerage_non_registered, taxable=True
    )
    ry = make_security(session, user, "RY", "CAD")

    committed = _batch(session, user, chequing, ImportBatchStatus.committed)
    pending = _batch(session, user, chequing, ImportBatchStatus.pending_review)

    # income: 3000 salary + 50 dividend trade
    _txn(session, user, chequing, committed, "2025-01-05", "3000.00", salary)
    # expenses: 120 Groceries (child of Food), 60 Dining (child of Food),
    # 40 Transport, 12.50 uncategorized, 999.00 on a PENDING batch (excluded)
    _txn(session, user, chequing, committed, "2025-01-08", "-120.00", groceries)
    _txn(session, user, chequing, committed, "2025-02-03", "-60.00", dining)
    _txn(session, user, chequing, committed, "2025-02-14", "-40.00", transport)
    _txn(session, user, chequing, committed, "2025-03-02", "-12.50", None)
    _txn(session, user, chequing, pending, "2025-03-05", "-999.00", groceries)

    add_trade(session, user, brokerage, ry, TA.dividend, "2025-01-15", "0", "50.00")
    # 4 shares @ 25 = 100 invested, 1.00 commission is an expense
    add_trade(session, user, brokerage, ry, TA.buy, "2025-02-20", "4", "25.00", "1.00")
    # a fee on the brokerage account -> its own destination
    add_trade(session, user, brokerage, ry, TA.fee, "2025-03-10", "0", "5.00")
    _price(session, user, ry.id, "2025-02-20", "25.00")

    return user, {
        "salary": salary,
        "food": food,
        "groceries": groceries,
        "dining": dining,
        "transport": transport,
        "ry": ry,
    }


def _nodes(body: dict) -> dict[str, dict]:
    return {node["id"]: node for node in body["nodes"]}


def test_sankey_shape_totals_and_conservation(client: TestClient, session: Session) -> None:
    user, refs = _seed(session)
    body = client.get(
        "/api/v1/reports/cashflow-sankey", params=RANGE, headers=_headers(user)
    ).json()

    # `from_` serializes under its wire alias `from` (docs/08 §Endpoints)
    assert set(body) == {"from", "to", "columns", "nodes", "links", "totals"}
    assert body["columns"] == ["source", "total", "destination"]
    nodes = _nodes(body)
    links = body["links"]

    # sources: salary 3000.00 and the dividend trade 50.00 (>= 2 income nodes)
    assert nodes["src:cat:{}".format(refs["salary"].id)]["amount_cad"] == "3000.00"
    assert nodes["src:trade:dividend"]["amount_cad"] == "50.00"
    assert nodes["src:trade:dividend"]["label"] == "Dividends"
    sources = [n for n in body["nodes"] if n["column"] == "source"]
    assert len(sources) >= 2

    # destinations: expense categories roll up to their TOP-LEVEL ancestor
    # (docs/12 §2), so Groceries 120 + Dining 60 land on Food 180 — plus
    # uncategorized, fee, commission and investments (>= 3 expense categories)
    assert f"dst:cat:{refs['groceries'].id}" not in nodes
    assert f"dst:cat:{refs['dining'].id}" not in nodes
    assert nodes[f"dst:cat:{refs['food'].id}"]["amount_cad"] == "180.00"
    assert nodes[f"dst:cat:{refs['food'].id}"]["label"] == "Food"
    assert nodes[f"dst:cat:{refs['food'].id}"]["group"] == "spent"
    assert nodes[f"dst:cat:{refs['transport'].id}"]["amount_cad"] == "40.00"
    assert nodes["dst:cat:null"]["amount_cad"] == "12.50"
    assert nodes["dst:cat:null"]["label"] == "Uncategorized"
    assert nodes["dst:trade:fee"]["amount_cad"] == "5.00"
    assert nodes["dst:trade:commission"]["amount_cad"] == "1.00"
    assert nodes["dst:invested"]["amount_cad"] == "100.00"
    assert nodes["dst:invested"]["group"] == "invested"
    expense_categories = [
        n for n in body["nodes"] if n["column"] == "destination" and n["group"] == "spent"
    ]
    assert len(expense_categories) >= 3

    # totals hand-added: income 3050.00, expenses 120+60+40+12.50+5+1 = 238.50
    assert body["totals"] == {
        "income_cad": "3050.00",
        "expenses_cad": "238.50",
        "invested_cad": "100.00",
        "saved_cad": "2711.50",
        "drawn_cad": "0.00",
        "money_in_cad": "3050.00",
    }
    # surplus -> Savings destination, no balancing source
    assert nodes["dst:saved"]["amount_cad"] == "2711.50"
    assert nodes["dst:saved"]["group"] == "saved"
    assert "src:drawn" not in nodes

    # conservation: sources == total == destinations, to the cent
    total_in = sum((D(n["amount_cad"]) for n in sources), D("0"))
    total_out = sum(
        (D(n["amount_cad"]) for n in body["nodes"] if n["column"] == "destination"),
        D("0"),
    )
    hub = D(nodes["total:in"]["amount_cad"])
    assert total_in == hub == total_out == D("3050.00")

    # every link touches the hub, and each side of the hub sums to the hub
    assert all(lnk["source"] == "total:in" or lnk["target"] == "total:in" for lnk in links)
    hub_in = sum((D(lnk["amount_cad"]) for lnk in links if lnk["target"] == "total:in"), D("0"))
    hub_out = sum((D(lnk["amount_cad"]) for lnk in links if lnk["source"] == "total:in"), D("0"))
    assert hub_in == hub_out == hub

    # pending batch row (999.00) never reached the graph
    assert all(D(n["amount_cad"]) != D("999.00") for n in body["nodes"])


def test_sankey_deterministic_ordering(client: TestClient, session: Session) -> None:
    user, _refs = _seed(session)
    first = client.get(
        "/api/v1/reports/cashflow-sankey", params=RANGE, headers=_headers(user)
    ).json()
    second = client.get(
        "/api/v1/reports/cashflow-sankey", params=RANGE, headers=_headers(user)
    ).json()
    assert first == second
    for column in ("source", "destination"):
        amounts = [n["amount_cad"] for n in first["nodes"] if n["column"] == column]
        assert amounts == sorted(amounts, key=lambda a: -D(a))


def test_sankey_deficit_adds_a_balancing_source(client: TestClient, session: Session) -> None:
    user = _user(session)
    rent = _category(session, user, "Rent")
    groceries = _category(session, user, "Groceries")
    transport = _category(session, user, "Transport")
    chequing = make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    batch = _batch(session, user, chequing, ImportBatchStatus.committed)

    _txn(session, user, chequing, batch, "2025-01-05", "500.00", None)  # income (uncategorized)
    _txn(session, user, chequing, batch, "2025-01-09", "-300.00", rent)
    _txn(session, user, chequing, batch, "2025-02-02", "-180.00", groceries)
    _txn(session, user, chequing, batch, "2025-03-04", "-90.00", transport)

    body = client.get(
        "/api/v1/reports/cashflow-sankey", params=RANGE, headers=_headers(user)
    ).json()
    nodes = _nodes(body)

    # outgo 570.00 > income 500.00 -> 70.00 drawn, and the graph still balances
    assert body["totals"]["income_cad"] == "500.00"
    assert body["totals"]["expenses_cad"] == "570.00"
    assert body["totals"]["drawn_cad"] == "70.00"
    assert body["totals"]["saved_cad"] == "0.00"
    assert "dst:saved" not in nodes
    assert nodes["src:drawn"]["amount_cad"] == "70.00"
    assert nodes["src:drawn"]["label"] == "Transfers & savings in"

    total_in = sum((D(n["amount_cad"]) for n in body["nodes"] if n["column"] == "source"), D("0"))
    total_out = sum(
        (D(n["amount_cad"]) for n in body["nodes"] if n["column"] == "destination"),
        D("0"),
    )
    assert total_in == total_out == D("570.00")


def test_sankey_excludes_unresolved_cad_and_unconverted_trades(
    client: TestClient, session: Session
) -> None:
    user = _user(session)
    groceries = _category(session, user, "Groceries")
    chequing = make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    usd = Account(
        user_id=user.id,
        name="USD Brokerage",
        type=AccountType.brokerage_non_registered,
        currency="USD",
    )
    session.add(usd)
    session.commit()
    session.refresh(usd)
    batch = _batch(session, user, chequing, ImportBatchStatus.committed)
    ry = make_security(session, user, "RY", "CAD")

    txn = _txn(session, user, chequing, batch, "2025-01-05", "-10.00", groceries)
    txn.cad_amount = None  # unresolved CAD is excluded from every side
    session.add(txn)
    # USD trade with no cached fx rate -> excluded, never fetched
    add_trade(session, user, usd, ry, TA.dividend, "2025-01-15", "0", "25.00")
    session.commit()

    body = client.get(
        "/api/v1/reports/cashflow-sankey", params=RANGE, headers=_headers(user)
    ).json()
    assert body["nodes"] == []
    assert body["totals"] == {
        "income_cad": "0.00",
        "expenses_cad": "0.00",
        "invested_cad": "0.00",
        "saved_cad": "0.00",
        "drawn_cad": "0.00",
        "money_in_cad": "0.00",
    }


def test_sankey_converts_trades_at_the_cached_rate(client: TestClient, session: Session) -> None:
    user = _user(session)
    usd = Account(
        user_id=user.id,
        name="USD Brokerage",
        type=AccountType.brokerage_non_registered,
        currency="USD",
    )
    session.add(usd)
    session.commit()
    session.refresh(usd)
    ry = make_security(session, user, "RY", "CAD")
    session.add(
        FxRate(
            base="USD",
            quote="CAD",
            date=date(2025, 1, 1),
            rate=D("1.25"),
            source=FxSource.manual,
        )
    )
    session.commit()
    add_trade(session, user, usd, ry, TA.dividend, "2025-01-15", "0", "20.00")

    body = client.get(
        "/api/v1/reports/cashflow-sankey", params=RANGE, headers=_headers(user)
    ).json()
    nodes = _nodes(body)
    assert nodes["src:trade:dividend"]["amount_cad"] == "25.00"


def test_sankey_rejects_inverted_range(client: TestClient, session: Session) -> None:
    user = _user(session)
    res = client.get(
        "/api/v1/reports/cashflow-sankey",
        params={"from": "2025-03-31", "to": "2025-01-01"},
        headers=_headers(user),
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_date_range"

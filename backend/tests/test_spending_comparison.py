"""Spending comparison endpoint.

The two money-math regressions below are the reason this file is not just a
smoke test. `services/spending_comparison.py` used to run its own
`sum(abs(amount_native))` — two defects every other spending surface had
already fixed in `services/insights.py::_category_flows`:

1. `abs()` counted a REFUND as extra spending (and inflow rows as spend);
2. `amount_native` is the row's OWN currency, so a USD row was added to a CAD
   row unconverted.

`tests/test_insights.py` pins the same two rules for the insights path; these
pin them for the comparison path, which is a SEPARATE route and would
otherwise drift again.
"""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.fx_rates import FxRate, FxSource
from tests.test_deep_common import (
    auth,
    create_account,
    get_category,
    register,
    seed_transaction,
)


def _auth(client: TestClient) -> dict[str, str]:
    from datetime import UTC, datetime

    email = f"comp-{datetime.now(UTC).timestamp()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    r = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_comparison_empty_periods(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get(
        "/api/v1/reports/spending-comparison?current_start=2026-01-01&current_end=2026-02-01",
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert float(body["current_period"]["total"]) == 0
    assert float(body["previous_period"]["total"]) == 0
    assert float(body["total_change"]) == 0
    assert body["total_change_pct"] == 0.0
    assert body["deltas"] == []


def test_comparison_response_shape(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get(
        "/api/v1/reports/spending-comparison?current_start=2026-06-01&current_end=2026-07-01",
        headers=headers,
    )
    body = r.json()
    for key in ["current_period", "previous_period", "total_change", "total_change_pct", "deltas"]:
        assert key in body, f"Missing key: {key}"
    assert "start" in body["current_period"]
    assert "end" in body["current_period"]
    assert "total" in body["current_period"]


def test_comparison_requires_auth(client: TestClient) -> None:
    r = client.get(
        "/api/v1/reports/spending-comparison?current_start=2026-01-01&current_end=2026-02-01"
    )
    assert r.status_code == 401


# ---------------------------------- money math: signed, and converted to CAD


def _period(client: TestClient, headers: dict, start: str, end: str) -> dict:
    r = client.get(
        f"/api/v1/reports/spending-comparison?current_start={start}&current_end={end}",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_a_refund_offsets_the_purchase_it_belongs_with(
    client: TestClient, session: Session
) -> None:
    """A -100 purchase and a +40 refund in one category net to 60 spent.

    Under the old `sum(abs(amount_native))` this reported 140 — the refund was
    *added* to the spend. The per-category number is the signed net, which is
    what docs/12 §Population rules require.

    Both rows carry their CAD mirror, which is the shape the importer writes:
    the offset happens in the SIGNED `sum(cad_amount)` that every other
    spending surface already used. See
    `test_an_unmirrored_refund_is_not_spending` for the no-mirror path.
    """
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    acct = create_account(client, headers)
    cat = get_category(session, user_id, "Shopping")

    for payee, amount in (("BIG-BUY", "-100.00"), ("REFUND", "40.00")):
        txn = seed_transaction(
            session, user_id, acct, payee, amount=amount, day=date(2026, 1, 5), category_id=cat
        )
        txn.cad_amount = Decimal(amount)
        session.add(txn)
        session.commit()

    data = _period(client, headers, "2026-01-01", "2026-02-01")
    assert Decimal(data["current_period"]["total"]) == Decimal("60.00")


def test_an_unmirrored_refund_is_not_spending(client: TestClient, session: Session) -> None:
    """No CAD mirror → the FALLBACK path, where an inflow is not spending.

    `_spend_cad` prices one row and returns ZERO for a positive amount (an
    inflow is not spending, docs/12), so a refund that arrives without its
    mirror is ignored rather than offsetting. Pinned here because it is a real
    asymmetry between the two paths, not an accident of this endpoint: the
    signed offset only happens once a row has its `cad_amount`. Both paths are
    now exercised, so a future change to either is caught here.
    """
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    acct = create_account(client, headers)
    cat = get_category(session, user_id, "Shopping")

    seed_transaction(
        session, user_id, acct, "BIG-BUY", amount="-100.00", day=date(2026, 1, 5), category_id=cat
    )
    seed_transaction(
        session, user_id, acct, "REFUND", amount="40.00", day=date(2026, 1, 6), category_id=cat
    )

    data = _period(client, headers, "2026-01-01", "2026-02-01")
    assert Decimal(data["current_period"]["total"]) == Decimal("100.00")


def test_a_foreign_row_is_priced_at_its_cad_mirror_not_its_native_amount(
    client: TestClient, session: Session
) -> None:
    """A USD 100 charge with a -150.00 CAD mirror counts as 150 spent.

    Under the old `amount_native` sum this added `100.00` — the row's own
    currency — to the CAD totals, so a USD book reported in CAD came out 50
    short for free.
    """
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    acct = create_account(client, headers)
    cat = get_category(session, user_id, "Travel")

    txn = seed_transaction(
        session,
        user_id,
        acct,
        "TOKYO-HOTEL",
        amount="-100.00",
        day=date(2026, 1, 5),
        category_id=cat,
    )
    txn.currency = "USD"
    txn.cad_amount = Decimal("-150.00")
    session.add(txn)
    session.commit()

    data = _period(client, headers, "2026-01-01", "2026-02-01")
    assert Decimal(data["current_period"]["total"]) == Decimal("150.00")


def test_a_foreign_row_without_a_mirror_falls_back_to_the_cached_rate(
    client: TestClient, session: Session
) -> None:
    """No stored mirror → the newest cached rate on or before the row's date.

    The mirror is a cache, not the contract; a row imported before its FX
    landed must still price in the reporting currency rather than drop out.
    """
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    acct = create_account(client, headers)
    cat = get_category(session, user_id, "Travel")

    session.add(
        FxRate(
            base="USD",
            quote="CAD",
            rate=Decimal("1.25"),
            date=date(2026, 1, 1),
            source=FxSource.manual,
        )
    )
    session.commit()

    txn = seed_transaction(
        session,
        user_id,
        acct,
        "TOKYO-HOTEL",
        amount="-100.00",
        day=date(2026, 1, 5),
        category_id=cat,
    )
    txn.currency = "USD"
    txn.cad_amount = None
    session.add(txn)
    session.commit()

    data = _period(client, headers, "2026-01-01", "2026-02-01")
    assert Decimal(data["current_period"]["total"]) == Decimal("125.00")


def test_an_inflow_is_not_spending(client: TestClient, session: Session) -> None:
    """A +500 payroll row in a category of its own contributes nothing.

    `_spending_buckets` drops income-only buckets, so a refund-shaped credit
    in a category that never saw an outflow cannot be reported as spend.
    """
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    acct = create_account(client, headers)
    cat = get_category(session, user_id, "Salary")

    seed_transaction(
        session, user_id, acct, "EMPLOYER", amount="500.00", day=date(2026, 1, 15), category_id=cat
    )

    data = _period(client, headers, "2026-01-01", "2026-02-01")
    assert Decimal(data["current_period"]["total"]) == Decimal("0.00")
    assert all(d["category_id"] != cat for d in data["deltas"])

"""M55 — subscription → bill draft autofill (docs/25).

Covers the contract pinned in docs/25 §M55: the four derived fields, the
tenancy-strict `from_subscription_id` resolution, the cadence mapping and the
"explicit request values win" rule.
"""

from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.subscriptions import detect_subscriptions
from app.services.txn_filters import today_et
from tests.test_deep_common import auth, create_account, register, seed_transaction

PAYEE = "NETFLIX.COM"


def _setup(client: TestClient) -> tuple[dict, int, int]:
    """headers, user_id, account_id for a fresh tenant."""
    body = register(client)
    headers = auth(body["access_token"])
    return headers, body["user"]["id"], create_account(client, headers)


def _seed_cadence(
    session: Session,
    user_id: int,
    account_id: int,
    payee: str,
    *,
    amount: str,
    gap_days: int,
    count: int = 3,
) -> None:
    """`count` same-payee charges `gap_days` apart -> one detected subscription."""
    today = today_et()
    for i in range(count, 0, -1):
        seed_transaction(
            session,
            user_id,
            account_id,
            payee,
            amount=amount,
            day=today - timedelta(days=gap_days * i),
        )


def _subscription(session: Session, user_id: int, payee: str = PAYEE) -> dict:
    """The detector's own output for `payee` — the assertions' source of truth."""
    found = [sub for sub in detect_subscriptions(session, user_id) if sub["payee"] == payee]
    assert len(found) == 1, f"expected exactly one detected {payee!r} subscription"
    return found[0]


def test_autofill_derives_every_field_from_the_detector(
    client: TestClient, session: Session
) -> None:
    headers, user_id, account = _setup(client)
    _seed_cadence(session, user_id, account, PAYEE, amount="-15.99", gap_days=30)
    expected = _subscription(session, user_id)
    assert expected["frequency"] == "monthly"

    r = client.post("/api/v1/bills", headers=headers, json={"from_subscription_id": PAYEE})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == expected["payee"]
    assert Decimal(body["amount"]) == expected["avg_amount"]
    assert Decimal(body["amount"]) == Decimal("15.99")
    assert body["frequency"] == "monthly"
    assert body["next_due_date"] == expected["next_expected"].isoformat()
    assert body["remind_days_before"] == 3
    assert body["enabled"] is True

    # The draft really is on /bills for this tenant.
    listed = client.get("/api/v1/bills", headers=headers)
    assert listed.status_code == 200
    assert [b["id"] for b in listed.json()] == [body["id"]]


def test_foreign_subscription_id_is_404_and_writes_nothing(
    client: TestClient, session: Session
) -> None:
    # Tenant A owns the NETFLIX detection.
    _a_headers, a_user, a_account = _setup(client)
    _seed_cadence(session, a_user, a_account, PAYEE, amount="-15.99", gap_days=30)
    assert _subscription(session, a_user)

    # Tenant B has their own (different) detected subscription, but not A's.
    b_headers, b_user, b_account = _setup(client)
    _seed_cadence(session, b_user, b_account, "SPOTIFY", amount="-9.99", gap_days=30)
    b_subs = {s["payee"] for s in detect_subscriptions(session, b_user)}
    assert b_subs == {"SPOTIFY"}
    assert PAYEE not in b_subs

    r = client.post("/api/v1/bills", headers=b_headers, json={"from_subscription_id": PAYEE})
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "subscription_not_found"
    assert client.get("/api/v1/bills", headers=b_headers).json() == []


def test_unknown_subscription_id_is_404(client: TestClient, session: Session) -> None:
    headers, _, _ = _setup(client)
    r = client.post("/api/v1/bills", headers=headers, json={"from_subscription_id": "NOPE"})
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "subscription_not_found"
    assert client.get("/api/v1/bills", headers=headers).json() == []


def test_unmappable_cadence_is_422(client: TestClient, session: Session) -> None:
    """weekly/biweekly have no bill cadence — and an explicit `frequency`
    does not smuggle them past the check (docs/25 check order)."""
    headers, user_id, account = _setup(client)
    _seed_cadence(session, user_id, account, "COFFEE CLUB", amount="-4.50", gap_days=7)
    assert _subscription(session, user_id, "COFFEE CLUB")["frequency"] == "weekly"

    r = client.post("/api/v1/bills", headers=headers, json={"from_subscription_id": "COFFEE CLUB"})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "unsupported_bill_cadence"

    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={"from_subscription_id": "COFFEE CLUB", "frequency": "monthly"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "unsupported_bill_cadence"
    assert client.get("/api/v1/bills", headers=headers).json() == []


def test_biweekly_cadence_is_also_422(client: TestClient, session: Session) -> None:
    headers, user_id, account = _setup(client)
    _seed_cadence(session, user_id, account, "GYM", amount="-40.00", gap_days=14)
    assert _subscription(session, user_id, "GYM")["frequency"] == "biweekly"

    r = client.post("/api/v1/bills", headers=headers, json={"from_subscription_id": "GYM"})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "unsupported_bill_cadence"


def test_explicit_values_win_over_the_derived_draft(client: TestClient, session: Session) -> None:
    headers, user_id, account = _setup(client)
    _seed_cadence(session, user_id, account, PAYEE, amount="-15.99", gap_days=30)

    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={
            "from_subscription_id": PAYEE,
            "name": "Our Netflix",
            "next_due_date": "2027-01-15",
            "frequency": "yearly",
            "amount": None,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Our Netflix"
    assert body["next_due_date"] == "2027-01-15"
    assert body["frequency"] == "yearly"
    # Explicit null is a value for the nullable field, so it beats the derivation.
    assert body["amount"] is None


def test_category_is_still_checked_after_the_subscription(
    client: TestClient, session: Session
) -> None:
    headers, user_id, account = _setup(client)
    _seed_cadence(session, user_id, account, PAYEE, amount="-15.99", gap_days=30)

    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={"from_subscription_id": PAYEE, "category_id": 999_999},
    )
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "category_not_found"
    assert client.get("/api/v1/bills", headers=headers).json() == []


def test_without_the_id_the_m40_contract_is_unchanged(client: TestClient, session: Session) -> None:
    """Missing draft fields still 422 with the schema's own envelope."""
    headers, _, _ = _setup(client)

    r = client.post("/api/v1/bills", headers=headers, json={"frequency": "monthly"})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_error"

    r = client.post("/api/v1/bills", headers=headers, json={"name": "Rent", "frequency": "monthly"})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_error"

    # Garbage date is still rejected at 422 (docs/25 M47 row).
    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={"name": "Rent", "frequency": "monthly", "next_due_date": "not-a-date"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_error"

    # The classic non-autofill create still works.
    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={"name": "Rent", "frequency": "monthly", "next_due_date": "2026-10-01"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["amount"] is None


def test_blank_subscription_id_is_rejected_by_the_schema(
    client: TestClient, session: Session
) -> None:
    headers, _, _ = _setup(client)
    r = client.post("/api/v1/bills", headers=headers, json={"from_subscription_id": ""})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_error"

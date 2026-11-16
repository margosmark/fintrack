"""Transactions router: AND-composed filters, pagination, inline PATCH corrections."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.fx_rates import FxRate, FxSource
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.llm_suggestions import LlmSuggestion
from app.models.transactions import Transaction
from tests.test_cat_common import (
    auth,
    category_by_name,
    create_account,
    make_transaction,
    register_and_login,
)


def _seed(client: TestClient, session: Session) -> tuple[dict, int, dict, int]:
    """One user, two accounts, a spread of transactions.

    Returns ``(headers, acct1, cats, user_id)`` — the tenant id comes back too,
    because a category lookup is scoped by it (M178: the suite database always
    carries a foreign tenant's categories).
    """
    body = register_and_login(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    account_id = create_account(client, headers)
    account2_id = create_account(client, headers, name="Savings")
    groceries = category_by_name(session, "groceries", user_id)
    dining = category_by_name(session, "dining", user_id)
    make_transaction(
        session,
        user_id,
        account_id,
        "Loblaws",
        amount="-82.50",
        day=date(2026, 7, 2),
        category_id=groceries.id,
    )
    make_transaction(
        session,
        user_id,
        account_id,
        "Tim Hortons",
        raw="THTIMATONS 44",
        amount="-4.25",
        day=date(2026, 7, 10),
        category_id=dining.id,
    )
    make_transaction(
        session, user_id, account_id, "Mystery charge", amount="-120.00", day=date(2026, 8, 1)
    )
    make_transaction(
        session, user_id, account2_id, "Payroll deposit", amount="2500.00", day=date(2026, 7, 15)
    )
    return headers, account_id, {"groceries": groceries.id, "dining": dining.id}, user_id


def test_list_returns_paginated_envelope(client: TestClient, session: Session) -> None:
    headers, _, _, _user_id = _seed(client, session)
    body = client.get("/api/v1/transactions?limit=3", headers=headers).json()
    # M107 (docs/44 §4.1): `split_parents` is ADDITIVE — every key the page
    # answered with before is still there, plus the group headers (`[]` when
    # the page carries no children, which is what this seed has).
    assert set(body) == {"items", "total", "limit", "offset", "split_parents", "withheld"}
    assert body["split_parents"] == []
    assert body["total"] == 4 and len(body["items"]) == 3


def test_limit_above_max_is_422(client: TestClient, session: Session) -> None:
    headers, _, _, _user_id = _seed(client, session)
    response = client.get("/api/v1/transactions?limit=201", headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_filter_account_and_category_compose(client: TestClient, session: Session) -> None:
    headers, account_id, cats, _ = _seed(client, session)
    # account AND categorized -> only the Loblaws row.
    response = client.get(
        f"/api/v1/transactions?account_id={account_id}&category_id={cats['groceries']}",
        headers=headers,
    )
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["normalized_payee"] == "Loblaws"


def test_filter_uncategorized_via_null(client: TestClient, session: Session) -> None:
    headers, _, _, _user_id = _seed(client, session)
    response = client.get("/api/v1/transactions?category_id=null", headers=headers)
    payees = {i["normalized_payee"] for i in response.json()["items"]}
    assert payees == {"Mystery charge", "Payroll deposit"}


def test_filter_date_range_and_amount_range(client: TestClient, session: Session) -> None:
    headers, _, _, _user_id = _seed(client, session)
    july = client.get("/api/v1/transactions?from=2026-07-01&to=2026-07-31", headers=headers).json()
    assert july["total"] == 3
    big = client.get("/api/v1/transactions?amount_min=-100&amount_max=-50", headers=headers).json()
    assert big["total"] == 1
    assert big["items"][0]["amount_native"] == "-82.50"


def test_filter_payee_matches_normalized_or_raw_description(
    client: TestClient, session: Session
) -> None:
    headers, _, _, _user_id = _seed(client, session)
    hits = client.get("/api/v1/transactions?payee=tim", headers=headers).json()
    assert hits["total"] == 1  # matched via normalized_payee case-insensitively
    raw_hits = client.get("/api/v1/transactions?payee=THTIMATONS", headers=headers).json()
    assert raw_hits["total"] == 1  # matched via raw_description
    none = client.get("/api/v1/transactions?payee=zzz-nothing", headers=headers).json()
    assert none["total"] == 0


def test_filters_all_compose_to_empty(client: TestClient, session: Session) -> None:
    headers, account_id, _, _ = _seed(client, session)
    response = client.get(
        f"/api/v1/transactions?account_id={account_id}&category_id=null&from=2026-07-01"
        "&to=2026-07-31&payee=mystery&amount_max=-200",
        headers=headers,
    )
    assert response.json()["total"] == 0  # Mystery is August; filters still valid


def test_bad_category_filter_rejected(client: TestClient, session: Session) -> None:
    headers, _, _, _user_id = _seed(client, session)
    response = client.get("/api/v1/transactions?category_id=bogus", headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_patch_assigns_category(client: TestClient, session: Session) -> None:
    headers, _, _, user_id = _seed(client, session)
    uncategorized = client.get(
        "/api/v1/transactions?category_id=null&payee=Mystery", headers=headers
    ).json()["items"][0]
    response = client.patch(
        f"/api/v1/transactions/{uncategorized['id']}",
        json={"category_id": category_by_name(session, "housing", user_id).id},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["category_id"] == category_by_name(session, "housing", user_id).id


def test_patch_clears_category_with_null(client: TestClient, session: Session) -> None:
    headers, _, _, user_id = _seed(client, session)
    categorized = client.get(
        f"/api/v1/transactions?category_id={category_by_name(session, 'groceries', user_id).id}",
        headers=headers,
    ).json()["items"][0]
    response = client.patch(
        f"/api/v1/transactions/{categorized['id']}", json={"category_id": None}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["category_id"] is None


def test_patch_corrects_normalized_payee(client: TestClient, session: Session) -> None:
    headers, _, _, _user_id = _seed(client, session)
    txn = client.get("/api/v1/transactions?payee=Mystery", headers=headers).json()["items"][0]
    response = client.patch(
        f"/api/v1/transactions/{txn['id']}",
        json={"normalized_payee": "Unknown EFT"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["normalized_payee"] == "Unknown EFT"


def test_patch_foreign_category_404(client: TestClient, session: Session) -> None:
    headers, _, _, _user_id = _seed(client, session)
    txn = client.get("/api/v1/transactions", headers=headers).json()["items"][0]
    response = client.patch(
        f"/api/v1/transactions/{txn['id']}", json={"category_id": 999999}, headers=headers
    )
    assert response.status_code == 404


def test_patch_missing_transaction_404(client: TestClient, session: Session) -> None:
    headers, _, _, _user_id = _seed(client, session)
    missing = client.patch("/api/v1/transactions/999999", json={}, headers=headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


def test_transactions_require_auth(client: TestClient) -> None:
    assert client.get("/api/v1/transactions").status_code == 401


def test_manual_create_defaults_currency_and_uses_exact_cached_fx(
    client: TestClient, session: Session
) -> None:
    headers, account_id, cats, _ = _seed(client, session)
    created = client.post(
        "/api/v1/transactions",
        json={
            "account_id": account_id,
            "date": "2026-08-20",
            "raw_description": "  New   purchase  ",
            "amount_native": "-10.00",
            "category_id": cats["groceries"],
        },
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["currency"] == "CAD"
    assert created.json()["normalized_payee"] == "New purchase"
    assert created.json()["cad_amount"] == "-10.00"

    usd = client.post(
        "/api/v1/accounts",
        json={"name": "USD", "type": "chequing", "currency": "USD"},
        headers=headers,
    ).json()["id"]
    session.add(
        FxRate(
            base="USD",
            quote="CAD",
            rate=Decimal("1.35"),
            date=date(2026, 8, 20),
            source=FxSource.manual,
        )
    )
    session.commit()
    converted = client.post(
        "/api/v1/transactions",
        json={
            "account_id": usd,
            "date": "2026-08-20",
            "raw_description": "USD purchase",
            "amount_native": "-10.00",
        },
        headers=headers,
    )
    assert converted.status_code == 201
    assert converted.json()["cad_amount"] == "-13.50"

    unresolved = client.post(
        "/api/v1/transactions",
        json={
            "account_id": usd,
            "date": "2026-08-21",
            "raw_description": "No rate",
            "amount_native": "-2.00",
        },
        headers=headers,
    )
    assert unresolved.status_code == 201
    assert unresolved.json()["cad_amount"] is None


def test_create_duplicate_and_update_recompute_identity(
    client: TestClient, session: Session
) -> None:
    headers, account_id, _, _ = _seed(client, session)
    payload = {
        "account_id": account_id,
        "date": "2026-09-01",
        "raw_description": "Unique ledger row",
        "amount_native": "-11.00",
    }
    created = client.post("/api/v1/transactions", json=payload, headers=headers)
    assert created.status_code == 201
    duplicate = client.post("/api/v1/transactions", json=payload, headers=headers)
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "duplicate_transaction"
    updated = client.patch(
        f"/api/v1/transactions/{created.json()['id']}",
        json={"date": "2026-09-02", "amount_native": "-20.00", "raw_description": "Moved row"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["date"] == "2026-09-02"
    assert updated.json()["amount_native"] == "-20.00"
    assert updated.json()["cad_amount"] == "-20.00"


def test_create_tenancy_and_update_duplicate_conflicts(
    client: TestClient, session: Session
) -> None:
    headers, account_id, _, _ = _seed(client, session)
    foreign_account = client.post(
        "/api/v1/accounts",
        json={"name": "Second", "type": "chequing", "currency": "CAD"},
        headers=headers,
    ).json()["id"]
    base = {
        "date": "2026-09-05",
        "raw_description": "Same row",
        "amount_native": "-12.00",
    }
    first = client.post(
        "/api/v1/transactions", json={**base, "account_id": account_id}, headers=headers
    )
    second = client.post(
        "/api/v1/transactions", json={**base, "account_id": foreign_account}, headers=headers
    )
    assert first.status_code == 201 and second.status_code == 201
    moved = client.patch(
        f"/api/v1/transactions/{first.json()['id']}",
        json={"account_id": foreign_account},
        headers=headers,
    )
    assert moved.status_code == 409
    assert moved.json()["code"] == "duplicate_transaction"
    bad_account = client.post(
        "/api/v1/transactions", json={**base, "account_id": 999999}, headers=headers
    )
    assert bad_account.status_code == 404
    bad_category = client.post(
        "/api/v1/transactions",
        json={**base, "account_id": account_id, "category_id": 999999},
        headers=headers,
    )
    assert bad_category.status_code == 404


def test_pending_rows_are_hidden_and_protected(client: TestClient, session: Session) -> None:
    headers, account_id, _, _ = _seed(client, session)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    batch = ImportBatch(
        user_id=user_id,
        account_id=account_id,
        institution="",
        file_name="pending.qfx",
        status=ImportBatchStatus.pending_review,
    )
    session.add(batch)
    session.flush()
    pending = Transaction(
        user_id=user_id,
        account_id=account_id,
        date=date(2026, 9, 3),
        raw_description="Pending",
        normalized_payee="Pending",
        amount_native=Decimal("-3.00"),
        currency="CAD",
        import_batch_id=batch.id,
        dedup_hash="pending-hash",
    )
    session.add(pending)
    session.commit()
    assert client.get("/api/v1/transactions", headers=headers).json()["total"] == 4
    response = client.patch(
        f"/api/v1/transactions/{pending.id}", json={"raw_description": "Nope"}, headers=headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "transaction_in_pending_batch"
    response = client.delete(f"/api/v1/transactions/{pending.id}", headers=headers)
    assert response.status_code == 409


def test_delete_cleans_suggestion_and_unlinks_pair(client: TestClient, session: Session) -> None:
    headers, account_id, cats, _ = _seed(client, session)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    other_account = create_account(client, headers, name="Other")
    first = make_transaction(session, user_id, account_id, "Delete me", amount="-7.00")
    second = make_transaction(
        session, user_id, other_account, "Counterpart", amount="7.00", category_id=cats["dining"]
    )
    first.transfer_pair_id = second.id
    second.transfer_pair_id = first.id
    session.add(
        LlmSuggestion(
            user_id=user_id,
            transaction_id=first.id,
            proposed_category_id=cats["dining"],
            confidence=0.9,
            model_used="test",
        )
    )
    session.add(first)
    session.add(second)
    session.commit()
    response = client.delete(f"/api/v1/transactions/{first.id}", headers=headers)
    assert response.status_code == 204
    session.expire_all()
    assert session.get(Transaction, first.id) is None
    counterpart = session.get(Transaction, second.id)
    assert counterpart is not None and counterpart.transfer_pair_id is None
    assert (
        session.exec(select(LlmSuggestion).where(LlmSuggestion.transaction_id == first.id)).first()
        is None
    )

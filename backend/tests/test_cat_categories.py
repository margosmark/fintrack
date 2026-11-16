"""Categories CRUD: pagination, hierarchy, is_system rename-only, five delete guards."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.categories import Category
from app.models.learned_payees import LearnedPayee
from app.models.llm_suggestions import LlmSuggestion
from app.models.rules import Rule, RuleMatchType
from tests.test_cat_common import (
    auth,
    create_account,
    make_transaction,
    register_and_login,
)


def _setup(client: TestClient) -> tuple[dict, dict]:
    body = register_and_login(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    return headers, {"id": user_id}


def _leaf(client: TestClient, headers: dict, name: str = "Leaf") -> dict:
    """A non-system category (the seeded defaults are all is_system)."""
    response = client.post("/api/v1/categories", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_list_categories_paginated(client: TestClient) -> None:
    headers, _ = _setup(client)
    response = client.get("/api/v1/categories?limit=3&offset=2", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 10  # seeded system defaults (docs/02)
    assert len(body["items"]) == 3
    assert body["limit"] == 3 and body["offset"] == 2


def test_create_and_get_category_with_parent(client: TestClient) -> None:
    headers, _ = _setup(client)
    items = client.get("/api/v1/categories", headers=headers).json()["items"]
    parent = next(c for c in items if c["name"] == "groceries")
    response = client.post(
        "/api/v1/categories",
        json={"name": "Coffee beans", "parent_id": parent["id"]},
        headers=headers,
    )
    assert response.status_code == 201
    child = response.json()
    assert child["parent_id"] == parent["id"]
    assert child["is_system"] is False

    fetched = client.get(f"/api/v1/categories/{child['id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Coffee beans"


def test_create_category_foreign_parent_404(client: TestClient) -> None:
    headers, _ = _setup(client)
    response = client.post(
        "/api/v1/categories", json={"name": "x", "parent_id": 999999}, headers=headers
    )
    assert response.status_code == 404


def test_patch_rename_and_move(client: TestClient) -> None:
    headers, _ = _setup(client)
    items = client.get("/api/v1/categories", headers=headers).json()["items"]
    dining = next(c for c in items if c["name"] == "dining")
    housing = next(c for c in items if c["name"] == "housing")

    renamed = client.patch(
        f"/api/v1/categories/{dining['id']}", json={"name": "Restaurants"}, headers=headers
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Restaurants"

    moved = client.patch(
        f"/api/v1/categories/{dining['id']}", json={"parent_id": housing["id"]}, headers=headers
    )
    assert moved.status_code == 200
    assert moved.json()["parent_id"] == housing["id"]

    # Cycle guard: housing cannot become a child of its own descendant.
    cycle = client.patch(
        f"/api/v1/categories/{housing['id']}", json={"parent_id": dining["id"]}, headers=headers
    )
    assert cycle.status_code == 422
    assert cycle.json()["code"] == "category_cycle"


def test_self_parent_cycle_rejected(client: TestClient) -> None:
    headers, _ = _setup(client)
    leaf = _leaf(client, headers)
    response = client.patch(
        f"/api/v1/categories/{leaf['id']}", json={"parent_id": leaf["id"]}, headers=headers
    )
    assert response.status_code == 422


def test_system_category_rename_ok_delete_rejected(client: TestClient) -> None:
    headers, _ = _setup(client)
    items = client.get("/api/v1/categories", headers=headers).json()["items"]
    groceries = next(c for c in items if c["name"] == "groceries")
    renamed = client.patch(
        f"/api/v1/categories/{groceries['id']}", json={"name": "Groceries"}, headers=headers
    )
    assert renamed.status_code == 200
    deleted = client.delete(f"/api/v1/categories/{groceries['id']}", headers=headers)
    assert deleted.status_code == 409
    assert deleted.json()["code"] == "system_category"


def _delete_expect_in_use(client: TestClient, headers: dict, category_id: int) -> None:
    response = client.delete(f"/api/v1/categories/{category_id}", headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "category_in_use"


def test_delete_guard_transaction_reference(client: TestClient, session: Session) -> None:
    headers, user = _setup(client)
    account_id = create_account(client, headers)
    cat = _leaf(client, headers)
    make_transaction(session, user["id"], account_id, "Store", category_id=cat["id"])
    _delete_expect_in_use(client, headers, cat["id"])


def test_delete_guard_child_category(client: TestClient, session: Session) -> None:
    headers, user = _setup(client)
    parent = _leaf(client, headers)
    session.add(Category(user_id=user["id"], name="Child", parent_id=parent["id"]))
    session.commit()
    _delete_expect_in_use(client, headers, parent["id"])


def test_delete_guard_rule_reference(client: TestClient, session: Session) -> None:
    headers, user = _setup(client)
    cat = _leaf(client, headers)
    session.add(
        Rule(
            user_id=user["id"],
            match_type=RuleMatchType.contains,
            pattern="hydro",
            priority=1,
            category_id=cat["id"],
        )
    )
    session.commit()
    _delete_expect_in_use(client, headers, cat["id"])


def test_delete_guard_learned_payee_reference(client: TestClient, session: Session) -> None:
    headers, user = _setup(client)
    cat = _leaf(client, headers)
    session.add(
        LearnedPayee(user_id=user["id"], normalized_payee="pharmacy", category_id=cat["id"])
    )
    session.commit()
    _delete_expect_in_use(client, headers, cat["id"])


def test_delete_guard_suggestion_reference(client: TestClient, session: Session) -> None:
    headers, user = _setup(client)
    account_id = create_account(client, headers)
    cat = _leaf(client, headers)
    txn = make_transaction(session, user["id"], account_id, "Cinema")
    session.add(
        LlmSuggestion(
            user_id=user["id"],
            transaction_id=txn.id,
            proposed_category_id=cat["id"],
            confidence=0.5,
            model_used="fake-model",
        )
    )
    session.commit()
    _delete_expect_in_use(client, headers, cat["id"])


def test_delete_unused_category_succeeds(client: TestClient, session: Session) -> None:
    headers, _ = _setup(client)
    created = _leaf(client, headers, name="Temp")
    response = client.delete(f"/api/v1/categories/{created['id']}", headers=headers)
    assert response.status_code == 204
    assert session.exec(select(Category).where(Category.id == created["id"])).first() is None


def test_category_routes_scoped_to_owner(client: TestClient) -> None:
    first_headers, _ = _setup(client)
    client.post(
        "/api/v1/auth/register",
        json={"email": "other-cat@example.com", "password": "pw-horse"},
    )
    other = client.post(
        "/api/v1/auth/login",
        json={"email": "other-cat@example.com", "password": "pw-horse"},
    )
    assert other.status_code == 200
    other_headers = auth(other.json()["access_token"])
    mine = client.get("/api/v1/categories", headers=first_headers).json()["items"][0]
    foreign = client.post("/api/v1/categories", json={"name": "Yours"}, headers=other_headers)

    # Cross-user reads/mutations 404.
    assert client.get(f"/api/v1/categories/{mine['id']}", headers=other_headers).status_code == 404
    assert (
        client.delete(f"/api/v1/categories/{mine['id']}", headers=other_headers).status_code == 404
    )
    # A second user may create their own category with the same name (per-user copies).
    assert foreign.status_code == 201

"""Rules CRUD: validation (regex compilability), priority ordering, ownership."""

from fastapi.testclient import TestClient

from tests.test_cat_common import auth, register_and_login


def _setup(client: TestClient) -> tuple[dict, int]:
    body = register_and_login(client)
    headers = auth(body["access_token"])
    groceries = next(
        c
        for c in client.get("/api/v1/categories", headers=headers).json()["items"]
        if c["name"] == "groceries"
    )
    return headers, groceries["id"]


def _rule_payload(category_id: int, **overrides) -> dict:
    payload = {
        "match_type": "contains",
        "pattern": "loblaws",
        "priority": 10,
        "category_id": category_id,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def test_create_get_list_rule(client: TestClient) -> None:
    headers, groceries_id = _setup(client)
    created = client.post("/api/v1/rules", json=_rule_payload(groceries_id), headers=headers)
    assert created.status_code == 201
    rule = created.json()
    assert rule["match_type"] == "contains" and rule["enabled"] is True

    fetched = client.get(f"/api/v1/rules/{rule['id']}", headers=headers)
    assert fetched.status_code == 200 and fetched.json()["id"] == rule["id"]

    listed = client.get("/api/v1/rules", headers=headers).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == rule["id"]


def test_empty_pattern_422(client: TestClient) -> None:
    headers, groceries_id = _setup(client)
    response = client.post(
        "/api/v1/rules", json=_rule_payload(groceries_id, pattern=""), headers=headers
    )
    assert response.status_code == 422


def test_invalid_regex_pattern_422(client: TestClient) -> None:
    headers, groceries_id = _setup(client)
    response = client.post(
        "/api/v1/rules",
        json=_rule_payload(groceries_id, match_type="regex", pattern="([unclosed"),
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_regex"


def test_valid_regex_accepted(client: TestClient) -> None:
    headers, groceries_id = _setup(client)
    response = client.post(
        "/api/v1/rules",
        json=_rule_payload(groceries_id, match_type="regex", pattern="^lob.*s$"),
        headers=headers,
    )
    assert response.status_code == 201


def test_regex_becomes_invalid_on_patch(client: TestClient) -> None:
    headers, groceries_id = _setup(client)
    rule = client.post("/api/v1/rules", json=_rule_payload(groceries_id), headers=headers).json()
    response = client.patch(
        f"/api/v1/rules/{rule['id']}",
        json={"match_type": "regex", "pattern": "*bad"},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_regex"


def test_foreign_category_for_rule_404(client: TestClient) -> None:
    headers, _ = _setup(client)
    response = client.post("/api/v1/rules", json=_rule_payload(999999), headers=headers)
    assert response.status_code == 404


def test_rules_sorted_by_priority_then_id(client: TestClient) -> None:
    headers, groceries_id = _setup(client)
    low_first = client.post(
        "/api/v1/rules",
        json=_rule_payload(groceries_id, pattern="a", priority=5),
        headers=headers,
    ).json()
    high_second = client.post(
        "/api/v1/rules",
        json=_rule_payload(groceries_id, pattern="b", priority=50),
        headers=headers,
    ).json()
    tie_low_id = client.post(
        "/api/v1/rules",
        json=_rule_payload(groceries_id, pattern="c", priority=50),
        headers=headers,
    ).json()
    listed = [
        r["id"] for r in client.get("/api/v1/rules?limit=200", headers=headers).json()["items"]
    ]
    assert listed == [low_first["id"], high_second["id"], tie_low_id["id"]]


def test_patch_enable_disable_and_delete(client: TestClient) -> None:
    headers, groceries_id = _setup(client)
    rule = client.post("/api/v1/rules", json=_rule_payload(groceries_id), headers=headers).json()
    toggled = client.patch(f"/api/v1/rules/{rule['id']}", json={"enabled": False}, headers=headers)
    assert toggled.json()["enabled"] is False
    deleted = client.delete(f"/api/v1/rules/{rule['id']}", headers=headers)
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/rules/{rule['id']}", headers=headers).status_code == 404


def test_rule_routes_scoped_to_owner(client: TestClient) -> None:
    headers, groceries_id = _setup(client)
    rule = client.post("/api/v1/rules", json=_rule_payload(groceries_id), headers=headers).json()
    client.post(
        "/api/v1/auth/register",
        json={"email": "rules-other@example.com", "password": "pw-horse"},
    )
    other = client.post(
        "/api/v1/auth/login",
        json={"email": "rules-other@example.com", "password": "pw-horse"},
    )
    other_headers = auth(other.json()["access_token"])
    assert client.get(f"/api/v1/rules/{rule['id']}", headers=other_headers).status_code == 404
    assert (
        client.patch(
            f"/api/v1/rules/{rule['id']}", json={"pattern": "x"}, headers=other_headers
        ).status_code
        == 404
    )

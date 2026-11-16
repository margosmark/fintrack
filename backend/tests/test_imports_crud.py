"""institutions + accounts CRUD tests (docs/08 pagination contract, M2 scope)."""

from fastapi.testclient import TestClient

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery"

VALID_PROFILE = {
    "columns": {
        "date": "Trans. Date",
        "amount_native": "Amount",
        "raw_description": "Description",
    },
    "date_format": "%d/%m/%Y",
    "decimal_comma": True,
    "credits_positive": False,
}


def _register(client: TestClient, email: str = EMAIL) -> dict:
    client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- institutions -----------------------------------------------------------


def _create_institution(client: TestClient, token: str, **overrides) -> dict:
    payload = {"name": "Test Bank", **overrides}
    if "profile" in overrides:
        payload["importer_profile"] = overrides.pop("profile")
    response = client.post("/api/v1/institutions", json=payload, headers=_headers(token))
    assert response.status_code == 201, response.text
    return response.json()


def _create_institution(client: TestClient, token: str, **overrides) -> dict:
    profile = overrides.pop("profile", None)
    payload = {"name": "Test Bank", "importer_profile": profile, **overrides}
    response = client.post("/api/v1/institutions", json=payload, headers=_headers(token))
    assert response.status_code == 201, response.text
    return response.json()


def test_institution_profile_unknown_keys_rejected(client: TestClient) -> None:
    auth = _register(client)
    bad = {**VALID_PROFILE, "decimal_point": ","}  # unknown top-level key
    response = client.post(
        "/api/v1/institutions",
        json={"name": "Bad Bank", "importer_profile": bad},
        headers=_headers(auth["access_token"]),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_institution_profile_missing_required_columns_rejected(
    client: TestClient,
) -> None:
    auth = _register(client)
    bad = {"columns": {"date": "Date"}, "credits_positive": False}
    response = client.post(
        "/api/v1/institutions",
        json={"name": "Bad Bank", "importer_profile": bad},
        headers=_headers(auth["access_token"]),
    )
    assert response.status_code == 422


def test_institution_list_pagination_shape_and_limits(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    for n in range(3):
        _create_institution(client, token, name=f"Bank {n}")

    default_page = client.get("/api/v1/institutions", headers=_headers(token))
    assert default_page.status_code == 200
    body = default_page.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 3
    assert body["limit"] == 50 and body["offset"] == 0
    assert len(body["items"]) == 3

    paged = client.get("/api/v1/institutions?limit=2&offset=1", headers=_headers(token))
    items = paged.json()["items"]
    assert [i["name"] for i in items] == ["Bank 1", "Bank 2"]

    too_big = client.get("/api/v1/institutions?limit=201", headers=_headers(token))
    assert too_big.status_code == 422


def test_institution_get_patch_delete_lifecycle(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    inst = _create_institution(client, token)

    fetched = client.get(f"/api/v1/institutions/{inst['id']}", headers=_headers(token))
    assert fetched.status_code == 200
    assert fetched.json()["id"] == inst["id"]

    patched = client.patch(
        f"/api/v1/institutions/{inst['id']}",
        json={"name": "Renamed Bank"},
        headers=_headers(token),
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed Bank"

    deleted = client.delete(f"/api/v1/institutions/{inst['id']}", headers=_headers(token))
    assert deleted.status_code == 204
    gone = client.get(f"/api/v1/institutions/{inst['id']}", headers=_headers(token))
    assert gone.status_code == 404
    assert gone.json()["code"] == "not_found"


def test_institution_tenancy_isolation(client: TestClient) -> None:
    first = _register(client)
    second = _register(client, email="bob@example.com")
    inst = _create_institution(client, first["access_token"])
    foreign = client.get(
        f"/api/v1/institutions/{inst['id']}", headers=_headers(second["access_token"])
    )
    assert foreign.status_code == 404


def test_institution_with_accounts_cannot_be_deleted(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    inst = _create_institution(client, token)
    account = client.post(
        "/api/v1/accounts",
        json={
            "name": "Checking",
            "type": "chequing",
            "currency": "CAD",
            "institution_id": inst["id"],
        },
        headers=_headers(token),
    )
    assert account.status_code == 201
    blocked = client.delete(f"/api/v1/institutions/{inst['id']}", headers=_headers(token))
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "institution_in_use"


# --- accounts ---------------------------------------------------------------


def _create_account(client: TestClient, token: str, **overrides) -> dict:
    payload = {
        "name": "Checking",
        "type": "chequing",
        "currency": "CAD",
        **overrides,
    }
    response = client.post("/api/v1/accounts", json=payload, headers=_headers(token))
    assert response.status_code == 201, response.text
    return response.json()


def test_account_create_normalizes_currency_and_defaults(client: TestClient) -> None:
    auth = _register(client)
    body = _create_account(client, auth["access_token"], currency="usd")
    assert body["currency"] == "USD"
    assert body["taxable"] is False
    assert body["llm_autopilot"] is False


def test_account_requires_owned_institution(client: TestClient) -> None:
    first = _register(client)
    second = _register(client, email="bob@example.com")
    foreign_inst = _create_institution(client, second["access_token"])
    response = client.post(
        "/api/v1/accounts",
        json={
            "name": "Checking",
            "type": "chequing",
            "currency": "CAD",
            "institution_id": foreign_inst["id"],
        },
        headers=_headers(first["access_token"]),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_account_crud_lifecycle_and_pagination(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    created = []
    for n in range(3):
        created.append(_create_account(client, token, name=f"Acct {n}"))

    listing = client.get("/api/v1/accounts", headers=_headers(token)).json()
    assert listing["total"] == 3
    assert listing["limit"] == 50 and listing["offset"] == 0

    # M9 (docs/14 C1): taxable is server-derived; flipping type flips taxability.
    patched = client.patch(
        f"/api/v1/accounts/{created[0]['id']}",
        json={"type": "brokerage_non_registered"},
        headers=_headers(token),
    )
    assert patched.status_code == 200
    assert patched.json()["taxable"] is True

    fetched = client.get(f"/api/v1/accounts/{created[0]['id']}", headers=_headers(token))
    assert fetched.status_code == 200

    empty = created[-1]
    deleted = client.delete(f"/api/v1/accounts/{empty['id']}", headers=_headers(token))
    assert deleted.status_code == 204
    gone = client.get(f"/api/v1/accounts/{empty['id']}", headers=_headers(token))
    assert gone.status_code == 404


def test_account_with_transactions_cannot_be_deleted(client: TestClient) -> None:
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures"
    auth = _register(client)
    token = auth["access_token"]
    profile = {
        "columns": {
            "date": "Trans. Date",
            "amount_native": "Amount",
            "raw_description": "Description",
        },
        "date_format": "%d/%m/%Y",
        "decimal_comma": True,
        "credits_positive": False,
    }
    inst = _create_institution(client, token, profile=profile)
    account = client.post(
        "/api/v1/accounts",
        json={
            "name": "Checking",
            "type": "chequing",
            "currency": "CAD",
            "institution_id": inst["id"],
        },
        headers=_headers(token),
    ).json()
    upload = client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data={"account_id": str(account["id"])},
        files={
            "file": (
                "csv_decimal_comma.csv",
                (fixtures / "csv_decimal_comma.csv").read_bytes(),
                "text/csv",
            )
        },
    )
    assert upload.status_code == 201

    blocked = client.delete(f"/api/v1/accounts/{account['id']}", headers=_headers(token))
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "account_in_use"

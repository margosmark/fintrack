"""E2EE Phase 2 field tests (docs/33): opaque passthrough, enforcement, token filter."""

from fastapi.testclient import TestClient


def _register(client: TestClient, email: str) -> dict:
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_account(client: TestClient, token: str, **overrides) -> dict:
    payload = {"name": "Main", "type": "chequing", "currency": "CAD", **overrides}
    response = client.post("/api/v1/accounts", json=payload, headers=_headers(token))
    assert response.status_code == 201, response.text
    return response.json()


def _make_tx(client: TestClient, token: str, account_id: int, **overrides) -> tuple:
    payload = {
        "account_id": account_id,
        "date": "2026-01-15",
        "raw_description": "GROCERY STORE",
        "amount_native": "-12.50",
        **overrides,
    }
    response = client.post("/api/v1/transactions", json=payload, headers=_headers(token))
    return response.json(), response.status_code


def test_cipher_passthrough_round_trip(client: TestClient) -> None:
    auth = _register(client, "e2ee-roundtrip@example.com")
    token = auth["access_token"]
    account = _make_account(client, token, name_cipher="v1.iv.namect")
    assert account["name_cipher"] == "v1.iv.namect"

    body, status = _make_tx(
        client,
        token,
        account["id"],
        payee_cipher="v1.iv.payct",
        raw_desc_cipher="v1.iv.rawct",
        amount_cipher="v1.iv.amtct",
        cad_amount_cipher="v1.iv.cadct",
        payee_token="tok-abc",
    )
    assert status == 201, body
    assert body["payee_cipher"] == "v1.iv.payct"
    assert body["raw_desc_cipher"] == "v1.iv.rawct"
    assert body["amount_cipher"] == "v1.iv.amtct"
    assert body["cad_amount_cipher"] == "v1.iv.cadct"
    assert body["payee_token"] == "tok-abc"


def test_oversize_cipher_rejected(client: TestClient) -> None:
    auth = _register(client, "e2ee-oversize@example.com")
    token = auth["access_token"]
    account = _make_account(client, token)
    body, status = _make_tx(client, token, account["id"], payee_cipher="x" * 8001)
    assert status == 422
    assert body["code"] == "invalid_cipher"


def test_encryption_required_enforcement(client: TestClient) -> None:
    auth = _register(client, "e2ee-enforce@example.com")
    token = auth["access_token"]
    headers = _headers(token)

    # Flag set via PATCH /users/me.
    response = client.patch("/api/v1/users/me", json={"encryption_required": True}, headers=headers)
    assert response.status_code == 200
    assert response.json()["encryption_required"] is True

    # Account without cipher → 422, then with cipher → 201.
    response = client.post(
        "/api/v1/accounts",
        json={"name": "Vault", "type": "chequing", "currency": "CAD"},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "encryption_required"
    account = _make_account(client, token, name="Vault", name_cipher="v1.iv.acct")
    assert account["name_cipher"] == "v1.iv.acct"

    # Transaction without ciphers → 422, then with ciphers → 201.
    body, status = _make_tx(client, token, account["id"])
    assert status == 422
    assert body["code"] == "encryption_required"
    body, status = _make_tx(
        client,
        token,
        account["id"],
        payee_cipher="v1.iv.p",
        amount_cipher="v1.iv.a",
    )
    assert status == 201, body


def test_payee_token_filter_tenant_scoped(client: TestClient) -> None:
    auth_a = _register(client, "e2ee-token-a@example.com")
    auth_b = _register(client, "e2ee-token-b@example.com")
    acct_a = _make_account(client, auth_a["access_token"])
    acct_b = _make_account(client, auth_b["access_token"])

    _make_tx(
        client,
        auth_a["access_token"],
        acct_a["id"],
        payee_token="tok-shared",
        raw_description="AAA ONE",
    )
    _make_tx(
        client,
        auth_a["access_token"],
        acct_a["id"],
        payee_token="tok-other",
        raw_description="AAA TWO",
    )
    # Same token value under another tenant must not leak.
    _make_tx(
        client,
        auth_b["access_token"],
        acct_b["id"],
        payee_token="tok-shared",
        raw_description="BBB ONE",
    )

    response = client.get(
        "/api/v1/transactions",
        params={"payee_token": "tok-shared"},
        headers=_headers(auth_a["access_token"]),
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["payee_token"] == "tok-shared"
    assert items[0]["raw_description"] == "AAA ONE"

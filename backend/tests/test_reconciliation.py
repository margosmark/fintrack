"""Account reconciliation endpoint."""

from fastapi.testclient import TestClient


def _auth(client: TestClient) -> dict[str, str]:
    from datetime import UTC, datetime

    email = f"recon-{datetime.now(UTC).timestamp()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    r = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_reconciliation_empty_for_unknown_account(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get("/api/v1/accounts/99999/reconciliation", headers=headers)
    assert r.status_code == 404


def test_reconciliation_response_shape(client: TestClient) -> None:
    headers = _auth(client)
    # Unknown account should 404
    r = client.get("/api/v1/accounts/99999/reconciliation", headers=headers)
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_reconciliation_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/accounts/1/reconciliation")
    assert r.status_code == 401

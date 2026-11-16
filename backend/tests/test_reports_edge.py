"""Edge tests for reports endpoints: invalid filter handling (docs/12).

Confirms a malformed/inverted date range surfaces as a 422
`invalid_date_range` envelope (not a 500) and that a valid range returns 200.
"""

from fastapi.testclient import TestClient

EMAIL = "reports-edge@example.com"
PASSWORD = "correct-horse-battery"


def _register(client: TestClient, email: str = EMAIL) -> dict:
    client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_spending_rejects_inverted_date_range_422(client: TestClient) -> None:
    auth = _register(client)
    resp = client.get(
        "/api/v1/reports/spending?from=2025-02-01&to=2025-01-01",
        headers=_headers(auth["access_token"]),
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_date_range"


def test_net_worth_rejects_inverted_date_range_422(client: TestClient) -> None:
    auth = _register(client)
    resp = client.get(
        "/api/v1/reports/net-worth?from=2025-02-01&to=2025-01-01",
        headers=_headers(auth["access_token"]),
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_date_range"


def test_spending_accepts_valid_range_200(client: TestClient) -> None:
    auth = _register(client)
    resp = client.get(
        "/api/v1/reports/spending?from=2025-01-01&to=2025-02-01",
        headers=_headers(auth["access_token"]),
    )
    assert resp.status_code == 200
    assert "items" in resp.json()

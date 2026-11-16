"""currency_tracker_enabled feature flag (PATCH /users/me round-trip)."""

from fastapi.testclient import TestClient

API = "/api/v1"
REGISTER = f"{API}/auth/register"
ME = f"{API}/users/me"

EMAIL = "flag-user@example.com"
PASSWORD = "correct-horse-battery"


def _register(client: TestClient) -> dict:
    client.post(REGISTER, json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    return response.json()


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_currency_flag_defaults_on_and_toggles(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    headers = _auth_headers(token)

    # Default: ON (tab stays visible for existing users).
    me = client.get(ME, headers=headers).json()
    assert me["currency_tracker_enabled"] is True

    # Turn OFF.
    patched = client.patch(ME, json={"currency_tracker_enabled": False}, headers=headers)
    assert patched.status_code == 200
    assert patched.json()["currency_tracker_enabled"] is False
    assert client.get(ME, headers=headers).json()["currency_tracker_enabled"] is False

    # Turn back ON.
    restored = client.patch(ME, json={"currency_tracker_enabled": True}, headers=headers)
    assert restored.status_code == 200
    assert restored.json()["currency_tracker_enabled"] is True


def test_currency_flag_is_per_user(client: TestClient) -> None:
    a = _register(client)
    # Second, distinct user (different email) — fresh row, independent flag.
    client.post(REGISTER, json={"email": "flag-other@example.com", "password": PASSWORD}).json()
    b = client.post(
        "/api/v1/auth/login", json={"email": "flag-other@example.com", "password": PASSWORD}
    ).json()
    b_headers = _auth_headers(b["access_token"])

    client.patch(
        ME, json={"currency_tracker_enabled": False}, headers=_auth_headers(a["access_token"])
    )
    # B's flag is untouched by A's patch.
    assert client.get(ME, headers=b_headers).json()["currency_tracker_enabled"] is True

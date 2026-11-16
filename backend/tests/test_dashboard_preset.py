"""dashboard_preset — M97 layout presets round-trip (docs/09 §Dashboard layout presets).

The preset is a server-side preference like every other `users` column: it
defaults to `overview` (the first-visit default the tour teaches on), survives
a fresh session — i.e. a reload — and is scoped to the user who set it. The
demo tenant's write guard already covers `PATCH /users/me`
(test_auth_security_hardening.py), so the shared demo cannot move it.
"""

from fastapi.testclient import TestClient

API = "/api/v1"
REGISTER = f"{API}/auth/register"
LOGIN = f"{API}/auth/login"
ME = f"{API}/users/me"
PASSWORD = "correct-horse-battery"


def _headers(client: TestClient, email: str) -> dict[str, str]:
    client.post(REGISTER, json={"email": email, "password": PASSWORD})
    login = client.post(LOGIN, json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_preset_defaults_to_overview_and_round_trips(client: TestClient) -> None:
    headers = _headers(client, "preset-user@example.com")
    assert client.get(ME, headers=headers).json()["dashboard_preset"] == "overview"

    for preset in ("import_first", "budget_first", "overview"):
        patched = client.patch(ME, json={"dashboard_preset": preset}, headers=headers)
        assert patched.status_code == 200, patched.text
        assert patched.json()["dashboard_preset"] == preset

        # A NEW session reads it back: persistence is the server's, not a tab's.
        fresh = _headers(client, "preset-user@example.com")
        assert client.get(ME, headers=fresh).json()["dashboard_preset"] == preset


def test_unknown_preset_is_rejected_and_changes_nothing(client: TestClient) -> None:
    headers = _headers(client, "preset-bad@example.com")
    bad = client.patch(ME, json={"dashboard_preset": "drag-and-drop"}, headers=headers)
    assert bad.status_code == 422
    assert client.get(ME, headers=headers).json()["dashboard_preset"] == "overview"


def test_preset_is_per_user(client: TestClient) -> None:
    a = _headers(client, "preset-a@example.com")
    client.post(REGISTER, json={"email": "preset-b@example.com", "password": PASSWORD})
    login = client.post(LOGIN, json={"email": "preset-b@example.com", "password": PASSWORD})
    b = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.patch(ME, json={"dashboard_preset": "budget_first"}, headers=a).status_code == 200
    assert client.get(ME, headers=b).json()["dashboard_preset"] == "overview"

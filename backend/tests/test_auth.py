"""Auth + users/me contract tests (m1-contract shapes).

Security-wave coverage on top of the original contract:
- M1: `refresh_token` never appears in a JSON response body — cookie only.
- M2: the refresh cookie carries HttpOnly/Secure/SameSite/Path attributes.
- L1: unknown-email login pays the same bcrypt cost (dummy verify), so login
  timing cannot be used to enumerate accounts.
- Refresh/logout are COOKIE-ONLY: a body-carried token is not accepted
  (cookie delivery is the whole point of the httpOnly design).
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.config import get_settings
from app.models.categories import Category
from app.models.users import User

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/users/me"
CHANGE_PW = "/api/v1/users/me/change-password"

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery"


@contextmanager
def _cookies_over_http() -> Iterator[None]:
    """Relax `Secure` so httpx will actually send the cookie over http.

    TestClient talks plain http and httpx drops Secure cookies on a non-TLS
    request, so with the production default on, a cookie-only assertion would
    silently exercise the "no cookie presented" path instead.
    """
    settings = get_settings()
    original = settings.cookie_secure
    settings.cookie_secure = False
    try:
        yield
    finally:
        settings.cookie_secure = original


def _register(client: TestClient, email: str = EMAIL) -> dict:
    client.post(REGISTER, json={"email": email, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    return response.json()


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_register_is_uniform_and_seeds_categories(client: TestClient, session: Session) -> None:
    """M87 (docs/29 F15): register answers the SAME body for a free address.

    The account and its 10 default categories ARE created (the recovery path
    is "sign in with the password you just chose") — but no session, no cookie
    and no token may come back, because a session on this branch and an error
    on the other is an account-existence oracle.
    """
    response = client.post(REGISTER, json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "detail": (
            "Registration accepted: if this address was free, an account now exists "
            "with the password you just chose. Sign in to continue."
        ),
    }
    # No credential of any kind rides along with the acceptance.
    assert "access_token" not in response.json()
    assert client.cookies.get("refresh_token") is None

    # Scoped by the tenant that just registered: this suite's database always
    # carries a foreign tenant's 10 default categories (conftest `_decoy_tenant`,
    # M178), so an unscoped read here is 20 rows and a red test on every run.
    registered = session.exec(select(User).where(User.email == EMAIL)).one()
    cats = session.exec(select(Category).where(Category.user_id == registered.id)).all()
    assert len(cats) == 10
    assert {c.name for c in cats} == {
        "groceries",
        "dining",
        "housing",
        "transport",
        "utilities",
        "health",
        "entertainment",
        "income",
        "transfers",
        "uncategorized",
    }
    assert all(c.is_system for c in cats)
    assert all(c.parent_id is None for c in cats)


def test_register_responder_is_consistent_for_free_and_taken_addresses(
    client: TestClient,
) -> None:
    """M87 responder-consistency: byte-identical answers for both addresses.

    This is the account-existence oracle (M83 F-7) pinned shut: same status,
    same body — not merely a similar shape — plus the side-effect contract
    that keeps the flow usable (a free address still ends up registered and
    sign-in-able with the password that was posted).
    """
    free = client.post(REGISTER, json={"email": EMAIL, "password": PASSWORD})
    taken = client.post(REGISTER, json={"email": EMAIL, "password": "other-password"})

    assert free.status_code == taken.status_code == 200
    assert free.json() == taken.json()

    # The first registration still works end-to-end...
    signed_in = client.post(LOGIN, json={"email": EMAIL, "password": PASSWORD})
    assert signed_in.status_code == 200
    assert signed_in.json()["user"]["email"] == EMAIL
    # ...and the second (duplicate) POST changed nothing: the new password was
    # NOT adopted for the existing account.
    client.cookies.clear()
    rejected = client.post(LOGIN, json={"email": EMAIL, "password": "other-password"})
    assert rejected.status_code == 401
    assert rejected.json()["code"] == "invalid_credentials"


def test_register_never_answers_duplicate_email(client: TestClient) -> None:
    """The `409 duplicate_email` code is gone from the register path (it used
    to be the oracle). `PATCH /users/me/profile` still legitimately raises it —
    see docs/20 — so pin the register route itself."""
    _register(client)
    response = client.post(REGISTER, json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    assert response.json().get("code") != "duplicate_email"


def test_login_wrong_password_unauthorized(client: TestClient) -> None:
    _register(client)
    response = client.post(LOGIN, json={"email": EMAIL, "password": "wrong-password"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


def test_login_success_returns_pair(client: TestClient) -> None:
    _register(client)
    response = client.post(LOGIN, json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    # M1: no raw refresh credential in the login body.
    assert "refresh_token" not in body
    assert client.cookies.get("refresh_token")
    assert body["user"]["email"] == EMAIL


def test_refresh_rotates_and_rejects_old_token(client: TestClient) -> None:
    """Rotation + reuse detection, driven by the httpOnly cookie alone.

    The body token is no longer an accepted channel, so every request below
    sends an empty body exactly as the browser client does.
    """
    with _cookies_over_http():
        _register(client)
        old_refresh = client.cookies.get("refresh_token")
        assert old_refresh

        first = client.post(REFRESH, json={})
        assert first.status_code == 200
        new_pair = first.json()
        # M1: rotation response is cookie-only too.
        assert "refresh_token" not in new_pair
        new_refresh = client.cookies.get("refresh_token")
        assert new_refresh and new_refresh != old_refresh

        # The presented token was revoked by rotation: replaying it must fail.
        client.cookies.set("refresh_token", old_refresh, path="/api/v1/auth")
        replay = client.post(REFRESH, json={})
        assert replay.status_code == 401
        assert replay.json()["code"] == "invalid_or_revoked_refresh_token"

        # Reuse detection (security-hardening): a replayed token means the chain
        # is compromised, so EVERY refresh token of the user is revoked —
        # including the successor issued by the legitimate rotation.
        client.cookies.set("refresh_token", new_refresh, path="/api/v1/auth")
        again = client.post(REFRESH, json={})
        assert again.status_code == 401
        assert again.json()["code"] == "invalid_or_revoked_refresh_token"


def test_logout_revokes_presented_refresh_token(client: TestClient) -> None:
    with _cookies_over_http():
        _register(client)
        refresh_token = client.cookies.get("refresh_token")
        assert refresh_token

        response = client.post(LOGOUT, json={})
        assert response.status_code == 204
        # Logout clears the cookie. (The Secure attribute tracks
        # settings.cookie_secure, which is off in this context; the
        # Secure-on-clearing contract is pinned in the M2 attributes test.)
        assert client.cookies.get("refresh_token") is None

        client.cookies.set("refresh_token", refresh_token, path="/api/v1/auth")
        after = client.post(REFRESH, json={})
        assert after.status_code == 401
        assert after.json()["code"] == "invalid_or_revoked_refresh_token"


def test_me_requires_auth_and_hides_secret_fields(client: TestClient) -> None:
    anonymous = client.get(ME)
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "invalid_token"

    body = _register(client)
    response = client.get(ME, headers=_auth_headers(body["access_token"]))
    assert response.status_code == 200
    user = response.json()
    assert "hashed_password" not in user
    assert "llm_api_key" not in user
    assert user["email"] == EMAIL


def test_me_never_exposes_stored_secrets(client: TestClient, session: Session) -> None:
    _register(client)
    user = session.exec(select(User).where(User.email == EMAIL)).one()
    assert user.hashed_password.startswith("$2")
    assert user.llm_api_key is None

    response = client.get(
        ME,
        headers=_auth_headers(
            client.post(LOGIN, json={"email": EMAIL, "password": PASSWORD}).json()["access_token"]
        ),
    )
    assert response.status_code == 200
    payload = response.json()
    assert "hashed_password" not in payload and "llm_api_key" not in payload


def test_patch_me_updates_preferences_write_only_api_key(
    client: TestClient,
) -> None:
    access = _register(client)["access_token"]

    response = client.patch(
        ME,
        headers=_auth_headers(access),
        json={
            "llm_provider": "openai_compatible",
            "llm_base_url": "https://api.example.com/v1",
            "llm_model": "gpt-4o-mini",
            "llm_api_key": "sk-secret-value",
        },
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["privacy_mode"] is False
    assert updated["llm_provider"] == "openai_compatible"

    # Cloud provider + privacy mode is rejected (docs/11 step 5, M7).
    conflict = client.patch(
        ME,
        headers=_auth_headers(access),
        json={"privacy_mode": True},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "privacy_mode_requires_local_llm"

    response = client.patch(
        ME,
        headers=_auth_headers(access),
        json={
            "privacy_mode": True,
            "llm_provider": "ollama",
            "llm_model": "gemma4:e4b",
            "llm_api_key": "sk-secret-value",
        },
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["privacy_mode"] is True
    assert updated["llm_provider"] == "ollama"
    assert updated["llm_base_url"] == "https://api.example.com/v1"  # untouched by this PATCH
    assert updated["llm_model"] == "gemma4:e4b"
    assert "llm_api_key" not in updated

    fetched = client.get(ME, headers=_auth_headers(access))
    assert fetched.json()["privacy_mode"] is True
    assert "llm_api_key" not in fetched.json()


# --- M1/M2: cookie-only delivery + cookie attributes -------------------------


def test_refresh_cookie_attributes_httponly_secure_strict_path(
    client: TestClient,
) -> None:
    """M2: the session cookie is set with settings.cookie_secure + hardening."""
    assert get_settings().cookie_secure is True  # pre-condition (config default)

    client.post(REGISTER, json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Path=/api/v1/auth" in set_cookie

    # The clearing response carries the same Secure attribute. Logout is
    # cookie-only now, so the token is attached per-request (a Secure cookie
    # from the jar above is dropped by httpx over plain http).
    issued = client.cookies.get("refresh_token")
    assert issued
    logout = client.post(LOGOUT, json={}, cookies={"refresh_token": issued})
    assert logout.status_code == 204
    cleared = logout.headers.get("set-cookie", "")
    assert "Secure" in cleared
    assert "Max-Age=0" in cleared


def test_cookie_only_refresh_and_logout_flow(client: TestClient) -> None:
    """Browser flow: refresh/logout work from the cookie alone (empty body),
    and no auth response body ever carries the refresh token."""
    with _cookies_over_http():
        body = _register(client)
        assert "refresh_token" not in body
        first_cookie = client.cookies.get("refresh_token")
        assert first_cookie

        refreshed = client.post(REFRESH, json={})
        assert refreshed.status_code == 200
        assert "refresh_token" not in refreshed.json()
        rotated_cookie = client.cookies.get("refresh_token")
        assert rotated_cookie and rotated_cookie != first_cookie

        out = client.post(LOGOUT, json={})
        assert out.status_code == 204
        assert client.cookies.get("refresh_token") is None

        again = client.post(REFRESH, json={})
        assert again.status_code == 401
        assert again.json()["code"] == "missing_refresh_token"


def test_change_password_body_has_no_refresh_token(client: TestClient) -> None:
    """users/me/change-password shares TokenResponse — cookie-only there too."""
    body = _register(client)
    old_cookie = client.cookies.get("refresh_token")
    response = client.post(
        CHANGE_PW,
        json={"current_password": PASSWORD, "new_password": "new-correct-horse"},
        headers=_auth_headers(body["access_token"]),
    )
    assert response.status_code == 200
    assert "refresh_token" not in response.json()
    assert response.json()["user"]["email"] == EMAIL
    new_cookie = client.cookies.get("refresh_token")
    assert new_cookie and new_cookie != old_cookie


# --- L1: login timing must not enumerate accounts ---------------------------


def test_unknown_email_runs_dummy_bcrypt(client: TestClient, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("app.api.auth._dummy_bcrypt", calls.append)

    response = client.post(LOGIN, json={"email": "ghost@example.com", "password": PASSWORD})
    assert response.status_code == 401
    assert response.json() == {
        "detail": "Invalid email or password",
        "code": "invalid_credentials",
    }
    # Exactly one dummy verify ran, with the submitted password.
    assert calls == [PASSWORD]


def test_unknown_email_pays_bcrypt_cost(client: TestClient) -> None:
    """The previously-instant unknown-email path must now cost a real bcrypt
    verify (>= 50 ms — cost 12 runs ~100-400 ms), so timing cannot distinguish
    registered from unregistered addresses."""
    _register(client)
    start = time.perf_counter()
    response = client.post(LOGIN, json={"email": "ghost2@example.com", "password": PASSWORD})
    elapsed = time.perf_counter() - start
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"
    assert elapsed >= 0.05, f"unknown-email login took {elapsed:.3f}s — bcrypt did not run"

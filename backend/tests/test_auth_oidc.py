"""M74 optional OIDC SSO — behavior tests (docs/38).

The provider is an `httpx.MockTransport` behind `app.services.oidc._transport`
(its documented test seam): discovery, the token endpoint and JWKS are all
served in-process, so these tests exercise the REAL discovery/JWKS/signature
code paths — including the Basic→post client-secret fallback and a wrong
signing key — without a network.

Run: cd backend && uv run pytest tests/test_auth_oidc.py -q
"""

import base64
import time
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import parse_qs, parse_qsl, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlmodel import select

from app.api.auth_oidc import OIDC_PENDING_COOKIE, OIDC_STATE_COOKIE
from app.core.config import get_settings
from app.core.security import hash_password
from app.models.auth_factors import OidcLink
from app.models.users import User
from app.services import oidc as oidc_service
from app.services.demo_seed import DEMO_EMAIL

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/users/me"
OIDC_CONFIG = "/api/v1/auth/oidc/config"
OIDC_LOGIN = "/api/v1/auth/oidc/login"
OIDC_CALLBACK = "/api/v1/auth/oidc/callback"
OIDC_PREVIEW = "/api/v1/auth/oidc/preview"
OIDC_CONFIRM = "/api/v1/auth/oidc/confirm"

ISSUER = "https://idp.test"
AUTHORIZE = f"{ISSUER}/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/token"
JWKS_URI = f"{ISSUER}/jwks"
CLIENT_ID = "fintracker"
CLIENT_SECRET = "test-client-secret"  # a literal placeholder, not a credential
PASSWORD = "correct-horse-battery"

RATE_ENVELOPE = {"detail": "Too many attempts", "code": "rate_limited"}


# --- fake provider -----------------------------------------------------------


def _b64u(value: int | bytes) -> str:
    raw = (
        value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
        if isinstance(value, int)
        else value
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class FakeProvider:
    """Discovery + token endpoint + JWKS for one RSA signing key."""

    def __init__(self, email: str = "ada@example.com") -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.key.public_key().public_numbers()
        self.jwk = {
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "kid": "test-key",
            "n": _b64u(numbers.n),
            "e": _b64u(numbers.e),
        }
        self.email = email
        self.subject = "idp-sub-1"
        self.nonce: str | None = None  # filled from the browser's state cookie
        self.nonce_override: str | None = None
        self.with_email = True
        self.token_status = 200
        self.reject_basic_auth = False
        self.discovery_issuer = ISSUER
        self.token_requests: list[dict] = []

    def mint_id_token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self.discovery_issuer,
            "aud": CLIENT_ID,
            "sub": self.subject,
            "iat": now,
            "exp": now + 300,
            "nonce": self.nonce_override if self.nonce_override is not None else self.nonce,
        }
        if self.with_email:
            claims["email"] = self.email
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test-key"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{ISSUER}/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": self.discovery_issuer,
                    "authorization_endpoint": AUTHORIZE,
                    "token_endpoint": TOKEN_ENDPOINT,
                    "jwks_uri": JWKS_URI,
                },
            )
        if url == JWKS_URI:
            return httpx.Response(200, json={"keys": [self.jwk]})
        if url == TOKEN_ENDPOINT:
            authorization = request.headers.get("authorization", "")
            body = dict(parse_qsl(request.content.decode("utf-8")))
            self.token_requests.append({"basic": authorization, "body": body})
            if self.reject_basic_auth and authorization.startswith("Basic "):
                return httpx.Response(401, json={"error": "invalid_client"})
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "server_error"})
            return httpx.Response(
                200, json={"access_token": "provider-access", "id_token": self.mint_id_token()}
            )
        return httpx.Response(404, json={"error": "not_found"})


# --- fixtures ----------------------------------------------------------------


@contextmanager
def _oidc_enabled(provider: FakeProvider) -> Iterator[None]:
    """OIDC on + the mock transport installed; everything restored on exit."""
    settings = get_settings()
    saved = (
        settings.oidc_enabled,
        settings.oidc_issuer,
        settings.oidc_client_id,
        settings.oidc_client_secret,
        settings.oidc_redirect_uri,
        settings.oidc_scopes,
    )
    settings.oidc_enabled = True
    settings.oidc_issuer = ISSUER
    settings.oidc_client_id = CLIENT_ID
    settings.oidc_client_secret = CLIENT_SECRET
    settings.oidc_redirect_uri = ""  # derive from APP_ORIGIN
    oidc_service._transport = httpx.MockTransport(provider.handler)
    oidc_service.clear_cache()
    try:
        yield
    finally:
        (
            settings.oidc_enabled,
            settings.oidc_issuer,
            settings.oidc_client_id,
            settings.oidc_client_secret,
            settings.oidc_redirect_uri,
            settings.oidc_scopes,
        ) = saved
        oidc_service._transport = None
        oidc_service.clear_cache()


@contextmanager
def _cookies_over_http() -> Iterator[None]:
    """Relax `Secure` so httpx keeps and sends the cookies over plain http."""
    settings = get_settings()
    original = settings.cookie_secure
    settings.cookie_secure = False
    try:
        yield
    finally:
        settings.cookie_secure = original


def _register(client: TestClient, email: str) -> dict:
    client.post(REGISTER, json={"email": email, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def _start_flow(client: TestClient, provider: FakeProvider) -> str:
    """`GET /auth/oidc/login` -> the state value, with the nonce captured the
    way the callback will find it (inside this browser's cookie)."""
    response = client.get(OIDC_LOGIN, follow_redirects=False)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(AUTHORIZE)
    params = parse_qs(urlparse(location).query)
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["scope"] == [get_settings().oidc_scopes]
    assert params["redirect_uri"] == [
        f"{get_settings().app_origin.rstrip('/')}/api/v1/auth/oidc/callback"
    ]
    state = params["state"][0]

    cookie_header = response.headers.get_list("set-cookie")
    state_cookie = next((c for c in cookie_header if c.startswith(f"{OIDC_STATE_COOKIE}=")), "")
    assert "HttpOnly" in state_cookie
    assert "SameSite=lax" in state_cookie
    assert f"Path={'/api/v1/auth'}" in state_cookie

    stored = client.cookies.get(OIDC_STATE_COOKIE)
    assert stored is not None, "the state cookie must survive to the callback"
    stored_state, stored_nonce = stored.split("|", 1)
    assert stored_state == state
    provider.nonce = stored_nonce
    return state


def _callback(client: TestClient, state: str, code: str = "auth-code-1"):
    return client.get(OIDC_CALLBACK, params={"code": code, "state": state}, follow_redirects=False)


def _assert_error_redirect(response, code: str) -> None:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location == f"{get_settings().app_origin.rstrip('/')}/login/oidc?error={code}"
    # Every failure clears the state cookie — one flow per attempt.
    assert OIDC_STATE_COOKIE in deleted_cookie_names(response)


def deleted_cookie_names(response) -> list[str]:
    """Cookie names this response is EXPIRING (Max-Age=0 / epoch expiry)."""
    return [
        c.split("=", 1)[0]
        for c in response.headers.get_list("set-cookie")
        if "Max-Age=0" in c or "Expires=Thu, 01 Jan 1970" in c
    ]


# --- disabled by default -----------------------------------------------------


def test_oidc_disabled_by_default(client: TestClient) -> None:
    config = client.get(OIDC_CONFIG)
    assert config.status_code == 200
    assert config.json() == {"enabled": False}

    for method, path, kwargs in [
        ("get", OIDC_LOGIN, {}),
        ("get", OIDC_PREVIEW, {}),
        ("post", OIDC_CONFIRM, {}),
    ]:
        response = getattr(client, method)(path, follow_redirects=False, **kwargs)
        assert response.status_code == 404, (path, response.text)
        assert response.json()["code"] == "oidc_disabled"


# --- happy path --------------------------------------------------------------


def test_full_flow_creates_link_and_session(client: TestClient, session) -> None:
    _register(client, "ada@example.com")
    provider = FakeProvider()

    with _cookies_over_http(), _oidc_enabled(provider):
        # 1. The login page can ask whether SSO exists at all.
        assert client.get(OIDC_CONFIG).json() == {"enabled": True}

        # 2. Start: 302 to the provider, state+nonce parked in httpOnly cookie.
        state = _start_flow(client, provider)

        # 3. Provider redirect back: pending parked, SPA reached, no token in URL.
        callback = _callback(client, state)
        assert callback.status_code == 302, callback.text
        web_path = f"{get_settings().app_origin.rstrip('/')}/login/oidc"
        assert callback.headers["location"] == web_path
        assert "access_token" not in callback.headers["location"]
        pending_cookies = [
            c
            for c in callback.headers.get_list("set-cookie")
            if c.startswith(f"{OIDC_PENDING_COOKIE}=")
        ]
        assert len(pending_cookies) == 1
        assert "HttpOnly" in pending_cookies[0] and "SameSite=lax" in pending_cookies[0]
        assert client.cookies.get(OIDC_PENDING_COOKIE)

        # 4. Preview before confirming: not linked yet.
        preview = client.get(OIDC_PREVIEW)
        assert preview.status_code == 200, preview.text
        assert preview.json() == {"email": "ada@example.com", "linked": False}

        # 5. Confirmation issues the SAME session shape a password login does.
        confirm = client.post(OIDC_CONFIRM)
        assert confirm.status_code == 200, confirm.text
        pair = confirm.json()
        assert pair["token_type"] == "bearer"
        assert pair["access_token"]
        assert "refresh_token" not in pair  # cookie-only (audit M1)
        assert client.cookies.get("refresh_token")

        me = client.get(ME, headers={"Authorization": f"Bearer {pair['access_token']}"})
        assert me.status_code == 200
        assert me.json()["email"] == "ada@example.com"

        # Exactly one link, and the pending cookie is spent.
        links = session.exec(select(OidcLink)).all()
        assert len(links) == 1
        assert links[0].issuer == ISSUER
        assert links[0].subject == provider.subject
        assert client.cookies.get(OIDC_PENDING_COOKIE) is None

        # 6. Second sign-in: linked NOW, still one row, a fresh session.
        state = _start_flow(client, provider)
        assert _callback(client, state).status_code == 302
        preview = client.get(OIDC_PREVIEW).json()
        assert preview == {"email": "ada@example.com", "linked": True}
        second = client.post(OIDC_CONFIRM)
        assert second.status_code == 200, second.text
        assert second.json()["access_token"]
        assert len(session.exec(select(OidcLink)).all()) == 1


# --- failure modes -----------------------------------------------------------


def test_callback_rejects_state_and_provider_errors(client: TestClient) -> None:
    provider = FakeProvider()
    with _cookies_over_http(), _oidc_enabled(provider):
        # No cookie at all -> the state cannot be validated.
        no_cookie = client.get(
            OIDC_CALLBACK, params={"code": "x", "state": "anything"}, follow_redirects=False
        )
        _assert_error_redirect(no_cookie, "oidc_state_mismatch")

        # Cookie present, state from a different flow.
        _start_flow(client, provider)
        swapped = client.get(
            OIDC_CALLBACK, params={"code": "x", "state": "forged"}, follow_redirects=False
        )
        _assert_error_redirect(swapped, "oidc_state_mismatch")

        # The provider itself reports a failure.
        provider_error = client.get(
            OIDC_CALLBACK, params={"error": "access_denied"}, follow_redirects=False
        )
        _assert_error_redirect(provider_error, "oidc_provider_error")

    # Token endpoint blowing up is a provider error, not a session.
    failing = FakeProvider()
    failing.token_status = 500
    with _cookies_over_http(), _oidc_enabled(failing):
        state = _start_flow(client, failing)
        _assert_error_redirect(_callback(client, state), "oidc_provider_error")

    # A discovery document whose issuer does not match the configured one:
    # rejected before any token is exchanged (502 envelope on /auth/oidc/login).
    mismatched = FakeProvider()
    mismatched.discovery_issuer = "https://other.test"
    with _oidc_enabled(mismatched):
        response = client.get(OIDC_LOGIN, follow_redirects=False)
        assert response.status_code == 502, response.text
        assert response.json()["code"] == "oidc_provider_error"

    # Nothing was ever parked, so preview/confirm are expired, not stale-valid.
    with _oidc_enabled(FakeProvider()):
        preview = client.get(OIDC_PREVIEW)
        assert preview.status_code == 401
        assert preview.json()["code"] == "oidc_pending_expired"
        confirm = client.post(OIDC_CONFIRM)
        assert confirm.status_code == 401
        assert confirm.json()["code"] == "oidc_pending_expired"


def test_callback_rejects_bad_id_tokens(client: TestClient) -> None:
    # Wrong nonce: a token minted for another flow never opens this one.
    wrong_nonce = FakeProvider()
    wrong_nonce.nonce_override = "not-the-browser-nonce"
    with _cookies_over_http(), _oidc_enabled(wrong_nonce):
        state = _start_flow(client, wrong_nonce)
        _assert_error_redirect(_callback(client, state), "oidc_token_invalid")

    # No email claim: there is nothing to match an account against.
    no_email = FakeProvider()
    no_email.with_email = False
    with _cookies_over_http(), _oidc_enabled(no_email):
        state = _start_flow(client, no_email)
        _assert_error_redirect(_callback(client, state), "oidc_email_required")

    # Signed by a key the provider never published (same kid, wrong key).
    forged = FakeProvider()
    forged.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with _cookies_over_http(), _oidc_enabled(forged):
        state = _start_flow(client, forged)
        _assert_error_redirect(_callback(client, state), "oidc_token_invalid")
        # The JWKS path was actually exercised: the cache holds one document.
        assert oidc_service.discovery(ISSUER)["issuer"] == ISSUER


def test_callback_never_creates_an_account(client: TestClient, session) -> None:
    provider = FakeProvider(email="stranger@example.com")
    with _cookies_over_http(), _oidc_enabled(provider):
        state = _start_flow(client, provider)
        _assert_error_redirect(_callback(client, state), "oidc_unknown_account")

    # No user, no link, no session — SSO is link-only (docs/38 decision 7).
    assert session.exec(select(User).where(User.email == "stranger@example.com")).first() is None
    assert session.exec(select(OidcLink)).all() == []
    assert client.cookies.get(OIDC_PENDING_COOKIE) is None


def test_demo_account_cannot_confirm_sso(client: TestClient, session) -> None:
    session.add(User(email=DEMO_EMAIL, hashed_password=hash_password("demo-password")))
    session.commit()

    provider = FakeProvider(email=DEMO_EMAIL)
    with _cookies_over_http(), _oidc_enabled(provider):
        state = _start_flow(client, provider)
        # The callback still resolves (the demo account exists locally)…
        assert _callback(client, state).status_code == 302
        preview = client.get(OIDC_PREVIEW)
        assert preview.status_code == 200
        assert preview.json()["email"] == DEMO_EMAIL

        # …but confirmation — the only write — is refused.
        confirm = client.post(OIDC_CONFIRM)
        assert confirm.status_code == 403, confirm.text
        assert confirm.json()["code"] == "forbidden"

    assert session.exec(select(OidcLink)).all() == []


# --- client-secret transport -------------------------------------------------


def test_token_endpoint_falls_back_from_basic_to_post(client: TestClient) -> None:
    """client_secret_basic first, client_secret_post second — never both in one
    request (RFC 6749 §2.3.1), which is what lets one deployment serve both
    provider defaults."""
    provider = FakeProvider()
    provider.reject_basic_auth = True
    _register(client, "ada@example.com")

    with _cookies_over_http(), _oidc_enabled(provider):
        state = _start_flow(client, provider)
        callback = _callback(client, state)
        assert callback.status_code == 302, callback.text
        assert callback.headers["location"] == f"{get_settings().app_origin.rstrip('/')}/login/oidc"

        confirm = client.post(OIDC_CONFIRM)
        assert confirm.status_code == 200, confirm.text

    assert len(provider.token_requests) == 2
    first, second = provider.token_requests
    assert first["basic"].startswith("Basic ")
    assert "client_secret" not in first["body"]  # secret rode in the header
    assert second["basic"] == ""
    assert second["body"]["client_secret"] == CLIENT_SECRET
    assert second["body"]["grant_type"] == "authorization_code"


def test_token_endpoint_basic_auth_succeeds_first_time(client: TestClient) -> None:
    provider = FakeProvider()
    _register(client, "ada@example.com")

    with _cookies_over_http(), _oidc_enabled(provider):
        state = _start_flow(client, provider)
        assert _callback(client, state).status_code == 302
        assert client.post(OIDC_CONFIRM).status_code == 200

    assert len(provider.token_requests) == 1
    assert provider.token_requests[0]["basic"].startswith("Basic ")

"""LLM egress host policy, key routing and privacy lock (audit H1/H3, M8).

Covers the slice contract end to end:
- PATCH /users/me validates llm_base_url at write time (422 invalid_llm_base_url):
  loopback allowed, RFC1918/link-local/CGNAT/ULA/unspecified denied, plain http
  only for loopback, operator-host + LLM_ALLOW_PRIVATE_HOSTS exemptions;
- the send seam enforces the same policy AND the privacy loopback lock, so no
  non-loopback httpx call can fire under privacy mode;
- the operator env LLM_API_KEY is attached only to the operator's own env
  LLM_BASE_URL destination, never to a user-supplied host;
- upstream failures surface as the generic 502 llm_upstream_error envelope
  (status/URL/transport details logged server-side only, never keys);
- PATCH /users/me is demo-guarded (M8).

No test opens a real socket: every httpx.post is swapped for a recorder, and
every host is a literal (127.0.0.1, 192.168.x, 169.254.169.254, 203.0.113.x,
192.168.1.6, fd00::1, fe80::1) or exempt/localhost name — nothing needs DNS.
Run: cd backend && uv run pytest tests/test_llm_egress.py -q
"""

import contextvars
import logging
import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api.auth import _issue_pair
from app.core.config import Settings, get_settings
from app.core.errors import APIError
from app.core.security import hash_password
from app.llm.base import build_provider, resolve_settings
from app.models.accounts import Account, AccountType
from app.models.llm_suggestions import LlmSuggestion
from app.models.transactions import Transaction
from app.models.users import LlmProvider, User
from app.services import categorization
from app.services.demo_seed import DEMO_EMAIL

ME = "/api/v1/users/me"
LLM_TEST = "/api/v1/users/me/llm-test"
SUGGESTIONS_RUN = "/api/v1/suggestions/run"
SUGGEST_PROFILE = "/api/v1/imports/suggest-profile"

LOOPBACK = "http://127.0.0.1:11434/v1"
FOREIGN_HTTPS = "https://203.0.113.10/v1"  # TEST-NET-3 literal — public, no DNS
METADATA_IP = "169.254.169.254"
TRUSTED_HOST = "trusted-operator.test"  # autouse env LLM_BASE_URL host (never resolved)


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch):
    """Deterministic settings for every test in this file: a sentinel operator
    host (equality-checked only, never resolved), private hosts denied, no env
    key. Teardown clears the settings cache so later tests rebuild from the
    restored environment."""
    monkeypatch.setenv("LLM_BASE_URL", f"http://{TRUSTED_HOST}")
    monkeypatch.setenv("LLM_ALLOW_PRIVATE_HOSTS", "false")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    get_settings.cache_clear()
    # conftest's session fixture runs alembic, whose fileConfig() applies the
    # logging.config default disable_existing_loggers=True — every app.* logger
    # imported beforehand (conftest imports app.main first) gets its per-logger
    # `disabled` flag flipped, silently dropping ALL app log records under
    # pytest. Re-enable ours so the server-side log assertions below observe
    # the real stdlib logging call (monkeypatch restores the original flag).
    monkeypatch.setattr(logging.getLogger("app.llm.base"), "disabled", False)
    yield
    get_settings.cache_clear()


def _auth(client: TestClient) -> dict[str, str]:
    email = f"egress-{uuid.uuid4().hex}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "secret123"})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": "secret123"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


class _Recorder:
    """Fake transport: records every httpx.post call, never touches the network."""

    def __init__(self, status: int = 200, body: dict | None = None, exc: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.status = status
        self.body = (
            body if body is not None else {"choices": [{"message": {"content": '{"ok": true}'}}]}
        )
        self.exc = exc

    def __call__(self, url, **kwargs):
        self.calls.append((str(url), kwargs))
        if self.exc is not None:
            raise self.exc
        return httpx.Response(self.status, json=self.body)


# --- PATCH write-time validation (H1) -----------------------------------------


def test_patch_accepts_loopback_base_urls(client: TestClient) -> None:
    headers = _auth(client)
    for url in (LOOPBACK, "http://localhost:11434/v1"):
        response = client.patch(ME, json={"llm_base_url": url}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["llm_base_url"] == url


def test_patch_accepts_public_https_target(client: TestClient) -> None:
    headers = _auth(client)
    response = client.patch(ME, json={"llm_base_url": FOREIGN_HTTPS}, headers=headers)
    assert response.status_code == 200, response.text


def test_patch_denies_rfc1918_targets(client: TestClient) -> None:
    headers = _auth(client)
    denied = (
        "http://192.168.1.50:11434/v1",
        "https://192.168.1.50:8443/v1",
        "http://10.0.0.1/v1",
        "http://172.16.0.9:11434/v1",
        "http://10.0.0.1:11434/v1",
    )
    for url in denied:
        response = client.patch(ME, json={"llm_base_url": url}, headers=headers)
        assert response.status_code == 422, (url, response.text)
        assert response.json()["code"] == "invalid_llm_base_url"


def test_patch_denies_cloud_metadata_endpoint(client: TestClient) -> None:
    headers = _auth(client)
    for url in (
        f"http://{METADATA_IP}/latest/meta-data/",
        f"https://{METADATA_IP}/",
    ):
        response = client.patch(ME, json={"llm_base_url": url}, headers=headers)
        assert response.status_code == 422, (url, response.text)
        assert response.json()["code"] == "invalid_llm_base_url"


def test_patch_denies_cgnat_ula_linklocal_unspecified(client: TestClient) -> None:
    headers = _auth(client)
    for url in (
        "https://100.64.0.1/",  # CGNAT (RFC6598)
        "https://[fd00::1]:8443/",  # ULA
        "https://[fe80::1]/",  # link-local v6
        "https://0.0.0.0/",  # unspecified
    ):
        response = client.patch(ME, json={"llm_base_url": url}, headers=headers)
        assert response.status_code == 422, (url, response.text)
        assert response.json()["code"] == "invalid_llm_base_url"


def test_patch_denies_non_http_schemes(client: TestClient) -> None:
    headers = _auth(client)
    for url in ("ftp://files.example.com/x", "file:///etc/passwd", "gopher://x/"):
        response = client.patch(ME, json={"llm_base_url": url}, headers=headers)
        assert response.status_code == 422, (url, response.text)
        assert response.json()["code"] == "invalid_llm_base_url"


def test_patch_denies_plain_http_to_non_loopback_target(client: TestClient) -> None:
    # Slice contract: http only for loopback targets; https elsewhere.
    headers = _auth(client)
    response = client.patch(ME, json={"llm_base_url": "http://203.0.113.10/v1"}, headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_llm_base_url"


def test_patch_tolerates_unresolvable_name_at_write_time(client: TestClient) -> None:
    # Nothing to deny yet when DNS is down; the send seam re-resolves and
    # fails CLOSED at send time (verified in the seam tests below), so this
    # can never become an egress path. api.example.com is the historic
    # benign value used by tests/test_auth.py.
    headers = _auth(client)
    response = client.patch(
        ME, json={"llm_base_url": "https://api.example.com/v1"}, headers=headers
    )
    assert response.status_code == 200, response.text


def test_patch_operator_host_is_exempt(client: TestClient, monkeypatch) -> None:
    # The operator's own env destination is trusted even over plain http on a
    # private address (LAN gateway) — host equality, no DNS needed.
    monkeypatch.setenv("LLM_BASE_URL", "http://192.168.1.6:11434")
    get_settings.cache_clear()
    headers = _auth(client)
    response = client.patch(
        ME, json={"llm_base_url": "http://192.168.1.6:11434/v1"}, headers=headers
    )
    assert response.status_code == 200, response.text
    # A different private host is still denied.
    denied = client.patch(ME, json={"llm_base_url": "http://192.168.1.50/v1"}, headers=headers)
    assert denied.status_code == 422
    assert denied.json()["code"] == "invalid_llm_base_url"


def test_patch_allow_private_hosts_opt_in(client: TestClient, monkeypatch) -> None:
    headers = _auth(client)
    # Default (autouse env): denied.
    denied = client.patch(
        ME, json={"llm_base_url": "http://192.168.1.50:11434/v1"}, headers=headers
    )
    assert denied.status_code == 422
    # Operator opt-in for LAN Ollama: accepted.
    monkeypatch.setenv("LLM_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    allowed = client.patch(
        ME, json={"llm_base_url": "http://192.168.1.50:11434/v1"}, headers=headers
    )
    assert allowed.status_code == 200, allowed.text


# --- PATCH under privacy mode (H3) ---------------------------------------------


def test_patch_rejects_non_loopback_url_under_privacy(client: TestClient) -> None:
    headers = _auth(client)
    assert client.patch(ME, json={"privacy_mode": True}, headers=headers).status_code == 200
    denied = client.patch(ME, json={"llm_base_url": FOREIGN_HTTPS}, headers=headers)
    assert denied.status_code == 422
    assert denied.json()["code"] == "invalid_llm_base_url"
    allowed = client.patch(ME, json={"llm_base_url": LOOPBACK}, headers=headers)
    assert allowed.status_code == 200, allowed.text


def test_patch_allows_privacy_toggle_with_stored_cloud_url(client: TestClient) -> None:
    # Enabling privacy does NOT 422 on a pre-stored cloud URL (test_auth.py
    # pins this order): the send seam blocks it, and Settings.tsx overwrites
    # it with the loopback default in the same PATCH (docs/11 verification (a)).
    headers = _auth(client)
    stored = client.patch(ME, json={"llm_base_url": FOREIGN_HTTPS}, headers=headers)
    assert stored.status_code == 200, stored.text
    toggled = client.patch(ME, json={"privacy_mode": True}, headers=headers)
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["privacy_mode"] is True


# --- Env LLM_API_KEY routing (H1) ----------------------------------------------


def test_env_key_only_reaches_operator_destination(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-env-operator-fake-key")
    get_settings.cache_clear()
    recorder = _Recorder()
    monkeypatch.setattr(httpx, "post", recorder)
    headers = _auth(client)

    # Operator destination → the env key is attached.
    ok = client.post(
        LLM_TEST,
        json={"llm_provider": "ollama", "llm_base_url": f"http://{TRUSTED_HOST}/v1"},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert recorder.calls[0][1]["headers"]["Authorization"] == "Bearer sk-env-operator-fake-key"

    # Foreign destination with no user key → NO Authorization header at all.
    foreign = client.post(LLM_TEST, json={"llm_base_url": FOREIGN_HTTPS}, headers=headers)
    assert foreign.status_code == 200, foreign.text
    assert "Authorization" not in recorder.calls[1][1]["headers"]

    # Foreign destination with the user's own stored key → only that key.
    saved = client.patch(ME, json={"llm_api_key": "sk-user-own"}, headers=headers)
    assert saved.status_code == 200, saved.text
    byo = client.post(LLM_TEST, json={"llm_base_url": FOREIGN_HTTPS}, headers=headers)
    assert byo.status_code == 200, byo.text
    assert recorder.calls[2][1]["headers"]["Authorization"] == "Bearer sk-user-own"

    # Redirects are never followed at the send point.
    assert all(call[1].get("follow_redirects") is False for call in recorder.calls)


def test_resolve_settings_key_routing() -> None:
    operator = Settings(llm_base_url="http://192.168.1.6:11434", llm_api_key="sk-env")
    plain_user = User(
        id=1,
        email="routing@example.com",
        hashed_password="x",
        llm_provider=LlmProvider.ollama,
    )
    # Operator destination (env fallback) → env key.
    _, base_url, key, _ = resolve_settings(plain_user, operator)
    assert base_url == "http://192.168.1.6:11434"
    assert key == "sk-env"

    # User-supplied host → env key must NOT follow.
    plain_user.llm_base_url = FOREIGN_HTTPS
    _, _, key, _ = resolve_settings(plain_user, operator)
    assert key is None

    # User's own stored key still travels to their own endpoint.
    from app.core.crypto import encrypt_secret

    plain_user.llm_api_key = encrypt_secret("sk-user-own")
    _, _, key, _ = resolve_settings(plain_user, operator)
    assert key == "sk-user-own"

    # Nothing configured anywhere: operator default path keeps the env key
    # (parity with tests/test_security_hardening.py::..._null_stored_key).
    # llm_base_url is passed explicitly because the autouse fixture pins an
    # env LLM_BASE_URL that Settings() would otherwise pick up.
    unset = Settings(llm_api_key="sk-env", llm_base_url="")
    fresh = User(id=2, email="routing2@example.com", hashed_password="x")
    _, base_url, key, _ = resolve_settings(fresh, unset)
    assert base_url == ""
    assert key == "sk-env"


# --- Generic upstream error envelope (H1/L16) -----------------------------------


def test_upstream_http_failure_is_generic_502(client: TestClient, monkeypatch, caplog) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-env-operator-fake-key")
    get_settings.cache_clear()
    recorder = _Recorder(status=500)
    monkeypatch.setattr(httpx, "post", recorder)
    headers = _auth(client)

    with caplog.at_level(logging.WARNING, logger="app.llm.base"):
        response = client.post(LLM_TEST, json={"llm_base_url": LOOPBACK}, headers=headers)

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["code"] == "llm_upstream_error"
    # The envelope must not echo the upstream status, URL or transport text.
    assert "500" not in body["detail"]
    assert "127.0.0.1" not in body["detail"]
    # Server-side log carries status + sanitized target — and no keys.
    assert "HTTP 500" in caplog.text
    assert "127.0.0.1" in caplog.text
    assert "sk-env-operator-fake-key" not in caplog.text
    # The env key never left home either (loopback is not the operator host).
    assert "Authorization" not in recorder.calls[0][1]["headers"]
    assert recorder.calls[0][1].get("follow_redirects") is False


def test_upstream_transport_failure_is_generic_502(client: TestClient, monkeypatch, caplog) -> None:
    recorder = _Recorder(exc=httpx.ReadTimeout("timed out talking to 127.0.0.1:11434"))
    monkeypatch.setattr(httpx, "post", recorder)
    headers = _auth(client)

    with caplog.at_level(logging.WARNING, logger="app.llm.base"):
        response = client.post(LLM_TEST, json={"llm_base_url": LOOPBACK}, headers=headers)

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["code"] == "llm_upstream_error"
    assert "timed out" not in body["detail"]  # transport message never echoed
    assert "ReadTimeout" in caplog.text  # ...but classified server-side
    assert "127.0.0.1" in caplog.text


# --- Privacy loopback lock at the seam (H3) --------------------------------------


def test_llm_test_blocked_under_privacy_without_any_request(client, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(httpx, "post", recorder)
    headers = _auth(client)
    assert (
        client.patch(ME, json={"llm_base_url": FOREIGN_HTTPS}, headers=headers).status_code == 200
    )
    assert client.patch(ME, json={"privacy_mode": True}, headers=headers).status_code == 200

    # Stored cloud destination under privacy → blocked, zero requests sent.
    blocked = client.post(LLM_TEST, json={}, headers=headers)
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["code"] == "privacy_mode_blocks_external_api"
    assert recorder.calls == []

    # Candidate cloud URL is blocked the same way.
    candidate = client.post(LLM_TEST, json={"llm_base_url": FOREIGN_HTTPS}, headers=headers)
    assert candidate.status_code == 403
    assert recorder.calls == []

    # Loopback keeps working under privacy (no over-blocking).
    local = client.post(LLM_TEST, json={"llm_base_url": LOOPBACK}, headers=headers)
    assert local.status_code == 200, local.text
    assert len(recorder.calls) == 1


def test_build_provider_binds_lock_and_seam_blocks_worker_path(monkeypatch) -> None:
    """The categorization/suggest-profile path: build_provider(user) publishes
    privacy_mode, provider.chat reaches the seam, and NOTHING hits httpx."""
    recorder = _Recorder()
    monkeypatch.setattr(httpx, "post", recorder)
    user = User(
        id=1,
        email="worker@example.com",
        hashed_password="x",
        privacy_mode=True,
        llm_provider=LlmProvider.openai_compatible,
        llm_base_url=FOREIGN_HTTPS,
    )

    def run() -> None:
        resolved = build_provider(user, Settings())
        assert resolved.provider is not None  # construction succeeds; the SEAM blocks
        with pytest.raises(APIError) as excinfo:
            resolved.provider.chat([{"role": "user", "content": "hi"}], {"type": "object"})
        assert excinfo.value.status_code == 403
        assert excinfo.value.code == "privacy_mode_blocks_external_api"

    # Isolated context: the ContextVar bind must not leak into other tests.
    contextvars.copy_context().run(run)
    assert recorder.calls == []


def test_build_provider_privacy_still_allows_loopback(monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(httpx, "post", recorder)
    user = User(
        id=2,
        email="worker-local@example.com",
        hashed_password="x",
        privacy_mode=True,
        llm_provider=LlmProvider.ollama,
        llm_base_url=LOOPBACK,
    )

    def run() -> None:
        resolved = build_provider(user, Settings())
        assert resolved.provider is not None
        result = resolved.provider.chat([{"role": "user", "content": "hi"}], {"type": "object"})
        assert result == {"ok": True}

    contextvars.copy_context().run(run)
    assert len(recorder.calls) == 1


def test_suggest_profile_never_calls_httpx_under_privacy(client, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(httpx, "post", recorder)
    headers = _auth(client)
    assert (
        client.patch(ME, json={"llm_base_url": FOREIGN_HTTPS}, headers=headers).status_code == 200
    )
    assert client.patch(ME, json={"privacy_mode": True}, headers=headers).status_code == 200

    response = client.post(
        SUGGEST_PROFILE,
        files={
            "file": (
                "sample.csv",
                b"Date,Description,Amount\n2026-01-01,Coffee,4.50\n",
                "text/csv",
            )
        },
        headers=headers,
    )
    # column_mapping falls back to heuristics when the LLM seam refuses...
    assert response.status_code == 200, response.text
    # ...and crucially no non-loopback request was ever attempted.
    assert recorder.calls == []


def test_suggestions_worker_never_calls_httpx_under_privacy(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """POST /suggestions/run → BackgroundTask → build_provider → chat: the
    whole worker path inherits the lock; zero httpx calls fire."""
    import contextlib

    recorder = _Recorder()
    monkeypatch.setattr(httpx, "post", recorder)

    @contextlib.contextmanager
    def _shared_session():
        yield session

    monkeypatch.setattr(categorization, "_new_session", _shared_session)

    headers = _auth(client)
    user_id = client.get(ME, headers=headers).json()["id"]
    assert (
        client.patch(ME, json={"llm_base_url": FOREIGN_HTTPS}, headers=headers).status_code == 200
    )
    assert client.patch(ME, json={"privacy_mode": True}, headers=headers).status_code == 200

    account = Account(user_id=user_id, name="Chq", type=AccountType.chequing, currency="CAD")
    session.add(account)
    session.commit()
    session.refresh(account)
    assert account.id is not None
    session.add(
        Transaction(
            user_id=user_id,
            account_id=account.id,
            date=date(2026, 8, 1),
            raw_description="Mystery Merchant",
            normalized_payee="mystery merchant",
            amount_native=Decimal("-10.00"),
            currency="CAD",
            dedup_hash="llm-egress-worker-1",
        )
    )
    session.commit()

    response = client.post(SUGGESTIONS_RUN, json={}, headers=headers)
    assert response.status_code == 202, response.text
    # A batch WAS queued, so the worker really exercised the LLM path...
    assert response.json()["queued"] == 1
    # ...and not a single outbound request fired.
    assert recorder.calls == []
    assert session.exec(select(LlmSuggestion)).all() == []


# --- M8: demo account guard on update_me -----------------------------------------


def test_patch_me_blocked_for_demo(client: TestClient, session: Session) -> None:
    user = User(email=DEMO_EMAIL, hashed_password=hash_password("secret123"))
    session.add(user)
    session.commit()
    session.refresh(user)
    token = _issue_pair(session, user).access_token
    headers = {"Authorization": f"Bearer {token}"}

    for payload in (
        {"llm_base_url": LOOPBACK},
        {"privacy_mode": True},
        {"name": "Nope"},
    ):
        response = client.patch(ME, json=payload, headers=headers)
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "demo_account_restricted"

    # The demo row was not mutated.
    session.refresh(user)
    assert user.llm_base_url is None
    assert user.privacy_mode is False
    assert user.name is None

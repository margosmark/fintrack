"""MCP BackendClient tests: M10 fail-closed 401, M11 auth-mode exclusivity, and
request-safe shared identity state under overlapping requests.

No real network: every request goes through httpx.MockTransport.
"""

from __future__ import annotations

import contextvars
import json
import os
import threading

import httpx
import pytest

from mcp_server.client import AuthModeError, BackendClient, configured_auth_modes, new_http_client

MODE_VARS = (
    "FIN_TRACKER_TOKEN",
    "FIN_TRACKER_EMAIL",
    "FIN_TRACKER_PASSWORD",
    "MCP_INTERNAL_SECRET",
)


@pytest.fixture(autouse=True)
def clean_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every auth-mode var so ambient env can't blur the matrix."""
    for var in MODE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FIN_TRACKER_URL", "http://mcp-tests.invalid")


def _refresh_cookie_value(cookie_header: str) -> str | None:
    """The `refresh_token` value from a request's Cookie header, if present."""
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "refresh_token":
            return value
    return None


def _client(handler, monkeypatch: pytest.MonkeyPatch) -> BackendClient:
    """BackendClient with its httpx transport swapped for a mock.

    MCP_INTERNAL_SECRET is (re)set here: the autouse fixture strips it, and
    mode 3 needs it for `_get_token_for_user` — still exactly one configured
    mode, so the startup guard stays quiet.
    """
    monkeypatch.setenv("MCP_INTERNAL_SECRET", "test-internal-secret")
    bc = BackendClient()
    # new_http_client: the suite must exercise the REAL jar policy — the
    # shared no-store jar (M84), not a per-test one that still extracts.
    bc._http = new_http_client(base_url=bc.base_url, transport=httpx.MockTransport(handler))
    return bc


# ---------------------------------------------------------------- M10 ------


def test_per_user_401_never_falls_back_to_shared_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected per-user JWT must not be retried as the shared identity."""
    from mcp_server.auth import current_user_id

    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        seen.append((request.url.path, auth))
        path = request.url.path
        if path.endswith("/internal-issue-token"):
            return httpx.Response(200, json={"access_token": "user-7-jwt"})
        if path.endswith("/auth/refresh"):
            # The dangerous old path: refresh succeeds, so the OLD code would
            # retry as the shared identity. It must never get here.
            return httpx.Response(200, json={"access_token": "shared-2", "refresh_token": "r2"})
        return httpx.Response(401, json={"detail": "expired", "code": "unauthorized"})

    bc = _client(handler, monkeypatch)
    # A shared identity exists at runtime (auth_login/auth_demo tool calls can
    # set one even though env config is mode-3 only).
    bc.set_token("shared-tenant-jwt", "shared-refresh-token")

    token_reset = current_user_id.set(7)
    try:
        result = bc.request("GET", "/api/v1/accounts")
    finally:
        current_user_id.reset(token_reset)

    assert isinstance(result, dict)
    assert result["error"] == 401
    assert isinstance(result["detail"], dict)
    assert result["detail"]["code"] == "mcp_unauthorized"
    # Exactly: issue-token + one authorized attempt. No refresh, no retry.
    assert [path for path, _ in seen] == [
        "/api/v1/auth/internal-issue-token",
        "/api/v1/accounts",
    ]
    assert seen[1][1] == "Bearer user-7-jwt"
    assert all("shared" not in (auth or "") for _, auth in seen), (
        "the shared token must never appear on the wire"
    )


def test_refresh_sends_cookie_never_a_body_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API is cookie-only: refresh must carry the token as a cookie, not JSON.

    Regression guard for a cross-slice break: the auth hardening made refresh
    and logout read the refresh token ONLY from the httpOnly cookie, while this
    client still posted `{"refresh_token": ...}` in the body. Against the real
    API that is a 401 — the MCP client could never rotate its session.

    The mock handler enforces the server's actual contract: a body token is
    rejected, a `refresh_token` cookie is accepted.
    """
    seen: list[tuple[str, str | None, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        cookie = request.headers.get("cookie", "")
        seen.append((request.url.path, body.get("refresh_token"), {"cookie": cookie}))
        if request.url.path.endswith("/auth/refresh"):
            if not body.get("refresh_token") and "refresh_token=" not in cookie:
                return httpx.Response(
                    401, json={"detail": "Missing refresh token", "code": "invalid_token"}
                )
            return httpx.Response(200, json={"access_token": "shared-2"})
        if request.headers.get("authorization") == "Bearer shared-2":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(401, json={"detail": "expired", "code": "unauthorized"})

    bc = _client(handler, monkeypatch)
    bc.set_token("shared-1", "refresh-1")
    # The jar is the authoritative store: login's Set-Cookie landed there.
    bc._http.cookies.set("refresh_token", "refresh-1", path="/api/v1/auth")

    result = bc.request("GET", "/api/v1/accounts")

    assert result == {"items": []}
    refresh_calls = [entry for entry in seen if entry[0].endswith("/auth/refresh")]
    assert len(refresh_calls) == 1, seen
    _, body_token, meta = refresh_calls[0]
    assert body_token is None, "the refresh token must never be sent in the request body"
    assert "refresh_token=refresh-1" in meta["cookie"]


def test_logout_and_explicit_refresh_tools_send_the_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auth_logout` / `auth_refresh` tools use the same cookie channel.

    These tools took the token as a required argument and posted it in the body,
    which the cookie-only API rejects. The token now travels as a cookie.
    """
    from mcp_server import server as server_mod

    seen: list[tuple[str, str | None, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        cookie = request.headers.get("cookie", "")
        seen.append((request.url.path, body.get("refresh_token"), cookie))
        return httpx.Response(200, json={"access_token": "shared-2", "revoked": True})

    bc = _client(handler, monkeypatch)
    monkeypatch.setattr(server_mod, "client", bc)
    bc._http.cookies.set("refresh_token", "jar-token", path="/api/v1/auth")

    server_mod.auth_logout("jar-token")  # type: ignore[arg-type]

    assert seen, "no request reached the backend"
    for path, body_token, cookie in seen:
        assert path.endswith("/auth/logout")
        assert body_token is None, f"logout sent the token in the body: {body_token!r}"
        assert "refresh_token=jar-token" in cookie


def test_same_identity_refresh_still_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mode-2 refresh keeps working when the identity does not change."""
    attempts = {"accounts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/auth/refresh"):
            return httpx.Response(200, json={"access_token": "shared-2", "refresh_token": "r2"})
        attempts["accounts"] += 1
        if attempts["accounts"] == 1:
            return httpx.Response(401, json={"detail": "expired", "code": "x"})
        assert request.headers.get("authorization") == "Bearer shared-2"
        return httpx.Response(200, json={"items": []})

    bc = _client(handler, monkeypatch)
    bc.set_token("shared-1", "refresh-1")

    result = bc.request("GET", "/api/v1/accounts")

    assert result == {"items": []}
    assert attempts["accounts"] == 2  # original + same-identity retry


def test_unresolved_user_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/29 F4: no per-user token → RuntimeError, zero requests sent."""
    from mcp_server.auth import current_user_id

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    bc = _client(handler, monkeypatch)
    bc.set_token("shared-tenant-jwt")
    monkeypatch.setattr(bc, "_get_token_for_user", lambda user_id: None)

    token_reset = current_user_id.set(42)
    try:
        with pytest.raises(RuntimeError, match="refusing"):
            bc.request("GET", "/api/v1/accounts")
    finally:
        current_user_id.reset(token_reset)
    assert seen == []


# ---------------------------------------------------------------- M11 ------


def test_configured_auth_modes_matrix() -> None:
    assert configured_auth_modes({}) == []
    assert configured_auth_modes({"FIN_TRACKER_TOKEN": "t"}) == ["static_token"]
    assert configured_auth_modes({"FIN_TRACKER_EMAIL": "a@b.c"}) == ["auto_login"]
    # A half-set auto-login pair still counts as the auto-login mode.
    assert configured_auth_modes({"FIN_TRACKER_PASSWORD": "p"}) == ["auto_login"]
    assert configured_auth_modes({"MCP_INTERNAL_SECRET": "s"}) == ["mcp_token"]
    modes = configured_auth_modes({"FIN_TRACKER_TOKEN": "t", "MCP_INTERNAL_SECRET": "s"})
    assert modes == ["static_token", "mcp_token"]


def test_startup_rejects_static_token_plus_mcp_token() -> None:
    os.environ["FIN_TRACKER_TOKEN"] = "sekrit-static-token-value"
    os.environ["MCP_INTERNAL_SECRET"] = "sekrit-internal-value"
    try:
        with pytest.raises(AuthModeError) as excinfo:
            BackendClient()
    finally:
        os.environ.pop("FIN_TRACKER_TOKEN", None)
        os.environ.pop("MCP_INTERNAL_SECRET", None)
    message = str(excinfo.value)
    assert "FIN_TRACKER_TOKEN" in message and "MCP_INTERNAL_SECRET" in message
    # Names of variables only — values never leak into the startup error.
    assert "sekrit" not in message


def test_startup_rejects_static_token_plus_auto_login() -> None:
    os.environ["FIN_TRACKER_TOKEN"] = "sekrit-static-token-value"
    os.environ["FIN_TRACKER_EMAIL"] = "sekrit-email@example.com"
    try:
        with pytest.raises(AuthModeError) as excinfo:
            BackendClient()
    finally:
        os.environ.pop("FIN_TRACKER_TOKEN", None)
        os.environ.pop("FIN_TRACKER_EMAIL", None)
    assert "static_token" in str(excinfo.value)
    assert "auto_login" in str(excinfo.value)
    assert "sekrit" not in str(excinfo.value)


def test_single_mode_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIN_TRACKER_TOKEN", "static-token")
    bc = BackendClient()
    assert bc.token == "static-token"
    bc._http.close()


def test_no_mode_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    bc = BackendClient()
    assert bc.token is None
    bc._http.close()


# ---------------------------------------------------------------- L19 ------


def test_bind_host_defaults_to_loopback_and_is_overridable() -> None:
    from mcp_server.__main__ import DEFAULT_HOST, resolve_bind_host

    assert DEFAULT_HOST == "127.0.0.1"
    assert resolve_bind_host([], {}) == "127.0.0.1"
    assert resolve_bind_host([], {"MCP_HOST": "0.0.0.0"}) == "0.0.0.0"  # noqa: S104 -- resolution test, no socket
    # The flag wins over the env var.
    assert resolve_bind_host(["--host", "10.1.2.3"], {"MCP_HOST": "0.0.0.0"}) == "10.1.2.3"  # noqa: S104 -- no socket
    # A truncated --host falls back instead of crashing.
    assert resolve_bind_host(["--host"], {"MCP_HOST": "::1"}) == "::1"


# ------------------------------------------- shared identity is request-safe


def test_overlapping_requests_never_swap_bearer_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request in flight must keep the identity it started with.

    ``BackendClient`` is a module-level singleton, so under streamable-http
    concurrent tool calls share one ``token``/``refresh_token`` pair. The
    old code read the *live* attributes after the 401, so a sibling
    request's ``set_token`` (any ``auth_login`` / ``auth_demo`` /
    ``auth_refresh`` / ``users_change_password`` tool call) could retarget
    a request that was already on the wire:

        B: GET /accounts        -> Bearer X-jwt  -> 401
        A: auth_login           -> set_token(Y-jwt, Y-refresh)
        B: POST /auth/refresh   {"refresh_token": "Y-refresh"}  <- wrong tenant
        B: GET /accounts        -> Bearer Y-jwt                 <- wrong tenant

    B silently executes as A's tenant. It must refresh and retry under its
    own identity, or fail — never under the one installed meanwhile.

    Deterministic: the transport blocks B at the first attempt until the
    test has swapped the shared token. Events, not sleeps.
    """
    b_at_wire = threading.Event()
    a_loaded = threading.Event()
    # (path, authorization, body, cookie header) — the cookie header is read by
    # the refresh assertion below, since the API is cookie-only.
    wire: list[tuple[str, str, str, str]] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = request.read().decode()
        auth = request.headers.get("authorization", "")
        cookie = request.headers.get("cookie", "")
        with lock:
            wire.append((path, auth, body, cookie))
            first_accounts_call = not any(p == "/api/v1/accounts" for p, _, _, _ in wire[:-1])
        if path.endswith("/auth/refresh"):
            return httpx.Response(
                200, json={"access_token": "X-jwt-2", "refresh_token": "X-refresh-2"}
            )
        if path.endswith("/api/v1/accounts"):
            if first_accounts_call:
                b_at_wire.set()
                a_loaded.wait(timeout=5)  # A switches identity while B is here
                return httpx.Response(401, json={"detail": "expired", "code": "x"})
            return httpx.Response(200, json={"items": []})
        return httpx.Response(404, json={})

    bc = _client(handler, monkeypatch)

    def as_request(fn) -> threading.Thread:
        """Run fn as its own /mcp request, the way uvicorn would.

        Two things are reproduced: uvicorn runs each request in a task that
        COPIES the caller's contextvars at spawn (a bare ``Thread(target=fn)``
        starts from an empty context and would miss the regression), and
        ``McpAuthMiddleware`` marks the serving context with
        ``in_mcp_request`` for the duration of the request.
        """
        from mcp_server.auth import in_mcp_request

        def body() -> None:
            in_mcp_request.set(True)
            fn()

        thread = threading.Thread(target=contextvars.copy_context().run, args=(body,))
        thread.start()
        return thread

    def request_b() -> None:
        bc.set_token("X-jwt", "X-refresh")  # B's own login, before it starts
        b_result["b"] = bc.request("GET", "/api/v1/accounts")

    b_result: dict[str, object] = {}
    thread_b = as_request(request_b)
    b_at_wire.wait(timeout=5)

    # Request A: a concurrent auth_login in a different request context.
    as_request(lambda: bc.set_token("Y-jwt", "Y-refresh"))
    a_loaded.set()

    thread_b.join(timeout=10)
    assert not thread_b.is_alive(), "B never finished"

    with lock:
        sent = list(wire)
    # B refreshed and retried under its OWN identity. Pre-fix, the refresh
    # carried A's refresh token, the backend answered with A's access
    # token, and B retried as A.
    #
    # The token travels as a COOKIE, not a JSON body (the API is cookie-only),
    # so the assertion reads the Cookie header.
    refresh_tokens = [
        _refresh_cookie_value(cookie) for path, _, _, cookie in sent if path.endswith("/refresh")
    ]
    assert refresh_tokens == ["X-refresh"], f"refreshed another request's session: {sent}"
    assert [(path, auth) for path, auth, _, _ in sent] == [
        ("/api/v1/accounts", "Bearer X-jwt"),
        ("/api/v1/auth/refresh", ""),
        ("/api/v1/accounts", "Bearer X-jwt-2"),
    ], sent
    assert b_result["b"] == {"items": []}


def test_per_user_request_ignores_a_concurrent_identity_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode 3 stays on the resolved user even if ``set_token`` fires mid-flight.

    The per-user branch already refuses to fall back (M10); this pins that
    the fail-closed answer is still the one returned when a sibling request
    installs a usable-looking shared token in the meantime — the 401 must
    not be "rescued" into a success under that identity.
    """
    from mcp_server.auth import current_user_id

    at_wire = threading.Event()
    swapped = threading.Event()
    seen: list[tuple[str, str]] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        with lock:
            seen.append((path, request.headers.get("authorization", "")))
        if path.endswith("/internal-issue-token"):
            return httpx.Response(200, json={"access_token": "user-2-jwt"})
        if path.endswith("/api/v1/accounts"):
            at_wire.set()
            swapped.wait(timeout=5)
            return httpx.Response(401, json={"detail": "expired", "code": "x"})
        return httpx.Response(404, json={})

    bc = _client(handler, monkeypatch)
    bc.set_token("Y-jwt", "Y-refresh")

    result: dict[str, object] = {}

    def run_b() -> None:
        token_reset = current_user_id.set(2)
        try:
            result["b"] = bc.request("GET", "/api/v1/accounts")
        finally:
            current_user_id.reset(token_reset)

    thread_b = threading.Thread(target=run_b)
    thread_b.start()
    at_wire.wait(timeout=5)
    bc.set_token("Y-jwt", "Y-refresh")
    swapped.set()
    thread_b.join(timeout=10)
    assert not thread_b.is_alive(), "B never finished"

    b_result = result.get("b")
    assert isinstance(b_result, dict)
    assert b_result["error"] == 401
    assert b_result["detail"]["code"] == "mcp_unauthorized"  # type: ignore[index]
    with lock:
        paths = [path for path, _ in seen]
        auths = [auth for _, auth in seen]
    # issue-token + exactly one attempt: no refresh, no retry under Y.
    assert paths == ["/api/v1/auth/internal-issue-token", "/api/v1/accounts"]
    assert auths == ["", "Bearer user-2-jwt"]


def test_token_state_is_not_shared_across_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A /mcp request's identity is isolated from every other request's.

    Two requests run concurrently against the same singleton. Each must see
    and use only its own identity — and, within one request, a login still
    applies to the next tool call (the SDK's per-``call_tool`` context copy
    is INSIDE the request, so the request's own contextvars do carry it).
    """
    from mcp_server.auth import in_mcp_request

    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"items": []})

    bc = _client(handler, monkeypatch)

    def request_context() -> contextvars.Context:
        ctx = contextvars.copy_context()
        ctx.run(in_mcp_request.set, True)
        return ctx

    request_a = request_context()
    request_b = request_context()

    # Request A logs in, then uses the identity on a later tool call.
    request_a.run(bc.set_token, "tenant-one", "refresh-one")
    request_b.run(bc.set_token, "tenant-two", "refresh-two")

    assert request_a.run(lambda: bc.token) == "tenant-one"
    assert request_b.run(lambda: bc.token) == "tenant-two"
    assert request_a.run(lambda: bc.refresh_token) == "refresh-one"
    assert request_b.run(lambda: bc.refresh_token) == "refresh-two"

    # And each request's wire call carries its own bearer.
    request_a.run(lambda: bc.request("GET", "/api/v1/accounts"))
    request_b.run(lambda: bc.request("GET", "/api/v1/accounts"))
    assert sent == ["Bearer tenant-one", "Bearer tenant-two"]


def test_session_identity_persists_outside_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdio: the SDK gives each ``call_tool`` a fresh context, and a login
    must STILL apply to the next tool call.

    Measured on the SDK: ``mcp.call_tool`` copies the context per call, so a
    purely contextvar-backed identity would be lost between ``auth_login``
    and the tool that uses it. Outside a /mcp request the identity is held
    on the client instead. This is the documented stdio behaviour (docs/23
    mode matrix) and must not regress while the HTTP path stays isolated.
    """
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"items": []})

    bc = _client(handler, monkeypatch)

    # Each call runs in its OWN context, as the SDK's call_tool does.
    contextvars.copy_context().run(lambda: bc.set_token("stdio-jwt", "stdio-refresh"))
    assert bc.token == "stdio-jwt"
    assert bc.refresh_token == "stdio-refresh"

    contextvars.copy_context().run(lambda: bc.request("GET", "/api/v1/accounts"))
    assert sent == ["Bearer stdio-jwt"]


# ---------------------------------------------------------------- M84 ------


def test_shared_jar_never_stores_a_response_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """F-1: the shared jar must absorb NOTHING, ever.

    One `BackendClient` serves every tenant, and httpx extracts each
    response's `Set-Cookie` into that client's jar and replays it on any
    later request whose path matches the cookie's `Path`. A jar that stores
    is therefore a cross-tenant credential store. Every auth response keeps
    its own token in the response object instead.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie", ""))
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(
                200,
                json={"access_token": "a-access"},
                headers=[
                    (
                        "set-cookie",
                        "refresh_token=a-refresh; Path=/api/v1/auth; HttpOnly",
                    )
                ],
            )
        return httpx.Response(200, json={"ok": True})

    bc = _client(handler, monkeypatch)
    bc.request("POST", "/api/v1/auth/login", json={"email": "a@x.z", "password": "pw"})

    assert list(bc._http.cookies.jar) == [], "a response cookie reached the shared jar"
    # And it was not replayed onto the next request.
    bc.request("GET", "/api/v1/auth/refresh")
    assert seen[-1] == "", "the jar replayed one tenant's cookie onto another request"


def test_refresh_cookie_comes_from_the_caller_not_the_jar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The F-1 leak, stated as the assertion it now has to satisfy.

    Tenant A's refresh token is sitting in the jar (what the pre-fix code
    read first). Tenant B asks for a refresh of ITS OWN token. B must be
    answered with B's token — and if B holds no token at all, the request
    must go out with NO refresh cookie rather than silently spending A's.
    """
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        sent.append(cookie)
        if request.url.path.endswith("/auth/refresh"):
            if "refresh_token=tenant-a" in cookie:
                return httpx.Response(200, json={"access_token": "A-access"})
            if "refresh_token=tenant-b" in cookie:
                return httpx.Response(200, json={"access_token": "B-access"})
            return httpx.Response(
                401, json={"detail": "Missing refresh token", "code": "invalid_token"}
            )
        return httpx.Response(200, json={"ok": True})

    bc = _client(handler, monkeypatch)
    # Tenant A's cookie, planted the way the pre-fix shared jar held it.
    bc._http.cookies.set("refresh_token", "tenant-a", path="/api/v1/auth")

    # (a) B holds its own token -> B's token is what goes out. The pre-fix
    # code preferred the jar first, so this is the F-1 assertion.
    bc.set_token("b-jwt", "tenant-b")
    assert bc._refresh_cookie("tenant-b") == {"Cookie": "refresh_token=tenant-b"}
    assert bc._refresh_cookie() == {"Cookie": "refresh_token=tenant-b"}

    from mcp_server import server as server_mod

    monkeypatch.setattr(server_mod, "client", bc)
    server_mod.auth_refresh("tenant-b")  # type: ignore[arg-type]
    assert sent[-1] == "refresh_token=tenant-b"
    assert "tenant-a" not in sent[-1], "the jar's tenant A cookie won over the caller"

    # (b) B holds no token -> the header is simply omitted. There is no jar
    # value to fall back on any more (M84): `_refresh_cookie` never reads it.
    bc.set_token("b-jwt")
    bc.refresh_token = None  # set_token only overwrites when given a value
    assert bc._refresh_cookie() == {}, "the jar was consulted for a refresh token"
    assert bc._refresh_cookie(None) == {}

    # Wire level, with the jar in its PRODUCTION state (always empty — the
    # no-store jar absorbs nothing): no cookie header goes out at all.
    bc._http.cookies.clear()
    server_mod.auth_refresh()  # type: ignore[arg-type]
    assert sent[-1] == "", "a token-less refresh still emitted a refresh cookie"


def test_refresh_cookie_is_read_off_the_response_not_the_jar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotation lands in the caller's own scoped slot.

    The refresh response's rotated successor arrives as a `Set-Cookie`; it
    must be readable for the next rotation WITHOUT the jar holding anything.
    """
    refresh_cookies: list[str] = []
    attempts = {"accounts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/refresh"):
            refresh_cookies.append(request.headers.get("cookie", ""))
            return httpx.Response(
                200,
                json={"access_token": "new-jwt"},
                headers=[
                    (
                        "set-cookie",
                        "refresh_token=rotated-2; Path=/api/v1/auth; HttpOnly",
                    )
                ],
            )
        attempts["accounts"] += 1
        if attempts["accounts"] == 1:
            return httpx.Response(401, json={"detail": "expired", "code": "x"})
        assert request.headers.get("authorization") == "Bearer new-jwt"
        return httpx.Response(200, json={"items": []})

    bc = _client(handler, monkeypatch)
    bc.set_token("old-jwt", "rotated-1")

    assert bc.request("GET", "/api/v1/accounts") == {"items": []}
    assert refresh_cookies == ["refresh_token=rotated-1"]
    assert bc.refresh_token == "rotated-2", "the successor was not absorbed off the response"
    assert list(bc._http.cookies.jar) == [], "rotation still leaned on the shared jar"

    # The NEXT rotation spends the successor, not the revoked predecessor.
    attempts["accounts"] = 0
    assert bc.request("GET", "/api/v1/accounts") == {"items": []}
    assert refresh_cookies[-1] == "refresh_token=rotated-2"


def test_logout_clear_cookie_empties_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit `Max-Age=0` clear must drop the token, not be ignored.

    The cookiejar drops an expired cookie, so reading `resp.cookies` would
    report "no cookie" and leave a revoked session token behind for a later
    refresh to replay.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/logout"):
            return httpx.Response(
                204,
                headers=[
                    (
                        "set-cookie",
                        "refresh_token=; Path=/api/v1/auth; Max-Age=0; HttpOnly",
                    )
                ],
            )
        return httpx.Response(401, json={"detail": "x", "code": "x"})

    bc = _client(handler, monkeypatch)
    bc.set_token("jwt", "session-refresh")

    bc.request("POST", "/api/v1/auth/logout")
    assert bc.refresh_token is None, "logout left a refresh token behind"


def test_refresh_cookie_never_lands_in_request_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit M1: the token travels ONLY as a cookie, never in the body.

    A body token would land in the proxy's request logs. Asserted here on the
    wire bytes, not just the parsed dict.
    """
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(bytes(request.content))
        return httpx.Response(200, json={"access_token": "x", "revoked": True})

    bc = _client(handler, monkeypatch)
    from mcp_server import server as server_mod

    monkeypatch.setattr(server_mod, "client", bc)
    bc.set_token("jwt", "session-refresh")
    server_mod.auth_refresh()  # type: ignore[arg-type]

    assert bodies, "no request reached the backend"
    for body in bodies:
        assert b"refresh_token" not in body, body

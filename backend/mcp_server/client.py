"""Thin HTTP client for the fin_tracker backend."""

from __future__ import annotations

import contextvars
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

log = logging.getLogger(__name__)

# The cookie the API sets for session rotation (app/api/auth.py REFRESH_COOKIE).
REFRESH_COOKIE = "refresh_token"


class _NoStoreCookies(httpx.Cookies):
    """A jar that never absorbs a response's ``Set-Cookie`` (M84, docs/40 §2).

    ``BackendClient`` is ONE module-level ``httpx.Client`` shared by every
    tenant, and httpx extracts each response's cookies into that shared jar
    and re-sends them on any later request whose path the cookie's ``Path``
    matches. A jar that stores anything is therefore a cross-tenant
    credential store — the F-1 leak: tenant B's ``auth_refresh`` returned
    tenant A's access token because the jar held A's session.

    Refresh state lives in the request/session-scoped ``refresh_token``
    ContextVar instead, and a response's cookie is read off that response
    (see ``BackendClient._absorb_refresh_cookie``), so the jar has no
    remaining job. Overriding ``extract_cookies`` rather than clearing the
    jar is what makes that true for every concurrent request at once.
    """

    def extract_cookies(self, response: httpx.Response) -> None:
        """Deliberately does nothing — see the class docstring."""


def new_http_client(**kwargs: Any) -> httpx.Client:
    """An ``httpx.Client`` whose shared jar can never hold a credential.

    ``Client.__init__`` re-wraps whatever ``cookies`` it is handed in a plain
    ``httpx.Cookies`` (``httpx/_client.py``: ``self._cookies =
    Cookies(cookies)``), so the no-store jar is installed AFTER construction.
    Tests build their mock-transport client through this helper too, so the
    whole suite runs against the real jar policy.
    """
    http = httpx.Client(**kwargs)
    http._cookies = _NoStoreCookies()
    return http


def refresh_cookie_from_response(resp: httpx.Response) -> tuple[bool, str | None]:
    """``(seen, value)`` for this response's ``refresh_token`` Set-Cookie.

    Read off the RAW headers rather than ``resp.cookies``: an explicit clear
    (logout's empty ``Max-Age=0`` cookie) is dropped by the cookiejar as
    expired, and "no token left" has to be distinguishable from "no cookie in
    this response". ``seen`` False means the response set no such cookie at
    all, so the caller's current slot is left untouched; ``seen`` True with
    ``value`` None means the session was ended and the slot must be emptied.
    """
    seen = False
    value: str | None = None
    for header in resp.headers.get_list("set-cookie"):
        first, _, _ = header.partition(";")
        name, _, raw = first.partition("=")
        if name.strip() != REFRESH_COOKIE:
            continue
        seen = True
        value = raw.strip().strip('"') or None
    return seen, value


# The three auth modes of docs/23 — mutually exclusive. A mode counts as
# "configured" when ANY of its variables is set (a half-set auto-login pair is
# still an auto-login attempt, not silence).
MODE_ENV_VARS: dict[str, tuple[str, ...]] = {
    "static_token": ("FIN_TRACKER_TOKEN",),
    "auto_login": ("FIN_TRACKER_EMAIL", "FIN_TRACKER_PASSWORD"),
    "mcp_token": ("MCP_INTERNAL_SECRET",),
}


class AuthModeError(RuntimeError):
    """More than one MCP auth mode configured — refuse to start (M11, docs/23)."""


def configured_auth_modes(env: Mapping[str, str] | None = None) -> list[str]:
    """Names of the auth modes configured by env vars (values never read out)."""
    source = os.environ if env is None else env
    return [mode for mode, vars_ in MODE_ENV_VARS.items() if any(source.get(v) for v in vars_)]


def _mixed_modes_message(modes: Sequence[str]) -> str:
    detail = "; ".join(f"{mode} via {' or '.join(MODE_ENV_VARS[mode])}" for mode in modes)
    return (
        f"MCP auth modes are mutually exclusive (docs/23) but {len(modes)} are "
        f"configured: {detail}. Set variables for exactly one mode and restart."
    )


class BackendClient:
    """Wraps httpx to proxy MCP tool calls to the running FastAPI backend.

    Auth modes (mutually exclusive — enforced at startup via AuthModeError):
    1. FIN_TRACKER_TOKEN — static token, used directly
    2. FIN_TRACKER_EMAIL + FIN_TRACKER_PASSWORD — auto-login at startup
    3. MCP token resolved by TokenVerifier → internal-issue-token endpoint

    Identity state is **scoped to the serving request** whenever one is in
    flight, and process-level otherwise.

    The module-level singleton is shared by every concurrent
    streamable-http request, and each ASGI task runs in its own
    ``contextvars`` copy, so ``token`` / ``refresh_token`` are held in a
    ContextVar while ``auth.in_mcp_request`` is set: a concurrent
    ``auth_login`` (or ``auth_demo`` / ``auth_refresh`` /
    ``users_change_password``) in another request can no longer retarget a
    request that is already on the wire.

    **Cookie state is scoped the same way, and never shared (M84).** The
    httpx client itself keeps NO cookies: a shared jar would re-send one
    tenant's ``Set-Cookie`` to the next tenant's auth call (F-1, the
    cross-tenant credential leak). The refresh cookie a response sets is
    read off that response and stored in the same scoped
    ``refresh_token`` slot as the access token.

    Outside an HTTP request (stdio — one long-lived session, and the SDK's
    per-``call_tool`` copied contexts there) the same properties hold the
    session identity, so a login still applies to the next tool call.
    """

    def __init__(self) -> None:
        modes = configured_auth_modes()
        if len(modes) > 1:
            # Fail at startup (module import builds the singleton below),
            # before any request can run under a blended identity (M11).
            raise AuthModeError(_mixed_modes_message(modes))
        self.base_url = os.getenv("FIN_TRACKER_URL", "http://localhost:8000").rstrip("/")
        self._token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            "mcp_backend_client_token"
        )
        self._refresh_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            "mcp_backend_client_refresh_token"
        )
        # Session-level identity, used when no /mcp request is in flight.
        self._session_token: str | None = None
        self._session_refresh_token: str | None = None
        self._internal_secret = os.getenv("MCP_INTERNAL_SECRET", "")
        # Serializes rotating-refresh spends process-wide (rotation invalidates
        # the predecessor, so two concurrent spends would revoke the session).
        self._refresh_lock = threading.Lock()
        # No-store jar (M84): the shared client must never hold a cookie.
        self._http = new_http_client(base_url=self.base_url, timeout=30)
        static_token = os.getenv("FIN_TRACKER_TOKEN")
        if static_token:
            self.set_token(static_token)
        elif not self.token:
            # Auto-login from email/password env vars if no static token.
            self._auto_login()

    # -- identity state (request-scoped under HTTP, session-scoped on stdio) --

    @staticmethod
    def _request_scoped() -> bool:
        """True when a /mcp request is being served in this context."""
        try:
            from mcp_server.auth import in_mcp_request
        except ImportError:  # pragma: no cover - auth is a sibling module
            return False
        return in_mcp_request.get()

    @property
    def token(self) -> str | None:
        """Access token for the current request (or session on stdio)."""
        if self._request_scoped():
            return self._token.get(None)
        return self._session_token

    @token.setter
    def token(self, value: str | None) -> None:
        if self._request_scoped():
            self._token.set(value)
        else:
            self._session_token = value

    @property
    def refresh_token(self) -> str | None:
        """Rotating refresh token for the current request (or session)."""
        if self._request_scoped():
            return self._refresh_token.get(None)
        return self._session_refresh_token

    @refresh_token.setter
    def refresh_token(self, value: str | None) -> None:
        if self._request_scoped():
            self._refresh_token.set(value)
        else:
            self._session_refresh_token = value

    # -- auth helpers --------------------------------------------------------

    def _absorb_refresh_cookie(self, resp: httpx.Response, body_token: str | None = None) -> None:
        """Store THIS response's refresh token in the caller's scoped slot.

        Auth responses deliver the refresh token only as an ``httpOnly``
        cookie (audit M1 — bodies exclude it), so the successor has to be
        picked up somewhere. It used to be picked up out of the shared jar,
        which is exactly how one tenant came to hold another's session (F-1).
        Reading the response object instead keeps the value bound to the
        request that received it: under HTTP the write lands in the
        request-scoped ContextVar (dead when the request ends), on stdio in
        the single session identity.

        ``body_token`` is the legacy body field, honoured only when no cookie
        arrived — the cookie channel is the authoritative one (M1).

        An explicit CLEAR (logout's empty ``Max-Age=0`` cookie) drops the
        token so a later refresh cannot replay a revoked session.
        """
        seen, value = refresh_cookie_from_response(resp)
        if seen:
            self.refresh_token = value
        elif body_token:
            self.refresh_token = body_token

    def _auto_login(self) -> None:
        """Login using FIN_TRACKER_EMAIL / FIN_TRACKER_PASSWORD env vars."""
        email = os.getenv("FIN_TRACKER_EMAIL")
        password = os.getenv("FIN_TRACKER_PASSWORD")
        if not email or not password:
            return
        try:
            resp = self._http.post(
                "/api/v1/auth/login",
                json={"email": email, "password": password},
            )
            if resp.status_code == 200:
                data = resp.json()
                self._absorb_refresh_cookie(resp, data.get("refresh_token"))
                self.set_token(data["access_token"])
                log.info("Auto-logged in as %s", email)
            else:
                log.warning("Auto-login failed: %s", resp.status_code)
        except Exception as e:
            log.warning("Auto-login connection error: %s", e)

    def set_token(self, token: str, refresh_token: str | None = None) -> None:
        self.token = token
        if refresh_token is not None:
            self.refresh_token = refresh_token

    def _refresh_cookie(self, refresh_token: str | None = None) -> dict[str, str]:
        """Cookie header carrying the refresh token for an auth request.

        The API reads the refresh token ONLY from the httpOnly cookie; a body
        token is ignored (and would leak the token into request logs).

        **Only the caller-bound token is ever used (M84, docs/40 §2).** The
        argument wins; otherwise the caller's own scoped ``refresh_token``
        (request-scoped under HTTP, the session's own on stdio). The shared
        httpx jar is never consulted — preferring it here was the F-1
        cross-tenant leak, and it is gone for good: with no token in either
        slot the header is simply omitted and the API answers 401.

        The cookie is set explicitly rather than left to the jar because
        httpx drops `Secure` cookies on a plain-http request, and
        `FIN_TRACKER_URL` is http://api:8000 inside the compose network.
        """
        value = refresh_token or self.refresh_token
        if not value:
            return {}
        return {"Cookie": f"refresh_token={value}"}

    def _try_refresh(self, refresh_token: str) -> str | None:
        """Rotate ONE refresh token, under the lock. Returns the new access token.

        ``refresh_token`` is passed in, not read off ``self``: the caller
        already snapshotted the identity it started with, so a concurrent
        request's login cannot substitute its token here. The lock serializes
        rotation so two in-flight requests in the SAME context cannot both
        spend one rotating refresh token (the second would trip
        reuse-detection and revoke the session). Returns ``None`` on any
        failure — the caller then reports the original 401.
        """
        with self._refresh_lock:
            try:
                resp = self._http.post(
                    "/api/v1/auth/refresh",
                    headers=self._refresh_cookie(refresh_token),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # Body excludes the refresh token (M1); the rotated
                    # successor arrives as a Set-Cookie on THIS response and
                    # is absorbed into the caller's own scoped slot — storing
                    # the stale predecessor would trip reuse-detection and
                    # revoke the session.
                    self._absorb_refresh_cookie(resp, data.get("refresh_token"))
                    self.set_token(data["access_token"])
                    return str(data["access_token"])
            except Exception:
                log.debug("refresh attempt failed", exc_info=True)
        return None

    def _get_token_for_user(self, user_id: int) -> str | None:
        """Get a backend JWT for a user via the internal endpoint."""
        if not self._internal_secret:
            return None
        try:
            resp = self._http.post(
                "/api/v1/auth/internal-issue-token",
                json={"user_id": user_id},
                headers={"X-Internal-Secret": self._internal_secret},
            )
            if resp.status_code == 200:
                return resp.json().get("access_token")
        except Exception as e:
            log.warning("Internal token issue failed: %s", e)
        return None

    def post_internal(self, path: str, payload: dict) -> bool:
        """POST to a backend internal endpoint behind `MCP_INTERNAL_SECRET`.

        Service-to-service (M80, docs/40 §3): these routes authorize on the
        shared secret alone, so NO Authorization header travels with them —
        an audit receipt must not be scoped to whichever backend JWT happens
        to be in flight. Returns False when the secret is unset (stdio modes
        1/2) or the call failed; the caller decides whether that is fatal
        (identity issuance: fail closed; audit: best effort).
        """
        if not self._internal_secret:
            return False
        try:
            resp = self._http.post(
                path, json=payload, headers={"X-Internal-Secret": self._internal_secret}
            )
        except Exception as exc:
            log.warning("Internal POST %s failed: %s", path, exc)
            return False
        if resp.status_code not in (200, 204):
            log.warning("Internal POST %s answered %s", path, resp.status_code)
            return False
        return True

    # -- core request --------------------------------------------------------

    def request(self, method: str, path: str, **kwargs: object) -> dict | list | str:
        """Make an HTTP request; refresh once, but never switch identity.

        Fail-closed on 401 (M10): a call that ran as the middleware-resolved
        MCP user (mode 3) whose backend JWT is rejected returns a structured
        auth error — the shared static/auto-login token belongs to a different
        identity's tenant and is never retried as a fallback. Only a request
        that already used the shared token may refresh it (same identity).

        The identity is snapshotted on entry and that snapshot is used for
        every hop of THIS call. A concurrent request's ``set_token`` (its own
        ``auth_login``/``auth_refresh``) must not be able to retarget a request
        already on the wire — that would execute it in another tenant.
        """
        # Check if there's a resolved MCP user (from TokenVerifier).
        token = self.token
        refresh_token = self.refresh_token  # snapshot: the identity we started with
        resolved_user: int | None = None
        try:
            from mcp_server.auth import current_user_id

            resolved_user = current_user_id.get(None)
        except ImportError:
            resolved_user = None
        if resolved_user is not None:
            # Get a fresh backend JWT for this user.
            token = self._get_token_for_user(resolved_user)
            if not token:
                # Fail closed (docs/29 F4): issuing the per-user token
                # failed (backend 403, network error, unset secret).
                # Falling back to self.token would execute another
                # tenant's context — never do that.
                raise RuntimeError(
                    "MCP token resolved to a user but no backend token "
                    f"could be issued for user_id={resolved_user}; refusing request"
                )

        headers: dict[str, str] = {}
        # Caller-supplied headers (e.g. the auth tools' refresh cookie) are
        # merged FIRST, and `request()` owns the Authorization header: it is
        # set AFTER the merge so a caller can never override the identity this
        # method just resolved. Merging the other way round would let a `headers`
        # argument silently re-point the call at another tenant's context —
        # `request()` is the single choke point for every MCP tool call, so the
        # invariant is enforced here rather than trusted to each call site.
        extra_headers = kwargs.pop("headers", None)
        if isinstance(extra_headers, dict):
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
        # Defensive: a caller dict is untrusted input, so drop the key outright
        # rather than relying on ordering alone.
        headers.pop("Authorization", None)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = self._http.request(method, path, headers=headers, **kwargs)
        # Any Set-Cookie this response carries belongs to THIS request's
        # identity — absorb it into the scoped slot (M84): on the HTTP path
        # that is the request's own ContextVar, never a process-wide jar.
        self._absorb_refresh_cookie(resp)
        if resp.status_code == 401:
            if resolved_user is not None:
                # Fail closed (M10): per-user identity rejected. Retrying with
                # self.token would execute the static/auto-login tenant —
                # return a clear auth error instead, with no retry.
                return {
                    "error": 401,
                    "detail": {
                        "detail": (
                            "Backend rejected the per-user token for "
                            f"user_id={resolved_user}; refusing to fall back "
                            "to another identity"
                        ),
                        "code": "mcp_unauthorized",
                    },
                }
            # Same identity only: rotate the refresh token THIS request
            # started with, never one a concurrent request installed.
            if refresh_token:
                refreshed = self._try_refresh(refresh_token)
                if refreshed:
                    headers["Authorization"] = f"Bearer {refreshed}"
                    resp = self._http.request(method, path, headers=headers, **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            return {"error": resp.status_code, "detail": detail}
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return resp.text


client = BackendClient()

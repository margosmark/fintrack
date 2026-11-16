"""OIDC client — discovery, code exchange, ID-token verification (M74, docs/38).

This module is the ONLY place the auth code performs network I/O, and it only
runs when `OIDC_ENABLED=true`. Every URL it fetches comes from operator
config (`OIDC_ISSUER` and that issuer's own discovery document) — never from a
request parameter — so the SSRF posture is the trusted-operator one already
used for `LLM_BASE_URL`.

Tests inject an `httpx.MockTransport` through `_transport`, which keeps the
whole flow exercisable in-process with no real provider.
"""

import json
import time
from typing import Any

import httpx
import jwt
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from app.core.config import get_settings

# Accepted ID-token signature algorithms: RS256 (Authelia/Keycloak default)
# and ES256 (the other common provider default). `none` is never accepted.
ACCEPTED_ALGS = ("RS256", "ES256")
_ALG_KTY = {"RS256": "RSA", "ES256": "EC"}
_CACHE_TTL_SECONDS = 3600
_TIMEOUT_SECONDS = 10.0

# Test seam: an httpx.MockTransport (or None for real sockets).
_transport: httpx.BaseTransport | None = None
# issuer -> (fetched_at, discovery doc); issuer -> (fetched_at, JWKS doc)
_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, dict]] = {}


class OidcError(Exception):
    """Provider-side failure. `redirect_code` is the docs/38 code the browser
    callback carries as `?error=`; the same value is used for the envelope on
    the `/auth/oidc/login` path."""

    def __init__(self, redirect_code: str, detail: str) -> None:
        self.redirect_code = redirect_code
        self.detail = detail
        super().__init__(detail)


def clear_cache() -> None:
    """Drop cached discovery/JWKS (tests, and operators flipping config)."""
    _discovery_cache.clear()
    _jwks_cache.clear()


def _client() -> httpx.Client:
    return httpx.Client(transport=_transport, timeout=_TIMEOUT_SECONDS)


def _normalize(issuer: str) -> str:
    return issuer.rstrip("/")


def discovery(issuer: str) -> dict:
    """Fetch (and cache for an hour) the provider's discovery document."""
    issuer = _normalize(issuer)
    if not issuer:
        raise OidcError("oidc_provider_error", "OIDC_ISSUER is not configured")
    cached = _discovery_cache.get(issuer)
    if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    url = f"{issuer}/.well-known/openid-configuration"
    try:
        with _client() as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise OidcError("oidc_provider_error", f"discovery unreachable: {exc}") from exc
    if response.status_code != 200:
        raise OidcError(
            "oidc_provider_error",
            f"discovery returned HTTP {response.status_code}",
        )
    try:
        doc = response.json()
        doc_issuer = _normalize(str(doc["issuer"]))
        str(doc["authorization_endpoint"])
        str(doc["token_endpoint"])
        str(doc["jwks_uri"])
    except (ValueError, KeyError, TypeError) as exc:
        raise OidcError("oidc_provider_error", "malformed discovery document") from exc
    # The discovery document's own `issuer` must match the configured one —
    # otherwise a mispointed OIDC_ISSUER could trust another provider's keys.
    if doc_issuer != issuer:
        raise OidcError(
            "oidc_provider_error",
            "discovery issuer does not match OIDC_ISSUER",
        )
    _discovery_cache[issuer] = (time.monotonic(), doc)
    return doc


def authorize_url(issuer: str, client_id: str, redirect_uri: str, state: str, nonce: str) -> str:
    """The provider's authorization endpoint with the code-flow parameters."""
    from urllib.parse import urlencode

    doc = discovery(issuer)
    params = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": get_settings().oidc_scopes,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
        }
    )
    return f"{doc['authorization_endpoint']}?{params}"


def _token_request(
    token_endpoint: str,
    payload: dict[str, str],
    *,
    basic_auth: bool,
) -> httpx.Response:
    """One token-endpoint call, credentials sent either as Basic (the
    client_secret_basic default of Keycloak-class providers) or in the form
    body (client_secret_post)."""
    body = dict(payload)
    kwargs: dict[str, Any] = {"headers": {"Accept": "application/json"}}
    if basic_auth:
        kwargs["auth"] = httpx.BasicAuth(payload["client_id"], payload["client_secret"])
        body.pop("client_secret", None)
    with _client() as client:
        return client.post(token_endpoint, data=body, **kwargs)


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Authorization-code exchange -> `{access_token, id_token, ...}`.

    Tries client_secret_basic first and falls back to client_secret_post on a
    400/401/403 — the two methods are used in SEQUENTIAL requests, never
    together in one (RFC 6749 §2.3.1 forbids multiple methods per request),
    which is what lets one deployment serve providers with either default.
    """
    settings = get_settings()
    doc = discovery(settings.oidc_issuer)
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": settings.oidc_client_id,
        "client_secret": settings.oidc_client_secret,
    }
    response = _token_request(doc["token_endpoint"], payload, basic_auth=True)
    if response.status_code in (400, 401, 403):
        response = _token_request(doc["token_endpoint"], payload, basic_auth=False)
    if response.status_code != 200:
        raise OidcError(
            "oidc_provider_error",
            f"token endpoint returned HTTP {response.status_code}",
        )
    try:
        tokens = response.json()
    except ValueError as exc:
        raise OidcError("oidc_provider_error", "token response is not JSON") from exc
    if not isinstance(tokens.get("id_token"), str):
        raise OidcError("oidc_provider_error", "token response has no id_token")
    return tokens


def _select_key(jwks: dict, kid: str | None, alg: str) -> Any:
    """The one JWKS key that can verify this token, or OidcError."""
    keys = jwks.get("keys")
    if not isinstance(keys, list) or not keys:
        raise OidcError("oidc_token_invalid", "provider published no keys")
    wanted_kty = _ALG_KTY[alg]
    candidates = [
        k
        for k in keys
        if isinstance(k, dict) and k.get("kty") == wanted_kty and k.get("alg", alg) == alg
    ]
    if kid is not None:
        candidates = [k for k in candidates if k.get("kid") == kid]
    if len(candidates) != 1:
        raise OidcError(
            "oidc_token_invalid",
            "no matching signing key for the id_token",
        )
    jwk = candidates[0]
    try:
        factory = RSAAlgorithm.from_jwk if wanted_kty == "RSA" else ECAlgorithm.from_jwk
        return factory(json.dumps(jwk))
    except (ValueError, KeyError, TypeError) as exc:
        raise OidcError("oidc_token_invalid", "unusable signing key") from exc


def verify_id_token(id_token: str, *, nonce: str) -> dict:
    """Full ID-token validation: JWKS signature, iss, aud, exp, nonce.

    Returns the claims on success; raises OidcError(`oidc_token_invalid`) on
    every failure — signature, issuer, audience, expiry and nonce are all
    checked before a single claim is trusted.
    """
    settings = get_settings()
    issuer = _normalize(settings.oidc_issuer)
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OidcError("oidc_token_invalid", "id_token is not a JWT") from exc
    alg = header.get("alg")
    if alg not in ACCEPTED_ALGS:
        raise OidcError("oidc_token_invalid", f"unsupported id_token alg {alg!r}")

    doc = discovery(issuer)
    jwks_url = str(doc["jwks_uri"])
    cached = _jwks_cache.get(jwks_url)
    if cached is None or time.monotonic() - cached[0] >= _CACHE_TTL_SECONDS:
        try:
            with _client() as client:
                response = client.get(jwks_url)
        except httpx.HTTPError as exc:
            raise OidcError("oidc_provider_error", f"jwks unreachable: {exc}") from exc
        if response.status_code != 200:
            raise OidcError("oidc_provider_error", f"jwks HTTP {response.status_code}")
        try:
            _jwks_cache[jwks_url] = (time.monotonic(), response.json())
        except ValueError as exc:
            raise OidcError("oidc_provider_error", "jwks is not JSON") from exc
    jwks = _jwks_cache[jwks_url][1]

    key = _select_key(jwks, header.get("kid"), alg)
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=[alg],
            audience=settings.oidc_client_id,
            issuer=issuer,
            leeway=60,
            options={"require": ["exp", "iat", "iss", "aud", "nonce", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OidcError("oidc_token_invalid", f"id_token rejected: {exc}") from exc
    if claims.get("nonce") != nonce:
        raise OidcError("oidc_token_invalid", "nonce mismatch")
    return claims

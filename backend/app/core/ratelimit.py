"""slowapi rate limiting shared by the auth and import endpoints — security review #1.

In-memory per-process storage (single-container deploy); if multi-worker
uvicorn is ever adopted, swap in a Redis storage_uri. Enabled state is read
per-request by the decorator wrapper and flipped live in main.lifespan so
tests can opt in via settings without rebuilding the app.

Buckets are keyed (client key, route function): the Limiter uses
``key_style="endpoint"`` so routes whose path carries a parameter (e.g.
``/imports/{batch_id}/commit``) share ONE bucket per client instead of
minting a fresh bucket per path — a path parameter can never be used to
multiply a client's limit. Rate limiting is on unless settings
``rate_limit_enabled`` is false (config default true); tests/conftest sets
``RATE_LIMIT_ENABLED=false`` and tests opt in by flipping ``limiter.enabled``.

Two extra shapes beyond ``human_paced_limit`` (security t_95eb8caf,
F3-a/F3-b): ``shared_limit`` binds SEVERAL routes to one named bucket, and
``_tenant_key`` keys a bucket per authenticated tenant instead of per client
IP — needed where the caller has no client IP of its own (the MCP server
calls the API from the platform's own container address).
"""

from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from app.core.rls import tenant_ctx


def _client_key(request: Request) -> str:
    """Rightmost X-Forwarded-For entry; fall back to the socket peer, then "?".

    SECURITY (audit M3 — resolved): the chain is single-trusted-hop by
    construction. The front proxy terminates TLS and sets
    X-Forwarded-For to the real client; the in-repo nginx rewrites
    ``$remote_addr`` from XFF ONLY for that peer (``set_real_ip_from``
    naming the trusted proxy hop + ``real_ip_header`` + ``real_ip_recursive``) and then
    REPLACES the header with ``proxy_set_header X-Forwarded-For
    $remote_addr`` — so the rightmost entry seen here is always one real
    client, and a client-supplied XFF never reaches the API. Direct LAN
    callers (not the proxy) keep their socket IP and cannot spoof.
    Accepted residual (see the docker-compose.yml ``web`` ports comment): no
    host-firewall or DOCKER-USER source restriction exists, so the listener
    is published to the whole LAN in cleartext and any LAN host can reach it
    directly, not only the front proxy — that exposure is documented, not
    enforced away. It does not weaken this key: reachability is not trust, and
    the realip rules above still ignore XFF from every peer but the trusted hop,
    so a direct LAN caller keeps its socket IP and cannot spoof its bucket.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[-1].strip()
    if request.client is not None:
        return request.client.host
    return "?"


def _no_forwarded_hop(request: Request) -> bool:
    """True when the request reached us DIRECTLY, with no proxy hop to key on.

    That is the MCP server's own call into the API (docs/59 §8): its socket
    address is the platform's container address, not any tenant's, so a
    per-client-IP ceiling measured there would be ONE bucket for every tenant
    on the instance — a single looping connector would consume everyone's
    propose budget (security F3-b, t_95eb8caf). The per-IP ceiling is skipped
    for such a request and the per-tenant ceiling below carries it instead.
    """
    return not (request.headers.get("x-forwarded-for") or "").strip()


def _tenant_key(request: Request) -> str:
    """Rate-limit key per AUTHENTICATED tenant: the RLS `tenant_ctx`.

    Set only from a verified access / MCP policy token (docs/07 §RLS) — never
    from a client-supplied header — so it cannot be reset the way an
    X-Forwarded-For hop can: one caller minting a fresh XFF per request still
    hits the same 12/minute tenant bucket. Falls back to the client key when
    nothing is bound; an unauthenticated request never reaches a limited
    endpoint (auth answers 401 first).
    """
    return tenant_ctx.get() or _client_key(request)


limiter = Limiter(
    key_func=_client_key,
    storage_uri="memory://",
    enabled=False,
    key_style="endpoint",
)

# Human-paced per-IP limits for the mutating ingestion endpoints (docs/03
# "Rate limits"): a few requests per minute — enough headroom for a person
# importing statements one at a time, useless as a CPU/LLM amplifier.
IMPORT_UPLOAD_LIMIT = "6/minute"  # /imports/upload, /imports/suggest-profile, /trades/import
IMPORT_MUTATE_LIMIT = "10/minute"  # /imports/{id}/commit | /discard | /revert
# Bank sync (docs/39 § Rate limits): every pull is an outbound provider call,
# so the mutating sync routes get the same human-paced ceiling as a commit.
SYNC_MUTATE_LIMIT = "10/minute"  # /sync/agreements | /{id}/pull | /{id}/revoke
# Household sharing (docs/43 §11): the invite route is the spam avenue — a
# single owner must not mint an unbounded pile of live tokens (the 5-outstanding
# cap and one-active-link-per-side partial unique cover the hours timescale).
SHARING_INVITE_LIMIT = "5/minute"  # POST /sharing/invites
SHARING_MUTATE_LIMIT = "10/minute"  # accept | cancel | scope | revoke
# Receipt images (M108 P4, docs/09 §Receipt images): a 2 MB upload is a
# memory/IO amplifier if it can be replayed — a person attaches receipts one
# at a time, so the ceiling is generous for a human and useless as a soak.
RECEIPT_UPLOAD_LIMIT = "12/minute"  # PUT | DELETE /transactions/{id}/receipt
# Self-service credential routes (docs/38, docs/20). These verify the CURRENT
# password with a full bcrypt-cost hash on every call, so without a ceiling a
# stolen-but-undetected access token buys an unthrottled offline-speed oracle
# over the account's existing password — the exact hole the login limiter
# exists to close, left open on the other side of authentication. 10/minute is
# far above what a person changing a password or an email actually does.
CREDENTIAL_MUTATE_LIMIT = (
    "10/minute"  # POST /users/me/change-password | PATCH /users/me/profile | DELETE /users/me
)
# Manual FX entry (docs/07). `fx_rates` is the ONLY table with no `user_id` —
# it is the shared cross-tenant cache every tenant's valuations read — and
# `POST /fx` is the only write path any tenant has into it. Unthrottled, a
# freshly registered account could fill it with unbounded rows keyed on
# arbitrary currency codes. Human-paced: a person fills a gap or two.
FX_WRITE_LIMIT = "10/minute"  # POST /fx
# The plain-English assistant (docs/57): one `chat()` per ask, up to 60 s and a
# whole context's worth of tokens — human-paced, because the gesture is a
# person typing one question at a time and an unthrottled one is an LLM-cost
# amplifier (twice the import-suggest ceiling; an ask is one round trip, not a
# file upload).
ASSISTANT_ASK_LIMIT = "12/minute"  # POST /assistant/ask
# The other two assistant writes get their own ceilings, same pace: a person
# (or a connector acting for one) does not create a card or decide one
# thirteen times a minute. Security t_108c24f3 F3: `POST /assistant/proposals`
# carried NO limiter at all while docs/59 §8 claimed it "inherits
# /assistant/ask's middleware posture" — slowapi decorates per route and
# app/main.py has no rate-limit middleware, so there was nothing to inherit
# (measured on the candidate: 30 rapid proposes → {201: 30}, zero 429).
ASSISTANT_PROPOSE_LIMIT = "12/minute"  # POST /assistant/proposals
# ... and it runs that ceiling TWICE over (security t_95eb8caf F3-b): once
# per client IP — skipped when the request carries no forwarded hop, because
# that hop is what says WHO the client is — and once per AUTHENTICATED tenant
# (`_tenant_key`). The MCP server's own calls arrive from the platform's
# container address, so an IP-only key there would be one bucket for every
# tenant on the instance and one looping connector would consume everyone's
# propose budget.
# apply | dismiss | undo must sit in ONE bucket (QA F4 / security F3-a,
# t_828c243a): each is a write on the same card, so three separate 12/minute
# per-route buckets would be 36 writes a minute through the UI and a loop
# would need three windows to exhaust the route. `shared_limit` below binds
# all three to the one named scope; the path parameter still cannot mint a
# bucket per card. They also run their transaction in a worker thread
# (docs/01 "Sessions and the event loop"), so a hammering client must not be
# able to occupy threads without a ceiling either.
ASSISTANT_TRANSITION_LIMIT = "12/minute"  # POST /assistant/proposals/{id}/apply|dismiss|undo


def human_paced_limit(
    limit_value: str,
    *,
    key_func: Callable[..., str] | None = None,
    exempt_when: Callable[..., bool] | None = None,
) -> Callable[..., Any]:
    """Decorator factory for human-paced per-client route limits (docs/03).

    Returns the slowapi route decorator. The bucket scope is the route
    function (see ``key_style="endpoint"`` above), one bucket per client.
    Decorated endpoints must declare a ``request: Request`` parameter
    (slowapi requires it). Nothing is enforced while the limiter is disabled.

    ``key_func`` overrides the client key (the propose route also keys one
    bucket per authenticated tenant — security F3-b) and ``exempt_when``
    skips this ceiling for a request that carries no hop to key on (the MCP
    server's direct call). slowapi only forwards those to the key function
    when its first parameter is literally named ``request``.
    """
    return limiter.limit(limit_value, key_func=key_func, exempt_when=exempt_when)


def shared_limit(limit_value: str, scope: str) -> Callable[..., Any]:
    """Decorator factory binding SEVERAL routes to ONE named bucket.

    Same human-paced pace and per-client key as ``human_paced_limit``, but
    the bucket scope is the string given here instead of the route function,
    so every route decorated with the same scope hits the same bucket. Used
    for apply/dismiss/undo (security F3-a): one bucket, not three.
    """
    return limiter.shared_limit(limit_value, scope, key_func=_client_key)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    del request, exc
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many attempts", "code": "rate_limited"},
    )

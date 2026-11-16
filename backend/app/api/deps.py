"""Shared router dependencies: DB session + current-user resolution (docs/01).

Also exposes the pagination helpers and `owned_or_404` guard so routers can
drop their per-resource ownership checks (docs/08 contract).
"""

import logging
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Select
from sqlalchemy.orm import Session
from sqlmodel import Session as SQLModelSession
from sqlmodel import func, select
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.errors import api_error
from app.core.rls import attach_request_stamping, bind_request_tenant
from app.core.security import decode_access_token
from app.llm.egress import bind_privacy_lock as bind_llm_privacy_lock
from app.models.mcp_tokens import McpToken, hash_mcp_token
from app.models.users import User
from app.services import fx as fx_service
from app.services import market_data as market_data_service
from app.services.demo_seed import DEMO_EMAIL

_engine = None
_request_engine = None
# The URL the request engine was BUILT from. Compared as a plain string:
# `str(engine.url)` masks the password (`***`), so comparing against the URL
# object would rebuild the engine on every request.
_request_engine_url: str | None = None
_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)

# The shared demo tenant is re-seeded by `POST /auth/demo` and read by every
# anonymous visitor, so a mutation is never scoped to one visitor: it rewrites
# (or deletes) data everyone is looking at. The guard lives HERE, in the
# dependency every ordinary router already depends on, so a new router inherits
# it without remembering to call anything.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Escape hatches, as exact paths. `/acb/rebuild` recomputes `acb_pools` — a
# derived cache owned by this user alone — from that user's own trades
# (app/tax/acb.py::rebuild_user_pools is its only write path). It cannot
# change any seeded transaction, trade, price or balance, and docs/30 depends
# on it to populate the demo's pool cache once fx gaps are filled. Blocking it
# would remove a repair path while adding no protection.
#
# `/auth/demo/reset` is the THIRD exemption (M50) — the demo tenant's own
# recovery path, and the reason a future gap in this guard is no longer
# permanent damage. It stays inside the boundary instead of punching a hole in
# it: the route still authenticates through `get_current_user` (so it is logged
# and rate-limited like everything else), it rejects every caller who is not
# the demo user with 403 `forbidden`, it is capped at 5/min exactly like `POST
# /auth/demo`, and its only possible output is the deterministic M10 seed — a
# visitor cannot make the shared dataset show a value of their choosing, only
# restore the published one. The alternative (a manual psql wipe, the only
# recovery before M50) is strictly worse: unauthenticated, and outside the
# transaction that guarantees a wipe is never half-done.
#
# The MCP credential routes are the FOURTH and FIFTH exemptions (M81, docs/40 §1):
# and revoke of the demo tenant's OWN `mcp_tokens` rows. They are the one
# mutation that cannot touch the shared dataset — no seeded transaction, trade,
# price or balance moves, only a credential row the guard's own subject owns.
# The PM decision behind it: the demo account is the evaluation front door, and
# "connect the demo to your agent" is the moment an evaluator is convinced, so
# the demo tenant may mint (docs/40 forces the policy to `read_only`, 422
# `demo_mcp_read_only` for an explicit `full`) and must therefore be able to
# revoke again — otherwise a visitor fills MCP_TOKEN_ACTIVE_CAP with tokens no
# visitor can delete for 90 days. Revoke is scoped by PREFIX (the id segment)
# and mint by exact path; nothing else joins either set without the
# justification comment sitting next to it.
#
# `POST /projections/run` is the SIXTH exemption (M157, K-12; docs/12 §7). It
# is the only exempted route that never touches the database AT ALL — the
# handler takes no `Session`, and `app/services/projections.py::project_scenario`
# is a pure function of the request body (docs/12: "pure math, no DB reads").
# The guard fires because the METHOD is mutating, and here it is not: nothing
# is written, no seeded transaction, trade, price or balance moves, and the
# response is a deterministic function of the caller's own input, so one
# visitor's run is invisible to the next. Blocking it cost the demo tenant the
# headline calculator on /reports (M61 #2 rendered an ErrorPanel before a
# single chart line drew), which pushed the whole projection run into the
# browser as a CLIENT-only compute — the demo write-guard artifact M135 §2
# K-12 restores. A real tenant's run is server-computed either way.
DEMO_MUTATION_EXEMPT_PATHS = frozenset(
    {
        "/api/v1/acb/rebuild",
        "/api/v1/auth/demo/reset",
        "/api/v1/auth/mcp-tokens",
        "/api/v1/projections/run",
    }
)
# Path-prefix exemptions, matched against the request path only after the
# exact set above misses. Currently one entry: MCP token revocation.
DEMO_MUTATION_EXEMPT_PREFIXES: tuple[str, ...] = ("/api/v1/auth/mcp-tokens/",)


def _assert_demo_mutation_allowed(request: Request, user: User) -> None:
    """403 `demo_account_restricted` for demo-tenant writes; reads pass through.

    Method-scoped, not router-scoped: GET/HEAD/OPTIONS on the demo tenant are
    exactly what the demo is FOR, so they stay open. Login and `POST /auth/demo`
    are outside this dependency entirely and are unaffected.
    """
    if request.method not in MUTATING_METHODS:
        return
    path = request.url.path
    if path in DEMO_MUTATION_EXEMPT_PATHS:
        return
    if any(path.startswith(prefix) for prefix in DEMO_MUTATION_EXEMPT_PREFIXES):
        return
    if user.email != DEMO_EMAIL:
        return
    logger.warning("Blocked %s %s for the demo tenant", request.method, request.url.path)
    raise api_error(
        403,
        "The demo account is read-only; changes are not allowed",
        "demo_account_restricted",
    )


def get_engine():
    """The MAINTENANCE engine (DATABASE_URL): schema owner, RLS-exempt.

    Background workers, post-response BackgroundTasks, the demo seed, scripts
    and alembic all connect through here — system paths that legitimately
    cross tenants (docs/55 §Out of scope). Request handlers use
    `get_db`/`get_request_engine` instead, which carry the tenant stamp.
    """
    global _engine
    if _engine is None:
        from sqlalchemy import create_engine

        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_request_engine():
    """The REQUEST engine: the unprivileged app role when configured (docs/55)

    Returns the maintenance engine unchanged while `APP_DATABASE_URL` is
    unset (local dev, the test suite, and any deployment that has not
    rolled the second lock out yet) — the tenant stamp on transactions is
    harmless there because the schema owner is never subject to its own
    table policies.
    """
    global _request_engine, _request_engine_url
    app_url = get_settings().app_database_url
    if not app_url:
        return get_engine()
    if _request_engine is None or _request_engine_url != app_url:
        from sqlalchemy import create_engine

        if _request_engine is not None:
            _request_engine.dispose()
        _request_engine = create_engine(app_url, pool_pre_ping=True)
        _request_engine_url = app_url
        # Stamp the current request's tenant (transaction-locally) onto every
        # transaction this engine begins — the whole point of routing request
        # sessions through a role the policies can constrain.
        attach_request_stamping(_request_engine)
    return _request_engine


def get_db() -> Generator[Session, None, None]:
    with SQLModelSession(get_request_engine(), expire_on_commit=False) as session:
        yield session


def _decode_user_id(
    credentials: HTTPAuthorizationCredentials | None,
    token: str | None,
) -> int:
    """Resolve the authenticated user id from an Authorization header OR a
    query-string token (SSE).

    Returns 401 (via `api_error`) when neither is present or the token is
    malformed/expired/wrong-type — the same signal both caller paths would
    raise on their own. The privacy-lock ContextVar bind is intentionally left
    to the callers so each request path keeps its own lock scope.
    """
    if credentials is not None:
        try:
            return decode_access_token(credentials.credentials)
        except jwt.PyJWTError:
            raise api_error(401, "Invalid or expired access token", "invalid_token") from None
    if token is not None:
        try:
            return decode_access_token(token)
        except jwt.PyJWTError:
            raise api_error(401, "Invalid or expired access token", "invalid_token") from None
    raise api_error(401, "Not authenticated", "invalid_token")


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    # async on purpose (docs/11 step 6): sync dependencies run in an anyio
    # worker thread whose ContextVar writes never propagate back to the
    # endpoint's task. Resolving the user in the event-loop context lets the
    # privacy-lock bind below stick for the rest of the request.
    #
    # M148: the QUERY, however, must NOT run on the event loop. `Session.get`
    # blocks on the SQLAlchemy pool, and a blocked loop cannot run a single
    # other request — including the handlers that hold the connections this
    # one is waiting for. That is the 2026-10-11 outage: a burst checked out
    # the pool (5 + 10), the requests that queued for a connection froze the
    # loop for the full 30 s pool timeout each, so the in-flight handlers
    # could never finish and never released their sessions. A self-deadlock
    # that only `docker restart` clears. Run it in a worker thread instead:
    # the wait happens off the loop, the burst drains, and a saturated pool
    # degrades to bounded 500s instead of a whole-API freeze.
    user_id = _decode_user_id(credentials, None)
    # Publish the tenant BEFORE the first query on this session: the request
    # engine's `begin` listener stamps it transaction-locally, so every
    # statement the handler (and its service calls) run on this session is
    # scoped by the database as well as by the app-level filters (docs/55).
    bind_request_tenant(user_id)
    user = await run_in_threadpool(db.get, User, user_id)
    if user is None:
        raise api_error(401, "Invalid or expired access token", "invalid_token")
    # Global demo guard: runs AFTER authentication (so the demo tenant still
    # logs in and reads) and BEFORE any handler body, so a blocked write never
    # touches a row. Every router that depends on this inherits it.
    _assert_demo_mutation_allowed(request, user)
    # Privacy lockdown (docs/11 step 6): publish the user's privacy_mode to the
    # service-layer guards for the lifetime of this request. ContextVars are
    # task-scoped, so concurrent requests cannot see each other's flag.
    fx_service.bind_privacy_lock(user.privacy_mode)
    market_data_service.bind_privacy_lock(user.privacy_mode)
    # LLM egress lock (audit H3): app/llm/egress reads this at the single send
    # point, so llm-test and every request-path LLM send inherits the
    # loopback-only rule without threading flags through call signatures.
    # Background workers get the same bind from app.llm.base.build_provider.
    bind_llm_privacy_lock(user.privacy_mode)
    return user


async def get_current_user_with_query_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    token: Annotated[str | None, Query()] = None,
    db: Annotated[Session, Depends(get_db)] = None,
) -> User:
    """Accept token from Authorization header OR query parameter (for SSE).

    The demo mutation guard is deliberately NOT applied here. This dependency
    exists for `EventSource`, which cannot set headers, and its only caller is
    the GET price stream — a read. It also keeps this signature free of
    `request` so the SSE path stays a pure token resolver. Any future
    MUTATING route must use `get_current_user`, where the guard lives.
    """
    # M148: same rule as get_current_user — the query waits OFF the event
    # loop, so an empty pool can never freeze every other request.
    user_id = _decode_user_id(credentials, token)
    # Same tenant publish as get_current_user — the SSE read path runs on the
    # same request engine and is scoped identically (docs/55).
    bind_request_tenant(user_id)
    user = await run_in_threadpool(db.get, User, user_id)
    if user is None:
        raise api_error(401, "Invalid or expired access token", "invalid_token")

    # Privacy lockdown
    fx_service.bind_privacy_lock(user.privacy_mode)
    market_data_service.bind_privacy_lock(user.privacy_mode)
    # LLM egress lock (audit H3) — same bind as get_current_user, so the SSE
    # request path carries it too.
    bind_llm_privacy_lock(user.privacy_mode)
    # SSE streams stay open for hours; FastAPI only tears down yield-
    # dependencies when the response completes, so without this close the
    # auth session's connection sits `idle in transaction` for the whole
    # EventSource lifetime (pins a pool slot, holds back vacuum). The only
    # caller is stream_prices, which reads current_user.id above nothing else;
    # Session.close() is idempotent, so get_db's own exit stays a no-op.
    db.close()
    return user


def owned_or_404(model, id, user, db):
    """Return `model` row `id` owned by `user`, else 404 with the envelope."""
    obj = db.get(model, id)
    if obj is None or getattr(obj, "user_id", None) != user.id:
        raise api_error(404, f"{getattr(model, '__name__', 'Resource')} not found", "not_found")
    return obj


class Pagination:
    def __init__(self, limit: int, offset: int) -> None:
        self.limit = limit
        self.offset = offset


# M98b F-4: `limit` has always been bounded (`le=200`) but `offset` was
# not, and a page request with `offset >= 2**63` made Postgres reject the
# generated bigint OFFSET ("integer out of range") -> an unhandled 500
# envelope. 1e6 rows deep is far past any real list here (and 5,000 pages of
# the 200-row `limit`), so the ceiling is a cheap constant both pagers share:
# the query layer answers 422 `validation_error` through FastAPI's own
# validator, and `paginate` re-checks so a caller that builds its own page
# window without going through `get_pagination` cannot reintroduce the 500.
MAX_OFFSET = 1_000_000


def get_pagination(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


def paginate(db, stmt: Select, *, limit: int, offset: int) -> tuple[list, int]:
    """Run `stmt` (optionally already ordered) for a page; return (rows, total)."""
    if not 0 <= offset <= MAX_OFFSET:
        raise api_error(422, f"offset must be between 0 and {MAX_OFFSET}", "validation_error")
    total = db.exec(select(func.count()).select_from(stmt.subquery())).one()
    rows = db.exec(stmt.limit(limit).offset(offset)).all()
    return rows, total


def require_docs_access(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[SQLModelSession, Depends(get_db)],
) -> None:
    """401 the API map (`/docs`, `/redoc`, `/openapi.json`) for anonymous callers.

    M87 (docs/42): FastAPI serves those three routes open by default, and an
    openapi.json is a complete, annotated map of every endpoint of a now
    internet-facing app. A developer's map, not a stranger's.

    Two credential shapes clear the guard, because those are the two a
    developer actually holds:
      - a session access token (JWT, `type=access`) — the exact token
        `get_current_user` decodes;
      - a live MCP token (opaque, hashed at rest) — a GET of the schema leaks
        no state, so read-only policy is fine, and an expired token is
        refused the same way `POST /auth/validate-mcp-token` refuses it.

    `API_DOCS_PUBLIC=true` skips the guard entirely — the keep-working path
    for local dev (`.env.example` sets it). The setting defaults to false so a
    deployment that forgets the key fails CLOSED, not open.
    """
    if get_settings().api_docs_public:
        return
    if credentials is not None:
        raw = credentials.credentials
        try:
            decode_access_token(raw)
            return
        except jwt.PyJWTError:
            pass
        row = db.exec(select(McpToken).where(McpToken.token_hash == hash_mcp_token(raw))).first()
        if row is not None and (row.expires_at is None or row.expires_at > datetime.now(UTC)):
            return
    raise api_error(
        401,
        "Authentication required to read the API documentation",
        "docs_auth_required",
    )

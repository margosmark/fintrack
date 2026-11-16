"""auth — register · login · refresh · logout · demo · mcp-tokens (docs/08)."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session
from sqlmodel import func, select

from app.api.deps import get_current_user, get_db
from app.core.config import get_settings
from app.core.errors import api_error
from app.core.ratelimit import limiter
from app.core.rls import set_tenant_now
from app.core.security import (
    CHALLENGE_TTL_SECONDS,
    PASSWORD_CHALLENGE_METHOD,
    create_access_token,
    create_challenge_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.models.auth_factors import TotpFactor
from app.models.categories import Category
from app.models.mcp_audit import McpAudit
from app.models.mcp_tokens import (
    MCP_TOKEN_TTL_DAYS,
    McpToken,
    McpTokenPolicy,
    hash_mcp_token,
    new_mcp_token,
)
from app.models.refresh_tokens import RefreshToken
from app.models.users import User
from app.schemas.auth import (
    LoginChallenge,
    LoginRequest,
    RegisterAccepted,
    RegisterRequest,
    TokenResponse,
)
from app.schemas.mcp_tokens import (
    McpTokenCreate,
    McpTokenCreated,
    McpTokenPage,
)
from app.schemas.users import UserPublic
from app.services.demo_seed import DEMO_EMAIL, ensure_demo_account, reset_demo_tenant

router = APIRouter(prefix="/auth", tags=["auth"])

DEFAULT_CATEGORIES = [
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
]

REFRESH_COOKIE = "refresh_token"
REFRESH_PATH = "/api/v1/auth"

# MCP token lifecycle (audit M12): at most MCP_TOKEN_ACTIVE_CAP live tokens
# per user (400 mcp_token_cap), creation rate-limited (429 rate_limited).
MCP_TOKEN_ACTIVE_CAP = 10

# Lazily built bcrypt hash for the unknown-email login path (audit L1).
_DUMMY_PASSWORD_HASH: str | None = None


# --- internal helpers ------------------------------------------------------


def _set_refresh_cookie(response: Response, token: str) -> None:
    """Set the refresh token as an httpOnly cookie.

    This is the ONLY delivery channel for the refresh token (audit M1) — the
    JSON body of login/refresh/register/demo/change-password never carries it
    (TokenResponse marks the field exclude=True). Every cookie attribute here
    is security-relevant: httpOnly keeps it from same-origin script, Secure
    follows settings.cookie_secure (M2), SameSite=strict + path scoping limit
    where it is sent.
    """
    settings = get_settings()
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.refresh_token_days * 86400,
        path=REFRESH_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Clear the refresh token cookie (same attributes as the set — including
    Secure, so every refresh-cookie response passes settings.cookie_secure)."""
    settings = get_settings()
    response.delete_cookie(
        key=REFRESH_COOKIE,
        path=REFRESH_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def _dummy_bcrypt(password: str) -> None:
    """Spend one bcrypt verify on a throwaway hash for unknown emails (L1).

    Login used to return instantly for unregistered emails while a known
    email cost a ~150 ms bcrypt verify — a timing oracle for account
    enumeration. Running the same cost on a dummy hash makes both paths
    indistinguishable by timing. The dummy hash is generated once, lazily,
    so module import stays free.
    """
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = hash_password(secrets.token_hex(16))
    verify_password(password, _DUMMY_PASSWORD_HASH)


def _refresh_token_from_cookie(request: Request) -> str:
    """The refresh token, read from the httpOnly cookie — and nowhere else.

    The cookie is the ONLY accepted channel (audit M1 delivery rule: the token
    is never handed to same-origin script, so it must not be accepted back
    from one either). A body-carried token is ignored, not honoured: accepting
    it would let any script holding — or replaying from a log, a proxy capture,
    or an XSS exfiltration — mint a fresh access token without the cookie's
    HttpOnly/Secure/SameSite/Path protections, re-establishing exactly the
    long-lived-credential-in-JS threat the cookie design removed.
    """
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise api_error(
            401,
            "Missing refresh token",
            "missing_refresh_token",
        )
    return token


def _totp_enabled(db: Session, user_id: int) -> bool:
    """True when the account has an ACTIVE TOTP factor (M74, docs/38).

    Lives here rather than in the second-factor router on purpose: `login`
    must decide whether to challenge WITHOUT importing that module, which
    imports this one for `_issue_pair` (one-directional dependency).
    """
    # Look up by `user_id`, NOT with `db.get(TotpFactor, user_id)`: this table
    # has a SURROGATE `id` PK (docs/02) plus a UNIQUE `user_id`, so a
    # get-by-PK here would silently read the row whose `id` happens to match —
    # correct only while the id sequences stay in lockstep with users.id.
    factor = db.exec(select(TotpFactor).where(TotpFactor.user_id == user_id)).first()
    return factor is not None and factor.secret is not None


def _issue_pair(db: Session, user: User) -> TokenResponse:
    settings = get_settings()
    raw, token_hash = new_refresh_token()
    row = RefreshToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
    )
    db.add(row)
    db.commit()
    # refresh_token rides on the model only so callers can set the httpOnly
    # cookie; TokenResponse excludes it from the JSON body (audit M1).
    return TokenResponse(
        user=UserPublic.model_validate(user),
        access_token=create_access_token(user.id),
        refresh_token=raw,
    )


def _lookup_refresh_row(db: Session, raw_token: str) -> RefreshToken | None:
    return db.exec(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_token))
    ).first()


def _claim_refresh_token(db: Session, token_hash: str, now: datetime) -> bool:
    """Atomically consume a live refresh token. True iff THIS caller won it.

    A conditional UPDATE, not a read-then-write: the `revoked_at IS NULL`
    predicate is evaluated by the database inside one statement, so two
    concurrent refreshes for the same token serialize on the row lock and
    exactly one sees a rowcount of 1. The loser gets False and takes the
    documented reuse path (`_revoke_all_refresh_tokens`) — with a plain SELECT
    both callers could read `revoked_at = NULL`, both believe they won, and one
    rotating token would yield two live sessions.

    The statement COMMITS itself. The claim is the durable part of rotation: if
    it stayed inside the caller's open transaction a later rollback would
    un-spend the token and hand the winner's successor a session that the
    loser's chain-wide revocation had already invalidated.

    Expiry is deliberately NOT part of the predicate: a claim is the reuse
    check, and an expired-but-never-spent token is not a compromise signal, so
    claiming it must not revoke the user's other sessions.
    """
    result = db.execute(
        sa_update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.commit()  # the claim is durable on its own; see the docstring
    return result.rowcount == 1


def _revoke_all_refresh_tokens(db: Session, user_id: int, now: datetime) -> None:
    """Revoke every live refresh token of a user (reuse detection)."""
    for live in db.exec(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
    ).all():
        live.revoked_at = now
    db.commit()


# --- public auth endpoints -------------------------------------------------


REGISTER_ACCEPTED_DETAIL = (
    "Registration accepted: if this address was free, an account now exists with the "
    "password you just chose. Sign in to continue."
)


@router.post("/register", response_model=RegisterAccepted, status_code=200)
@limiter.limit("5/minute")
def register(
    request: Request,
    payload: RegisterRequest,
    db: Annotated[Session, Depends(get_db)],
) -> RegisterAccepted:
    """Create the account — and answer the SAME way whether it existed or not.

    M87 (docs/29 F15), closes M83's F-7 registration oracle. The old contract
    (`201` + token pair vs `409 duplicate_email`) told any anonymous caller
    which addresses were registered. Both branches now return a byte-identical
    token-free body:

    - free address → the account and its default categories are still created,
      so the credentials just chosen work on the very next `POST /auth/login`
      (there is no email-verification flow to defer to — this install supports
      running with no SMTP at all, docs/37);
    - taken address → nothing changes, and `_dummy_bcrypt` spends the same
      one bcrypt cost the fresh branch pays in `hash_password`, so timing does
      not separate them either.

    No branch may ever return a session: a working token on one side and an
    error on the other is the same oracle wearing a different status code.
    """
    if len(payload.password.encode()) > 72:
        raise api_error(422, "Password exceeds 72 bytes", "password_too_long")
    existing = db.exec(select(User).where(User.email == payload.email)).first()
    if existing is not None:
        _dummy_bcrypt(payload.password)
        return RegisterAccepted(status="accepted", detail=REGISTER_ACCEPTED_DETAIL)

    user = User(email=payload.email, hashed_password=hash_password(payload.password))
    db.add(user)
    db.flush()
    # docs/55: the default categories are tenant rows written BEFORE this
    # request has any identity — stamp the brand-new tenant onto the live
    # transaction so the WITH CHECK of the categories policy sees the writes
    # as the new tenant's own. Harmless on an RLS-exempt connection.
    if user.id is None:  # flush() assigned the id
        raise RuntimeError("register: user id was not assigned by flush()")
    set_tenant_now(db, user.id)
    for name in DEFAULT_CATEGORIES:
        db.add(Category(user_id=user.id, name=name, is_system=True))
    db.commit()
    return RegisterAccepted(status="accepted", detail=REGISTER_ACCEPTED_DETAIL)


@router.post("/login", response_model=TokenResponse | LoginChallenge)
@limiter.limit("10/minute")
def login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    user = db.exec(select(User).where(User.email == payload.email)).first()
    if user is None:
        # Audit L1: burn the same bcrypt cost as a known-email verify so
        # response timing does not reveal whether the email is registered.
        _dummy_bcrypt(payload.password)
        raise api_error(401, "Invalid email or password", "invalid_credentials")
    if not verify_password(payload.password, user.hashed_password):
        raise api_error(401, "Invalid email or password", "invalid_credentials")
    # M74 (docs/38): a password is only the FIRST factor for an account that
    # enrolled TOTP. Answer 200 with a challenge — no session, no cookie — so
    # the follow-up request can collect the second factor. The demo tenant is
    # excluded by design: its password is printed on the login page, so it
    # must never acquire (or be blocked by) a second factor.
    if user.email != DEMO_EMAIL and _totp_enabled(db, user.id):
        return LoginChallenge(
            challenge="totp",
            challenge_token=create_challenge_token(user.id, PASSWORD_CHALLENGE_METHOD),
            expires_in=CHALLENGE_TTL_SECONDS,
        )
    pair = _issue_pair(db, user)
    _set_refresh_cookie(response, pair.refresh_token)
    return pair


@router.post("/demo", response_model=TokenResponse)
@limiter.limit("5/minute")
def demo(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    if not get_settings().seed_demo_account:
        raise api_error(404, "Demo account is disabled on this server", "demo_disabled")
    user = ensure_demo_account(db)
    if user is None:
        raise api_error(404, "Demo account is disabled on this server", "demo_disabled")
    pair = _issue_pair(db, user)
    _set_refresh_cookie(response, pair.refresh_token)
    return pair


@router.post("/demo/reset", status_code=204)
@limiter.limit("5/minute")
def demo_reset(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Restore the demo tenant to a pristine M10 seed (docs/20, docs/30).

    Authenticated through `get_current_user`, which is exactly why the route
    carries an exemption in `DEMO_MUTATION_EXEMPT_PATHS` — the justification
    for that exemption lives next to the guard it opts out of.

    Two independent checks beyond the guard: the kill switch (`404
    demo_disabled`, same as `POST /auth/demo`) and demo-only ownership
    (`403 forbidden` for any other tenant) — the shared guard answers "is this
    the demo tenant", never "may this tenant reset itself", so that decision is
    made here, in the handler, where it is visible in one place.

    204, no body: the caller keeps its session (the `users` row and the
    credential tables survive the wipe) and simply refetches.
    """
    if not get_settings().seed_demo_account:
        raise api_error(404, "Demo account is disabled on this server", "demo_disabled")
    if current_user.email != DEMO_EMAIL:
        raise api_error(403, "Only the demo account can reset the demo tenant", "forbidden")
    if reset_demo_tenant(db) is None:
        # Defensive re-read of the kill switch (settings are read live, so a
        # flip between the check above and the wipe still fails closed).
        raise api_error(404, "Demo account is disabled on this server", "demo_disabled")


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("30/minute")
def refresh(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    """Rotate a refresh token, cookie-only.

    Order matters. The token is CLAIMED first, with a conditional UPDATE, so
    two concurrent refreshes carrying the same cookie cannot both succeed: the
    database decides the winner, not this process's read. Only then is the
    row's user/expiry state consulted — a claim that failed means the token was
    already spent, which is the reuse signal that revokes the whole chain.

    The endpoint takes NO request body. A body model here would make FastAPI
    require a JSON body, so the natural cookie-only caller — the MCP client,
    which sends the token as a cookie and nothing else — would be rejected 422
    before the cookie was ever read. There is no body field to honour anyway.
    """
    now = datetime.now(UTC)
    raw_token = _refresh_token_from_cookie(request)
    token_hash = hash_refresh_token(raw_token)

    # Reuse detection: a row that already exists but cannot be claimed was
    # spent earlier — the chain is compromised, so burn every live token of
    # that user, successor included, before answering.
    claimed = _claim_refresh_token(db, token_hash, now)
    if not claimed:
        row = _lookup_refresh_row(db, raw_token)
        # Cascade ONLY on genuine reuse. An EXPIRED token is not a compromise
        # signal: a stale browser tab or a retried proxy request would otherwise
        # log the user out of every other device on its second attempt (the
        # first attempt already stamped revoked_at, so the retry looks "spent").
        expired = row is not None and row.expires_at <= now
        if row is not None and row.revoked_at is not None and not expired:
            _revoke_all_refresh_tokens(db, row.user_id, now)
        _clear_refresh_cookie(response)
        raise api_error(
            401,
            "Invalid or revoked refresh token",
            "invalid_or_revoked_refresh_token",
        )

    row = _lookup_refresh_row(db, raw_token)
    if row is None or row.expires_at <= now:
        _clear_refresh_cookie(response)
        raise api_error(
            401,
            "Invalid or revoked refresh token",
            "invalid_or_revoked_refresh_token",
        )

    user = db.get(User, row.user_id)
    if user is None:
        _clear_refresh_cookie(response)
        raise api_error(
            401,
            "Invalid or revoked refresh token",
            "invalid_or_revoked_refresh_token",
        )

    db.commit()
    pair = _issue_pair(db, user)
    _set_refresh_cookie(response, pair.refresh_token)
    return pair


@router.post("/logout", status_code=204)
@limiter.limit("30/minute")
def logout(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Revoke the presented refresh token and clear the cookie (cookie-only).

    No request body, for the same reason as `refresh` above: a cookie-only
    caller sends no JSON, and a body model would reject it 422.
    """
    raw_token = _refresh_token_from_cookie(request)
    row = _lookup_refresh_row(db, raw_token)
    if row is None or row.revoked_at is not None:
        _clear_refresh_cookie(response)
        raise api_error(
            401,
            "Invalid or revoked refresh token",
            "invalid_or_revoked_refresh_token",
        )
    row.revoked_at = datetime.now(UTC)
    db.commit()
    _clear_refresh_cookie(response)


# ---------------------------------------------------------------------------
# MCP server tokens — opaque bearer tokens for MCP client auth
# ---------------------------------------------------------------------------


@router.get("/mcp-tokens", response_model=McpTokenPage)
def list_mcp_tokens(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> McpTokenPage:
    """List MCP tokens for the current user."""
    rows = db.exec(
        select(McpToken).where(McpToken.user_id == current_user.id).order_by(McpToken.id)
    ).all()
    return McpTokenPage(items=rows, total=len(rows))


@router.post("/mcp-tokens", response_model=McpTokenCreated, status_code=201)
@limiter.limit("10/minute")
def create_mcp_token(
    request: Request,
    payload: McpTokenCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> McpTokenCreated:
    """Create an MCP token. The raw token is shown ONCE — save it now.

    Lifecycle guards (audit M12): creation is rate-limited (429
    `rate_limited`), each user may hold at most MCP_TOKEN_ACTIVE_CAP
    non-expired tokens (400 `mcp_token_cap`), and the token expires
    MCP_TOKEN_TTL_DAYS after creation.

    M81 (docs/40 §1): the demo tenant is allowed through the shared write
    guard for mint (this route) and revoke (`DELETE /mcp-tokens/{id}`) and
    mints READ-ONLY tokens only — `policy` omitted
    resolves to `read_only`, and an explicit `full` (or `read_propose`, the
    BYO-AI scope — docs/59 §1) is refused with 422 `demo_mcp_read_only`
    instead of being silently downgraded, so the answer never depends on
    whether a client remembered to send the field.
    """
    if current_user.email == DEMO_EMAIL:
        if payload.policy is not None and payload.policy is not McpTokenPolicy.read_only:
            raise api_error(
                422,
                "The demo tenant can only mint read-only MCP tokens",
                "demo_mcp_read_only",
            )
        policy: McpTokenPolicy = McpTokenPolicy.read_only
    else:
        policy = payload.policy or McpTokenPolicy.full
    now = datetime.now(UTC)
    active = (
        db.exec(
            select(func.count())
            .select_from(McpToken)
            .where(
                McpToken.user_id == current_user.id,
                (McpToken.expires_at.is_(None)) | (McpToken.expires_at > now),
            )
        )
    ).one()
    if active >= MCP_TOKEN_ACTIVE_CAP:
        raise api_error(
            400,
            f"Active MCP token limit of {MCP_TOKEN_ACTIVE_CAP} reached; "
            "revoke a token before creating another",
            "mcp_token_cap",
        )
    raw, token_hash = new_mcp_token()
    row = McpToken(
        user_id=current_user.id,
        name=payload.name,
        token_hash=token_hash,
        policy=policy,
        expires_at=now + timedelta(days=MCP_TOKEN_TTL_DAYS),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return McpTokenCreated(
        id=row.id,  # type: ignore[arg-type]
        name=row.name,
        token=raw,
        policy=row.policy,
        created_at=row.created_at,  # type: ignore[arg-type]
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
    )


@router.delete("/mcp-tokens/{token_id}", status_code=204)
def delete_mcp_token(
    token_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Revoke an MCP token."""
    row = db.get(McpToken, token_id)
    if row is None or row.user_id != current_user.id:
        raise api_error(404, "Token not found", "not_found")
    db.delete(row)
    db.commit()


# ---------------------------------------------------------------------------
# MCP token validation — called by MCP server to resolve token → user
# ---------------------------------------------------------------------------


class ValidateMcpTokenResponse(BaseModel):
    user_id: int
    email: str
    # M80 (docs/40 §1-2): the MCP server filters the toolset from these, so
    # they are resolved HERE, server-side — the MCP process never re-derives
    # identity from an address.
    policy: McpTokenPolicy
    token_id: int
    is_demo: bool


@router.post("/validate-mcp-token", response_model=ValidateMcpTokenResponse)
@limiter.limit("120/minute")
def validate_mcp_token(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> ValidateMcpTokenResponse:
    """Validate an MCP token and return the associated user.

    Called by the MCP server's TokenVerifier. The MCP token is sent in
    the Authorization header as a Bearer token.

    Internet-routed (audit M9): rate-limited to 120/min per client (429
    `rate_limited`) so it cannot be used as an unbounded validity+email
    oracle. Expired tokens are rejected with the DISTINCT 401
    `mcp_token_expired` code (audit M12); valid use stamps `last_used_at`.
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise api_error(401, "Missing bearer token", "missing_token")
    raw_token = auth[7:]
    token_hash = hash_mcp_token(raw_token)
    row = db.exec(select(McpToken).where(McpToken.token_hash == token_hash)).first()
    if row is None:
        raise api_error(401, "Invalid MCP token", "invalid_mcp_token")
    now = datetime.now(UTC)
    if row.expires_at is not None and row.expires_at <= now:
        raise api_error(401, "MCP token expired", "mcp_token_expired")
    user = db.get(User, row.user_id)
    if user is None:
        raise api_error(401, "Invalid MCP token", "invalid_mcp_token")
    # Audit M12: record usage so stale/leaked tokens are visible to operators.
    row.last_used_at = now
    db.add(row)
    db.commit()
    token_id = row.id
    if token_id is None:  # a row we just loaded by primary key always has one
        raise api_error(401, "Invalid MCP token", "invalid_mcp_token")
    return ValidateMcpTokenResponse(
        user_id=row.user_id,
        email=user.email,
        policy=row.policy,
        token_id=token_id,
        is_demo=user.email == DEMO_EMAIL,
    )


# ---------------------------------------------------------------------------
# Internal: issue a backend JWT for a resolved user (MCP server only)
# ---------------------------------------------------------------------------


class IssueTokenRequest(BaseModel):
    user_id: int


@router.post("/internal-issue-token")
@limiter.limit("120/minute")
def internal_issue_token(
    request: Request,
    payload: IssueTokenRequest,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Issue a short-lived backend JWT for a user.

    Protected by MCP_INTERNAL_SECRET — called by the MCP server after
    resolving an MCP token to a user_id. Internet-routed (audit M9):
    rate-limited to 120/min per client (429 `rate_limited`); the
    compare_digest secret check stays fail-closed.
    """
    secret = request.headers.get("x-internal-secret", "")
    expected = get_settings().mcp_internal_secret
    if not expected or not secrets.compare_digest(secret, expected):
        raise api_error(403, "Forbidden", "forbidden")
    user = db.get(User, payload.user_id)
    if user is None:
        raise api_error(404, "User not found", "not_found")
    return {
        "access_token": create_access_token(user.id),
        "token_type": "bearer",
    }


class McpAuditIngest(BaseModel):
    """One MCP tool-call receipt, posted by the MCP server (docs/40 §3).

    `args_hash` is a sha256 hex digest, never the arguments themselves: tool
    arguments can carry a password (`auth_login`), so the raw values must not
    reach a log table.
    """

    user_id: int
    mcp_token_id: int | None = None
    tool: str = Field(min_length=1, max_length=100)
    args_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_code: int = Field(ge=100, le=599)
    duration_ms: int = Field(ge=0, le=3_600_000)


@router.post("/internal-mcp-audit", status_code=204)
@limiter.limit("120/minute")
def internal_mcp_audit(
    request: Request,
    payload: McpAuditIngest,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Ingest one MCP tool-call receipt (MCP server only; docs/40 §3).

    Same trust boundary as `internal-issue-token`: `MCP_INTERNAL_SECRET`
    compared with `secrets.compare_digest`, fail-closed, and rate-limited to
    120/min per client (429 `rate_limited`) like its internal siblings. The
    MCP server treats a failure here as best-effort — it logs and still
    returns the tool result (docs/40 §3).
    """
    secret = request.headers.get("x-internal-secret", "")
    expected = get_settings().mcp_internal_secret
    if not expected or not secrets.compare_digest(secret, expected):
        raise api_error(403, "Forbidden", "forbidden")
    if db.get(User, payload.user_id) is None:
        raise api_error(404, "User not found", "not_found")
    db.add(McpAudit(**payload.model_dump()))
    db.commit()
    return None

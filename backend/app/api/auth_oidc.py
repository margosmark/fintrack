"""Optional OIDC SSO — authorization-code flow (M74, docs/38).

Config-gated: every route except `GET /auth/oidc/config` answers
`404 oidc_disabled` while `OIDC_ENABLED=false`. Link-only: the provider can
NEVER create a user — an email match starts a link, the link is created only
after an explicit confirmation, and the session that confirmation issues comes
from the very same `_issue_pair` + `_set_refresh_cookie` a password login uses.

Hand-off is cookie-only (docs/38 decision 8): the callback parks its result in
the httpOnly `oidc_pending` cookie and redirects to the SPA, so no token ever
appears in a URL. `SameSite=Lax` is mandatory on both OIDC cookies — the
callback is a cross-site top-level navigation, where `Strict` would drop the
state cookie and `None` would weaken CSRF protection everywhere else.
"""

import secrets
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.auth import REFRESH_PATH, _issue_pair, _set_refresh_cookie
from app.api.deps import get_db
from app.core.config import Settings, get_settings
from app.core.errors import api_error
from app.core.ratelimit import limiter
from app.core.security import create_oidc_pending_token, decode_oidc_pending_token
from app.models.auth_factors import OidcLink
from app.models.users import User
from app.schemas.auth import (
    OidcConfigResponse,
    OidcPreviewResponse,
    TokenResponse,
)
from app.services import oidc as oidc_service
from app.services.demo_seed import DEMO_EMAIL

router = APIRouter(prefix="/auth", tags=["auth"])

OIDC_STATE_COOKIE = "oidc_state"
OIDC_PENDING_COOKIE = "oidc_pending"
OIDC_COOKIE_TTL_SECONDS = 600

STATE_PAIR_SEP = "|"


# --- helpers ----------------------------------------------------------------


def _require_enabled() -> Settings:
    settings = get_settings()
    if not settings.oidc_enabled:
        raise api_error(404, "SSO is disabled on this server", "oidc_disabled")
    return settings


def _redirect_base(settings: Settings) -> str:
    return settings.app_origin.rstrip("/")


def _redirect_uri(settings: Settings) -> str:
    """The URI registered at the provider; empty derives it from APP_ORIGIN."""
    if settings.oidc_redirect_uri:
        return settings.oidc_redirect_uri
    return f"{_redirect_base(settings)}/api/v1/auth/oidc/callback"


def _web_path(settings: Settings) -> str:
    return f"{_redirect_base(settings)}/login/oidc"


def _set_oidc_cookie(response: Response, key: str, value: str, settings: Settings) -> None:
    response.set_cookie(
        key=key,
        value=value,
        max_age=OIDC_COOKIE_TTL_SECONDS,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=REFRESH_PATH,
    )


def _clear_oidc_cookie(response: Response, key: str, settings: Settings) -> None:
    response.delete_cookie(
        key=key,
        path=REFRESH_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def _error_redirect(settings: Settings, code: str) -> RedirectResponse:
    """Browser-visible failure: a `?error=` the SPA maps to a message."""
    response = RedirectResponse(f"{_web_path(settings)}?error={code}", status_code=302)
    _clear_oidc_cookie(response, OIDC_STATE_COOKIE, settings)
    return response


def _pending_claims(request: Request, settings: Settings) -> dict:
    """The live `oidc_pending` blob, or the one uniform 401 (docs/38)."""
    token = request.cookies.get(OIDC_PENDING_COOKIE)
    if not token:
        raise api_error(401, "Sign-in link expired; start over", "oidc_pending_expired")
    try:
        claims = decode_oidc_pending_token(token)
    except jwt.PyJWTError:
        raise api_error(401, "Sign-in link expired; start over", "oidc_pending_expired") from None
    if not claims.get("email") or not claims.get("sub") or not claims.get("issuer"):
        raise api_error(401, "Sign-in link expired; start over", "oidc_pending_expired")
    return claims


# --- routes -----------------------------------------------------------------


@router.get("/oidc/config", response_model=OidcConfigResponse)
def oidc_config() -> OidcConfigResponse:
    """Always 200 so the login page can decide without an error path."""
    return OidcConfigResponse(enabled=get_settings().oidc_enabled)


@router.get("/oidc/login")
@limiter.limit("5/minute")
def oidc_login(request: Request) -> Response:
    """Browser navigation -> the provider's consent screen.

    `state` and `nonce` are bound in the httpOnly `oidc_state` cookie rather
    than in server memory: the cookie is what proves THIS browser started the
    flow when the provider redirects back, and it cannot be planted by a
    cross-site attacker (foreign sites cannot write our origin's cookies).
    """
    settings = _require_enabled()
    state = secrets.token_hex(16)
    nonce = secrets.token_hex(16)
    try:
        url = oidc_service.authorize_url(
            settings.oidc_issuer,
            settings.oidc_client_id,
            _redirect_uri(settings),
            state,
            nonce,
        )
    except oidc_service.OidcError as exc:
        raise api_error(502, exc.detail, exc.redirect_code) from None
    response: Response = RedirectResponse(url, status_code=302)
    _set_oidc_cookie(response, OIDC_STATE_COOKIE, f"{state}{STATE_PAIR_SEP}{nonce}", settings)
    return response


@router.get("/oidc/callback")
@limiter.limit("10/minute")
def oidc_callback(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """Provider redirect -> validated ID token -> `oidc_pending` cookie -> SPA."""
    settings = _require_enabled()

    if error:
        return _error_redirect(settings, "oidc_provider_error")

    cookie = request.cookies.get(OIDC_STATE_COOKIE, "")
    parts = cookie.split(STATE_PAIR_SEP, 1)
    if not code or not state or len(parts) != 2 or not secrets.compare_digest(state, parts[0]):
        return _error_redirect(settings, "oidc_state_mismatch")
    nonce = parts[1]

    try:
        tokens = oidc_service.exchange_code(code, _redirect_uri(settings))
        claims = oidc_service.verify_id_token(tokens["id_token"], nonce=nonce)
    except oidc_service.OidcError as exc:
        return _error_redirect(settings, exc.redirect_code)

    email = claims.get("email")
    subject = claims.get("sub")
    if not isinstance(email, str) or not email:
        return _error_redirect(settings, "oidc_email_required")
    if not isinstance(subject, str) or not subject:
        return _error_redirect(settings, "oidc_token_invalid")

    user = db.exec(select(User).where(User.email == email)).first()
    if user is None:
        # Link-only (docs/38 decision 7): SSO can never create an account.
        return _error_redirect(settings, "oidc_unknown_account")

    response: Response = RedirectResponse(_web_path(settings), status_code=302)
    _set_oidc_cookie(
        response,
        OIDC_PENDING_COOKIE,
        create_oidc_pending_token(email, settings.oidc_issuer.rstrip("/"), subject),
        settings,
    )
    _clear_oidc_cookie(response, OIDC_STATE_COOKIE, settings)
    return response


@router.get("/oidc/preview", response_model=OidcPreviewResponse)
def oidc_preview(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> OidcPreviewResponse:
    """What the SPA shows before asking for the confirmation click."""
    settings = _require_enabled()
    claims = _pending_claims(request, settings)
    link = db.exec(
        select(OidcLink).where(
            OidcLink.issuer == claims["issuer"],
            OidcLink.subject == claims["sub"],
        )
    ).first()
    return OidcPreviewResponse(email=str(claims["email"]), linked=link is not None)


@router.post("/oidc/confirm", response_model=TokenResponse)
@limiter.limit("5/minute")
def oidc_confirm(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    """The explicit confirmation: exchange the pending blob for a session.

    First sign-in creates the `(issuer, subject)` link; later sign-ins reuse
    it. Where a link already exists it WINS over the current email match — the
    confirmed binding is the identity, and an email reassigned in the meantime
    cannot silently re-point a provider identity at another account.
    """
    settings = _require_enabled()
    claims = _pending_claims(request, settings)
    issuer = str(claims["issuer"])
    subject = str(claims["sub"])

    link = db.exec(
        select(OidcLink).where(OidcLink.issuer == issuer, OidcLink.subject == subject)
    ).first()
    if link is not None:
        user = db.get(User, link.user_id)
    else:
        user = db.exec(select(User).where(User.email == str(claims["email"]))).first()
    if user is None:
        raise api_error(401, "Sign-in link expired; start over", "oidc_pending_expired")
    if user.email == DEMO_EMAIL:
        # The demo door stays local: SSO must never become a second way into
        # the shared public tenant.
        raise api_error(403, "SSO sign-in is not available for the demo account", "forbidden")

    if link is None:
        db.add(OidcLink(user_id=user.id, issuer=issuer, subject=subject))
        db.commit()

    _clear_oidc_cookie(response, OIDC_PENDING_COOKIE, settings)
    pair = _issue_pair(db, user)
    _set_refresh_cookie(response, pair.refresh_token)
    return pair

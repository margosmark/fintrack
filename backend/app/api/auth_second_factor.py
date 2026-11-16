"""Second factor: TOTP enrollment/verification, recovery codes, passkeys (docs/38).

Every route here issues its session through `app.api.auth`'s `_issue_pair` +
`_set_refresh_cookie`, so a TOTP, recovery-code or passkey login produces the
byte-identical `TokenResponse` + httpOnly refresh cookie a password login
produces — the MCP `BackendClient` contract is untouched by construction.

Demo tenant: enrollment/disable/register/delete are mutating routes behind
`get_current_user`, so the shared guard in `api/deps.py` answers
`403 demo_account_restricted`; the two login-side routes are public and never
match the demo account (it has no factor, and `POST /auth/login` skips the
challenge for it outright — docs/38 decision 10).
"""

import hashlib
import logging
import secrets
import threading
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlparse

import jwt
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session
from sqlmodel import delete, select
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    generate_challenge,
    options_to_json_dict,
    structs,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.api.auth import _issue_pair, _set_refresh_cookie
from app.api.deps import get_current_user, get_db
from app.core.config import get_settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.errors import api_error
from app.core.ratelimit import limiter
from app.core.security import (
    CHALLENGE_TTL_SECONDS,
    PASSWORD_CHALLENGE_METHOD,
    create_challenge_token,
    decode_challenge_token,
)
from app.models.auth_factors import RecoveryCode, TotpFactor, WebAuthnCredential
from app.models.users import User
from app.schemas.auth import (
    CodeRequest,
    PasskeyLoginOptionsRequest,
    PasskeyLoginRequest,
    PasskeyOptionsResponse,
    PasskeyPage,
    PasskeyPublic,
    PasskeyRegisterRequest,
    SecondFactorLoginRequest,
    TokenResponse,
    TotpConfirmResponse,
    TotpSetupResponse,
    TotpStatusResponse,
)
from app.services import totp as totp_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

PASSKEY_CAP = 10
# M87 (docs/29 F15): the subject bound into the challenge token issued to an
# address with NO usable credential. `users.id` is a serial that starts at 1,
# so 0 resolves to no row — `db.get(User, 0)` is None and the follow-up
# `POST /auth/passkeys/login` answers the uniform 401 after the assertion
# fails. Never an id that could exist. SEC-1 (t_4476a739): for the
# `passkey_login` purpose this subject rides SEALED inside the token
# (`BLINDED_SUBJECT_METHODS`, core/security.py) — same value, but no holder
# can read it, so keyless and keyed mints are indistinguishable in the
# payload too.
THROWAWAY_USER_ID = 0
# Informational hint only (what the browser reported at registration) — never a
# verification input, and filtered so arbitrary strings cannot reach the DB.
TRANSPORT_ALLOWLIST = frozenset({"usb", "nfc", "ble", "internal", "hybrid", "smart-card"})
# Discoverable (passwordless) sign-in — a challenge purpose that binds NO
# account (docs/38 §Passkeys). Kept distinct from `"passkey_login"` so an
# email-first token can never satisfy the passwordless ceremony or vice
# versa: on a discoverable sign-in the account comes only from the VERIFIED
# credential's `userHandle`, never from the token's subject.
DISCOVERABLE_CHALLENGE_METHOD = "passkey_discoverable"

# Single-use passkey challenges (docs/38). The challenge token is a
# stateless 5-minute JWT, so without this a captured assertion + token could
# be replayed inside its window — the sign-count clone rule only catches that
# on an authenticator that counts. A SUCCESSFUL sign-in marks the challenge's
# own digest spent until JWT expiry; keyed by the challenge bytes (not the
# token), so a re-issued token carrying the same challenge is caught too.
# In-process: uvicorn runs a single worker (backend/Dockerfile CMD), and a
# restart only restores the stateless posture for the minutes a token has left.
_SPENT_CHALLENGES: dict[str, datetime] = {}
_SPENT_CHALLENGES_LOCK = threading.Lock()


def _spend_challenge(challenge: bytes) -> bool:
    """False when this challenge was already spent (a replay)."""
    now = datetime.now(UTC)
    key = hashlib.sha256(challenge).hexdigest()
    with _SPENT_CHALLENGES_LOCK:
        for spent, expires_at in tuple(_SPENT_CHALLENGES.items()):
            if expires_at <= now:
                del _SPENT_CHALLENGES[spent]
        if key in _SPENT_CHALLENGES:
            return False
        _SPENT_CHALLENGES[key] = now + timedelta(seconds=CHALLENGE_TTL_SECONDS)
        return True


# --- shared helpers ---------------------------------------------------------


def _webauthn_origin() -> tuple[str, str]:
    """`(rp_id, expected_origin)` — pinned by APP_ORIGIN, never the request's
    `Origin` header (docs/38 decision 6: trusting the header would let a stolen
    access token register a credential bound to an attacker origin)."""
    origin = get_settings().app_origin.rstrip("/")
    rp_id = urlparse(origin).hostname
    if not rp_id:
        raise api_error(500, "APP_ORIGIN is not a valid origin", "invalid_app_origin")
    return rp_id, origin


def _decode_challenge(token: str, method: str) -> tuple[int, bytes | None]:
    """Challenge decode with the one uniform 401 the docs pin: tampered,
    expired, wrong-purpose and malformed all look identical to the client."""
    try:
        return decode_challenge_token(token, method)
    except jwt.PyJWTError:
        raise api_error(
            401,
            "Sign-in challenge is invalid or expired",
            "invalid_or_expired_challenge",
        ) from None


def _factor(db: Session, user_id: int) -> TotpFactor | None:
    return db.exec(select(TotpFactor).where(TotpFactor.user_id == user_id)).first()


def _active_secret(factor: TotpFactor | None) -> str | None:
    if factor is None or factor.secret is None:
        return None
    return decrypt_secret(factor.secret)


def _challenge_user(db: Session, challenge_token: str, method: str) -> User:
    """Resolve the user behind a login challenge; 401 when it no longer exists."""
    user_id, _ = _decode_challenge(challenge_token, method)
    user = db.get(User, user_id)
    if user is None:
        raise api_error(
            401,
            "Sign-in challenge is invalid or expired",
            "invalid_or_expired_challenge",
        )
    return user


def _decode_login_challenge(token: str) -> tuple[str, int, bytes | None]:
    """`(purpose, user_id, challenge)` for a live passkey-login token.

    Exactly two purposes reach `POST /auth/passkeys/login`: `"passkey_login"`
    (email-first — subject = the selected account SEALED at mint, or the
    sealed `THROWAWAY_USER_ID` for an address with no credential: SEC-1, a
    keyless reader of the token learns neither) and
    `DISCOVERABLE_CHALLENGE_METHOD`
    (passwordless — subject = `THROWAWAY_USER_ID`, because the account is
    resolved later from the verified credential). Everything else — a
    registration or TOTP challenge, a tampered or expired token — is the one
    uniform `401 invalid_or_expired_challenge`.
    """
    for method in ("passkey_login", DISCOVERABLE_CHALLENGE_METHOD):
        try:
            user_id, challenge = decode_challenge_token(token, method)
        except jwt.PyJWTError:
            continue
        return method, user_id, challenge
    raise api_error(
        401,
        "Sign-in challenge is invalid or expired",
        "invalid_or_expired_challenge",
    )


def _assert_user_handle(credential: dict, expected_user_id: int, *, required: bool) -> None:
    """The authenticator's account hint must name the account being signed in.

    `userHandle` carries the `user.id` bytes minted at registration
    (`str(users.id)`), so a mismatch is a cross-account assertion: the
    credential verified for one account while naming another. It is REQUIRED
    on the discoverable path — with no email there is nothing else to name the
    account — and checked whenever PRESENT on the email-first path (a
    non-resident credential simply omits it there, which is why absence is
    only fatal on the discoverable path). One uniform `401 invalid_passkey`
    either way, and neither the handle nor the credential is ever logged.
    """
    block = credential.get("response", {})
    raw = block.get("userHandle") if isinstance(block, dict) else None
    if raw is None or raw == "":
        if required:
            raise api_error(401, "Passkey authentication failed", "invalid_passkey")
        return
    try:
        handle = base64url_to_bytes(str(raw))
    except (ValueError, TypeError):
        raise api_error(401, "Passkey authentication failed", "invalid_passkey") from None
    if handle != str(expected_user_id).encode("utf-8"):
        logger.warning("Passkey userHandle does not match user %s", expected_user_id)
        raise api_error(401, "Passkey authentication failed", "invalid_passkey")


# --- TOTP -------------------------------------------------------------------


@router.get("/totp/status", response_model=TotpStatusResponse)
def totp_status(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TotpStatusResponse:
    """Whether an ACTIVE factor / a PENDING enrollment exists (docs/38)."""
    factor = _factor(db, current_user.id)  # type: ignore[arg-type]
    return TotpStatusResponse(
        enabled=factor is not None and factor.secret is not None,
        pending=factor is not None and factor.pending_secret is not None,
    )


@router.post("/totp/setup", response_model=TotpSetupResponse)
@limiter.limit("5/minute")
def totp_setup(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TotpSetupResponse:
    """Stage a new secret for confirmation.

    Only `pending_secret` is written, so re-enrollment while a factor is live
    cannot take the account offline: the active secret keeps protecting it
    until a real code confirms the replacement.
    """
    settings = get_settings()
    secret = totp_service.new_secret()
    factor = _factor(db, current_user.id)  # type: ignore[arg-type]
    if factor is None:
        factor = TotpFactor(user_id=current_user.id)  # type: ignore[arg-type]
    factor.pending_secret = encrypt_secret(secret)
    db.add(factor)
    db.commit()
    return TotpSetupResponse(
        secret=secret,
        otpauth_url=totp_service.otpauth_url(secret, current_user.email, settings.totp_issuer),
    )


@router.post("/totp/confirm", response_model=TotpConfirmResponse)
@limiter.limit("5/minute")
def totp_confirm(
    request: Request,
    payload: CodeRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TotpConfirmResponse:
    """Activate the pending secret and mint a fresh recovery-code set."""
    factor = _factor(db, current_user.id)  # type: ignore[arg-type]
    if factor is None or factor.pending_secret is None:
        raise api_error(409, "Two-factor enrollment has not been started", "totp_not_enrolled")
    secret = decrypt_secret(factor.pending_secret)
    now = datetime.now(UTC)
    # No replay guard here: the guard is a property of the ACTIVE secret, and
    # passing the previous secret's `last_used_step` would reject a perfectly
    # good confirmation code during re-enrollment.
    step = totp_service.verify_totp(secret, payload.code, now, None)
    if step is None:
        raise api_error(401, "Invalid or expired code", "invalid_totp_code")

    factor.secret = factor.pending_secret
    factor.pending_secret = None
    # Reset, don't stamp: `last_used_step` belongs to the PREVIOUS secret, and
    # carrying it over (or stamping the confirmation code) would reject every
    # login code in the same 30 s window — a dead zone right after enrolling.
    # The replay guard protects the LOGIN path; enrollment has its own proof.
    factor.last_used_step = None
    db.add(factor)
    # A confirm always re-rolls: the previous set (if any) dies with it, so a
    # leaked old code cannot outlive a re-enrollment.
    db.exec(delete(RecoveryCode).where(RecoveryCode.user_id == current_user.id))  # type: ignore[arg-type]
    codes = totp_service.new_recovery_codes()
    for code in codes:
        db.add(RecoveryCode(user_id=current_user.id, code_hash=totp_service.hash_recovery(code)))  # type: ignore[arg-type]
    db.commit()
    return TotpConfirmResponse(recovery_codes=codes)


@router.post("/totp/disable", status_code=204)
@limiter.limit("5/minute")
def totp_disable(
    request: Request,
    payload: CodeRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Disable 2FA. The code is required so a stolen access token alone cannot
    silently strip the account's second factor."""
    factor = _factor(db, current_user.id)  # type: ignore[arg-type]
    secret = _active_secret(factor)
    if secret is None:
        raise api_error(409, "Two-factor authentication is not enabled", "totp_not_enrolled")

    now = datetime.now(UTC)
    if totp_service.is_totp_shaped(payload.code):
        if totp_service.verify_totp(secret, payload.code, now, factor.last_used_step) is None:  # type: ignore[union-attr]
            raise api_error(401, "Invalid or expired code", "invalid_totp_code")
    else:
        claimed = db.execute(
            sa_update(RecoveryCode)
            .where(
                RecoveryCode.user_id == current_user.id,  # type: ignore[arg-type]
                RecoveryCode.code_hash == totp_service.hash_recovery(payload.code),
                RecoveryCode.used_at.is_(None),
            )
            .values(used_at=now)
        )
        if claimed.rowcount != 1:
            raise api_error(401, "Invalid or already used recovery code", "invalid_recovery_code")

    db.delete(factor)
    db.exec(delete(RecoveryCode).where(RecoveryCode.user_id == current_user.id))  # type: ignore[arg-type]
    db.commit()


# --- second-factor login ----------------------------------------------------


@router.post("/login/totp", response_model=TokenResponse)
@limiter.limit("10/minute")
def login_with_totp(
    request: Request,
    response: Response,
    payload: SecondFactorLoginRequest,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    """Second factor for the password challenge: `{challenge_token, code}`.

    Order matters: the replay guard (`last_used_step`) is committed BEFORE the
    session is minted, so a replayed code can never race its own consumption
    into a live pair.
    """
    user = _challenge_user(db, payload.challenge_token, PASSWORD_CHALLENGE_METHOD)
    factor = _factor(db, user.id)  # type: ignore[arg-type]
    secret = _active_secret(factor)
    if secret is None:
        raise api_error(401, "Two-factor authentication is no longer enabled", "totp_not_enabled")

    now = datetime.now(UTC)
    step = totp_service.verify_totp(secret, payload.code, now, factor.last_used_step)  # type: ignore[union-attr]
    if step is None:
        raise api_error(401, "Invalid or expired code", "invalid_totp_code")
    factor.last_used_step = step  # type: ignore[union-attr]
    db.add(factor)  # type: ignore[arg-type]
    db.commit()

    pair = _issue_pair(db, user)
    _set_refresh_cookie(response, pair.refresh_token)
    return pair


@router.post("/login/recovery", response_model=TokenResponse)
@limiter.limit("10/minute")
def login_with_recovery(
    request: Request,
    response: Response,
    payload: SecondFactorLoginRequest,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    """Single-use recovery code: conditional UPDATE, so the database decides
    which of two concurrent claims wins and a code can never be spent twice."""
    user = _challenge_user(db, payload.challenge_token, PASSWORD_CHALLENGE_METHOD)
    now = datetime.now(UTC)
    claimed = db.execute(
        sa_update(RecoveryCode)
        .where(
            RecoveryCode.user_id == user.id,
            RecoveryCode.code_hash == totp_service.hash_recovery(payload.code),
            RecoveryCode.used_at.is_(None),
        )
        .values(used_at=now)
    )
    db.commit()  # the claim is durable on its own, like _claim_refresh_token
    if claimed.rowcount != 1:
        raise api_error(401, "Invalid or already used recovery code", "invalid_recovery_code")

    pair = _issue_pair(db, user)
    _set_refresh_cookie(response, pair.refresh_token)
    return pair


# --- passkeys (WebAuthn) ----------------------------------------------------


def _exclude_credentials(db: Session, user_id: int) -> list[structs.PublicKeyCredentialDescriptor]:
    rows = db.exec(select(WebAuthnCredential).where(WebAuthnCredential.user_id == user_id)).all()
    return [
        structs.PublicKeyCredentialDescriptor(
            id=row.credential_id, type=structs.PublicKeyCredentialType.PUBLIC_KEY
        )
        for row in rows
    ]


def _credential_transports(raw: object) -> str | None:
    """Comma-joined transports from the browser, filtered to the known set."""
    if not isinstance(raw, list):
        return None
    values = [str(v) for v in raw if isinstance(v, str) and v in TRANSPORT_ALLOWLIST]
    return ",".join(values) if values else None


@router.get("/passkeys", response_model=PasskeyPage)
def list_passkeys(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PasskeyPage:
    rows = db.exec(
        select(WebAuthnCredential)
        .where(WebAuthnCredential.user_id == current_user.id)  # type: ignore[arg-type]
        .order_by(WebAuthnCredential.id)
    ).all()
    return PasskeyPage(items=rows, total=len(rows))


@router.post("/passkeys/options", response_model=PasskeyOptionsResponse)
@limiter.limit("10/minute")
def passkey_register_options(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PasskeyOptionsResponse:
    rp_id, _ = _webauthn_origin()
    exclude = _exclude_credentials(db, current_user.id)  # type: ignore[arg-type]
    if len(exclude) >= PASSKEY_CAP:
        raise api_error(
            400,
            f"Passkey limit of {PASSKEY_CAP} reached; remove one before adding another",
            "passkey_cap",
        )
    challenge = generate_challenge()
    token = create_challenge_token(current_user.id, "passkey", challenge)  # type: ignore[arg-type]
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name="fin_tracker",
        user_name=current_user.email,
        user_id=str(current_user.id).encode("utf-8"),
        user_display_name=current_user.name or current_user.email,
        challenge=challenge,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=exclude,
    )
    return PasskeyOptionsResponse(
        challenge_token=token,
        options=options_to_json_dict(options),
    )


@router.post("/passkeys", response_model=PasskeyPublic, status_code=201)
@limiter.limit("10/minute")
def passkey_register(
    request: Request,
    payload: PasskeyRegisterRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PasskeyPublic:
    rp_id, origin = _webauthn_origin()
    user_id, challenge = _decode_challenge(payload.challenge_token, "passkey")
    if user_id != current_user.id or challenge is None:
        raise api_error(
            401,
            "Sign-in challenge is invalid or expired",
            "invalid_or_expired_challenge",
        )
    if len(_exclude_credentials(db, current_user.id)) >= PASSKEY_CAP:  # type: ignore[arg-type]
        raise api_error(
            400,
            f"Passkey limit of {PASSKEY_CAP} reached; remove one before adding another",
            "passkey_cap",
        )
    try:
        verification = verify_registration_response(
            credential=payload.credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
        )
    except (WebAuthnException, ValueError, TypeError) as exc:
        # ValueError/TypeError as above: the library's JSON parse decodes
        # base64url/CBOR fields outside its own exception hierarchy.
        raise api_error(
            422, f"Passkey registration failed: {exc}", "invalid_passkey_response"
        ) from None

    existing = db.exec(
        select(WebAuthnCredential).where(
            WebAuthnCredential.credential_id == verification.credential_id
        )
    ).first()
    if existing is not None:
        raise api_error(409, "This passkey is already registered", "passkey_exists")

    response_block = payload.credential.get("response", {})
    raw_transports = response_block.get("transports") if isinstance(response_block, dict) else None
    row = WebAuthnCredential(
        user_id=current_user.id,  # type: ignore[arg-type]
        credential_id=verification.credential_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        transports=_credential_transports(raw_transports),
        name=(payload.name or "Passkey").strip() or "Passkey",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row  # type: ignore[return-value]


@router.delete("/passkeys/{passkey_id}", status_code=204)
def delete_passkey(
    passkey_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    row = db.get(WebAuthnCredential, passkey_id)
    # 404 for a foreign id — a 403 would confirm the row exists.
    if row is None or row.user_id != current_user.id:
        raise api_error(404, "Passkey not found", "not_found")
    db.delete(row)
    db.commit()


@router.post("/passkeys/login/options", response_model=PasskeyOptionsResponse)
@limiter.limit("10/minute")
def passkey_login_options(
    request: Request,
    payload: PasskeyLoginOptionsRequest,
    db: Annotated[Session, Depends(get_db)],
) -> PasskeyOptionsResponse:
    """Passwordless start — two ceremonies, one 200 envelope (docs/38).

    **Email-first** (`email` non-blank, M87): a challenge envelope for EVERY
    address — an account with passkeys gets a real `allowCredentials` list,
    an account without one and no account at all get a throwaway challenge
    with a random descriptor. The three cases ARE indistinguishable on the
    status channel (always 200 — the retired `404 no_passkeys` split was the
    oracle) and INSIDE the token (SEC-1, t_4476a739): `challenge_token` is
    signed, not encrypted, so this purpose's subject is SEALED at mint
    (`BLINDED_SUBJECT_METHODS`, core/security.py) — a keyless reader sees
    the same claim set, types and byte length whichever case minted it, and
    the real id / `THROWAWAY_USER_ID = 0` are equally unreadable. The
    ceremony is where the cases fail (`401 invalid_or_expired_challenge` /
    `401 invalid_passkey`). Between them these close the status-code and
    token-content halves of M83's F-7 passkey oracle.

    **Known residual AT THE responder (documented, accepted — docs/42 §2):**
    the `allowCredentials[0].id` VALUE still discloses whether the address
    has a registered passkey, so the responder is NOT a uniform channel.
    Email-first non-discoverable login must name the address's real
    credential ids (an empty list would be discoverable-only and strand
    non-resident credentials — a product-gated switch), so a real id —
    stable across requests, length following the stored credential (16
    bytes → 22 base64url chars) — separates from the per-request 32-byte
    throwaway (43 chars, regenerated every call) in ONE request by length or
    TWO by stability. The leak is partial: it confirms only the
    passkey-holding subset, a keyless account and a ghost are
    indistinguishable from each other, and no credential material or auth
    bypass is exposed. Pinned as documented by
    `test_passkey_login_options_id_channel_reveals_enrollment_as_documented`;
    closing the channel means the discoverable-only product decision plus a
    docs/42 §2 update in the same change.

    **Discoverable** (no `email`, or blank): the account is resolved at
    `POST /auth/passkeys/login` from the VERIFIED credential, so this
    challenge binds no account (`THROWAWAY_USER_ID`, purpose
    `DISCOVERABLE_CHALLENGE_METHOD`) and `allowCredentials` is EMPTY — the
    standard "offer every discoverable credential you hold for this rpId"
    signal — with `userVerification: "required"` because a passwordless
    assertion IS the second factor. Same status, same envelope keys and
    types as the email-first answer; the empty descriptor list is the WebAuthn
    wire form of "any discoverable credential" and discloses nothing about
    any account.
    """
    rp_id, _ = _webauthn_origin()
    challenge = generate_challenge()
    if payload.email is None or not payload.email.strip():
        token = create_challenge_token(THROWAWAY_USER_ID, DISCOVERABLE_CHALLENGE_METHOD, challenge)
        options = generate_authentication_options(
            rp_id=rp_id,
            challenge=challenge,
            allow_credentials=[],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return PasskeyOptionsResponse(
            challenge_token=token,
            options=options_to_json_dict(options),
        )
    user = db.exec(select(User).where(User.email == payload.email)).first()
    allow = _exclude_credentials(db, user.id) if user is not None else []
    if user is not None and allow:
        token = create_challenge_token(user.id, "passkey_login", challenge)
    else:
        token = create_challenge_token(THROWAWAY_USER_ID, "passkey_login", challenge)
        # Keep `allowCredentials` present and same-shaped as a real answer —
        # dropping the key (the library omits an empty list) would separate the
        # two bodies by key set, which is the oracle restated as JSON.
        allow = [
            structs.PublicKeyCredentialDescriptor(
                id=secrets.token_bytes(32), type=structs.PublicKeyCredentialType.PUBLIC_KEY
            )
        ]
    options = generate_authentication_options(
        rp_id=rp_id,
        challenge=challenge,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return PasskeyOptionsResponse(
        challenge_token=token,
        options=options_to_json_dict(options),
    )


@router.post("/passkeys/login", response_model=TokenResponse)
@limiter.limit("10/minute")
def passkey_login(
    request: Request,
    response: Response,
    payload: PasskeyLoginRequest,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    """Verify the assertion and issue the SAME session a password login issues.

    Two purposes, two account-resolution rules (docs/38 §Passkeys):

    - `passkey_login` (email-first): the account is the token's subject —
      SEALED at mint, opened only by `decode_challenge_token` (SEC-1: a
      holder cannot read which case minted the token) — and the credential
      must belong to it: a foreign credential resolves to no row and fails
      `401 invalid_passkey`.
    - `DISCOVERABLE_CHALLENGE_METHOD` (passwordless): the account is resolved
      ONLY from the credential — found by credential id alone (never by a
      client-supplied address), accepted only after the signature verifies
      against that row's stored public key, and only when the assertion's
      `userHandle` names that row's user. Unknown credential, missing or
      mismatched handle, and cross-account assertions all land on the same
      uniform `401 invalid_passkey`.

    Both paths then share every other rule: rpId/origin pinned to APP_ORIGIN,
    user verification REQUIRED on the discoverable path (PREFERRED on the
    email-first one, where the assertion is not necessarily a second factor),
    the sign-count clone rule, and a single-use challenge (a captured
    assertion cannot be replayed inside the token's window). No TOTP step
    follows: a WebAuthn assertion with user verification is the second factor
    (docs/38 decision 5). Nothing but user ids is ever logged.
    """
    rp_id, origin = _webauthn_origin()
    method, token_user_id, challenge = _decode_login_challenge(payload.challenge_token)
    if challenge is None:
        raise api_error(
            401,
            "Sign-in challenge is invalid or expired",
            "invalid_or_expired_challenge",
        )
    try:
        credential_id = base64url_to_bytes(str(payload.credential.get("id", "")))
    except (ValueError, TypeError):
        raise api_error(401, "Passkey authentication failed", "invalid_passkey") from None

    discoverable = method == DISCOVERABLE_CHALLENGE_METHOD
    if discoverable:
        # Account resolution ONLY from the credential: the token names no
        # account, so the row is looked up by credential id across all users
        # and every later check must agree with it before a session exists.
        row = db.exec(
            select(WebAuthnCredential).where(WebAuthnCredential.credential_id == credential_id)
        ).first()
        if row is None:
            logger.warning("Rejected passkey assertion for an unregistered credential")
            raise api_error(401, "Passkey authentication failed", "invalid_passkey")
        user = db.get(User, row.user_id)
        if user is None:
            raise api_error(401, "Passkey authentication failed", "invalid_passkey")
    else:
        user = db.get(User, token_user_id)
        if user is None:
            raise api_error(
                401,
                "Sign-in challenge is invalid or expired",
                "invalid_or_expired_challenge",
            )
        row = db.exec(
            select(WebAuthnCredential).where(
                WebAuthnCredential.credential_id == credential_id,
                WebAuthnCredential.user_id == user.id,
            )
        ).first()
        if row is None:
            raise api_error(401, "Passkey authentication failed", "invalid_passkey")

    try:
        verification = verify_authentication_response(
            credential=payload.credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=row.public_key,
            credential_current_sign_count=row.sign_count,
            require_user_verification=discoverable,
        )
    except (WebAuthnException, ValueError, TypeError) as exc:
        # ValueError/TypeError too: the library's own JSON parse decodes
        # base64url fields outside its exception hierarchy (binascii.Error is
        # a ValueError), and a malformed credential is a domain failure —
        # it must land on the envelope, never as a bare 500.
        # The exception CLASS only: library messages can quote input bytes,
        # and this log must never carry credential material (docs/38).
        logger.warning("Rejected passkey assertion for user %s (%s)", user.id, type(exc).__name__)
        raise api_error(401, "Passkey authentication failed", "invalid_passkey") from None

    # Only after the signature is proven: the assertion must name the account
    # it just proved possession for (required when passwordless, checked when
    # present otherwise).
    _assert_user_handle(payload.credential, user.id, required=discoverable)

    new_count = verification.new_sign_count
    if new_count and new_count <= row.sign_count:
        # A non-increasing counter on an authenticator that counts is the
        # clone signal — reject rather than adopt the weaker value.
        logger.warning(
            "Sign-count regression for passkey %s (%s -> %s) — possible clone",
            row.id,
            row.sign_count,
            new_count,
        )
        raise api_error(401, "Passkey authentication failed", "invalid_passkey")
    # Single-use challenge: everything verified, so this exact challenge is
    # now spent and the same assertion can no longer mint a second session.
    if not _spend_challenge(challenge):
        logger.warning("Replayed passkey challenge for user %s", user.id)
        raise api_error(
            401,
            "Sign-in challenge is invalid or expired",
            "invalid_or_expired_challenge",
        )
    if new_count:
        # M87 (M83 F-6): adopt the counter only when it COUNTS. `0` means the
        # authenticator does not maintain a signature counter (WebAuthn
        # §6.1.3); writing it back stored 0 over a non-zero value and erased
        # the very baseline every later clone check compares against, so a
        # single assertion from a non-counting authenticator permanently
        # disarmed the detection. A 0 therefore leaves `row.sign_count` alone.
        row.sign_count = new_count
    row.last_used_at = datetime.now(UTC)
    db.add(row)
    db.commit()

    pair = _issue_pair(db, user)
    _set_refresh_cookie(response, pair.refresh_token)
    return pair

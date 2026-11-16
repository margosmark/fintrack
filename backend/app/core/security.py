"""Password hashing (bcrypt, cost >= 12) and JWT/refresh token mechanics (docs/01).

Server-signed JWTs are RS256 with a `kid` header, split across two key families
(`app/core/jwt_keys.py`, t_d3176af6):

- ACCESS  family signs `type=access` bearer tokens.
- SESSION family signs `type=challenge` (2FA step-up) and `type=oidc_pending`
  (the OIDC hand-off blob) — the two token kinds that escalate into a session.

`jwt_secret` no longer signs any JWT. Refresh tokens are opaque random bytes
stored only as a SHA-256 hash (`new_refresh_token`), so they are independent of
the signing keys and are what keeps existing sessions alive across a key
rotation.
"""

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import get_settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.jwt_keys import decode_jwt, encode_jwt

_BCRYPT_ROUNDS = 12


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode(
        "utf-8"
    )


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: int) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
    }
    return encode_jwt("access", payload)


def decode_access_token(token: str) -> int:
    """Return the user id encoded in a valid access JWT.

    Raises jwt.PyJWTError (which callers map to 401) for any malformed, expired,
    wrong-type, wrong-key or subject-less token — never a bare KeyError.
    """
    payload = decode_jwt("access", token)
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    try:
        return int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise jwt.InvalidTokenError("missing or invalid subject claim") from exc


def new_refresh_token() -> tuple[str, str]:
    """Issue an opaque 32-byte refresh token.

    Returns the client-facing token string and its SHA-256 hex hash for storage.
    """
    token = secrets.token_hex(32)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- second-factor challenge tokens (M74, docs/38) ---------------------------
#
# A challenge token proves "the password was verified N seconds ago" without
# issuing any session, so the account's second factor can be collected on a
# follow-up request. It is a signed JWT like the access token, but with
# `type: "challenge"` — `decode_access_token` rejects it, so a challenge can
# never be used as a bearer token, and vice versa.

CHALLENGE_TTL_SECONDS = 300
OIDC_PENDING_TTL_SECONDS = 600
# The password step proves the FIRST factor. ONE token method serves both
# second-factor endpoints — `/auth/login/totp` and `/auth/login/recovery` —
# so the user can switch between a TOTP code and a recovery code on the same
# challenge without going back to the password step.
PASSWORD_CHALLENGE_METHOD = "second_factor"  # noqa: S105 -- JWT method claim, not a credential

# SEC-1 (t_4476a739): purposes whose challenge token is minted to
# UNAUTHENTICATED callers. `encode_jwt` is RS256 — signed, NOT encrypted — so
# any holder decodes `sub` without the key, and `passkey_login` is minted per
# ADDRESS: a subject in the clear is a one-request account/passkey
# enumeration oracle (docs/42 §S1b). For these purposes the subject is SEALED
# with the at-rest key (`crypto.py`) at a FIXED width, so a keyless reader
# sees the same claim set, the same types and the same byte length whichever
# case minted the token — no positive-integer subject, no length tell. The
# binding itself is unchanged: it lives inside the ciphertext, under the
# signature, and opens only on the server's own decode path.
BLINDED_SUBJECT_METHODS = frozenset({"passkey_login"})
# Fixed ⇒ equal-length ciphertext for every id (a serial fits easily); the
# width is not secret — `int()` re-derives the id after decryption.
_SUBJECT_WIDTH = 20


def _seal_subject(user_id: int) -> str:
    return encrypt_secret(f"{user_id:0{_SUBJECT_WIDTH}d}")


def _unseal_subject(raw: object) -> int:
    """Inverse of `_seal_subject`; anything else raises `InvalidTokenError`.

    `decrypt_secret` passes non-Fernet input through unchanged, so the
    fixed-width digit shape is checked HERE: a clear subject (a hand-signed
    forge, or a token minted before SEC-1) is refused with the same uniform
    401 every other bad challenge token gets.
    """
    if isinstance(raw, str):
        plaintext = decrypt_secret(raw)
        if len(plaintext) == _SUBJECT_WIDTH and plaintext.isdigit():
            return int(plaintext)
    raise jwt.InvalidTokenError("missing or invalid subject claim")


def create_challenge_token(user_id: int, method: str, challenge: bytes | None = None) -> str:
    """Sign a short-lived, purpose-bound challenge.

    `challenge` carries the server-generated WebAuthn challenge so passkey
    verification stays STATELESS: the response is checked against the value
    inside this token, never against a server-side challenge table that could
    leak or expire separately.

    For `BLINDED_SUBJECT_METHODS` the subject is sealed (SEC-1) instead of
    sitting in the payload as a readable integer — same stateless posture,
    opaque to the token's holder.
    """
    now = datetime.now(UTC)
    payload: dict = {
        "sub": _seal_subject(user_id) if method in BLINDED_SUBJECT_METHODS else str(user_id),
        "type": "challenge",
        "method": method,
        "iat": now,
        "exp": now + timedelta(seconds=CHALLENGE_TTL_SECONDS),
    }
    if challenge is not None:
        payload["challenge"] = base64.urlsafe_b64encode(challenge).decode("ascii").rstrip("=")
    return encode_jwt("session", payload)


def decode_challenge_token(token: str, method: str) -> tuple[int, bytes | None]:
    """Return `(user_id, challenge)` for a live token of THIS `method`.

    For `BLINDED_SUBJECT_METHODS` the id comes out of the sealed subject
    (`_unseal_subject`); for every other purpose it is read from `sub` in the
    clear, exactly as before.

    Raises jwt.PyJWTError for a malformed, expired, wrong-type, wrong-method
    or subject-less token — callers map that to 401
    `invalid_or_expired_challenge`.
    """
    payload = decode_jwt("session", token)
    if payload.get("type") != "challenge" or payload.get("method") != method:
        raise jwt.InvalidTokenError("wrong challenge purpose")
    try:
        raw_sub = payload["sub"]
        if method in BLINDED_SUBJECT_METHODS:
            user_id = _unseal_subject(raw_sub)
        else:
            user_id = int(raw_sub)
    except (KeyError, TypeError, ValueError) as exc:
        raise jwt.InvalidTokenError("missing or invalid subject claim") from exc
    challenge_b64 = payload.get("challenge")
    if challenge_b64 is None:
        return user_id, None
    padded = challenge_b64 + "=" * (-len(challenge_b64) % 4)
    return user_id, base64.urlsafe_b64decode(padded)


def create_oidc_pending_token(email: str, issuer: str, subject: str) -> str:
    """Sign the OIDC callback's result for hand-off to the SPA.

    Delivered ONLY as an httpOnly cookie (docs/38 decision 8): no token ever
    rides in a URL, and same-origin script can neither read nor forge it.
    """
    now = datetime.now(UTC)
    return encode_jwt(
        "session",
        {
            "sub": subject,
            "type": "oidc_pending",
            "email": email,
            "issuer": issuer,
            "iat": now,
            "exp": now + timedelta(seconds=OIDC_PENDING_TTL_SECONDS),
        },
    )


def decode_oidc_pending_token(token: str) -> dict:
    """Claims of a live `oidc_pending` blob; raises jwt.PyJWTError otherwise."""
    payload = decode_jwt("session", token)
    if payload.get("type") != "oidc_pending":
        raise jwt.InvalidTokenError("wrong token type")
    return payload

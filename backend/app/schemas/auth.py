"""auth — request/response shapes (docs/08, m1-contract; M74 docs/38)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.schemas.users import UserPublic


class RegisterRequest(BaseModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


class TokenResponse(BaseModel):
    user: UserPublic
    access_token: str
    # SECURITY (audit M1): the refresh token is delivered ONLY via the
    # httpOnly cookie set by api/auth.py::_set_refresh_cookie. The field stays
    # on the model so callers (_issue_pair -> register/login/demo/refresh and
    # users.py change-password) can read `.refresh_token` to set that cookie,
    # but `exclude=True` keeps it out of every JSON response body — same-origin
    # script (XSS/compromised bundle) must never see a 30-day credential.
    refresh_token: str = Field(exclude=True)
    token_type: str = "bearer"


class RegisterAccepted(BaseModel):
    """What `POST /auth/register` answers — for EVERY address (M87, docs/29 F15).

    Deliberately token-free: a session on one branch and an error on the other
    is exactly the account-existence oracle this closes, so the two branches
    must be byte-identical (the responder-consistency test pins equality). The
    account IS created when the address was free — the caller's recovery path
    is "sign in with the password you just chose".
    """

    status: Literal["accepted"]
    detail: str


# --- M74 second factor + SSO (docs/38) --------------------------------------
class LoginChallenge(BaseModel):
    """What POST /auth/login returns instead of a pair when the account has an
    active TOTP factor: no session exists yet, and this token cannot be used as
    a bearer token (it is a signed `type: "challenge"` JWT, docs/38)."""

    challenge: Literal["totp"]
    challenge_token: str
    expires_in: int


class SecondFactorLoginRequest(BaseModel):
    """`POST /auth/login/totp` and `/auth/login/recovery` body: the password
    challenge that was just proved, plus the second factor itself."""

    challenge_token: str
    code: str = Field(max_length=64)


class CodeRequest(BaseModel):
    """A TOTP code, a recovery code, or a malformed attempt.

    Deliberately unconstrained: a bad-shape code must surface as the route's
    401 (`invalid_totp_code` / `invalid_recovery_code`), never as a 422, so a
    client has exactly one failure path.
    """

    code: str = Field(max_length=64)


class TotpStatusResponse(BaseModel):
    enabled: bool
    pending: bool


class TotpSetupResponse(BaseModel):
    secret: str
    otpauth_url: str


class TotpConfirmResponse(BaseModel):
    # Shown ONCE — nothing stores them in plaintext.
    recovery_codes: list[str]


class PasskeyOptionsResponse(BaseModel):
    challenge_token: str
    # PublicKeyCredentialCreationOptions / ...RequestOptions, base64url fields
    # (webauthn.helpers.options_to_json).
    options: dict[str, Any]


class PasskeyRegisterRequest(BaseModel):
    challenge_token: str
    credential: dict[str, Any]
    name: str | None = Field(default=None, max_length=100)


class PasskeyPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    transports: str | None = None


class PasskeyPage(BaseModel):
    items: list[PasskeyPublic]
    total: int


class PasskeyLoginOptionsRequest(BaseModel):
    # `null`/absent/blank = the discoverable (passwordless) start: no account
    # is named here, and the server resolves one from the VERIFIED credential
    # at `POST /auth/passkeys/login` (docs/38 §Passkeys, discoverable).
    # A non-blank value keeps the M87 email-first ceremony byte-for-byte.
    email: str | None = Field(default=None, max_length=255)


class PasskeyLoginRequest(BaseModel):
    challenge_token: str
    credential: dict[str, Any]


class OidcConfigResponse(BaseModel):
    enabled: bool


class OidcPreviewResponse(BaseModel):
    email: str
    linked: bool

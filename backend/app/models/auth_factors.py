"""Second-factor and SSO credential rows (M74 — docs/38).

Four user-scoped tables, all auth plumbing like `refresh_tokens`: every row
carries `user_id`, and account deletion wipes them through
`api/users.py::_USER_OWNED_MODELS`.

- `TotpFactor` — one row per user: an ACTIVE secret (Fernet ciphertext), an
  optional PENDING secret awaiting confirmation, and `last_used_step`, the
  highest accepted TOTP timestep (the replay guard).
- `RecoveryCode` — single-use hashed fallback codes, claimed atomically.
- `WebAuthnCredential` — passkeys: credential id (unique), COSE public key,
  monotonic sign count.
- `OidcLink` — a confirmed `(issuer, subject)` provider identity bound to one
  local account. Email only ever MATCHES an account for the first link; it is
  never the identity.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, LargeBinary, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class TotpFactor(TimestampMixin, table=True):
    __tablename__ = "totp_factors"
    __table_args__ = (UniqueConstraint("user_id", name="uq_totp_factors_user"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    # encrypt_secret() ciphertext of the ACTIVE base32 secret; NULL until the
    # first POST /auth/totp/confirm.
    secret: str | None = Field(default=None)
    # encrypt_secret() ciphertext of the secret awaiting confirmation. Setup
    # writes ONLY this column, so re-enrollment never takes a live factor down.
    pending_secret: str | None = Field(default=None)
    last_used_step: int | None = Field(default=None, sa_type=BigInteger)


class RecoveryCode(TimestampMixin, table=True):
    __tablename__ = "recovery_codes"
    __table_args__ = (UniqueConstraint("user_id", "code_hash", name="uq_recovery_codes_user_hash"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    code_hash: str
    used_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class WebAuthnCredential(TimestampMixin, table=True):
    __tablename__ = "webauthn_credentials"
    __table_args__ = (
        UniqueConstraint("credential_id", name="uq_webauthn_credentials_credential_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    credential_id: bytes = Field(sa_type=LargeBinary)
    public_key: bytes = Field(sa_type=LargeBinary)
    sign_count: int = Field(default=0, sa_type=BigInteger)
    # Comma-joined transports hint ("internal,hybrid") — a hint for the client,
    # never a verification input.
    transports: str | None = Field(default=None)
    name: str = Field(default="Passkey", max_length=100)
    last_used_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class OidcLink(TimestampMixin, table=True):
    __tablename__ = "oidc_links"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_oidc_links_issuer_subject"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    issuer: str
    subject: str

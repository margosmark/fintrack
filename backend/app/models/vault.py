"""vault_envelopes — per-device wrapped-DEK blobs (E2EE Phase 4, docs/33).

One row per browser profile that has enrolled in the encryption vault. The
`wrapped_blob` is an opaque, passphrase-locked bundle: the server stores and
returns it verbatim and can never open it (it has neither the passphrase nor
the KEK). Revocation is a soft delete that also DROPS the blob, so the copy the
database held stops existing:

    active   <=> revoked_at IS NULL  AND wrapped_blob IS NOT NULL
    revoked  <=> revoked_at IS NOT NULL AND wrapped_blob IS NULL

(at least one row per (user_id, device_id), enforced by ck_vault_envelopes_state)

**Demo reset, decided M63b:** `POST /auth/demo/reset` DELETES the demo tenant's
rows through `app/services/demo_seed.py::_RESET_EXTRA_MODELS`, in the same
transaction as the rest of the reset. The table sits outside
`delete_user_rows`'s `_USER_OWNED_MODELS` because a *restore* must never
replay it (docs/11: that would un-revoke a revoked device) — a reason that does
not apply to a reset, which wipes rather than replays — and the FK's
`ON DELETE CASCADE` never fires because the reset keeps the `users` row. The
demo tenant cannot enrol a server-side device at all (`403
demo_account_restricted`, docs/33: demo enrolment stays local-only), so no
legitimate demo row exists; a stray one would outlive the reset forever and
stay invisible to `seed_fingerprint`. Export/restore behaviour is unchanged:
the snapshot still carries the rows and `RESTORE_SKIP_TABLES` still refuses to
replay them.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Index, text
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class VaultEnvelope(TimestampMixin, table=True):
    __tablename__ = "vault_envelopes"
    __table_args__ = (
        # At most one ACTIVE envelope per device. A revoked row does not block
        # re-creation, so revocation history accumulates as separate rows.
        Index(
            "uq_vault_envelopes_active",
            "user_id",
            "device_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    # 32 lowercase hex chars, client-generated (localStorage); not a secret.
    device_id: str = Field(max_length=64, index=True)
    # Plaintext metadata (browser . OS) — accepted, docs/33 inventory K6.
    label: str | None = Field(default=None, max_length=64)
    # Opaque base64url JSON bundle; never decoded server-side.
    wrapped_blob: str | None = Field(default=None)
    revoked_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

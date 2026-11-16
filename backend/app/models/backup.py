"""backup_artifacts — scheduled client-side-encrypted backup blobs (M56, docs/11).

One row per backup period the caller has uploaded. `ciphertext` is the
`v1.<base64(iv)>.<base64(ciphertext)>` field envelope produced **in the
browser** under the user's vault DEK (`frontend/src/lib/fields.ts`, key
hierarchy in docs/33): the server stores it verbatim and can never open it —
it has neither the passphrase, the KEK nor the DEK. What a database reader
learns is the cadence, the size and ciphertext (docs/11 § Honest claims).

`uq_backup_artifacts_user_period` is the client-side scheduler's idempotency
key: two open tabs racing for the same period upsert into ONE row.

Deliberately NOT in `app/api/users.py::_USER_OWNED_MODELS` — a restore must
not delete the backups it is restoring from (and account deletion reaches
this table through `users.id ON DELETE CASCADE`, same as `vault_envelopes`).
It is excluded from `GET /export/full` for the mirror-image reason: a backup
must never contain the backups.

**Consequence, decided in M63:** `delete_user_rows` therefore never touches
this table, and `POST /auth/demo/reset` keeps the `users` row — so the FK
cascade never fires there either, and a reset WOULD strand a visitor's
ciphertext forever. Cross-reset custody is NOT intended: a reset returns the
shared demo tenant to the published seed, and a leftover artifact is visitor
state. `reset_demo_tenant` wipes this table explicitly through
`app/services/demo_seed.py::_RESET_EXTRA_MODELS` (same transaction as the
cascade), `seed_fingerprint` reads it for the same reason, and
`tests/test_demo_seed.py::test_reset_clears_stale_backup_artifacts` pins both
(docs/11 § "Demo reset").
"""

from sqlalchemy import Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class BackupArtifact(TimestampMixin, table=True):
    __tablename__ = "backup_artifacts"
    __table_args__ = (
        UniqueConstraint("user_id", "period", name="uq_backup_artifacts_user_period"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    # Idempotency key of the schedule: daily `YYYY-MM-DD`, weekly `YYYY-Www`
    # (ISO week), monthly `YYYY-MM`, in the runner's local time (docs/11).
    period: str = Field(max_length=16)
    # Opaque field envelope; never decoded server-side (size + shape only).
    ciphertext: str
    # len(ciphertext) — the artifact is pure ASCII, so chars == bytes.
    size_bytes: int

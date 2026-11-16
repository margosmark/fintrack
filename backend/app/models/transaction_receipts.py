"""transaction_receipts — ONE image per transaction (M108 P4, docs/02).

**Name check.** M106's `transaction_events` rows are also described as
"receipts" in this codebase — an ACTIVITY receipt: an append-only audit line
saying what changed. This table is the other thing entirely, the paper
receipt you photographed. They never share a column, a route or a helper; the
word is disambiguated as "activity receipt" vs "receipt image" everywhere the
two appear in prose.

Storage: base64 in a TEXT column, not `bytea`. The reason is the export
seam — `app/api/export.py::_jsonable` renders every registered row with
`model_dump(mode="json")` and `json.dumps` it, and neither step can carry
raw bytes; a `bytea` column would turn `GET /export/full` (and the encrypted
browser backup that shares its payload, docs/11) into a 500. Opaque ASCII in
a TEXT column is the shape `backup_artifacts.ciphertext` and
`vault_envelopes.wrapped_blob` already use for exactly this reason, and it
means the image round-trips through export, ZIP, backup and restore with no
special case anywhere.

`size_bytes` is the DECODED image length — the number the user's own file
system reported — so it stays a stable, comparable figure even though base64
inflates `len(data_base64)` by 4/3.

Privacy posture (docs/11, the Monarch-scan rule the card carries): the image
is stored locally and served back only to its owner over the authenticated
API. No cloud OCR, no email forwarding — an always-open inbound endpoint is
an abuse surface, and a third party seeing a receipt is exactly what the
scan said to prevent.
"""

from sqlalchemy import Column, ForeignKey, Integer, String, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class TransactionReceipt(TimestampMixin, table=True):
    __tablename__ = "transaction_receipts"
    __table_args__ = (
        # One image per transaction is the v1 contract (no bulk, docs/09) —
        # the UNIQUE constraint is what makes an upsert a replace.
        UniqueConstraint("transaction_id", name="uq_transaction_receipts_transaction"),
    )

    id: int | None = Field(default=None, primary_key=True)
    # Owner, so the row joins `_USER_OWNED_MODELS` and every export/delete path
    # reaches it through the same scoping rule as the rest of the ledger.
    user_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    transaction_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("transactions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    # The browser's canvas re-encode result — `image/webp` in practice, but the
    # server accepts the four formats it can serve back with a correct header.
    content_type: str = Field(sa_column=Column(String(length=64), nullable=False))
    # Decoded image bytes, not base64 length (see module docstring).
    size_bytes: int = Field(sa_column=Column(Integer, nullable=False))
    # base64 of the image itself; TEXT so export/backup/restore carry it.
    data_base64: str

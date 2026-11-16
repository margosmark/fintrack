"""transaction_tags — many-to-many join between transactions and tags (docs/02)."""

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class TransactionTag(TimestampMixin, table=True):
    __tablename__ = "transaction_tags"
    __table_args__ = (
        UniqueConstraint("transaction_id", "tag_id", name="uq_transaction_tags_txn_tag"),
    )

    id: int | None = Field(default=None, primary_key=True)
    transaction_id: int = Field(foreign_key="transactions.id")
    tag_id: int = Field(foreign_key="tags.id")

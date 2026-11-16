"""import_batches — staged import lifecycle (docs/02, docs/03)."""

import enum

from sqlalchemy import Column, Enum
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class ImportBatchStatus(enum.StrEnum):
    pending_review = "pending_review"
    committed = "committed"
    discarded = "discarded"
    reverted = "reverted"


class ImportBatch(TimestampMixin, table=True):
    __tablename__ = "import_batches"

    id: int | None = Field(default=None, primary_key=True)
    # indexed: every domain read scopes by user_id (docs/02 §tenancy);
    # see alembic 0050_tenancy_idx
    user_id: int = Field(foreign_key="users.id", index=True)
    account_id: int = Field(foreign_key="accounts.id", index=True)
    institution: str
    file_name: str
    status: ImportBatchStatus = Field(
        default=ImportBatchStatus.pending_review,
        sa_column=Column(Enum(ImportBatchStatus, native_enum=False, length=32), nullable=False),
    )
    imported: int = 0
    duplicates_skipped: int = 0
    unresolved: int = 0

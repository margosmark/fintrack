"""llm_suggestions — categorization suggestion queue (docs/02, docs/04)."""

import enum

from sqlalchemy import Column, Enum, Index, text
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class SuggestionStatus(enum.StrEnum):
    suggested = "suggested"
    accepted = "accepted"
    rejected = "rejected"


class LlmSuggestion(TimestampMixin, table=True):
    __tablename__ = "llm_suggestions"
    __table_args__ = (
        Index(
            "ix_llm_suggestions_transaction_pending",
            "transaction_id",
            postgresql_where=text("status = 'suggested'"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    transaction_id: int = Field(foreign_key="transactions.id")
    proposed_category_id: int = Field(foreign_key="categories.id")
    confidence: float
    status: SuggestionStatus = Field(
        default=SuggestionStatus.suggested,
        sa_column=Column(Enum(SuggestionStatus, native_enum=False, length=32), nullable=False),
    )
    model_used: str

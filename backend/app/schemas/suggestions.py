"""suggestions — queue + pipeline trigger shapes (docs/04, docs/08)."""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.llm_suggestions import SuggestionStatus


class SuggestionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_ids: list[int] | None = None
    from_: dt.date | None = Field(default=None, alias="from")
    to: dt.date | None = None

    @model_validator(mode="after")
    def _paired_dates(self) -> "SuggestionRunRequest":
        if (self.from_ is None) != (self.to is None):
            raise ValueError("from and to must be provided together")
        return self


class RunAccepted(BaseModel):
    queued: int


class SuggestionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    transaction_id: int
    payee: str
    proposed_category_id: int
    category_name: str
    confidence: float
    status: SuggestionStatus
    model_used: str


class SuggestionPage(BaseModel):
    items: list[SuggestionPublic]
    total: int
    limit: int
    offset: int

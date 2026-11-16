"""goals — CRUD shapes."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class GoalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    target_amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    target_date: str | None = None
    account_id: int | None = None
    current_amount_override: Decimal | None = None


class GoalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    target_amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    target_date: str | None = None
    account_id: int | None = None
    current_amount_override: Decimal | None = None


class GoalPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    target_amount: Decimal
    target_date: str | None
    account_id: int | None
    current_amount_override: Decimal | None


class GoalWithProgress(GoalPublic):
    current_amount: Decimal
    progress_pct: float
    monthly_needed: Decimal | None
    status: str  # on_track | behind | ahead | completed

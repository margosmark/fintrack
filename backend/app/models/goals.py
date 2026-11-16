"""goals — financial savings targets with progress tracking."""

from decimal import Decimal

from sqlmodel import Field

from app.models.mixins import TimestampMixin


class Goal(TimestampMixin, table=True):
    __tablename__ = "goals"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=200)
    target_amount: Decimal = Field(max_digits=12, decimal_places=2)
    target_date: str | None = Field(default=None, max_length=10)  # YYYY-MM-DD
    account_id: int | None = Field(default=None, foreign_key="accounts.id")
    current_amount_override: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)

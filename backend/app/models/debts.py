"""debts — mortgage, loan, credit card tracking with payoff projections."""

from decimal import Decimal

from sqlmodel import Field

from app.models.mixins import TimestampMixin


class Debt(TimestampMixin, table=True):
    __tablename__ = "debts"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    account_id: int = Field(foreign_key="accounts.id")
    name: str = Field(max_length=200)
    debt_type: str = Field(max_length=50)  # mortgage, credit_card, loan, student_loan, other
    original_amount: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    interest_rate: Decimal | None = Field(default=None, max_digits=5, decimal_places=2)
    minimum_payment: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    payment_frequency: str | None = Field(default=None, max_length=20)
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    notes: str | None = Field(default=None, max_length=500)

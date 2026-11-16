"""debts — shapes."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

# docs/29 debt schema: payment_frequency is monthly | biweekly.
_PAYMENT_FREQUENCY = "^(monthly|biweekly)$"
_DEBT_TYPE = "^(mortgage|credit_card|loan|student_loan|other)$"


class DebtCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    account_id: int
    debt_type: str = Field(pattern=_DEBT_TYPE)
    # Principal and payment are money owed/paid — never negative. A negative
    # minimum_payment previously projected "paid off in 0 months" instead of
    # being rejected (docs/29 F7: validation beats silent tolerance).
    original_amount: Decimal | None = Field(default=None, ge=0)
    interest_rate: Decimal | None = Field(default=None, ge=0, le=100)
    minimum_payment: Decimal | None = Field(default=None, ge=0)
    payment_frequency: str | None = Field(default=None, pattern=_PAYMENT_FREQUENCY)
    start_date: str | None = None
    end_date: str | None = None
    notes: str | None = None


class DebtUpdate(BaseModel):
    """Mirrors `DebtCreate` constraints (docs/29 F7) — every field nullable so
    PATCH stays partial."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    debt_type: str | None = Field(default=None, pattern=_DEBT_TYPE)
    original_amount: Decimal | None = Field(default=None, ge=0)
    interest_rate: Decimal | None = Field(default=None, ge=0, le=100)
    minimum_payment: Decimal | None = Field(default=None, ge=0)
    payment_frequency: str | None = Field(default=None, pattern=_PAYMENT_FREQUENCY)
    start_date: str | None = None
    end_date: str | None = None
    notes: str | None = None


class DebtPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    account_id: int
    name: str
    debt_type: str
    original_amount: Decimal | None
    interest_rate: Decimal | None
    minimum_payment: Decimal | None
    payment_frequency: str | None
    start_date: str | None
    end_date: str | None
    notes: str | None

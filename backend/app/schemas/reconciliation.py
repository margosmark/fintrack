"""reconciliation — shapes."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class StatementEntry(BaseModel):
    """One typed row of an uploaded bank statement (docs/29 F7)."""

    model_config = ConfigDict(extra="forbid")

    # Kept as a pattern-validated string: the ReconciliationItem row stores
    # statement_date as VARCHAR(10) "YYYY-MM-DD".
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    amount: Decimal
    description: str | None = Field(default=None, max_length=500)


class ReconciliationUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[StatementEntry] = Field(max_length=2000)


class ReconciliationItemPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    transaction_id: int | None
    statement_date: str
    statement_amount: Decimal
    statement_description: str
    status: str

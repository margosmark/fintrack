"""reconciliation — compare imported transactions against bank statements."""

from decimal import Decimal

from sqlmodel import Field

from app.models.mixins import TimestampMixin


class ReconciliationItem(TimestampMixin, table=True):
    __tablename__ = "reconciliation_items"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    account_id: int = Field(foreign_key="accounts.id", index=True)
    transaction_id: int | None = Field(default=None, foreign_key="transactions.id")
    statement_date: str = Field(max_length=10)  # YYYY-MM-DD
    statement_amount: Decimal = Field(max_digits=12, decimal_places=2)
    statement_description: str = Field(default="", max_length=500)
    status: str = Field(default="unmatched", max_length=20)  # matched | unmatched

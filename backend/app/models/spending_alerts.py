"""spending_alerts — budget threshold notifications."""

from decimal import Decimal

from sqlmodel import Field

from app.models.mixins import TimestampMixin


class SpendingAlert(TimestampMixin, table=True):
    __tablename__ = "spending_alerts"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    budget_id: int = Field(foreign_key="budgets.id")
    alert_type: str = Field(max_length=20)  # warning | critical
    threshold_pct: Decimal = Field(max_digits=5, decimal_places=2)
    actual_pct: Decimal = Field(max_digits=5, decimal_places=2)
    spent: Decimal = Field(max_digits=12, decimal_places=2)
    budget_amount: Decimal = Field(max_digits=12, decimal_places=2)
    acknowledged: bool = Field(default=False)

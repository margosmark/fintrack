"""tax_projections — estimated tax liability by year."""

from decimal import Decimal

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class TaxProjection(TimestampMixin, table=True):
    __tablename__ = "tax_projections"
    # One projection per user per year (migration 0021, `uq_tax_proj_user_year`).
    __table_args__ = (UniqueConstraint("user_id", "year", name="uq_tax_proj_user_year"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    year: int = Field()
    province: str = Field(default="ON", max_length=2)
    estimated_income: Decimal = Field(default=0, max_digits=12, decimal_places=2)
    deductions: Decimal = Field(default=0, max_digits=12, decimal_places=2)
    capital_gains_override: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    dividend_income_override: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)

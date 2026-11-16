"""fx_rates — shared reference cache, NO user_id (docs/02 tenancy exception)."""

import enum
from datetime import date
from decimal import Decimal

from sqlalchemy import Column, Enum, Numeric, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class FxSource(enum.StrEnum):
    boc_valet = "boc_valet"
    manual = "manual"


class FxRate(TimestampMixin, table=True):
    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint("base", "quote", "date", name="uq_fx_rates_base_quote_date"),
    )

    id: int | None = Field(default=None, primary_key=True)
    base: str
    quote: str
    rate: Decimal = Field(sa_type=Numeric(20, 8))
    date: date
    source: FxSource = Field(
        sa_column=Column(Enum(FxSource, native_enum=False, length=32), nullable=False)
    )

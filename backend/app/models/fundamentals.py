"""fundamentals — cached per-security research data (EPS, P/E, sector, …).

One row per security; `source=yfinance` rows refresh through a 7-day
staleness window while `source=manual` rows win permanently (docs/07
manual-wins convention).
"""

import enum
from datetime import date
from decimal import Decimal

from sqlalchemy import Column, Date, Enum, Numeric, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class FundamentalsSource(enum.StrEnum):
    yfinance = "yfinance"
    manual = "manual"


class Fundamentals(TimestampMixin, table=True):
    __tablename__ = "fundamentals"
    __table_args__ = (UniqueConstraint("security_id", name="uq_fundamentals_security"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    security_id: int = Field(foreign_key="securities.id")
    trailing_eps: Decimal | None = Field(default=None, sa_type=Numeric(20, 8))
    pe_ratio: Decimal | None = Field(default=None, sa_type=Numeric(20, 8))
    market_cap: Decimal | None = Field(default=None, sa_type=Numeric(30, 2))
    dividend_yield_pct: Decimal | None = Field(default=None, sa_type=Numeric(10, 4))
    beta: Decimal | None = Field(default=None, sa_type=Numeric(10, 4))
    sector: str | None = Field(default=None, max_length=80)
    industry: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=80)
    fifty_two_week_high: Decimal | None = Field(default=None, sa_type=Numeric(20, 8))
    fifty_two_week_low: Decimal | None = Field(default=None, sa_type=Numeric(20, 8))
    source: FundamentalsSource = Field(
        sa_column=Column(Enum(FundamentalsSource, native_enum=False, length=16), nullable=False)
    )
    fetched_at: date = Field(sa_column=Column(Date, nullable=False))

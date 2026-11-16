"""prices — manual + yfinance price history per security/date (docs/02, docs/07)."""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Column, DateTime, Enum, Numeric, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class PriceSource(enum.StrEnum):
    manual = "manual"
    yfinance = "yfinance"


class Price(TimestampMixin, table=True):
    __tablename__ = "prices"
    __table_args__ = (UniqueConstraint("security_id", "date", name="uq_prices_security_date"),)

    id: int | None = Field(default=None, primary_key=True)
    # indexed: every domain read scopes by user_id (docs/02 §tenancy);
    # see alembic 0050_tenancy_idx
    user_id: int = Field(foreign_key="users.id", index=True)
    security_id: int = Field(foreign_key="securities.id")
    date: date
    price: Decimal = Field(sa_type=Numeric(20, 8))
    # Human data overwrites machine data; the yfinance backfill never touches manual rows.
    source: PriceSource = Field(
        sa_column=Column(Enum(PriceSource, native_enum=False, length=32), nullable=False)
    )
    # REAL provider-fetch evidence (Option A, t_bd5ec95f): the UTC instant a
    # provider response actually landed for this row. Stamped ONLY by
    # `market_data._cache_closes` on a `source=yfinance` row (docs/07 §Fetch
    # provenance), cleared by any human edit (docs/08 manual-wins), and NEVER
    # backfilled — alembic 0057 adds the column empty, because copying
    # created_at/updated_at into it would fabricate fetch evidence. NULL =
    # no evidence: the API emits null and the client omits the fetch-age
    # segment entirely. Sub-day age may come only from this stamp.
    fetched_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

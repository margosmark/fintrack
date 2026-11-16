"""portfolio — position and exposure response shapes (docs/05, docs/08)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class PositionRead(BaseModel):
    account_id: int
    account_name: str
    security_id: int
    symbol: str
    listing_currency: str
    quantity: Decimal
    # Newest usable price; None when no price exists and the fetch degraded.
    price_date: date | None = None
    price: Decimal | None = None
    price_source: str | None = None
    price_age_days: int | None = None
    # REAL provider-fetch evidence (Option A, t_bd5ec95f): the UTC instant a
    # provider response actually landed for the cached row — stamped only by
    # `market_data._cache_closes` at the fetch seam, cleared by any human
    # edit, never backfilled (alembic 0057). NULL = NO evidence: the client
    # omits the fetch-age segment entirely (never "0s", never "just now").
    # Market-date freshness (`price_date`/`price_age_days`/`price_stale`) is
    # computed ONLY from `price_date` and is unaffected by this field.
    price_fetched_at: datetime | None = None
    price_stale: bool | None = None
    market_value_native: Decimal | None = None
    market_value_cad: Decimal | None = None
    # Cost basis in CAD (w12 pin 1): persisted acb_pools value for non-
    # registered accounts; pure average-cost replay for registered ones.
    # None when the non-registered pool cache is missing.
    book_cost_cad: Decimal | None = None
    # Up to the LAST 30 closes <= today ascending (Decimal-as-string); [] when
    # the security has no prices.
    spark: list[str] = Field(default_factory=list)


class ExposureRow(BaseModel):
    listing_currency: str
    total_market_value_native: Decimal
    holdings_count: int

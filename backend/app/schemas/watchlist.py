"""watchlist — request/response shapes (W11)."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.prices import PriceSource


class WatchlistAdd(BaseModel):
    security_id: int


class WatchlistItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    security_id: int
    symbol: str
    name: str
    listing_currency: str
    latest_price: Decimal | None = None
    price_date: date | None = None
    price_source: PriceSource | None = None
    price_age_days: int | None = None
    # REAL provider-fetch evidence (Option A, t_bd5ec95f): the UTC instant a
    # provider response actually landed for the cached row — stamped only by
    # `market_data._cache_closes` at the fetch seam, cleared by any human
    # edit, never backfilled (alembic 0057). NULL = NO evidence: the client
    # omits the fetch-age segment entirely (never "0s", never "just now",
    # never "fetch time unavailable" — t_a16b7663). Market-date freshness is
    # computed ONLY from `price_date` and is unaffected by this field.
    price_fetched_at: datetime | None = None
    # null when no cached price exists; otherwise age > 7 days (docs/07).
    price_stale: bool | None = None
    # M141: the row has been declared quote-less by Yahoo (docs/07 §Dead
    # symbols) — the watchlist says "no public quote" instead of the
    # forever-"yet" "no price".
    dead_symbol: bool = False
    dead_symbol_reason: str | None = None


class WatchlistPage(BaseModel):
    items: list[WatchlistItemRead]
    total: int

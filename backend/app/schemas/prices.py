"""prices — request/response shapes (docs/02, docs/08).

POST /prices is an upsert keyed (security_id, date): the created row and any
overwritten row both carry source='manual' — human data overwrites machine data.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.prices import PriceSource


class PriceCreate(BaseModel):
    security_id: int
    date: date
    price: Decimal


class PriceUpdate(BaseModel):
    price: Decimal


class PriceRead(BaseModel):
    # `populate_by_name` so both the field name (constructed payloads) and
    # the validation alias (ORM attribute lookup via from_attributes) work.
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    security_id: int
    date: date
    price: Decimal
    source: PriceSource
    # REAL provider-fetch evidence (Option A, t_bd5ec95f): the instant a
    # provider response landed for this row — stamped only by
    # `market_data._cache_closes`, and CLEARED by any human write to this
    # endpoint (manual data has no fetch time). NULL = no evidence. Named
    # `price_fetched_at` on the wire (cross-surface consistency with
    # positions/watchlist), read from the row's `fetched_at` column.
    price_fetched_at: datetime | None = Field(default=None, validation_alias="fetched_at")


class PricePage(BaseModel):
    items: list[PriceRead]
    total: int
    limit: int
    offset: int


class PriceHistoryPoint(BaseModel):
    """One chart point. `price` is a Decimal-as-string — the same wire shape
    the security overview's `history` list already uses (docs/08)."""

    date: date
    price: str


class PriceHistoryPage(BaseModel):
    """M126 — `GET /prices/history` (docs/08 §Price history).

    `from` is the window actually SERVED (the caller's floor, else the oldest
    cached close, else `to`), so a client can assert coverage without knowing
    what the cache holds. `granularity` may differ from `requested_granularity`
    when the server auto-picked it or coarsened an explicit ask to stay inside
    the point roofline (docs/12 §Range → granularity).
    """

    model_config = ConfigDict(populate_by_name=True)

    security_id: int
    from_: date = Field(serialization_alias="from")
    to: date
    granularity: str
    requested_granularity: str
    items: list[PriceHistoryPoint]
    count: int

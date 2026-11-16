"""fx — rate read/manual-entry shapes (docs/07, docs/08)."""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.models.fx_rates import FxSource

# An ISO-4217-shaped code, matching `AccountCreate.currency` (app/schemas/
# accounts.py) so the two entry points cannot drift apart. `fx_rates` is
# shared across tenants with no `user_id`, so this route's inputs are bounded
# as tightly as the ones that read it: unbounded free text here means unbounded
# distinct keys in a table every tenant reads.
_CurrencyCode = Annotated[str, StringConstraints(to_upper=True, min_length=3, max_length=3)]


class FxRateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    base: str
    quote: str
    date: date
    rate: Decimal
    source: FxSource


class FxRateCreate(BaseModel):
    base: _CurrencyCode
    quote: _CurrencyCode
    date: date
    rate: Decimal


class FxRangeRead(BaseModel):
    base: str
    quote: str
    start_date: date
    end_date: date
    items: list[FxRateRead]


class FxBoardPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: date
    rate: Decimal


class FxBoardItem(BaseModel):
    base: str
    latest_rate: Decimal | None
    as_of: date | None
    series: list[FxBoardPoint]


class FxBoardRead(BaseModel):
    currencies: list[FxBoardItem]


class FxPairStatus(BaseModel):
    """Freshness of one currency pair in the rate book (M150, docs/07).

    ``as_of`` is the newest cached row on or before today — NOT a carried
    forward value for some target date: a 10-day-old row is reported as 10 days
    old here, which is the whole point of the endpoint.
    """

    base: str
    quote: str
    as_of: date | None
    age_days: int | None
    #: Past `services.fx.CARRY_FORWARD_MAX_DAYS`, or no row at all.
    stale: bool
    source: FxSource | None


class FxStatusRead(BaseModel):
    """Is the rate book current for the currencies this tenant actually holds?

    Top level reports the WORST-covered pair in use (docs/07 §Rate-book
    status); ``pairs: []`` means no foreign currency is in use at all, so there
    is no FX dependency to report on.
    """

    reporting_currency: str
    as_of: date | None
    age_days: int | None
    stale: bool
    pairs: list[FxPairStatus]

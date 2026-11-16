"""Forecasting — request/response shapes (M109, docs/45).

The event set is a CLOSED, typed union: there is no formula editor and no
scripting surface, so an `event.type` outside `FORECAST_EVENT_TYPES` is a 422
`unknown_event_type` (raised by `validate_events`, deliberately OUTSIDE
pydantic so the envelope carries that code instead of the generic
`validation_error` a discriminated-union miss would produce).

Amounts cross the wire as cents-quantized Decimal strings (AGENTS §2).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from app.core.errors import api_error

ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"

FORECAST_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "house_purchase",
        "career_break",
        "salary_change",
        "one_off_cash_event",
        "recurring_cost_delta",
    }
)

# Bounds shared with the run query params (docs/45 §3.1).
HORIZON_MONTHS_DEFAULT = 24
HORIZON_MONTHS_MAX = 120
FLOW_WINDOW_DEFAULT = 12
FLOW_WINDOW_MAX = 36
INFLATION_DEFAULT = "2.00"
RETURN_DEFAULT = "0.00"
MAX_SCENARIOS_PER_RUN = 6
MAX_SCENARIOS_PER_USER = 25
MAX_EVENTS_PER_SCENARIO = 12

# A scenario is worth guarding against absurd magnitudes the same way docs/12
# §7 guards its calculator: legal inputs stay legal, nonsense is refused.
# Decimal throughout — money and rates never touch a float (AGENTS §2).
_AMOUNT_MAX = Decimal("100000000")  # 100M CAD per single event amount


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=8, max_length=64, pattern=ID_PATTERN)


class HousePurchaseEvent(_EventBase):
    """Buy a house on `date`: cash leaves, equity and a mortgage arrive."""

    type: Literal["house_purchase"]
    date: dt.date
    home_price_cad: Decimal = Field(gt=0, le=_AMOUNT_MAX)
    down_payment_cad: Decimal = Field(ge=0, le=_AMOUNT_MAX)
    mortgage_rate_pct: Decimal = Field(ge=0, le=25)
    amortization_years: int = Field(ge=1, le=50)

    @model_validator(mode="after")
    def _down_payment_inside_price(self) -> HousePurchaseEvent:
        if self.down_payment_cad >= self.home_price_cad:
            raise ValueError("down_payment_cad must be less than home_price_cad")
        return self


class CareerBreakEvent(_EventBase):
    """No income between the two dates; expenses continue."""

    type: Literal["career_break"]
    start_date: dt.date
    end_date: dt.date

    @model_validator(mode="after")
    def _ordered(self) -> CareerBreakEvent:
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class SalaryChangeEvent(_EventBase):
    type: Literal["salary_change"]
    date: dt.date
    monthly_delta_cad: Decimal = Field(ge=-_AMOUNT_MAX, le=_AMOUNT_MAX)


class OneOffCashEvent(_EventBase):
    type: Literal["one_off_cash_event"]
    date: dt.date
    amount_cad: Decimal = Field(ge=-_AMOUNT_MAX, le=_AMOUNT_MAX)


class RecurringCostDeltaEvent(_EventBase):
    """A new repeating cost (or income) from `start_date`, e.g. a child."""

    type: Literal["recurring_cost_delta"]
    start_date: dt.date
    monthly_amount_cad: Decimal = Field(ge=-_AMOUNT_MAX, le=_AMOUNT_MAX)
    end_date: dt.date | None = None

    @model_validator(mode="after")
    def _ordered(self) -> RecurringCostDeltaEvent:
        if self.end_date is not None and self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


ForecastEvent = Annotated[
    HousePurchaseEvent
    | CareerBreakEvent
    | SalaryChangeEvent
    | OneOffCashEvent
    | RecurringCostDeltaEvent,
    Field(discriminator="type"),
]


def validate_events(raw_events: list[dict[str, Any]]) -> list[ForecastEvent]:
    """Raise the SPEC-pinned envelope codes instead of pydantic's generic one.

    Order matters: the unknown-type check runs first so a typo'd `type`
    answers `unknown_event_type`, and only then does the union's own bounds
    checking run and answer `validation_error`.
    """
    if len(raw_events) > MAX_EVENTS_PER_SCENARIO:
        raise api_error(
            422,
            f"A scenario holds at most {MAX_EVENTS_PER_SCENARIO} events",
            "validation_error",
        )
    for raw in raw_events:
        kind = raw.get("type")
        if kind not in FORECAST_EVENT_TYPES:
            raise api_error(
                422,
                f"Unknown forecast event type {kind!r}; allowed: "
                + ", ".join(sorted(FORECAST_EVENT_TYPES)),
                "unknown_event_type",
            )
    adapter: TypeAdapter[ForecastEvent] = TypeAdapter(ForecastEvent)
    try:
        parsed = [adapter.validate_python(e) for e in raw_events]
    except ValidationError as exc:  # bounds / missing field / extra field
        raise api_error(422, _event_detail(exc), "validation_error") from None
    return parsed


def _event_detail(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first["loc"])
    return f"{loc}: {first['msg']}"


class ScenarioIn(BaseModel):
    """Write shape for create/patch: `events` stays a raw list so
    `validate_events` can pick the envelope code (docs/45 §4)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=8, max_length=64, pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=80)
    events: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _name_not_blank(self) -> ScenarioIn:
        stripped = self.name.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        self.name = stripped
        return self


class CopyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, min_length=8, max_length=64, pattern=ID_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=80)


class ScenarioOut(BaseModel):
    """Stored shape — the wire form of one entry of `users.forecast_scenarios`."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    events: list[ForecastEvent]


class ScenarioList(BaseModel):
    items: list[ScenarioOut]
    count: int


class RunParams(BaseModel):
    """Validated `GET /forecast/run` query parameters (docs/45 §3.1)."""

    scenario_ids: list[str] = Field(default_factory=list, max_length=MAX_SCENARIOS_PER_RUN)
    horizon_months: int = Field(default=HORIZON_MONTHS_DEFAULT, ge=1, le=HORIZON_MONTHS_MAX)
    granularity: Literal["day", "week", "month"] = "day"
    inflation_pct: Decimal = Field(default=Decimal("2.00"), ge=0, le=20)
    annual_return_pct: Decimal = Field(default=Decimal("0.00"), ge=-50, le=50)
    flow_window_months: int = Field(default=FLOW_WINDOW_DEFAULT, ge=1, le=FLOW_WINDOW_MAX)


class RunInputs(BaseModel):
    cash_cad: str
    positions_cad: str
    net_worth_cad: str
    history_from: str
    history_to: str
    income_monthly_cad: str
    expense_monthly_cad: str
    recurring_income_monthly_cad: str
    recurring_expense_monthly_cad: str
    residual_income_monthly_cad: str
    residual_expense_monthly_cad: str
    recurring_rows: int


class RunSeries(BaseModel):
    id: str
    name: str
    values: list[str]
    real_values: list[str]
    final_cad: str
    delta_cad: str


class RunResponse(BaseModel):
    as_of: str
    horizon_months: int
    granularity: str
    inflation_pct: str
    annual_return_pct: str
    flow_window_months: int
    reporting_currency: str
    buckets: list[str]
    inputs: RunInputs
    baseline: RunSeries
    scenarios: list[RunSeries]
    disclaimer: str


class DefaultsResponse(BaseModel):
    as_of: str
    reporting_currency: str
    net_worth_cad: str
    cash_cad: str
    positions_cad: str
    income_monthly_cad: str
    expense_monthly_cad: str
    flow_window_months: int
    recurring_rows: int
    scenario_count: int

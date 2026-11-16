"""projections — request/response shapes (M15, docs/08, docs/12).

Amounts cross the wire as cents-quantized Decimal strings; bounds are pinned
by Field constraints and surface as 422 `validation_error` envelopes via the
standard handler.
"""

from decimal import Decimal

from pydantic import BaseModel, Field


class ProjectionScenarioInput(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    annual_return_pct: Decimal = Field(ge=-50, le=50)


class ProjectionRequest(BaseModel):
    start_value_cad: Decimal = Field(ge=0)
    annual_contribution_cad: Decimal = Field(ge=0)
    years: int = Field(ge=1, le=50)
    inflation_pct: Decimal = Field(ge=0, le=20)
    scenarios: list[ProjectionScenarioInput] = Field(min_length=1, max_length=8)


class ProjectionYearRow(BaseModel):
    year: int
    contributed_cad: str
    value_cad: str
    value_real_cad: str


class ProjectionScenarioResult(BaseModel):
    label: str
    series: list[ProjectionYearRow]
    final_value_cad: str
    total_contributed_cad: str
    total_gain_cad: str


class ProjectionResponse(BaseModel):
    start_value_cad: str
    years: int
    inflation_pct: str
    scenarios: list[ProjectionScenarioResult]


class ProjectionDefaults(BaseModel):
    """Current portfolio market value CAD for UI prefill; null when there is
    nothing priced to prefill from."""

    start_value_cad: str | None

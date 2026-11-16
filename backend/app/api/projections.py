"""projections — deterministic future-value scenario runs (M15, docs/08, docs/12)."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.users import User
from app.schemas.projections import (
    ProjectionDefaults,
    ProjectionRequest,
    ProjectionResponse,
    ProjectionScenarioResult,
    ProjectionYearRow,
)
from app.services import projections as projections_service
from app.services import valuation
from app.tax.acb import cents

router = APIRouter(prefix="/projections", tags=["projections"])


@router.post("/run", response_model=ProjectionResponse)
def run_projections(
    body: ProjectionRequest,
    current_user: Annotated[User, Depends(get_current_user)],  # tenancy discipline
) -> ProjectionResponse:
    """Run every requested scenario through the pure monthly-compounding
    projector; body bounds surface as 422 `validation_error` envelopes."""
    results: list[ProjectionScenarioResult] = []
    for scenario in body.scenarios:
        rows = projections_service.project_scenario(
            body.start_value_cad,
            body.annual_contribution_cad,
            scenario.annual_return_pct,
            body.years,
            body.inflation_pct,
        )
        final_value = Decimal(rows[-1]["value_cad"])
        contributed = Decimal(rows[-1]["contributed_cad"])
        total_gain = cents(final_value - body.start_value_cad - contributed)
        results.append(
            ProjectionScenarioResult(
                label=scenario.label,
                series=[ProjectionYearRow(**row) for row in rows],
                final_value_cad=str(final_value),
                total_contributed_cad=str(contributed),
                total_gain_cad=str(total_gain),
            )
        )
    return ProjectionResponse(
        start_value_cad=str(cents(body.start_value_cad)),
        years=body.years,
        inflation_pct=str(cents(body.inflation_pct)),
        scenarios=results,
    )


@router.get("/defaults", response_model=ProjectionDefaults)
def projection_defaults(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProjectionDefaults:
    """Current portfolio market value CAD (sum of non-null
    `valuation.compute_positions` rows) for UI prefill; null when there are no
    positions or none of them carry a market value."""
    positions = valuation.compute_positions(db, current_user.id)
    values = [row["market_value_cad"] for row in positions if row["market_value_cad"] is not None]
    if not values:
        return ProjectionDefaults(start_value_cad=None)
    return ProjectionDefaults(start_value_cad=str(cents(sum(values, Decimal("0")))))

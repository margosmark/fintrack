"""forecast — free local what-if scenarios (M109, docs/45).

Owner surface: every route authenticates with `get_current_user`, i.e. the
caller's OWN tenant, which also carries the demo write guard. Nothing here is
reachable from `/partner/*` — forecast is the owner's decision surface, the
first owner-model-only feature in the app (docs/45 §Tenancy).
"""

from __future__ import annotations

import secrets
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import get_settings
from app.core.errors import api_error
from app.models.users import User
from app.schemas.forecast import (
    FLOW_WINDOW_DEFAULT,
    FLOW_WINDOW_MAX,
    HORIZON_MONTHS_DEFAULT,
    HORIZON_MONTHS_MAX,
    INFLATION_DEFAULT,
    MAX_SCENARIOS_PER_RUN,
    MAX_SCENARIOS_PER_USER,
    RETURN_DEFAULT,
    CopyIn,
    DefaultsResponse,
    RunParams,
    RunResponse,
    ScenarioIn,
    ScenarioList,
    ScenarioOut,
    validate_events,
)
from app.services import forecast as forecast_service
from app.services.txn_filters import today_et

router = APIRouter(prefix="/forecast", tags=["forecast"])


def _stored(user: User) -> list[dict]:
    return list(user.forecast_scenarios or [])


def _find(user: User, scenario_id: str) -> dict:
    for entry in _stored(user):
        if entry.get("id") == scenario_id:
            return entry
    # Unknown AND another tenant's id answer the SAME 404 — no existence oracle.
    raise api_error(404, "Forecast scenario not found", "scenario_not_found")


def _persist(user: User, db: Session, entries: list[dict]) -> None:
    user.forecast_scenarios = entries
    db.add(user)
    db.commit()
    db.refresh(user)


def _guard_capacity(user: User) -> None:
    if len(_stored(user)) >= MAX_SCENARIOS_PER_USER:
        raise api_error(
            409,
            f"A tenant holds at most {MAX_SCENARIOS_PER_USER} forecast scenarios",
            "scenario_limit_reached",
        )


def _guard_unique_id(user: User, scenario_id: str) -> None:
    if any(entry.get("id") == scenario_id for entry in _stored(user)):
        raise api_error(409, f"Scenario id {scenario_id!r} already exists", "scenario_exists")


def _parse_body(body: ScenarioIn) -> ScenarioOut:
    """Validate events HERE so the envelope carries the spec's own codes."""
    events = validate_events(body.events)
    return ScenarioOut(id=body.id, name=body.name, events=events)


# ---------------------------------------------------------------- scenarios


@router.get("/scenarios", response_model=ScenarioList)
def list_scenarios(current_user: Annotated[User, Depends(get_current_user)]) -> ScenarioList:
    entries = _stored(current_user)
    items = [ScenarioOut.model_validate(e) for e in entries]
    return ScenarioList(items=items, count=len(items))


@router.post("/scenarios", response_model=ScenarioOut, status_code=201)
def create_scenario(
    body: ScenarioIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ScenarioOut:
    _guard_capacity(current_user)
    _guard_unique_id(current_user, body.id)
    scenario = _parse_body(body)
    _persist(current_user, db, [*_stored(current_user), scenario.model_dump(mode="json")])
    return scenario


@router.get("/scenarios/{scenario_id}", response_model=ScenarioOut)
def get_scenario(
    scenario_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> ScenarioOut:
    return ScenarioOut.model_validate(_find(current_user, scenario_id))


@router.patch("/scenarios/{scenario_id}", response_model=ScenarioOut)
def replace_scenario(
    scenario_id: str,
    body: ScenarioIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ScenarioOut:
    """FULL replace — the id in the path wins, so a rename cannot fork a row."""
    _find(current_user, scenario_id)
    if body.id != scenario_id:
        _guard_unique_id(current_user, body.id)
    scenario = _parse_body(body)
    entries = [
        scenario.model_dump(mode="json") if e.get("id") == scenario_id else e
        for e in _stored(current_user)
    ]
    _persist(current_user, db, entries)
    return scenario


@router.post("/scenarios/{scenario_id}/copy", response_model=ScenarioOut, status_code=201)
def copy_scenario(
    scenario_id: str,
    body: CopyIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ScenarioOut:
    source = _find(current_user, scenario_id)
    _guard_capacity(current_user)
    new_id = body.id or secrets.token_urlsafe(12)  # url-safe → matches _ID_PATTERN
    _guard_unique_id(current_user, new_id)
    name = (body.name or f"Copy of {source['name']}").strip()
    if not name:
        raise api_error(422, "name must not be blank", "validation_error")
    scenario = ScenarioOut(id=new_id, name=name[:80], events=source["events"])
    _persist(current_user, db, [*_stored(current_user), scenario.model_dump(mode="json")])
    return scenario


@router.delete("/scenarios/{scenario_id}", status_code=204)
def delete_scenario(
    scenario_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    _find(current_user, scenario_id)
    _persist(current_user, db, [e for e in _stored(current_user) if e.get("id") != scenario_id])
    return Response(status_code=204)


# ------------------------------------------------------------------ the run


@router.get("/defaults", response_model=DefaultsResponse)
def forecast_defaults(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> DefaultsResponse:
    as_of = today_et()
    inputs = forecast_service.collect_inputs(db, current_user.id, as_of, FLOW_WINDOW_DEFAULT)
    return DefaultsResponse(
        as_of=str(as_of),
        reporting_currency=get_settings().reporting_currency,
        net_worth_cad=forecast_service.money_str(inputs.cash + inputs.positions),
        cash_cad=forecast_service.money_str(inputs.cash),
        positions_cad=forecast_service.money_str(inputs.positions),
        income_monthly_cad=forecast_service.money_str(inputs.income_monthly),
        expense_monthly_cad=forecast_service.money_str(inputs.expense_monthly),
        flow_window_months=FLOW_WINDOW_DEFAULT,
        recurring_rows=inputs.recurring_rows,
        scenario_count=len(_stored(current_user)),
    )


@router.get("/run", response_model=RunResponse)
def run_forecast(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    scenario_ids: Annotated[str | None, Query(description="comma-separated, max 6")] = None,
    horizon_months: Annotated[int, Query(ge=1, le=HORIZON_MONTHS_MAX)] = HORIZON_MONTHS_DEFAULT,
    granularity: Annotated[str, Query(pattern="^(day|week|month)$")] = "day",
    inflation_pct: Annotated[Decimal, Query(ge=0, le=20)] = Decimal(INFLATION_DEFAULT),
    annual_return_pct: Annotated[Decimal, Query(ge=-50, le=50)] = Decimal(RETURN_DEFAULT),
    flow_window_months: Annotated[int, Query(ge=1, le=FLOW_WINDOW_MAX)] = FLOW_WINDOW_DEFAULT,
) -> RunResponse:
    ids = [raw.strip() for raw in (scenario_ids or "").split(",") if raw.strip()]
    if len(ids) > MAX_SCENARIOS_PER_RUN:
        raise api_error(
            422,
            f"At most {MAX_SCENARIOS_PER_RUN} scenarios per run",
            "validation_error",
        )
    if len(set(ids)) != len(ids):
        raise api_error(422, "scenario_ids contains a duplicate", "validation_error")

    stored = {entry.get("id"): entry for entry in _stored(current_user)}
    scenarios: list[tuple[str, str, list]] = []
    for sid in ids:
        entry = stored.get(sid)
        if entry is None:
            raise api_error(404, "Forecast scenario not found", "scenario_not_found")
        scenarios.append((entry["id"], entry["name"], validate_events(entry["events"])))

    try:
        params = RunParams(
            scenario_ids=ids,
            horizon_months=horizon_months,
            granularity=granularity,  # type: ignore[arg-type]
            inflation_pct=inflation_pct,
            annual_return_pct=annual_return_pct,
            flow_window_months=flow_window_months,
        )
    except ValidationError as exc:
        raise api_error(422, str(exc.errors()[0]["msg"]), "validation_error") from None

    as_of = today_et()
    inputs = forecast_service.collect_inputs(db, current_user.id, as_of, params.flow_window_months)
    return forecast_service.run_forecast(as_of, inputs, params, scenarios)

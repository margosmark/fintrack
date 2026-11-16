"""tax_projection — estimate annual tax liability (docs/27)."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BeforeValidator
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import get_current_user, get_db
from app.models.tax_projections import TaxProjection
from app.models.users import User
from app.services.tax_projection import project_tax


def _empty_to_none(v: object) -> object:
    """Treat an empty query value (`?param=`) as an explicit clear to null."""
    return None if v == "" else v


# URL query params cannot carry JSON null; the empty value is the "clear it"
# convention for the override fields (M47, docs/27).
OverrideDecimal = Annotated[Decimal | None, BeforeValidator(_empty_to_none)]

router = APIRouter(prefix="/taxes", tags=["taxes"])


@router.get("/projection")
def get_projection(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    year: int = Query(default=2026, ge=2000, le=2100),
) -> dict:
    proj = db.exec(
        select(TaxProjection).where(
            TaxProjection.user_id == current_user.id,
            TaxProjection.year == year,
        )
    ).first()

    emp_income = proj.estimated_income if proj else Decimal("0")
    deductions = proj.deductions if proj else Decimal("0")
    cap_gains = (
        proj.capital_gains_override if proj and proj.capital_gains_override else Decimal("0")
    )
    div_income = (
        proj.dividend_income_override if proj and proj.dividend_income_override else Decimal("0")
    )
    province = proj.province if proj else "ON"

    result = project_tax(emp_income, cap_gains, div_income, deductions, province, year)
    result["year"] = year
    result["province"] = province
    # Raw stored inputs for form prefill (docs/27 M47) — Decimals as strings,
    # NOT the zero-coalesced computed values above.
    result["stored"] = {
        "year": year,
        "province": province,
        "estimated_income": str(proj.estimated_income) if proj else "0",
        "deductions": str(proj.deductions) if proj else "0",
        "capital_gains_override": (
            str(proj.capital_gains_override)
            if proj and proj.capital_gains_override is not None
            else None
        ),
        "dividend_income_override": (
            str(proj.dividend_income_override)
            if proj and proj.dividend_income_override is not None
            else None
        ),
    }
    return result


@router.patch("/projection")
def update_projection(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    year: int = Query(default=2026, ge=2000, le=2100),
    province: str = Query(
        default="ON",
        min_length=2,
        max_length=2,
        pattern="^[A-Z]{2}$",
    ),
    estimated_income: Decimal | None = Query(default=None),
    deductions: Decimal | None = Query(default=None),
    capital_gains_override: Annotated[OverrideDecimal, Query()] = None,
    dividend_income_override: Annotated[OverrideDecimal, Query()] = None,
) -> dict:
    # A query param cannot carry null, so "provided" is detected by presence
    # in the raw query string; absent params keep the stored value (M47).
    def _provided(name: str) -> bool:
        return name in request.query_params

    proj = db.exec(
        select(TaxProjection).where(
            TaxProjection.user_id == current_user.id,
            TaxProjection.year == year,
        )
    ).first()

    if proj:
        proj.province = province
        # Absent params keep the stored value (the frontend sends only
        # edited fields; missing no longer zeroes or clears overrides).
        if _provided("estimated_income"):
            proj.estimated_income = estimated_income
        if _provided("deductions"):
            proj.deductions = deductions
        if _provided("capital_gains_override"):
            proj.capital_gains_override = capital_gains_override
        if _provided("dividend_income_override"):
            proj.dividend_income_override = dividend_income_override
    else:
        proj = TaxProjection(
            user_id=current_user.id,
            year=year,
            province=province,
            estimated_income=estimated_income or Decimal("0"),
            deductions=deductions or Decimal("0"),
            capital_gains_override=capital_gains_override,
            dividend_income_override=dividend_income_override,
        )
        db.add(proj)

    db.commit()
    db.refresh(proj)

    result = project_tax(
        proj.estimated_income,
        proj.capital_gains_override or Decimal("0"),
        proj.dividend_income_override or Decimal("0"),
        proj.deductions,
        proj.province,
        year,
    )
    result["year"] = year
    result["province"] = proj.province
    return result

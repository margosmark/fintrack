"""spending-comparison — period-over-period comparison (docs/08)."""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.users import User
from app.services.spending_comparison import compare_periods

router = APIRouter(prefix="/reports", tags=["reports"])

# Owner-lens expansion (docs/12 §Owner dimension, docs/08 §Report routes): the
# comparison sums committed transactions only — the one population the M93
# projection delivers (docs/43 §6) — so it carries the household scope
# honestly. Routes whose population mixes in trade cash actions, balances or
# prices keep the own-only singleton and REFUSE `household` at `query.scope`
# (M179 contract) instead of silently answering a narrower population.
ReportScope = Literal["own", "household"]


@router.get("/spending-comparison")
def spending_comparison(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    current_start: date = Query(description="Current period start"),
    current_end: date = Query(description="Current period end"),
    previous_start: date | None = Query(
        default=None, description="Previous period start (auto-computed if omitted)"
    ),
    previous_end: date | None = Query(
        default=None, description="Previous period end (auto-computed if omitted)"
    ),
    scope: ReportScope = "own",
) -> dict:
    # Auto-compute previous period as same-length window ending at current_start
    if previous_start is None or previous_end is None:
        from datetime import timedelta

        length = (current_end - current_start).days
        previous_end = current_start
        previous_start = current_start - timedelta(days=length)
    return compare_periods(
        db,
        current_user.id,
        current_start,
        current_end,
        previous_start,
        previous_end,
        household=scope == "household",
    )

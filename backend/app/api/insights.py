"""insights — spending velocity and burn rate (docs/08)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.users import User
from app.services.insights import compute_insights

router = APIRouter(prefix="/reports", tags=["reports"])

# Owner-lens expansion (docs/12 §Owner dimension, docs/08 §Report routes): this
# route's ENTIRE population is committed transactions, which is exactly what the
# M93 projection delivers (docs/43 §6), so it can carry the household
# population honestly. The trade/price-backed routes keep the own-only
# singleton — an unsupported population is refused at `query.scope`, never
# silently narrowed (the M179 refusal contract).
ReportScope = Literal["own", "household"]


@router.get("/insights")
def spending_insights(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    scope: ReportScope = "own",
) -> dict:
    return compute_insights(db, current_user.id, household=scope == "household")

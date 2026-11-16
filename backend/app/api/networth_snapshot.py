"""networth-snapshot — per-account breakdown at a date (docs/08)."""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api._report_flags import assert_report_route_live
from app.api.deps import get_current_user, get_db
from app.models.users import User
from app.services.networth_snapshot import networth_snapshot

router = APIRouter(prefix="/reports", tags=["reports"])
OwnReportScope = Literal["own"]


@router.get("/networth-snapshot")
def get_networth_snapshot(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    target_date: date = Query(description="Date to snapshot"),
    scope: OwnReportScope = "own",
) -> dict:
    # M137 P1-1: the LAST orphan (no frontend caller at all) — 410 under
    # `SERVER_REPORTS_DEPRECATED`. The other three orphans left the set with
    # M160-R2 (t_85311c34): the UI consumes them now.
    assert_report_route_live("/reports/networth-snapshot")
    return networth_snapshot(db, current_user.id, target_date)

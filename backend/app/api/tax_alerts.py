"""tax-alerts — unrealized loss harvesting opportunities (docs/08)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.users import User
from app.services.tax_alerts import compute_tax_loss_alerts

router = APIRouter(prefix="/taxes", tags=["taxes"])


@router.get("/alerts")
def tax_loss_alerts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    alerts = compute_tax_loss_alerts(db, current_user.id)
    total_loss = sum(a["unrealized_loss_cad"] for a in alerts)
    total_benefit = sum(a["estimated_tax_benefit_cad"] for a in alerts)
    return {
        "alerts": alerts,
        "summary": {
            "count": len(alerts),
            "total_unrealized_loss_cad": total_loss,
            "total_estimated_tax_benefit_cad": total_benefit,
        },
    }

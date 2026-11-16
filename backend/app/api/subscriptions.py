"""subscriptions — detected recurring transactions (docs/08, docs/24).

M137 P1-2 (docs/33 §Server-first computing): RE-MOUNTED. M48-S3 deleted this
router and the browser computed the report over a full-ledger download; the
detection itself was never removed — `services/subscriptions.py` kept running
as the reference `bill_reminders`' cash-flow calendar calls, so the route is
the same engine exposed again, not a rewrite.

The roll-up is the removed route's, byte-for-byte: monthly cadences at face
value, quarterly/yearly divided by 12, weekly/biweekly multiplied by 4.33.
"""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.users import User
from app.services.subscriptions import detect_subscriptions

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.get("")
def list_subscriptions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    subs = detect_subscriptions(db, current_user.id)
    total_monthly = (
        sum(s["avg_amount"] for s in subs if s["frequency"] == "monthly")
        + sum(
            s["avg_amount"] / Decimal("12")
            for s in subs
            if s["frequency"] in ("quarterly", "yearly")
        )
        + sum(
            s["avg_amount"] * Decimal("4.33")
            for s in subs
            if s["frequency"] in ("weekly", "biweekly")
        )
    )
    total_monthly = (
        total_monthly.quantize(Decimal("0.01"))
        if isinstance(total_monthly, Decimal)
        else Decimal("0")
    )
    return {
        "subscriptions": subs,
        "total_monthly": total_monthly,
        "total_annual": (total_monthly * 12).quantize(Decimal("0.01")),
    }

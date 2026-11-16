"""spending_alerts — budget threshold alerts (docs/26, docs/35).

The spend it measures comes from `app/services/budgets.py::budget_spent` —
the SAME predicate `GET /budgets` uses, so the Dashboard alert's percentage and
the Budgets card's percentage are two renderings of one number (M61 #3).
"""

from datetime import datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import get_current_user, get_db
from app.core.config import get_settings
from app.core.errors import api_error
from app.models.budgets import Budget
from app.models.spending_alerts import SpendingAlert
from app.models.users import User
from app.services.budgets import budget_spent, carry_in, progress
from app.services.reports import FxCache
from app.services.txn_filters import today_et

_ET = ZoneInfo("America/Toronto")

# spending_alerts.actual_pct is Numeric(5,2) — max 999.99. It is BOTH the
# storage ceiling for an extreme overspend (a bare 500 would overflow the
# column) and the number stored when a carried debt leaves no denominator at
# all (`progress` answers None for a non-positive spendable — the row is over
# before its first dollar, so there is no ratio to store, only an "already
# over" verdict to record). The display pct on the card stays exact.
_STORED_CEILING = Decimal("999.99")

router = APIRouter(prefix="/budgets", tags=["budgets"])


def _check_alerts(db: Session, user_id: int) -> list[SpendingAlert]:
    """Check all budgets and create alerts where thresholds are crossed."""
    today = today_et()
    # Lower bound of "today" in ET — rows created at any point of the local
    # day satisfy it, and dedup is exact regardless of server timezone.
    et_midnight = datetime.combine(today, dt_time.min, tzinfo=_ET)

    budgets = db.exec(select(Budget).where(Budget.user_id == user_id)).all()
    reporting = get_settings().reporting_currency
    fx = FxCache(db, reporting)

    new_alerts = []
    for budget in budgets:
        # M65 (M61 #3): one predicate for the alert AND the Budgets card —
        # `budget_spent` is signed reporting-currency net spend over the budget's
        # own window (docs/26 decision 6, docs/29 F1), committed rows only. The
        # old inline `sum(abs(amount_native))` double-failed AGENTS §2: abs()
        # counted refunds as spending, and native amounts billed a CAD budget
        # with raw USD face value — either one put a different number on the
        # Dashboard than on the card.
        spent = budget_spent(db, user_id, budget.category_id, budget.period, reporting, fx)
        # M65 (M61 #3) extended by M110: the alert measures the SAME
        # denominator the card prints. With a carry on, that is the SPENDABLE
        # pool (`amount + carried_in`), not the bare assignment — otherwise a
        # carried leftover would let the card read 40% while the alert fired at
        # 100% of `amount`. `budget_amount` stores the denominator actually
        # used, so the stored row still reproduces `actual_pct` (identical to
        # `amount` for every budget that never opted in).
        carried = carry_in(db, budget, reporting, fx)
        spendable, _, pct = progress(budget.amount, spent, carried)

        # Check thresholds (use defaults if not set on budget)
        warn_threshold = Decimal("80")
        crit_threshold = Decimal("100")

        # Check if alert already exists today
        existing = db.exec(
            select(SpendingAlert).where(
                SpendingAlert.user_id == user_id,
                SpendingAlert.budget_id == budget.id,
                SpendingAlert.created_at >= et_midnight,
            )
        ).first()

        if existing:
            continue

        if pct is None:
            # A carried debt swallowed the whole assignment: there is no ratio
            # to store, and the row is over before it spent anything.
            stored_pct = _STORED_CEILING
        else:
            stored_pct = min(pct.quantize(Decimal("0.01")), _STORED_CEILING)

        if pct is None or pct >= crit_threshold:
            alert = SpendingAlert(
                user_id=user_id,
                budget_id=budget.id,
                alert_type="critical",
                threshold_pct=crit_threshold,
                actual_pct=stored_pct,
                spent=spent,
                budget_amount=spendable,
            )
            new_alerts.append(alert)
        elif pct >= warn_threshold:
            alert = SpendingAlert(
                user_id=user_id,
                budget_id=budget.id,
                alert_type="warning",
                threshold_pct=warn_threshold,
                actual_pct=stored_pct,
                spent=spent,
                budget_amount=spendable,
            )
            new_alerts.append(alert)

    for alert in new_alerts:
        db.add(alert)
    if new_alerts:
        db.commit()

    return new_alerts


@router.get("/alerts")
def list_alerts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    _check_alerts(db, current_user.id)
    alerts = db.exec(
        select(SpendingAlert)
        .where(
            SpendingAlert.user_id == current_user.id,
            SpendingAlert.acknowledged.is_(False),
        )
        .order_by(SpendingAlert.created_at.desc())
    ).all()
    return {
        "alerts": [
            {
                "id": a.id,
                "budget_id": a.budget_id,
                "alert_type": a.alert_type,
                "threshold_pct": str(a.threshold_pct),
                "actual_pct": str(a.actual_pct),
                "spent": str(a.spent),
                "budget_amount": str(a.budget_amount),
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in alerts
        ],
        "count": len(alerts),
    }


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(
    alert_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    alert = db.get(SpendingAlert, alert_id)
    if alert is None or alert.user_id != current_user.id:
        raise api_error(404, "Alert not found", "not_found")
    alert.acknowledged = True
    db.add(alert)
    db.commit()
    return {"status": "acknowledged"}

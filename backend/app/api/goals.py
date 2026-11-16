"""goals — financial savings targets (docs/08)."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import get_current_user, get_db
from app.core.errors import api_error
from app.models.accounts import Account
from app.models.goals import Goal
from app.models.import_batches import ImportBatch
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.goals import GoalCreate, GoalPublic, GoalUpdate, GoalWithProgress
from app.services.txn_filters import countable_tx_conditions

router = APIRouter(prefix="/goals", tags=["goals"])


def _compute_progress(goal: Goal, db: Session, user_id: int) -> dict:
    """Compute current amount and status for a goal."""
    if goal.current_amount_override is not None:
        current = goal.current_amount_override
    elif goal.account_id is not None:
        # Use account's committed cash balance as current amount (docs/29 F1).
        # The balance starts from `Account.starting_balance or 0` — the same
        # seed `api/accounts.py` and the net-worth snapshot use — so an account
        # opened with an opening balance and no activity still counts it.
        account = db.get(Account, goal.account_id)
        start = (
            Decimal(str(account.starting_balance)) if account and account.starting_balance else 0
        )
        result = db.exec(
            select(func.coalesce(func.sum(Transaction.amount_native), 0))
            .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
            .where(
                *countable_tx_conditions(user_id),
                Transaction.account_id == goal.account_id,
            )
        ).one()
        current = start + Decimal(str(result))
    else:
        current = Decimal("0")

    progress = float(current / goal.target_amount * 100) if goal.target_amount > 0 else 0
    remaining = max(goal.target_amount - current, Decimal("0"))

    # Monthly needed
    monthly_needed = None
    status = "on_track"
    if goal.target_date:
        from datetime import datetime

        try:
            target = datetime.strptime(goal.target_date, "%Y-%m-%d").date()
            from datetime import date as date_type

            today = date_type.today()
            months_left = max((target.year - today.year) * 12 + target.month - today.month, 1)
            monthly_needed = (remaining / months_left).quantize(Decimal("0.01"))
            # Rough check: if remaining > 0 and we're behind pace
            expected_pct = (1 - months_left / max((target - today).days / 30, 1)) * 100
            if progress < expected_pct * 0.8:
                status = "behind"
            elif progress > expected_pct * 1.2:
                status = "ahead"
        except (ValueError, TypeError):
            pass

    if progress >= 100:
        status = "completed"

    return {
        "current_amount": current.quantize(Decimal("0.01")),
        "progress_pct": min(round(progress, 1), 100),
        "monthly_needed": monthly_needed,
        "status": status,
    }


@router.get("", response_model=list[GoalWithProgress])
def list_goals(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[GoalWithProgress]:
    rows = db.exec(select(Goal).where(Goal.user_id == current_user.id).order_by(Goal.name)).all()
    result = []
    for goal in rows:
        prog = _compute_progress(goal, db, current_user.id)
        result.append(
            GoalWithProgress(
                id=goal.id,
                user_id=goal.user_id,
                name=goal.name,
                target_amount=goal.target_amount,
                target_date=goal.target_date,
                account_id=goal.account_id,
                current_amount_override=goal.current_amount_override,
                **prog,
            )
        )
    return result


@router.post("", response_model=GoalPublic, status_code=201)
def create_goal(
    payload: GoalCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Goal:
    if payload.account_id is not None:
        acct = db.get(Account, payload.account_id)
        if acct is None or acct.user_id != current_user.id:
            raise api_error(404, "Account not found", "not_found")
    goal = Goal(
        user_id=current_user.id,
        name=payload.name,
        target_amount=payload.target_amount,
        target_date=payload.target_date,
        account_id=payload.account_id,
        current_amount_override=payload.current_amount_override,
    )
    db.add(goal)
    db.commit()
    db.refresh(goal)
    return goal


@router.patch("/{goal_id}", response_model=GoalPublic)
def update_goal(
    goal_id: int,
    payload: GoalUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Goal:
    goal = db.get(Goal, goal_id)
    if goal is None or goal.user_id != current_user.id:
        raise api_error(404, "Goal not found", "not_found")
    if payload.account_id is not None:
        # Ownership re-validation on PATCH mirrors create_goal (docs/29 F7).
        acct = db.get(Account, payload.account_id)
        if acct is None or acct.user_id != current_user.id:
            raise api_error(404, "Account not found", "not_found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(goal, field, value)
    db.add(goal)
    db.commit()
    db.refresh(goal)
    return goal


@router.delete("/{goal_id}", status_code=204)
def delete_goal(
    goal_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    goal = db.get(Goal, goal_id)
    if goal is None or goal.user_id != current_user.id:
        raise api_error(404, "Goal not found", "not_found")
    db.delete(goal)
    db.commit()

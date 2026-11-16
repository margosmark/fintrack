"""budgets — per-category spending limits (docs/08, docs/26, docs/09).

The spent/pct helpers live in `app/services/budgets.py` because the spending
alerts read them too — one predicate, two surfaces (M61 #3). The same is true
of the M110 carry: `services.budgets.carry_in` is the only producer of a
`carried_in` value, and `services.budgets.progress` the only layering of
spendable / remaining / pct_used.
"""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlmodel import delete, select

from app.api.deps import get_current_user, get_db, owned_or_404
from app.core.config import get_settings
from app.core.errors import api_error
from app.models.budgets import Budget, BudgetCarryover
from app.models.categories import Category
from app.models.import_batches import ImportBatch
from app.models.spending_alerts import SpendingAlert
from app.models.transactions import RecurringCadence, Transaction
from app.models.users import User
from app.schemas.budgets import (
    BudgetCreate,
    BudgetPublic,
    BudgetUpdate,
    BudgetWithSpent,
    RecurringSummary,
)
from app.services.budgets import budget_spent, carry_in, progress
from app.services.reports import FxCache
from app.services.txn_filters import countable_tx_conditions, transfer_exclusion_conditions

router = APIRouter(prefix="/budgets", tags=["budgets"])


@router.get("", response_model=list[BudgetWithSpent])
def list_budgets(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[BudgetWithSpent]:
    rows = db.exec(
        select(Budget, Category.name)
        .join(Category, Budget.category_id == Category.id)
        .where(Budget.user_id == current_user.id)
        .order_by(Category.name)
    ).all()
    reporting = get_settings().reporting_currency
    fx = FxCache(db, reporting)
    result = []
    for budget, cat_name in rows:
        spent = budget_spent(db, current_user.id, budget.category_id, budget.period, reporting, fx)
        # First read after the boundary materializes the snapshot (one write,
        # then frozen); carry off returns None without touching the DB.
        carried = carry_in(db, budget, reporting, fx)
        # The pool (assigned + carried_in) is derivable from two wire fields,
        # so it is not published — only what hangs off it is (docs/08 §Budgets).
        _, remaining, pct = progress(budget.amount, spent, carried)
        result.append(
            BudgetWithSpent(
                id=budget.id,
                user_id=budget.user_id,
                category_id=budget.category_id,
                amount=budget.amount,
                period=budget.period,
                carry=budget.carry,
                category_name=cat_name,
                spent=spent,
                carried_in=carried,
                remaining=remaining,
                # M61 #3 / M65: the TRUE ratio, never clamped — `pct_used` is
                # what the card label prints, and capping it at 100 made a 2x
                # overspend read as "100% used" (2% over). The BAR may still
                # cap at 100% visually; the number never does. `progress` is
                # the same computation the alert generator uses; it answers
                # `null` only when a carried debt left no positive denominator.
                pct_used=None if pct is None else float(pct),
            )
        )
    return result


# Occurrences per month for each fitted cadence (30.4375-day average month) —
# the SAME table `frontend/src/lib/recurring.ts` labels with, so the server
# answer and the retiring client twin are comparable field-for-field.
#
# Decimal, not float: this multiplies a MONEY amount, and AGENTS.md §2 pins
# money as Decimal end-to-end. As binary floats the 30.4375/7 division is
# inexact, so a weekly row's monthly contribution was off in the ~15th
# significant digit before rounding — harmless for display, but it made the
# sum depend on accumulation order. `Decimal` division is exact to the context
# precision, and the one `quantize` at the end is the only rounding.
_AVG_MONTH_DAYS = Decimal("30.4375")
_CENT = Decimal("0.01")
_PER_MONTH: dict[RecurringCadence, Decimal] = {
    RecurringCadence.weekly: _AVG_MONTH_DAYS / Decimal(7),
    RecurringCadence.biweekly: _AVG_MONTH_DAYS / Decimal(14),
    RecurringCadence.four_weekly: _AVG_MONTH_DAYS / Decimal(28),
    RecurringCadence.monthly: Decimal(1),
}


@router.get("/recurring-summary", response_model=RecurringSummary)
def recurring_summary(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RecurringSummary:
    """M96's "committed monthly" label, computed server-side (M137 P0-2).

    The Budgets page used to earn this number by downloading every committed
    row (`api/s3Inputs.ts::loadCommittedTransactions`). One scan over the
    rows tagged `recurring` answers it instead — docs/09 §Recurring prompt is
    a LABEL, so it is deliberately its own read rather than a field bolted
    onto `GET /budgets`, whose 4-query budget it would not belong to (docs/34).
    """
    reporting = get_settings().reporting_currency
    rows = db.exec(
        select(
            Transaction.recurring_cadence,
            Transaction.currency,
            Transaction.amount_native,
            Transaction.cad_amount,
        )
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(
            *countable_tx_conditions(current_user.id),
            # docs/12 §Shared population rules: a recurring transfer is money
            # moved, so it cannot be part of "what this costs every month".
            *transfer_exclusion_conditions(current_user.id),
            Transaction.is_recurring.is_(True),
            Transaction.recurring_cadence.is_not(None),
        )
    ).all()
    count = 0
    monthly = Decimal("0")
    for cadence, currency, amount_native, cad_amount in rows:
        # docs/12: reporting currency only — a foreign row counts at its
        # reporting-currency mirror, and one with no mirror is EXCLUDED,
        # never counted at face value.
        value = amount_native if currency == reporting else cad_amount
        if value is None or cadence is None:
            continue
        count += 1
        monthly += Decimal(value) * _PER_MONTH[RecurringCadence(cadence)]
    return RecurringSummary(count=count, monthly=monthly.quantize(_CENT))


@router.post("", response_model=BudgetPublic, status_code=201)
def create_budget(
    payload: BudgetCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Budget:
    cat = db.get(Category, payload.category_id)
    if cat is None or cat.user_id != current_user.id:
        raise api_error(404, "Category not found", "not_found")
    existing = db.exec(
        select(Budget).where(
            Budget.user_id == current_user.id,
            Budget.category_id == payload.category_id,
        )
    ).first()
    if existing is not None:
        raise api_error(409, "Budget already exists for this category", "duplicate_budget")
    budget = Budget(
        user_id=current_user.id,
        category_id=payload.category_id,
        amount=payload.amount,
        period=payload.period,
        carry=payload.carry,
    )
    db.add(budget)
    db.commit()
    db.refresh(budget)
    return budget


@router.patch("/{budget_id}", response_model=BudgetPublic)
def update_budget(
    budget_id: int,
    payload: BudgetUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Budget:
    budget = owned_or_404(Budget, budget_id, current_user, db)
    if payload.amount is not None:
        budget.amount = payload.amount
    if payload.period is not None:
        budget.period = payload.period
    if payload.carry is not None:
        # Toggling the flag does NOT rewrite an existing snapshot: whatever was
        # carried into this period was computed at the boundary and stays as it
        # was (docs/09 §Budget carry). Turning carry ON for the first time
        # materializes on the next read.
        budget.carry = payload.carry
    db.add(budget)
    db.commit()
    db.refresh(budget)
    return budget


@router.delete("/{budget_id}", status_code=204)
def delete_budget(
    budget_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    budget = owned_or_404(Budget, budget_id, current_user, db)
    # Child rows first (docs/02): a snapshot (or a threshold alert) left behind
    # would make this DELETE fail on the FK instead of removing the budget.
    db.exec(delete(BudgetCarryover).where(BudgetCarryover.budget_id == budget_id))
    db.exec(delete(SpendingAlert).where(SpendingAlert.budget_id == budget_id))
    db.delete(budget)
    db.commit()

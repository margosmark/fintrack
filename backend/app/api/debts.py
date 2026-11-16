"""debts — debt tracking with payoff projections (docs/29)."""

from decimal import ROUND_CEILING, Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import get_current_user, get_db
from app.core.errors import api_error
from app.models.accounts import Account
from app.models.debts import Debt
from app.models.users import User
from app.schemas.debts import DebtCreate, DebtPublic, DebtUpdate

router = APIRouter(prefix="/debts", tags=["debts"])

_CENT = Decimal("0.01")
_ZERO = Decimal("0")
# Payment frequencies → payments per year (docs/29 debt schema). Anything else
# (including a NULL frequency) amortizes monthly, the historical behavior.
PERIODS_PER_YEAR: dict[str, int] = {"monthly": 12, "biweekly": 26}
# Loop guard, expressed in YEARS rather than periods: a 30-year biweekly
# mortgage legitimately needs 780 payments, so a flat 600-period cap silently
# reported "infinite" for long biweekly plans. `_max_periods` scales the guard
# with the frequency so the horizon stays 50 years for every frequency.
MAX_AMORTIZATION_YEARS = 50


def _max_periods(periods_per_year: int) -> int:
    return MAX_AMORTIZATION_YEARS * periods_per_year


def _periods_to_months(periods: int, periods_per_year: int) -> int:
    """Convert payment periods to whole calendar months, rounded up.

    `months` is the user-facing payoff timeline, so a biweekly plan is
    reported in months (26 payments/yr ≈ 2.17 payments per month) rather than
    in raw payment count.
    """
    return -((-periods * 12) // periods_per_year)


def _payoff_projection(
    balance: Decimal, rate: Decimal, payment: Decimal, periods_per_year: int = 12
) -> dict:
    """Amortize `balance` and return months / total_interest / total_paid.

    `rate` is the ANNUAL percentage; interest accrues once per payment period
    (`rate / 100 / periods_per_year`), so a biweekly plan accrues 26 times a year
    instead of 12.

    Zero-rate fixes (see tests/test_debts.py):
    - `months` is the ceiling of balance / payment.
    - `total_paid` is the balance plus interest — the same invariant the
      interest-bearing branch returns — NOT `payment x months`. The old branch
      reported a phantom overpayment (13 x $1,000 = $13,000 for a $12,345.67
      balance), inflating "total paid" by the final partial payment.
    """
    if balance <= 0 or payment <= 0:
        return {"months": 0, "total_interest": "0", "total_paid": "0"}

    cap = _max_periods(periods_per_year)
    period_rate = rate / 100 / Decimal(periods_per_year)
    if period_rate == 0:
        periods = int((balance / payment).quantize(Decimal("1"), rounding=ROUND_CEILING))
        if periods > cap:
            return _never_amortizes()
        return {
            "months": _periods_to_months(periods, periods_per_year),
            "total_interest": "0",
            "total_paid": str(balance.quantize(_CENT)),
        }

    # Amortization
    periods = 0
    total_interest = Decimal("0")
    remaining = balance
    while remaining > 0 and periods < cap:
        interest = remaining * period_rate
        total_interest += interest
        principal = payment - interest
        if principal <= 0:
            return _never_amortizes()
        remaining -= principal
        periods += 1

    if remaining > 0:
        # Still owed after the full amortization horizon (an under-payment so
        # slow it exceeds MAX_AMORTIZATION_YEARS) — same "never pays off" result
        # as a payment that cannot cover interest, never a silently truncated
        # timeline.
        return _never_amortizes()

    total_paid = balance + total_interest
    return {
        "months": _periods_to_months(periods, periods_per_year),
        "total_interest": str(total_interest.quantize(_CENT)),
        "total_paid": str(total_paid.quantize(_CENT)),
    }


def _never_amortizes() -> dict:
    """The documented "this debt never pays off" projection (docs/08)."""
    return {"months": -1, "total_interest": "infinite", "total_paid": "infinite"}


def _periods_per_year(debt: Debt) -> int:
    """Payments per year for this debt's `payment_frequency` (default monthly)."""
    return PERIODS_PER_YEAR.get((debt.payment_frequency or "monthly").lower(), 12)


@router.get("", response_model=list[DebtPublic])
def list_debts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[Debt]:
    return list(
        db.exec(select(Debt).where(Debt.user_id == current_user.id).order_by(Debt.name)).all()
    )


@router.post("", response_model=DebtPublic, status_code=201)
def create_debt(
    payload: DebtCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Debt:
    acct = db.get(Account, payload.account_id)
    if acct is None or acct.user_id != current_user.id:
        raise api_error(404, "Account not found", "not_found")
    debt = Debt(user_id=current_user.id, **payload.model_dump())
    db.add(debt)
    db.commit()
    db.refresh(debt)
    return debt


@router.patch("/{debt_id}", response_model=DebtPublic)
def update_debt(
    debt_id: int,
    payload: DebtUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Debt:
    debt = db.get(Debt, debt_id)
    if debt is None or debt.user_id != current_user.id:
        raise api_error(404, "Debt not found", "not_found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(debt, field, value)
    db.add(debt)
    db.commit()
    db.refresh(debt)
    return debt


@router.delete("/{debt_id}", status_code=204)
def delete_debt(
    debt_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    debt = db.get(Debt, debt_id)
    if debt is None or debt.user_id != current_user.id:
        raise api_error(404, "Debt not found", "not_found")
    db.delete(debt)
    db.commit()


@router.get("/projection")
def debt_projection(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Return payoff projections for all debts."""
    debts = db.exec(select(Debt).where(Debt.user_id == current_user.id)).all()

    projections = []
    total_debt = Decimal("0")
    total_interest = Decimal("0")

    for debt in debts:
        balance = debt.original_amount or _ZERO
        periods_per_year = _periods_per_year(debt)

        if debt.minimum_payment and debt.interest_rate is not None:
            proj = _payoff_projection(
                balance, debt.interest_rate, debt.minimum_payment, periods_per_year
            )
        else:
            proj = {"months": None, "total_interest": None, "total_paid": None}

        projections.append(
            {
                "debt_id": debt.id,
                "name": debt.name,
                "debt_type": debt.debt_type,
                "balance": str(balance.quantize(_CENT)),
                "interest_rate": str(debt.interest_rate) if debt.interest_rate else None,
                "minimum_payment": str(debt.minimum_payment) if debt.minimum_payment else None,
                # Echo the frequency actually amortized so the UI can label the
                # timeline (docs/29 schema: monthly | biweekly).
                "payment_frequency": debt.payment_frequency or "monthly",
                "periods_per_year": periods_per_year,
                **proj,
            }
        )
        total_debt += balance
        if proj.get("total_interest") and proj["total_interest"] != "infinite":
            total_interest += Decimal(proj["total_interest"])

    return {
        "debts": projections,
        "total_debt": str(total_debt.quantize(_CENT)),
        "total_interest": str(total_interest.quantize(_CENT)),
    }

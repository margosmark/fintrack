"""budgets — per-category monthly/yearly spending limits (docs/02, docs/08).

M110 (budget rollover): `carry` is the per-category opt-in flag (`off` |
`forward`, default `off` — a row that never opted in behaves exactly as it did
before the column existed), and `BudgetCarryover` is the frozen `carried_in`
snapshot per period (docs/09 §Budget carry). The snapshot is written ONCE at
the boundary switch and never recomputed, so going back a period shows the
value as it was at the time — audit-honest, not retroactively recalculated.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin

CARRY_OFF = "off"
CARRY_FORWARD = "forward"
CARRY_VALUES = (CARRY_OFF, CARRY_FORWARD)


class Budget(TimestampMixin, table=True):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("user_id", "category_id", name="uq_budgets_user_category"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    category_id: int = Field(foreign_key="categories.id")
    amount: Decimal = Field(max_digits=12, decimal_places=2)
    period: str = Field(default="monthly", max_length=20)  # monthly | yearly
    # M110: non-native enum (AGENTS §2) — VARCHAR(8), default off.
    carry: str = Field(default=CARRY_OFF, max_length=8)  # off | forward


class BudgetCarryover(TimestampMixin, table=True):
    """The `carried_in` a budget carried INTO one period, frozen at the moment
    it was materialized (docs/09 §Budget carry).

    One row per (budget, period_start): the target period's first day. `amount`
    is signed — a positive leftover is money to spend, a negative one is a debt
    the next period pays down first. Reading a period that has no row yet
    materializes it from the CLOSED source window; once the row exists it is
    never rewritten, which is what makes a back-view honest.
    """

    __tablename__ = "budget_carryovers"
    __table_args__ = (
        UniqueConstraint("budget_id", "period_start", name="uq_budget_carryovers_budget_period"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    budget_id: int = Field(foreign_key="budgets.id")
    period_start: date = Field()  # first day of the period carrying IN
    amount: Decimal = Field(max_digits=12, decimal_places=2)  # signed

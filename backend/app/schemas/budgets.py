"""budgets — CRUD shapes."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CarryMode = Literal["off", "forward"]


class BudgetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: int
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    period: str = Field(default="monthly", pattern="^(monthly|yearly)$")
    # M110 rollover: optional, default off — a client that never sends it
    # behaves exactly as before (docs/09 §Budget carry).
    carry: CarryMode = "off"


class BudgetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    period: str | None = Field(default=None, pattern="^(monthly|yearly)$")
    carry: CarryMode | None = None


class BudgetPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    category_id: int
    amount: Decimal
    period: str
    carry: CarryMode


class BudgetWithSpent(BudgetPublic):
    """`GET /budgets` item (docs/08 §Budgets).

    `carried_in` is its OWN field — never merged into `amount` — so a reader
    always sees the two layers the row is made of. `null` means "nothing was
    carried into this period" (carry off, or no governed prior period), never
    a hidden zero. `pct_used` is the true ratio against the SPENDABLE pool
    (`amount + carried_in`), unclamped, and `null` only when that pool is
    non-positive (no denominator exists — docs/09 §Budget carry).
    """

    category_name: str
    spent: Decimal
    carried_in: Decimal | None
    remaining: Decimal
    pct_used: float | None


class RecurringSummary(BaseModel):
    """`GET /budgets/recurring-summary` (M137 P0-2, docs/09 §Recurring prompt).

    The M96 label on the Budgets page — "committed monthly" over rows tagged
    `recurring` — used to be computed in the browser from a FULL-LEDGER
    download. It is a label, not a report, so it gets one tiny server read
    instead of the ledger.

    `monthly` is SIGNED (debits negative, credits positive), rounded to the
    cent, and excludes foreign rows with no reporting-currency mirror —
    never counted at face value (docs/12).

    It is a `Decimal`, so it serializes as a STRING like every other money
    field in the API (AGENTS.md §2: money is Decimal end-to-end, floats never
    touch it). It was `float` until the review that found `float(value)` on a
    `Transaction.amount_native` in the service below; the browser already
    parses the decimal string, so the wire format is unchanged for the UI
    (`formatMoney` does `parseFloat(v)` on a string either way) — but any
    non-browser consumer reading it as a JSON number must be updated.
    """

    count: int
    monthly: Decimal

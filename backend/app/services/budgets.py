"""budgets — the shared spending-window helpers (docs/08, docs/26, docs/35, docs/09).

M65 moved `_period_start` / `_budget_spent` out of `app/api/budgets.py` so
`GET /budgets` and the spending-alert generator read the SAME number for the
same period/category (M61 #3: the Dashboard alert said 201.94% while the
Budgets card said 100%). Two surfaces, one predicate — signed reporting-currency
net spend, floor at zero, unresolved-fx rows excluded, committed rows only.

M110 (budget rollover, docs/09 §Budget carry) adds the second half of that
promise: `carry_in` is the ONE place a `carried_in` value is produced (the
endpoints and the alert generator both call it, so no two surfaces can print
different carry), and `pct_ratio` is the ONE place `pct_used` is divided.

Two kinds of access live here and they are deliberately different:

* **Pure DB reads** — `period_start`, `spent_between`, `budget_spent`,
  `pct_ratio`, `progress`. No writes, no network (fx is a shared cache).
* **One read-path write** — `carry_in` materializes the boundary snapshot the
  first time a period is read after the boundary. That is the same
  auto-create-on-first-GET pattern `GET /notifications` uses (docs/35), and it
  is what makes the value audit-honest: the row is written once and never
  recomputed, so a later back-view cannot retroactively change history.

Money rounding goes through the canonical `app.tax.acb.cents`, with the mode
stated at each call: budget figures are ROUND_HALF_EVEN (see `spent_between`
for the parity proof), not the canonical half-up.
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.budgets import CARRY_FORWARD, Budget, BudgetCarryover
from app.models.import_batches import ImportBatch
from app.models.transactions import Transaction
from app.services.reports import FxCache
from app.services.txn_filters import (
    countable_tx_conditions,
    today_et,
    transfer_exclusion_conditions,
)
from app.tax.acb import cents

_ET = ZoneInfo("America/Toronto")
_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


def period_start(period: str) -> date:
    """First day of the budget's current window (docs/26 decision 6).

    `today_et()` is the single "today" for spending aggregates (docs/29 F1d), so
    a budget's spent window matches the alert window instead of drifting by a
    day against a UTC clock. The window is bounded ABOVE only by the period
    itself — a row dated later this month is inside the current month for both
    the card and the alert (no `<= today` filter: the two must agree, M61 #3).
    """
    today = today_et()
    return today.replace(month=1, day=1) if period == "yearly" else today.replace(day=1)


def _previous_period_start(period: str, start: date) -> date:
    """First day of the period BEFORE `start` — the source of a carry.

    Monthly: first day of the previous calendar month (2026-03-01 → 2026-02-01).
    Yearly: the previous Jan 1 (never a Feb 29 edge, and a yearly budget's
    leftover is measured over the whole previous year).
    """
    if period == "yearly":
        return start.replace(year=start.year - 1)
    return (start.replace(day=1) - timedelta(days=1)).replace(day=1)


def _spent_conditions(
    user_id: int,
    category_id: int,
    start: date,
    end_exclusive: date | None,
) -> list:
    conditions = [
        *countable_tx_conditions(user_id),
        *transfer_exclusion_conditions(user_id),
        Transaction.category_id == category_id,
        Transaction.date >= start,
    ]
    if end_exclusive is not None:
        # Only the SOURCE window is bounded above: it is closed, so nothing
        # booked into the current period may leak into a carried-in figure.
        conditions.append(Transaction.date < end_exclusive)
    return conditions


def spent_between(
    db: Session,
    user_id: int,
    category_id: int,
    start: date,
    end_exclusive: date | None,
    reporting: str,
    fx: FxCache,
) -> Decimal:
    """Signed reporting-currency spending for a category in `[start, end)`.

    `end_exclusive=None` means "the period is still open" — the current-period
    window deliberately bounds nothing above (docs/26 decision 6, M61 #3).

    Two regressions are pinned here (see tests/test_budgets.py):

    1. **Signed, not absolute.** The old query summed `abs(amount_native)`, so a
       refund (positive row) INFLATED spent instead of offsetting it, and any
       positive row booked into a budgeted category (interest, rebate) counted
       as spending. Net spend now floors at zero.
    2. **Reporting currency, not native.** The old query summed
       `amount_native`, so a USD charge was billed to a CAD budget at its raw
       face value. The stored `cad_amount` mirror is used when present; a row
       without one (e.g. a manual row on a date with no exact-date fx row —
       `cached_cad_amount` mirrors only the exact date) is priced at the newest
       cached rate on or before its date, and an unresolvable row is EXCLUDED
       rather than counted raw (docs/12 population rules). Fx semantics come
       from the shared `FxCache`; this function never fetches.

       Rounding is ROUND_HALF_EVEN, stated explicitly (it was a bare
       `.quantize(_CENT)` reading the process-wide decimal context, which is
       only half-even by accident). PRESERVED, not corrected: the client mirror
       `frontend/src/lib/s3Engine.ts::budgetsProgress` documents this exact mode,
       and `frontend/tests/fixtures/s3-vectors.json` is a golden captured from
       THIS function over the live stack (docs/33 §S3 parity, diffed by
       `node frontend/scripts/parity/parity.ts --slice s3`). Changing the mode
       would silently break that client/server parity contract; moving budgets
       onto the canonical half-up requires `s3Engine.dquantize` and that golden
       to move in the same change.
    """
    # Transfers are not spending (docs/12 §Shared population rules): a row
    # paired as a transfer, or booked to the system `transfers` category, never
    # feeds a budget — including a budget placed ON `transfers`, which then
    # truthfully reads 0.00 spent rather than counting money that only moved.
    conditions = _spent_conditions(user_id, category_id, start, end_exclusive)
    mirrored = db.exec(
        select(func.coalesce(func.sum(Transaction.cad_amount), 0))
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(*conditions, Transaction.cad_amount.is_not(None))
    ).one()
    net = Decimal(str(mirrored))

    fallback_rows = db.exec(
        select(Transaction.amount_native, Transaction.currency, Transaction.date)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(*conditions, Transaction.cad_amount.is_(None))
    ).all()
    for amount_native, currency, day in fallback_rows:
        rate = fx.rate(currency, reporting, day)
        if rate is not None:  # unresolved → excluded, never fetched
            net += Decimal(str(amount_native)) * rate

    return cents(max(-net, _ZERO), rounding=ROUND_HALF_EVEN)


def budget_spent(
    db: Session, user_id: int, category_id: int, period: str, reporting: str, fx: FxCache
) -> Decimal:
    """Signed reporting-currency spending for a category in the current period.

    The current-period window is bounded below only — see `spent_between`.
    """
    return spent_between(db, user_id, category_id, period_start(period), None, reporting, fx)


def carry_in(db: Session, budget: Budget, reporting: str, fx: FxCache) -> Decimal | None:
    """The frozen `carried_in` for the period the budget is currently in.

    Returns `None` — "nothing carries" — when:

    * `carry` is not `forward` (the default; a row that never opted in computes
      exactly the numbers it did before this feature existed, and it never
      writes a snapshot), or
    * the budget did not exist for the WHOLE source period (docs/09: no budget
      in a month, nothing carries — a budget created mid-month, or deleted and
      re-created, has no governed leftover to carry and can never invent one).

    Otherwise the snapshot for `period_start(period)` is read; if it does not
    exist yet it is materialized from the CLOSED source window
    `[previous start, this start)` and committed. Once written, a row is never
    updated: going back a period shows the value as it was at the time
    (audit-honest), and editing the source period afterwards cannot move it.
    """
    if budget.carry != CARRY_FORWARD:
        return None

    target = period_start(budget.period)
    row = db.exec(
        select(BudgetCarryover).where(
            BudgetCarryover.budget_id == budget.id,
            BudgetCarryover.period_start == target,
        )
    ).first()
    if row is not None:
        return row.amount

    # Eligibility (docs/09): the budget must have existed BEFORE the target
    # period began, i.e. it governed the whole source period. Compared in ET —
    # `today_et()` is the single "today" for these windows (docs/29 F1d).
    created = budget.created_at
    if created is None or created.astimezone(_ET).date() >= target:
        return None

    source_start = _previous_period_start(budget.period, target)
    spent = spent_between(
        db, budget.user_id, budget.category_id, source_start, target, reporting, fx
    )
    # Same half-even mode as `spent_between` above, for the same parity reason:
    # both are the money `s3Engine.budgetsProgress` mirrors.
    leftover = cents(budget.amount - spent, rounding=ROUND_HALF_EVEN)

    snapshot = BudgetCarryover(
        user_id=budget.user_id,
        budget_id=budget.id,
        period_start=target,
        amount=leftover,
    )
    db.add(snapshot)
    db.commit()
    return leftover


def pct_ratio(spent: Decimal, spendable: Decimal) -> Decimal | None:
    """`spent ÷ spendable x 100` as an exact Decimal — the M61 #3 / M65 true
    ratio, never clamped (docs/08 §Budgets).

    `None` when `spendable <= 0`: a carried debt that swallows the whole
    assignment leaves no positive denominator, and inventing one (or reporting
    "0%") would be a number the data does not support. The row's own deficit
    line carries the magnitude instead (docs/09 §Budget carry), and the alert
    generator treats the absence as "already over" — never as "under".
    """
    if spendable <= 0:
        return None
    return spent / spendable * 100


def progress(
    amount: Decimal, spent: Decimal, carried: Decimal | None
) -> tuple[Decimal, Decimal, Decimal | None]:
    """`(spendable, remaining, pct_used)` — the one layering of an honest budget
    row (docs/09 §Budget carry):

        spendable  = assigned + carried_in      (the denominator)
        remaining  = spendable - spent_so_far   (what is left to spend)
        pct_used   = spent ÷ spendable x 100    (`null` when spendable ≤ 0)

    `carried is None` (carry off / nothing carried) is byte-for-byte today's
    math: `spendable = amount`, `remaining = amount - spent`, `pct_used` the
    true ratio against `amount` — zero behaviour changes for a row that never
    opted in.
    """
    spendable = amount if carried is None else amount + carried
    remaining = spendable - spent
    if carried is None:
        ratio = spent / amount * _HUNDRED if amount else _ZERO
        return spendable, remaining, ratio
    return spendable, remaining, pct_ratio(spent, spendable)

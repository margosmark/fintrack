"""Spending comparison — month-over-month / year-over-year.

Returns both periods' data with computed deltas.

**Money math is NOT re-implemented here.** This service used to run its own
`sum(abs(amount_native))`, which had two defects the rest of the app does not
share (docs/12 §Population rules):

1. `abs()` turned a REFUND into extra spending, and counted inflow rows as
   spend;
2. `amount_native` is the row's OWN currency, so a USD row and a CAD row were
   added together unconverted.

Both were already fixed in `app/services/insights.py::_category_flows`, which
is now the single reader for "signed spending per category". This service
reuses it, so a refund here offsets the way it does on every other surface,
and a foreign row is priced at its CAD mirror or the newest cached rate.

**Owner-lens expansion (docs/12 §Owner dimension).** `household` widens only
WHO is summed: both windows and the category namespace go through the SAME
insights readers with the flag on, so the comparison keeps one predicate, one
population and one math.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.insights import _spending_buckets, cat_names
from app.services.reports import FxCache


def compare_periods(
    session: Session,
    user_id: int,
    current_start: date,
    current_end: date,
    previous_start: date,
    previous_end: date,
    *,
    household: bool = False,
) -> dict:
    """Compare spending between two periods with per-category deltas.

    `household` widens only who is summed (docs/12 §Owner dimension).
    """
    reporting = get_settings().reporting_currency
    fx = FxCache(session, reporting)

    def rate_of(currency: str, day: date) -> Decimal | None:
        return fx.rate(currency, reporting, day)

    curr = _period_sums(session, user_id, current_start, current_end, rate_of, household=household)
    prev = _period_sums(
        session, user_id, previous_start, previous_end, rate_of, household=household
    )

    # Category names for the population's namespace: the same merged tree every
    # widened household route resolves (a projected row carries the OWNER's
    # ids, docs/43 §6), so a foreign id names itself instead of falling back to
    # `Uncategorized`.
    cats = cat_names(session, user_id, household=household)

    all_cat_ids = set(curr.keys()) | set(prev.keys())
    deltas = []
    for cat_id in all_cat_ids:
        c = curr.get(cat_id, Decimal("0"))
        p = prev.get(cat_id, Decimal("0"))
        change = c - p
        change_pct = float(change / p * 100) if p > 0 else (100.0 if c > 0 else 0.0)
        deltas.append(
            {
                "category_id": cat_id,
                "category_name": cats.get(cat_id, "Uncategorized"),
                "current": c.quantize(Decimal("0.01")),
                "previous": p.quantize(Decimal("0.01")),
                "change": change.quantize(Decimal("0.01")),
                "change_pct": round(change_pct, 1),
            }
        )

    deltas.sort(key=lambda d: abs(d["change"]), reverse=True)

    curr_total = Decimal("0") + sum(curr.values())
    prev_total = Decimal("0") + sum(prev.values())
    total_change = curr_total - prev_total
    total_pct = float(total_change / prev_total * 100) if prev_total > 0 else 0.0

    return {
        "current_period": {
            "start": str(current_start),
            "end": str(current_end),
            "total": curr_total.quantize(Decimal("0.01")),
        },
        "previous_period": {
            "start": str(previous_start),
            "end": str(previous_end),
            "total": prev_total.quantize(Decimal("0.01")),
        },
        "total_change": total_change.quantize(Decimal("0.01")),
        "total_change_pct": round(total_pct, 1),
        "deltas": deltas,
    }


def _period_sums(
    session: Session, user_id: int, start: date, end: date, rate_of, *, household: bool = False
) -> dict[int | None, Decimal]:
    """Signed, FX-aware per-category SPENDING over `[start, end)`.

    A thin alias over `insights._spending_buckets`, kept as a named seam so the
    comparison's two windows read symmetrically at the call site. Values are
    POSITIVE magnitudes of spending; income-only buckets are out, and a refund
    offsets the purchase it belongs with (docs/12).
    """
    return _spending_buckets(session, user_id, start, end, rate_of, household=household)

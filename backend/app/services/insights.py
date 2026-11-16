"""Spending velocity and burn rate insights.

Computes month-over-month deltas, prorated projections, and per-category
trends from committed transactions. Spending is SIGNED and reported in the
configured reporting currency: refunds offset purchases, inflows are not
spending, and unresolvable FX rows are excluded (docs/12 population rules).

**Owner-lens expansion (docs/12 §Owner dimension).** Every population read in
this module takes `household`, which widens WHO is summed to exactly the rows
the caller's own active `sharing_links` row projects (docs/43 §6) — the same
union the three M159/M179 routes already serve. The math (signed net,
per-category floor, LEDGER split rule, staging, FX) is untouched: one
predicate swap, one population, no second reading of "who is in the
household", because `services/reports.py::household_ownership` is the only
builder of that predicate. The category namespace merges the counterpart's
tree under the same flag, so a projected row's OWNER id is named instead of
`Uncategorized`.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import case, func
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.config import get_settings
from app.models.categories import Category
from app.models.import_batches import ImportBatch
from app.models.transactions import Transaction
from app.services.reports import FxCache, household_counterpart, household_ownership
from app.services.txn_filters import (
    countable_tx_conditions,
    countable_tx_conditions_for,
    today_et,
    transfer_exclusion_conditions,
)

_CENT = Decimal("0.01")
_ZERO = Decimal("0")


def _month_range(d: date) -> tuple[date, date]:
    """Return (start, end) of the month containing d."""
    if d.month == 12:
        end = date(d.year + 1, 1, 1)
    else:
        end = date(d.year, d.month + 1, 1)
    return date(d.year, d.month, 1), end


def _spend_cad(
    amount_native: Decimal, currency: str, day: date, cad_amount: Decimal | None, rate_of
) -> Decimal | None:
    """Signed spending contribution of one row, in the reporting currency.

    Returns `None` when the row is not spending (a positive inflow) or cannot be
    priced in the reporting currency (excluded, never fetched). Negative CAD is
    returned as a POSITIVE magnitude; the caller sums magnitudes.
    """
    cad = cad_amount
    if cad is None:
        rate = rate_of(currency, day)
        if rate is None:
            return None  # unresolved FX → excluded (docs/12)
        cad = amount_native * rate
    return -cad if cad < 0 else _ZERO


def _population_conditions(session: Session, user_id: int, *, household: bool) -> list[object]:
    """The WHERE conditions for one insights population.

    `household=False` is the per-user rule every pre-expansion read ran
    (`countable_tx_conditions`). `household=True` substitutes the OWNERSHIP
    predicate `services/reports.py::household_ownership` resolves for this
    caller's own link — the same builder, the same projection, the same union
    the M159/M179 routes use — and everything else (committed-only staging,
    LEDGER split rule) comes from the identical shared lists, so the narrow and
    widened reads cannot drift apart (docs/12 §Owner dimension, docs/43 §6).

    The transfer population rule (docs/12 §Shared population rules) is layered
    onto BOTH branches: an insights figure is a spend figure, and a
    chequing → TFSA move is not spending however wide the scope gets. The
    counterpart's `transfers` category counts too — `household_ownership`
    already returns the counterpart as its second element, and the rule's
    flag-category subquery takes both tenants.
    """
    if not household:
        return [
            *countable_tx_conditions(user_id),
            *transfer_exclusion_conditions(user_id),
        ]
    ownership, counterpart = household_ownership(session, user_id, household=True)
    return [
        *countable_tx_conditions_for(ownership),
        *transfer_exclusion_conditions(user_id, counterpart),
    ]


def compute_insights(session: Session, user_id: int, *, household: bool = False) -> dict:
    """Compute spending velocity insights.

    `household` widens WHO is summed (docs/12 §Owner dimension); every number
    below is computed over that one population with the math untouched.
    """
    today = today_et()
    curr_start, curr_end = _month_range(today)
    days_elapsed = (today - curr_start).days + 1
    days_in_month = (curr_end - curr_start).days

    reporting = get_settings().reporting_currency
    fx = FxCache(session, reporting)

    def rate_of(currency: str, day: date) -> Decimal | None:
        return fx.rate(currency, reporting, day)

    # Current month total spending (through today) / previous month
    curr_total = db_exec_sum(session, user_id, curr_start, today, rate_of, household=household)
    # Previous month
    prev_start = (
        date(curr_start.year, curr_start.month - 1, 1)
        if curr_start.month > 1
        else date(curr_start.year - 1, 12, 1)
    )
    prev_end = curr_start
    prev_total = db_exec_sum(session, user_id, prev_start, prev_end, rate_of, household=household)

    # Prorated projection
    daily_rate = curr_total / days_elapsed if days_elapsed > 0 else _ZERO
    projected = (daily_rate * days_in_month).quantize(_CENT)

    # Month-over-month change
    if prev_total > 0:
        mom_change = float((curr_total - prev_total) / prev_total * 100)
    else:
        mom_change = 0.0

    # Per-category deltas
    category_deltas = _category_deltas(
        session, user_id, curr_start, today, prev_start, prev_end, rate_of, household=household
    )

    return {
        "current_month_spending": curr_total,
        "previous_month_spending": prev_total,
        "month_over_month_pct": round(mom_change, 1),
        "daily_burn_rate": daily_rate.quantize(_CENT),
        "projected_month_end": projected,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "category_deltas": category_deltas,
    }


def db_exec_sum(
    session: Session,
    user_id: int,
    start: date,
    end: date,
    rate_of,
    *,
    household: bool = False,
) -> Decimal:
    """Signed reporting-currency spending over [start, end).

    The old query summed `abs(amount_native)`, which turned a refund into extra
    "spending" and counted inflow rows as spend. Now: the stored `cad_amount`
    mirror is authoritative when present, otherwise the newest cached rate on or
    before the row's date is applied, and unresolvable rows are dropped.

    **M138 (D-1, M133's 2nd P1).** The headline used to net EVERY row in the
    window — payroll included — and then floor at zero, so any month where
    income exceeded spending printed `$0` while the same response's
    `category_deltas` and the same page's budget banner both showed the real
    number. The floor now applies to the sum over SPENDING buckets only: a
    bucket whose rows are all inflows is income, and it never reaches this
    total (docs/08 `GET /reports/insights`). Refunds still offset — they land
    in a bucket that also holds outflows, so that bucket stays in. `$0` now
    means "nothing went out", never "you earned more than you spent".

    `household` widens only who is summed (docs/12 §Owner dimension): the floor
    and the bucket rule are the same arithmetic over the wider population.
    """
    flows = _category_flows(session, user_id, start, end, rate_of, household=household)
    total = sum((bucket.net for bucket in flows.values() if bucket.has_outflow), _ZERO)
    return max(total, _ZERO).quantize(_CENT)


def _category_deltas(
    session: Session,
    user_id: int,
    curr_start: date,
    curr_end: date,
    prev_start: date,
    prev_end: date,
    rate_of,
    *,
    household: bool = False,
) -> list[dict]:
    """Compare per-category spending between current and previous month."""
    curr = _spending_buckets(session, user_id, curr_start, curr_end, rate_of, household=household)
    prev = _spending_buckets(session, user_id, prev_start, prev_end, rate_of, household=household)
    all_cats = set(curr.keys()) | set(prev.keys())
    names = cat_names(session, user_id, household=household)
    deltas = []
    for cat_id in all_cats:
        c = curr.get(cat_id, _ZERO)
        p = prev.get(cat_id, _ZERO)
        change_pct = float((c - p) / p * 100) if p > 0 else (100.0 if c > 0 else 0.0)
        deltas.append(
            {
                "category_id": cat_id,
                "category_name": names.get(cat_id, "Unknown"),
                "current": c,
                "previous": p,
                "change_pct": round(change_pct, 1),
            }
        )
    deltas.sort(key=lambda d: abs(d["change_pct"]), reverse=True)
    return deltas[:10]  # top 10 biggest changes


@dataclass(frozen=True)
class _Bucket:
    """One category's net over the window, plus whether it is a SPENDING bucket.

    `has_outflow` is the M138 discriminator: True when at least one row in the
    bucket went OUT. A bucket whose rows are all inflows (payroll, interest,
    a transfer credit) is income — it reports a net, but it is not spending and
    is excluded from the headline and the delta chips. A refund shares its
    bucket with the purchase it offsets, so that bucket keeps its outflow and
    the refund keeps netting against it.
    """

    net: Decimal
    has_outflow: bool


def _category_flows(
    session: Session, user_id: int, start: date, end: date, rate_of, *, household: bool = False
) -> dict[int | None, _Bucket]:
    """Per-category `(net, has_outflow)` over `[start, end)` — the ONE reader.

    Both the headline (`db_exec_sum`) and the delta chips derive from this, so
    "the parts sum to the whole" is structural rather than a coincidence the
    tests have to keep proving (docs/12 §Population rules).

    `household` swaps the OWNERSHIP conjunct only — same staging rule, same
    LEDGER split rule, same date window, same sign convention (docs/12
    §Owner dimension: one param, one population, one math).
    """
    conditions = [
        *_population_conditions(session, user_id, household=household),
        Transaction.date >= start,
        Transaction.date < end,
    ]
    rows = session.exec(
        select(
            Transaction.category_id,
            func.coalesce(func.sum(Transaction.cad_amount), 0),
            func.coalesce(func.sum(case((Transaction.cad_amount < 0, 1), else_=0)), 0),
        )
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(*conditions, Transaction.cad_amount.is_not(None))
        .group_by(Transaction.category_id)
    ).all()

    # Accumulate the SIGNED net per category. Do NOT clamp here: a per-category
    # floor would discard a refund that offsets a purchase booked to a
    # DIFFERENT category, and the breakdown would then sum to more than the
    # total above it (docs/12). A category that nets to a credit reports
    # NEGATIVE — that is a credit, not spending, and saying "0.00 spent" would
    # hide it.
    nets: dict[int | None, Decimal] = {}
    spend_buckets: set[int | None] = set()
    for category_id, total, outflow_rows in rows:
        nets[category_id] = -Decimal(str(total))
        if outflow_rows:
            spend_buckets.add(category_id)

    # Rows without a stored CAD mirror price at the newest cached rate.
    fallback_rows = session.exec(
        select(
            Transaction.category_id,
            Transaction.amount_native,
            Transaction.currency,
            Transaction.date,
        )
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(*conditions, Transaction.cad_amount.is_(None))
    ).all()
    for category_id, amount_native, currency, day in fallback_rows:
        contribution = _spend_cad(Decimal(str(amount_native)), currency, day, None, rate_of)
        if contribution is None:
            continue
        nets[category_id] = nets.get(category_id, _ZERO) + contribution
        if contribution > 0:
            spend_buckets.add(category_id)

    return {
        category_id: _Bucket(net=value.quantize(_CENT), has_outflow=category_id in spend_buckets)
        for category_id, value in nets.items()
    }


def _spending_buckets(
    session: Session, user_id: int, start: date, end: date, rate_of, *, household: bool = False
) -> dict[int | None, Decimal]:
    """`{category: net}` for SPENDING buckets only — income-only buckets out."""
    return {
        category_id: bucket.net
        for category_id, bucket in _category_flows(
            session, user_id, start, end, rate_of, household=household
        ).items()
        if bucket.has_outflow
    }


def cat_names(session: Session, user_id: int, *, household: bool = False) -> dict[int, str]:
    """`{category id: name}` for the population's namespace.

    Under `household` the counterpart's tree is layered over the caller's, the
    SAME way every widened route resolves names (docs/12 §Owner dimension,
    docs/43 §6): a projected row carries the OWNER's category ids, so the
    counterpart's entry wins a bare-id conflict and an id only one side carries
    keeps its own name. Without it a household delta chip would label a foreign
    id `Unknown`.
    """
    rows = session.exec(select(Category.id, Category.name).where(Category.user_id == user_id)).all()
    names = {cid: name for cid, name in rows}
    counterpart_id = household_counterpart(session, user_id, household=household)
    if counterpart_id is not None:
        rows = session.exec(
            select(Category.id, Category.name).where(Category.user_id == counterpart_id)
        ).all()
        names.update({cid: name for cid, name in rows})
    return names

"""M96 B2 — passive recurring-transactions detection (docs/09 §Recurring prompt).

**Local-only by construction.** This module reads rows the caller already
loaded and computes in-process: no LLM, no cloud call, no new dependency, and
no network client in its import list (pinned by
`tests/test_recurring.py::test_detection_module_makes_no_network_calls`).
Everything it needs — the grouping normalization, the committed-row predicate —
is reused from the modules that already own those rules.

The heuristic, in the order the spec asks for it:

1. **Same normalized merchant** — `services/subscriptions.py::normalize_payee_key`
   (uppercase, strip a trailing ``#?digits``), the ONE grouping normalization
   here, so ``SUPERSTORE 1234`` and ``Superstore #1234`` are the same merchant
   and the M27 tracker and this pass never disagree about identity.
2. **Amount band** — rows cluster per merchant by a ±15% band around the
   cluster's first amount, same sign required (a refund is not a repeat).
   An exactly-repeating amount is the degenerate case the same test covers.
3. **Cadence fit** over a date-sorted cluster: every gap must fit one pattern
   — monthly-same-day (day-of-month consistent, gaps 26-32), then the fixed
   28 / 30 / 31, 14 and 7-day patterns. One broken gap and the group is not
   recurring: a cadence that only *usually* holds is how a nag gets invented.
4. **≥ 3 occurrences** inside the caller's lookback window.

Output is a candidate GROUP (not a stored row): the prompt is computed on
demand, the user's accept writes the tags, the user's decline writes
`recurring_suppressions` — detection itself is never persisted.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.models.transactions import RecurringCadence
from app.services.match_terms import as_is, match_terms
from app.services.subscriptions import normalize_payee_key

# One year + a month: a 12-month lookback catches an annual charge's twin
# without letting years-old imports masquerade as an active commitment
# (the M27 tracker bounds itself the same way).
LOOKBACK_DAYS = 400
MIN_OCCURRENCES = 3
# ±15% of the cluster's first amount (spec: "±15% or exactly-repeating").
AMOUNT_BAND = Decimal("0.15")
# Average month length: what turns a per-occurrence amount into a monthly one.
AVERAGE_MONTH_DAYS = Decimal("30.4375")
_MONEY = Decimal("0.01")

# Days the fixed fits tolerate: a weekly charge can land a day off, a 28-day
# cycle cannot drift further than a day without becoming a different cadence.
_FIXED_FITS: tuple[tuple[RecurringCadence, int, int], ...] = (
    (RecurringCadence.four_weekly, 28, 1),
    (RecurringCadence.monthly, 30, 1),
    (RecurringCadence.monthly, 31, 1),
    (RecurringCadence.biweekly, 14, 2),
    (RecurringCadence.weekly, 7, 2),
)
# Monthly gaps: 28-31 real days depending on the month.
_MONTHLY_GAP_MIN = 26
_MONTHLY_GAP_MAX = 32
# Day-of-month spread a monthly cycle can show (Jan 31 → Feb 28 → Mar 31).
_DAY_OF_MONTH_SPREAD = 4

_PERIOD_DAYS: dict[RecurringCadence, Decimal] = {
    RecurringCadence.weekly: Decimal("7"),
    RecurringCadence.biweekly: Decimal("14"),
    RecurringCadence.four_weekly: Decimal("28"),
    RecurringCadence.monthly: AVERAGE_MONTH_DAYS,
}


@dataclass(frozen=True)
class LedgerRow:
    """One committed ledger row as the detector sees it."""

    id: int
    date: date
    payee: str
    amount: Decimal


@dataclass(frozen=True)
class RecurringGroup:
    """A candidate group — one merchant + one fitted cadence."""

    group_key: str
    payee: str
    merchant_key: str
    cadence: RecurringCadence
    transaction_ids: tuple[int, ...]
    occurrence_count: int
    monthly_total: Decimal
    first_date: date
    last_date: date
    next_expected_date: date


def merchant_key(payee: str) -> str:
    """Grouping identity for one merchant (the M27 normalization)."""
    return normalize_payee_key(payee or "")


def merchant_answered(group: RecurringGroup, tagged_merchants: Sequence[str]) -> bool:
    """Has the user already answered for this candidate's merchant? (M96/M121)

    True when the group's merchant key is one of the merchants that already
    carries a tagged row — "the decision is final, the prompt does not come
    back" (docs/09 §Recurring prompt).

    Written as the SHARED `services/match_terms.py::match_terms` ANY-of
    contract rather than a bare `in` (M121 D3, docs/25 §M121): this is the
    recurring detector's matcher, and it is now the SAME function a bill's
    term list and the categorization rules call, so "matches this merchant"
    means one thing in all three places. Both sides are already
    `normalize_payee_key` outputs, hence `as_is`; mode `any` + comparison
    `exact` over equal keys answers exactly what the `in` test did.
    """
    return match_terms(tagged_merchants, "any", as_is, group.merchant_key, "exact")


def make_group_key(payee: str, cadence: RecurringCadence) -> str:
    """Stable suppression/tag key: merchant + cadence, deliberately amount-free.

    An amount drift inside the ±15% band must not mint a NEW key, or a group
    the user already answered would re-prompt (docs/09 §Recurring prompt).
    Capped at 120 chars to fit `recurring_suppressions.group_key`.
    """
    key = merchant_key(payee)
    return f"{key[:100]}|{cadence.value}"


#: M138 (docs/09 §Bills you probably have) — the second suppression SOURCE.
#:
#: Both surfaces write `recurring_suppressions.group_key`, so the key itself
#: carries which surface the "no" was given on. `make_group_key` ends in a
#: `RecurringCadence` value (`weekly | biweekly | four_weekly | monthly`);
#: this one ends in `SUBSCRIPTIONS_SOURCE`, which is deliberately NOT one of
#: them — a dashboard dismissal can therefore never equal a prompt-group key,
#: and the M96 card keeps suggesting a merchant the subscriptions card was
#: told to drop (the boundary `test_dashboard_dismissal_leaves_the_m96_prompt_alone`
#: pins it). Same merchant normalization, same amount-free shape, same
#: 120-char ceiling.
SUBSCRIPTIONS_SOURCE = "subscriptions"


def make_subscriptions_group_key(payee: str) -> str:
    """Suppression key for ONE dismissed "Bills you probably have" suggestion.

    Per-normalized-payee on purpose: the subscriptions card has no cadence
    dimension the user answers (they are answering "this is not a bill", not
    "this is not monthly"), so a cadence in the key would make the same
    payee reappear under a different fit. Amount-free for the same reason
    `make_group_key` is.
    """
    key = merchant_key(payee)
    return f"{key[:100]}|{SUBSCRIPTIONS_SOURCE}"


def rekey_suppression(group_key: str) -> str:
    """Read a STORED suppression key through today's normalization (M165).

    `recurring_suppressions.group_key` holds the key exactly as it looked when
    the user answered, so a normalization that learns something later would
    silently un-answer every previously declined group — the "a decision
    cannot be re-asked" promise in docs/09 breaking on deploy day. Re-reading
    the merchant segment through the SAME `normalize_payee_key` the key is
    built with maps an older spelling onto the current one; the `|<cadence>` /
    `|subscriptions` suffix is copied verbatim (it was never normalized).

    Pure function of the stored value and idempotent: a key already written in
    the current form comes back unchanged, so this needs no data migration
    (and so rewriting rows in place could never trip
    `UNIQUE(user_id, group_key)`).

    Keys longer than `make_group_key`'s 100-char cap are stored already
    truncated, so a re-read of a TRUNCATED merchant can only re-normalize the
    prefix it kept — the same approximation the cap itself makes when two
    payees share their first 100 normalized characters.
    """
    merchant, separator, suffix = group_key.rpartition("|")
    if not separator:
        return group_key
    return f"{normalize_payee_key(merchant)}|{suffix}"


def _same_band(reference: Decimal, amount: Decimal) -> bool:
    if reference == 0 or amount == 0:
        return reference == amount
    if (reference < 0) != (amount < 0):
        return False
    return abs(amount - reference) <= AMOUNT_BAND * abs(reference)


def _cluster_by_amount(rows: Sequence[LedgerRow]) -> list[list[LedgerRow]]:
    """Greedy ±15% bands inside one merchant, in date order."""
    clusters: list[list[LedgerRow]] = []
    for row in sorted(rows, key=lambda r: (r.date, r.id)):
        for cluster in clusters:
            if _same_band(cluster[0].amount, row.amount):
                cluster.append(row)
                break
        else:
            clusters.append([row])
    return clusters


def _is_monthly_same_day(dates: Sequence[date]) -> bool:
    days = [d.day for d in dates]
    return max(days) - min(days) <= _DAY_OF_MONTH_SPREAD


def _fit_cadence(cluster: Sequence[LedgerRow]) -> RecurringCadence | None:
    """The cadence every consecutive gap fits, or None (one break = no group)."""
    dates = [row.date for row in cluster]
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    if not gaps:
        return None
    # Monthly FIRST: a 30/31-day cycle and a 28-day cycle have overlapping
    # gaps, and only the day-of-month spread separates them (a 28-day cycle
    # walks backwards through the month; a monthly one does not).
    if all(_MONTHLY_GAP_MIN <= gap <= _MONTHLY_GAP_MAX for gap in gaps) and _is_monthly_same_day(
        dates
    ):
        return RecurringCadence.monthly
    for cadence, days, tolerance in _FIXED_FITS:
        if all(abs(gap - days) <= tolerance for gap in gaps):
            return cadence
    return None


def monthly_total(amounts: Sequence[Decimal], cadence: RecurringCadence) -> Decimal:
    """Signed monthly commitment: mean occurrence x occurrences per month.

    Signed on purpose — a payroll credit and a subscription debit are both
    commitments, and the Budgets label prints the sign it is given.
    """
    if not amounts:
        return Decimal("0.00")
    mean = sum(amounts) / len(amounts)
    period = _PERIOD_DAYS[cadence]
    return (mean * (AVERAGE_MONTH_DAYS / period)).quantize(_MONEY)


def next_expected_date(last: date, cadence: RecurringCadence) -> date:
    """Next occurrence after the last one: same day next month for monthly."""
    if cadence is not RecurringCadence.monthly:
        return last + timedelta(days=int(_PERIOD_DAYS[cadence]))
    year = last.year + (last.month // 12)
    month = last.month % 12 + 1
    return date(year, month, min(last.day, calendar.monthrange(year, month)[1]))


def detect_recurring(rows: Iterable[LedgerRow]) -> list[RecurringGroup]:
    """Candidate groups over the rows the caller loaded, biggest first."""
    by_merchant: dict[str, list[LedgerRow]] = defaultdict(list)
    for row in rows:
        key = merchant_key(row.payee)
        if not key:
            continue
        by_merchant[key].append(row)

    groups: list[RecurringGroup] = []
    for merchant, merchant_rows in by_merchant.items():
        for cluster in _cluster_by_amount(merchant_rows):
            if len(cluster) < MIN_OCCURRENCES:
                continue
            cadence = _fit_cadence(cluster)
            if cadence is None:
                continue
            ordered = sorted(cluster, key=lambda r: (r.date, r.id))
            amounts = [row.amount for row in ordered]
            first, last = ordered[0].date, ordered[-1].date
            groups.append(
                RecurringGroup(
                    group_key=make_group_key(ordered[0].payee, cadence),
                    payee=ordered[0].payee,
                    merchant_key=merchant,
                    cadence=cadence,
                    transaction_ids=tuple(row.id for row in ordered),
                    occurrence_count=len(ordered),
                    monthly_total=monthly_total(amounts, cadence),
                    first_date=first,
                    last_date=last,
                    next_expected_date=next_expected_date(last, cadence),
                )
            )
    groups.sort(key=lambda g: abs(g.monthly_total), reverse=True)
    return groups

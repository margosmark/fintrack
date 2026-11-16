"""The ONE bill-recurrence rule (M180, docs/25 §M180, docs/36 decision 5).

A bill's schedule is **an anchor day-of-month plus a whole-month cadence**, and
every surface that projects a bill forward reads it from here:

* `api/bill_reminders.py::mark_paid` — the write (`next_due`);
* `services/cashflow_calendar.py` — the month grid (`bill_occurrences`);
* `services/confirmed_subscriptions.py` — the confirmed set's next date
  (`bill_next_on_or_after`).

**Why an anchor, and not just the stored date.** `bill_reminders.next_due_date`
is a real calendar date, so a bill due on the 31st cannot store the 31st in
February. Advancing from the previously CLAMPED date therefore loses the
schedule: Jan 31 → Feb 28 → **Mar 28** (the 31st is gone forever), and a
month-day that does not exist in the target month used to be written verbatim
(`2026-04-31` quarterly, `2029-02-29` yearly) — a value `date.fromisoformat`
refuses, which made `mark-paid` a 500 and silently dropped the bill from the
calendar. The anchor is the day the schedule REPEATS on
(`bill_reminders.anchor_day`, migration `0054_bill_due_anchor`), so month-end
clamping happens on the projection only: Jan 31 → Feb 28 → **Mar 31** →
Apr 30 → May 31, and a leap-day yearly bill (Feb 29) moves to Feb 28 in a
common year and **back to Feb 29** in the next leap year.

`anchor_day` is `NULL` exactly for rows that predate the column and were never
written by an API path (fixtures, a seed, a row whose stored string is not a
date at all). `NULL` means "the stored date's own day is the anchor" — the
pre-M180 behaviour, so such a row can never clamp differently from before, and
`mark_paid` pins the anchor the first time it advances one.

Only whole months: `monthly` = 1, `quarterly` = 3, `yearly` = 12. A cadence
that is not in that table (`one_time`, or anything unknown) never repeats, so
`next_due` answers the stored date itself and `occurrences` answers it once —
the same shape `docs/36` decision 5 pins for `one_time`.
"""

from __future__ import annotations

import calendar
from datetime import date

from app.models.bill_reminders import BillReminder

#: Cadences that advance by whole months. `one_time` is absent because it never
#: repeats (docs/25 schema, docs/36 decision 5).
STEP_MONTHS: dict[str, int] = {"monthly": 1, "quarterly": 3, "yearly": 12}

#: Hard bound on the fast-forward/collect loops. The cash-flow calendar's
#: request band is 1900-2200 (docs/36 decision 2), so this is unreachable in
#: practice — it only guarantees a corrupt row can never hang a request.
MAX_STEPS = 10_000


def anchor_day_for(due: date, anchor_day: int | None) -> int:
    """The day of the month this schedule repeats on.

    `NULL` (an un-anchored row — see the module docstring) means the stored
    date's own day, which is exactly what the pre-M180 projection did.
    """
    return due.day if anchor_day is None else anchor_day


def stored_due(raw: str | None) -> date | None:
    """`bill_reminders.next_due_date` (a `VARCHAR(10)`) as a real date, or `None`.

    The ONE way to read that column, so the three surfaces that project a bill
    forward agree on what an unreadable value means: `None` — the caller skips
    the bill rather than raising. `mark-paid` is the one caller that must NOT
    skip silently (the user asked for a write), so it turns the same `None` into
    the documented 422 `invalid_due_date` envelope (docs/25).
    """
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def advance_months(due: date, step_months: int, anchor_day: int | None = None) -> date:
    """`due` shifted by `step_months` whole months, day-of-month clamped.

    The day is the ANCHOR's (or `due`'s own when there is no anchor), clamped
    to the target month's length: 2026-01-31 + 1 month → 2026-02-28, and with
    the anchor kept, + 2 months → 2026-03-31. Clamping can never produce a
    non-existent date, because `calendar.monthrange` is the only thing that
    decides the day.
    """
    month_index = due.month - 1 + step_months
    year = due.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor_day_for(due, anchor_day), calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_due(due: date, frequency: str, anchor_day: int | None = None) -> date:
    """The occurrence AFTER `due` for `frequency` — `mark-paid`'s rule.

    A cadence with no whole-month step (`one_time`, or an unknown string) has
    no next occurrence and answers `due` itself, exactly as the pre-M180
    `_advance_due_date` did. The caller is responsible for `due` being a real
    date: `mark-paid` reads the stored column through `stored_due` and answers
    422 `invalid_due_date` when it is not one.
    """
    step = STEP_MONTHS.get(frequency)
    if step is None:
        return due
    return advance_months(due, step, anchor_day)


def occurrences(
    due: date, frequency: str, anchor_day: int | None, start: date, end: date
) -> list[date]:
    """Every occurrence of the schedule in `[start, end]`, ascending.

    **Forward projection only**: nothing before `due` is reconstructed — bills
    carry no payment history (docs/25 decision 4), so months before a bill's
    `next_due_date` are not replayed from its cadence (docs/36 decision 5).

    Each occurrence is computed from `due` plus `k` WHOLE periods, never from
    the previously emitted one, which is what keeps a clamped month from
    poisoning the rest of the series (the anchor supplies the day).
    """
    step = STEP_MONTHS.get(frequency)
    if step is None:
        # one_time (and any unknown cadence): exactly the stored date.
        return [due] if start <= due <= end else []

    def occurrence(periods: int) -> date:
        return advance_months(due, periods * step, anchor_day)

    periods = 0
    while occurrence(periods) < start:
        periods += 1
        if periods > MAX_STEPS:
            return []

    out: list[date] = []
    while periods <= MAX_STEPS:
        day = occurrence(periods)
        if day > end:
            break
        out.append(day)
        periods += 1
    return out


def first_on_or_after(
    due: date, frequency: str, anchor_day: int | None, today: date
) -> date | None:
    """The first occurrence on or after `today` — the confirmed-set read.

    Same forward-only rule as `occurrences`, stepped by `frequency` until it
    reaches `today`; `one_time` answers `due` itself (the caller drops it when
    `due < today`, the same way it drops a non-repeating bill that has passed).
    `None` means the bounded loop gave up (a corrupt row, unreachable inside
    the 1900-2200 band) — the caller skips that bill rather than hanging.
    """
    step = STEP_MONTHS.get(frequency)
    if step is None:
        return due
    periods = 0
    while True:
        day = advance_months(due, periods * step, anchor_day)
        if day >= today:
            return day
        periods += 1
        if periods > MAX_STEPS:
            return None


# --------------------------------------------------------------------------- #
# Row-level readers — the whole contract for a caller that only holds a row
# --------------------------------------------------------------------------- #


def bill_occurrences(bill: BillReminder, start: date, end: date) -> list[date]:
    """Every date `bill` falls on in `[start, end]` — the month-grid contract.

    Reads the three columns that describe the schedule
    (`next_due_date` + `frequency` + `anchor_day`) and answers `[]` for a row
    whose stored date is not a date, so a caller never has to parse the
    `VARCHAR(10)` itself nor decide what an unreadable one means. Callers that
    render a grid (`services/cashflow_calendar.py`, docs/36 decision 5) get
    forward-only projection and the anchor's day in one call.
    """
    due = stored_due(bill.next_due_date)
    if due is None:
        return []
    return occurrences(due, bill.frequency, bill.anchor_day, start, end)


def bill_next_on_or_after(bill: BillReminder, today: date) -> date | None:
    """`bill`'s first occurrence on or after `today`, or `None`.

    The confirmed-set contract (`services/confirmed_subscriptions.py`,
    docs/36 §M170): `None` for a row whose stored date is unreadable AND for a
    non-repeating bill (`one_time`, or an unknown cadence) whose date has
    already passed — neither has an upcoming occurrence.
    """
    due = stored_due(bill.next_due_date)
    if due is None:
        return None
    if bill.frequency not in STEP_MONTHS:
        return due if due >= today else None
    return first_on_or_after(due, bill.frequency, bill.anchor_day, today)

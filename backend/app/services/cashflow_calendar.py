"""Cash-flow calendar (docs/36) — one month grid over three existing sources.

Read-only composition of the app's forward-looking cash-flow math:

* **bills** — `BillReminder` rows (docs/25), projected forward from
  `next_due_date` by their stored cadence;
* **dividends** — `services/dividend_calendar.py` (M29), one
  `next_expected_date` per security;
* **subscriptions** — `services/confirmed_subscriptions.py` (M170), one
  `next_expected` per **user-confirmed** subscription: an active bill or a
  recurring group the user accepted. Never the detector's guesses (below).

There is **no detection of its own** here (a second detector would disagree
with the surfaces that own these numbers), **no fx read and no network**
(docs/07, docs/12), and every query is user-scoped (docs/01 tenancy).

**M170 follow-up (docs/36 §Currency): an amount is never printed under a
currency it was not recorded in.** Every event carries `amount` + `currency`
together or neither: bills are reporting-currency by schema and dividends are
CAD by convention, while an accepted recurring group is stated in the one
currency its tagged rows agree on. A group that is MIXED-currency or whose
currency is unknown arrives from the query as `None`/`None` and the client
renders the app's "no honest figure" dash — the native figure is never
relabelled CAD, and converting it would need the fx read this path refuses.

**M170: this surface states facts, so it only reads confirmed state.** Until
M170 the `subscription` series was `services/subscriptions.py::detect_subscriptions`
verbatim — the M27 detector's guess, with no cross-check against bills, no
suppression read and no dedup against the Bills series already on this same
grid. That is how Mark got `Google*Youtubeprmium -$25.98` (a guess) rendering
directly under his real `Google *Youtubeprmium -$25.98` bill, plus a bare
`Electronic Funds Transfer -$359.08`: two sources feeding one calendar, one of
them a prompt. Detector output belongs on the surfaces whose job is asking
(`/txns`, the dashboard's "Bills you probably have" card) — M133's film.ca
verdict for the dashboard, now extended here.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.bill_reminders import BillReminder
from app.services.bill_schedule import bill_occurrences
from app.services.confirmed_subscriptions import (
    SOURCE_RECURRING,
    SOURCE_TAG,
    confirmed_subscriptions,
)
from app.services.dividend_calendar import compute_dividend_calendar

KIND_BILL = "bill"
KIND_DIVIDEND = "dividend"
KIND_SUBSCRIPTION = "subscription"

#: Deterministic within-day order (docs/36 decision 9).
_KIND_RANK = {KIND_BILL: 0, KIND_DIVIDEND: 1, KIND_SUBSCRIPTION: 2}


def month_bounds(year: int, month: int) -> tuple[dt.date, dt.date]:
    """First and last calendar day of `year`-`month` (both validated upstream)."""
    start = dt.date(year, month, 1)
    if month == 12:
        end = dt.date(year, 12, 31)
    else:
        end = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return start, end


def _status(day: dt.date, today: dt.date) -> str:
    """Day-relative status against the single `today_et()` clock (docs/36
    decision 7; docs/29 F1d)."""
    if day < today:
        return "past"
    if day == today:
        return "today"
    return "upcoming"


def _day_range(start: dt.date, end: dt.date) -> list[dt.date]:
    days: list[dt.date] = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += dt.timedelta(days=1)
    return days


def _normalize_day(value: object) -> dt.date | None:
    """Projectors hand back `date`s; tolerate a datetime and reject anything
    else rather than crashing the endpoint."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None


def build_cashflow_calendar(
    session: Session,
    user_id: int,
    *,
    month_start: dt.date,
    month_end: dt.date,
    today: dt.date,
) -> dict:
    """Zero-filled `{month, today, days, confirmed_subscription_count}` payload
    for docs/36.

    All three sources are user-scoped and committed-only by construction
    (`compute_dividend_calendar` and the confirmed-subscription query apply the
    shared predicates; bills are manual rows), so staged import rows never
    appear.

    `confirmed_subscription_count` is the size of the user's WHOLE confirmed
    set (active bills + accepted recurring groups, deduped) — a fact about the
    user, not about this month. The month's grid answers "which of them fall
    here"; this count is what lets the client tell "no subscription payments
    this month" apart from "this user has no confirmed subscriptions at all"
    and render the honest empty state instead of silence (M170, docs/36
    §M170).
    """
    buckets: dict[dt.date, list[dict]] = {day: [] for day in _day_range(month_start, month_end)}

    def add(day_value: object, event: dict) -> None:
        day = _normalize_day(day_value)
        if day is None or day not in buckets:
            return
        event["status"] = _status(day, today)
        buckets[day].append(event)

    # --- bills (docs/25): enabled only, projected forward by cadence --------
    bills = session.exec(
        select(BillReminder).where(
            BillReminder.user_id == user_id,
            BillReminder.enabled.is_(True),
        )
    ).all()
    for bill in bills:
        amount: Decimal | None = None if bill.amount is None else -bill.amount
        # The projection is `services/bill_schedule.py::bill_occurrences` — the
        # ONE bill-recurrence rule (M180), which `mark-paid` and the
        # confirmed-subscription query also read, so the grid can never clamp a
        # schedule differently from the write that advances it. The helper
        # answers both halves of the CALENDAR's policy (docs/36 decision 5):
        # months before `next_due_date` are never reconstructed (bills do not
        # track payment history, docs/25 decision 4), and an unreadable stored
        # date yields no events instead of raising.
        for day in bill_occurrences(bill, month_start, month_end):
            add(
                day,
                {
                    "kind": KIND_BILL,
                    "label": bill.name,
                    "amount": amount,
                    # Bills are reporting-currency by schema (docs/25), and a
                    # currency with no figure denominate nothing (docs/36
                    # §Currency).
                    "currency": "CAD" if amount is not None else None,
                    "source_id": str(bill.id),
                },
            )

    # --- dividends (M29): exactly the projected next payment date -----------
    calendar_view = compute_dividend_calendar(session, user_id, today=today)
    for entry in calendar_view["upcoming"]:
        add(
            entry["next_expected_date"],
            {
                "kind": KIND_DIVIDEND,
                "label": f"{entry['symbol']} dividend",
                # M29's average remains its native mean; only state it when the
                # contributing rows agree on a denomination (docs/36 §Currency).
                "amount": entry["avg_amount"],
                "currency": entry["currency"] if entry["avg_amount"] is not None else None,
                "source_id": str(entry["security_id"]),
            },
        )

    # --- subscriptions (M170): CONFIRMED sources only -----------------------
    # The user's own assertions — an active bill, a recurring group they
    # accepted, or (M174) a group every row of which carries one of their
    # "recurring" tags — minus what the Bills series above already renders: one
    # subscription answers once, in the Bills color. Detector guesses never
    # reach this surface (docs/36 §M170); they belong on the prompt cards.
    # An entry confirmed by a BILL is skipped here because the Bills series
    # above already placed it; the two remaining sources share the
    # `subscription` kind, so a client needs no third vocabulary (M174).
    # The amount and its currency are the query's own answer, passed through
    # untouched: this endpoint never converts (docs/07) and never guesses a
    # denomination — a mixed/unknown group arrives as `None`/`None` and is
    # rendered as the app's "no honest figure" dash (docs/36 §Currency).
    confirmed = confirmed_subscriptions(session, user_id, today=today)
    for subscription in confirmed:
        if subscription.source not in (SOURCE_RECURRING, SOURCE_TAG):
            continue
        add(
            subscription.next_expected,
            {
                "kind": KIND_SUBSCRIPTION,
                "label": subscription.payee,
                "amount": subscription.amount,
                "currency": subscription.currency,
                "source_id": subscription.payee,
            },
        )

    days = []
    for day in sorted(buckets):
        events = sorted(
            buckets[day],
            key=lambda e: (_KIND_RANK[e["kind"]], e["label"], e["source_id"]),
        )
        days.append({"date": day, "events": events})

    return {
        "month": f"{month_start.year:04d}-{month_start.month:02d}",
        "today": today,
        "days": days,
        "confirmed_subscription_count": len(confirmed),
    }

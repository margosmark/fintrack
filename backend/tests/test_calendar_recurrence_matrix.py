"""Calendar recurrence ACCEPTANCE MATRIX — the four boundaries, one rule.

M170 (the calendar states each amount in its OWN currency) and M180 (a bill
keeps its own schedule day across a clamped month) landed as two separate
fixes, each pinned by its own module: `tests/test_bill_schedule.py` (the rule
as arithmetic, as the API's writes, as the row readers) and
`tests/test_cashflow_calendar.py` (the route's composition, the currency
cases). This module is the CONSOLIDATION the two of them left implicit: ONE
table-driven matrix over the boundary classes they share, every row exercised
against the SAME three surfaces of the ONE rule
(`app/services/bill_schedule.py`) —

* the **write path** — `next_due`, behind `POST /bills/{id}/mark-paid`;
* the **grid** — `occurrences` / `bill_occurrences`, behind
  `GET /reports/cashflow-calendar`;
* the **confirmed set** — `first_on_or_after` / `bill_next_on_or_after`,
  behind `services/confirmed_subscriptions.py`.

so a future divergence between the surfaces fails HERE, in one place, instead
of in whichever caller happened to change. Per-boundary detail stays in the
two modules above; this one pins the boundaries and the AGREEMENT.

The four boundary classes (the matrix rows carry their own `boundary` label):

1. **month-end** — a 29th/30th/31st schedule in every month that lacks the
   day, and the day coming BACK the next month that has it (the M180 defect:
   advancing from the previously CLAMPED date lost the 31st after one month).
2. **leap-day** — Feb 29 as a yearly schedule AND as a monthly anchor's day
   in February, both returning in the next leap year.
3. **DST** — the grid's day boundary is `today_et()` (America/Toronto), so a
   local day is 23 or 25 hours long twice a year. The clock must be
   zone-aware (a fixed UTC-4/UTC-5 estimate answers the wrong DATE around
   local midnight, which is a bill landing a day early or late), and a
   23/25-hour day must be neither dropped nor duplicated by the grid.
4. **currency** — an event's `amount` and `currency` are an all-or-nothing
   pair (M170): the figure is stated in the code its rows agree on, in ANY
   ISO code, and a mixed/unknown group states neither rather than relabelling
   a native figure CAD. No conversion anywhere on the path.

The matrix also carries a regression test for accepted-group month-end
anchor: `_group_next_date` derives the newest charge day and calls the shared
whole-month schedule rule. M174 tag-marked groups now use the same projection.
The accept route continues to store a valid clamped date; both sources project
April 30 for a January 31 monthly group.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.bill_reminders import BillReminder
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.models.transactions import RecurringCadence, Transaction
from app.services import bill_schedule, cashflow_calendar, txn_filters
from app.services.bill_schedule import (
    bill_next_on_or_after,
    bill_occurrences,
    first_on_or_after,
    next_due,
    occurrences,
)
from app.services.confirmed_subscriptions import confirmed_subscriptions
from app.services.recurring import next_expected_date

API = "/api/v1"
CAL = f"{API}/reports/cashflow-calendar"
BILLS = f"{API}/bills"
_ET = ZoneInfo("America/Toronto")
_DAY = dt.timedelta(days=1)

#: The clock bindings the calendar route reads (`conftest._TODAY_BINDINGS`,
#: narrowed to the four the route can reach). `today_et` is imported BY NAME
#: into each module, so freezing the clock means patching every binding.
_CALENDAR_CLOCK_BINDINGS = (
    "app.services.txn_filters.today_et",
    "app.services.confirmed_subscriptions.today_et",
    "app.services.dividend_calendar.today_et",
    "app.api.reports.today_et",
)


# --------------------------------------------------------------------------- #
# Fixtures and seeding helpers (the shapes the two sibling modules use)
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every input here is a cached row; a fetch attempt is a test failure."""

    def forbidden(*args, **kwargs):
        raise AssertionError("network fetch attempted although all data is cached")

    from app.services import fx as fx_service
    from app.services import market_data

    monkeypatch.setattr(market_data, "fetch_latest_close", forbidden)
    monkeypatch.setattr(market_data, "fetch_history", forbidden)
    monkeypatch.setattr(fx_service, "fetch_valet_series", forbidden)


def _today() -> dt.date:
    """The clock, read at CALL time (never captured at import)."""
    return txn_filters.today_et()


def _auth(client: TestClient) -> tuple[dict[str, str], int]:
    from datetime import UTC, datetime

    email = f"matrix-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    user_id = client.get(f"{API}/users/me", headers=headers).json()["id"]
    return headers, user_id


def _account(session: Session, user_id: int, *, currency: str = "CAD") -> Account:
    account = Account(
        user_id=user_id,
        name=f"Matrix {user_id} {currency}",
        type=AccountType.brokerage_non_registered,
        currency=currency,
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _bill(
    session: Session,
    user_id: int,
    *,
    name: str,
    due: str,
    amount: str | None = "16.99",
    frequency: str = "monthly",
    anchor_day: int | None = None,
) -> BillReminder:
    """A row written OUTSIDE the API — the only way to reach an unanchored row.

    `POST /bills` always anchors the day it was given (M180 D1), so the
    legacy/`one_time`/hand-edited shapes the readers must tolerate have to be
    seeded here, at the column level.
    """
    bill = BillReminder(
        user_id=user_id,
        name=name,
        amount=None if amount is None else Decimal(amount),
        frequency=frequency,
        next_due_date=due,
        anchor_day=anchor_day,
        enabled=True,
        match_term=name,
    )
    session.add(bill)
    session.commit()
    session.refresh(bill)
    return bill


def _txn(
    session: Session,
    user_id: int,
    account: Account,
    *,
    payee: str,
    day: dt.date,
    amount: str,
    currency: str = "CAD",
    dedup: str,
) -> Transaction:
    txn = Transaction(
        user_id=user_id,
        account_id=account.id,
        date=day,
        raw_description=payee,
        normalized_payee=payee,
        amount_native=Decimal(amount),
        currency=currency,
        dedup_hash=dedup,
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _accept(client: TestClient, headers: dict[str, str], txns: list[Transaction]) -> dict:
    """Accept a recurring group the way the user does — the REAL route (M96).

    `is_recurring` + `recurring_cadence` + `recurring_next_date` are the accept
    action's output and M170 makes that output the calendar's subscription
    series, so the user-asserted path is what these rows have to come from.
    """
    payee = txns[0].normalized_payee or txns[0].raw_description
    response = client.post(
        f"{API}/transactions/recurring/accept",
        headers=headers,
        json={
            "group_key": f"{payee.replace(' ', '').upper()}|monthly",
            "cadence": "monthly",
            "transaction_ids": [txn.id for txn in txns],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _repin_next(session: Session, txns: list[Transaction], day: dt.date) -> None:
    """Put an accepted group's `recurring_next_date` on `day` (the accept column)."""
    for txn in txns:
        txn.recurring_next_date = day
        session.add(txn)
    session.commit()


def _security(session: Session, user_id: int, symbol: str) -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} Inc",
        kind="stock",
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _dividend(
    session: Session,
    user_id: int,
    account: Account,
    security: Security,
    *,
    day: dt.date,
    amount: str,
) -> Trade:
    trade = Trade(
        user_id=user_id,
        account_id=account.id,
        security_id=security.id,
        action=TradeAction.dividend,
        date=day,
        quantity=Decimal("0"),
        price_native=Decimal(amount),
        commission=Decimal("0"),
    )
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def _get(client: TestClient, headers: dict[str, str], month: str | None = None) -> dict:
    params = {} if month is None else {"month": month}
    response = client.get(CAL, headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _events(body: dict, kind: str) -> list[tuple[str, dict]]:
    return [
        (day["date"], event)
        for day in body["days"]
        for event in day["events"]
        if event["kind"] == kind
    ]


def _create_bill(
    client: TestClient,
    headers: dict[str, str],
    *,
    due: str,
    frequency: str = "monthly",
    name: str = "Matrix Rent",
) -> dict:
    response = client.post(
        BILLS,
        headers=headers,
        json={"name": name, "amount": 1400, "frequency": frequency, "next_due_date": due},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mark_paid(client: TestClient, headers: dict[str, str], bill_id: int) -> dict:
    response = client.post(f"{BILLS}/{bill_id}/mark-paid", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _stored(session: Session, bill_id: int) -> tuple[str, int | None]:
    row = session.execute(
        text("SELECT next_due_date, anchor_day FROM bill_reminders WHERE id = :id"),
        {"id": bill_id},
    ).one()
    return row[0], row[1]


def _month_of(day: dt.date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _freeze_et_now(monkeypatch: pytest.MonkeyPatch, instant: dt.datetime) -> dt.date:
    """Pin the app's clock to `instant` and answer the ET date it implies.

    `today_et()` is `dt.datetime.now(_ET).date()`, so the freeze replaces the
    `dt` symbol INSIDE `app.services.txn_filters` (not the stdlib module
    object), then re-points every binding the calendar route can read at that
    one frozen function — the app's own code decides the date, the test only
    decides what time it is. The expected value is computed here from the
    stdlib zone database, never from app code.
    """
    assert instant.tzinfo is not None, "freeze with an AWARE instant"

    class _FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(
        txn_filters,
        "dt",
        SimpleNamespace(datetime=_FrozenDatetime, date=dt.date, timedelta=dt.timedelta),
    )
    real_today_et = txn_filters.today_et

    def frozen_today_et() -> dt.date:
        return real_today_et()

    for target in _CALENDAR_CLOCK_BINDINGS:
        monkeypatch.setattr(target, frozen_today_et)
    return instant.astimezone(_ET).date()


# --------------------------------------------------------------------------- #
# 1+2. The schedule matrix — month-end and leap-day, on all three surfaces
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScheduleCase:
    """One schedule: what is stored, and the dates every surface must answer."""

    name: str
    boundary: str
    frequency: str
    start: dt.date  # bill_reminders.next_due_date — ALWAYS a real, clamped date
    anchor: int | None  # bill_reminders.anchor_day (None = pre-M180 row)
    chain: tuple[str, ...]  # the next `mark-paid` results, in order


#: Every chain is hand-computed in the row, so the matrix itself is the spec.
SCHEDULE_MATRIX: tuple[ScheduleCase, ...] = (
    ScheduleCase(
        name="monthly-31st",
        boundary="month-end",
        frequency="monthly",
        start=dt.date(2026, 1, 31),
        anchor=31,
        chain=(
            "2026-02-28",  # clamped: February has no 31st
            "2026-03-31",  # and the ANCHOR brings it back (the M180 defect)
            "2026-04-30",
            "2026-05-31",
            "2026-06-30",
            "2026-07-31",
            "2026-08-31",
            "2026-09-30",
            "2026-10-31",
            "2026-11-30",
            "2026-12-31",
            "2027-01-31",  # year rollover
            "2027-02-28",
        ),
    ),
    ScheduleCase(
        name="monthly-29th-common-year",
        boundary="leap-day",
        frequency="monthly",
        start=dt.date(2026, 1, 29),
        anchor=29,
        chain=("2026-02-28", "2026-03-29", "2026-04-29"),
    ),
    ScheduleCase(
        name="monthly-29th-leap-year",
        boundary="leap-day",
        frequency="monthly",
        start=dt.date(2028, 1, 29),
        anchor=29,
        chain=("2028-02-29", "2028-03-29", "2028-04-29"),
    ),
    ScheduleCase(
        name="monthly-30th",
        boundary="month-end",
        frequency="monthly",
        start=dt.date(2026, 4, 30),
        anchor=30,
        chain=(
            "2026-05-30",
            "2026-06-30",
            "2026-07-30",
            "2026-08-30",
            "2026-09-30",
            "2026-10-30",
            "2026-11-30",
            "2026-12-30",
            "2027-01-30",
            "2027-02-28",  # the one month without a 30th
        ),
    ),
    ScheduleCase(
        name="monthly-28th-control",
        boundary="month-end",
        frequency="monthly",
        start=dt.date(2026, 1, 28),
        anchor=28,
        # Never clamped in ANY month — the control that proves the clamping
        # rows above are not an artefact of the walk itself.
        chain=("2026-02-28", "2026-03-28", "2026-04-28"),
    ),
    ScheduleCase(
        name="quarterly-31st",
        boundary="month-end",
        frequency="quarterly",
        start=dt.date(2026, 1, 31),
        anchor=31,
        # Pre-M180 the write path stored `2026-04-31` here — not a date at all.
        chain=("2026-04-30", "2026-07-31", "2026-10-31", "2027-01-31"),
    ),
    ScheduleCase(
        name="yearly-leap-day",
        boundary="leap-day",
        frequency="yearly",
        start=dt.date(2028, 2, 29),
        anchor=29,
        # Pre-M180 this stored `2029-02-29`, which `date.fromisoformat` refuses.
        chain=(
            "2029-02-28",
            "2030-02-28",
            "2031-02-28",
            "2032-02-29",  # the leap day comes BACK
            "2033-02-28",
        ),
    ),
    ScheduleCase(
        name="monthly-31st-unanchored",
        boundary="month-end",
        frequency="monthly",
        start=dt.date(2026, 2, 28),
        anchor=None,  # a pre-M180 row: the stored date's own day IS the anchor
        chain=("2026-03-28", "2026-04-28", "2026-05-28"),
    ),
    ScheduleCase(
        name="one-time",
        boundary="month-end",
        frequency="one_time",
        start=dt.date(2026, 3, 5),
        anchor=5,
        chain=(),  # no cadence, no next occurrence
    ),
)

_CHAINED = tuple(case for case in SCHEDULE_MATRIX if case.chain)
_CASE_IDS = [case.name for case in SCHEDULE_MATRIX]


def _chain_dates(case: ScheduleCase) -> list[dt.date]:
    return [dt.date.fromisoformat(day) for day in case.chain]


def _rule_occurrences(case: ScheduleCase) -> list[dt.date]:
    """The dates the shared rule puts in `[start, last chain date]`."""
    return [case.start, *_chain_dates(case)]


@pytest.mark.parametrize("case", SCHEDULE_MATRIX, ids=_CASE_IDS)
def test_matrix_write_path_advances_exactly_the_pinned_chain(case: ScheduleCase) -> None:
    """Surface 1 — `next_due`, the rule `mark-paid` writes with.

    The day is the ANCHOR's (clamped to the target month's length) and never
    the previously stored date's, which is the whole of M180.
    """
    due = case.start
    walked: list[str] = []
    for _ in case.chain:
        due = next_due(due, case.frequency, case.anchor)
        walked.append(due.isoformat())
    assert walked == list(case.chain)
    if not case.chain:
        # No cadence (or an unknown one) never advances: the stored date itself.
        assert next_due(case.start, case.frequency, case.anchor) == case.start


@pytest.mark.parametrize("case", SCHEDULE_MATRIX, ids=_CASE_IDS)
def test_matrix_grid_projects_the_same_dates_the_write_path_advances(case: ScheduleCase) -> None:
    """Surface 2 — `occurrences` / `first_on_or_after`, the grid's projection.

    For every matrix row the grid answers exactly the dates the write path
    walks (and the stored date itself), stepping by WHOLE periods from `start`
    with the anchor supplying the day, so one clamped month can never poison
    the rest of the series. Nothing before the stored date is reconstructed.
    """
    if not case.chain:
        assert occurrences(
            case.start, case.frequency, case.anchor, case.start - dt.timedelta(days=40), case.start
        ) == [case.start]
        assert (
            occurrences(
                case.start,
                case.frequency,
                case.anchor,
                case.start + _DAY,
                case.start + dt.timedelta(days=40),
            )
            == []
        )
        assert first_on_or_after(case.start, case.frequency, case.anchor, case.start) == case.start
        return

    expected = _rule_occurrences(case)
    assert (
        occurrences(
            case.start,
            case.frequency,
            case.anchor,
            case.start,
            expected[-1],  # type: ignore[arg-type]
        )
        == expected
    )
    # Forward-only: everything before the stored date is history the app does
    # not track (bills carry no payment history).
    assert (
        occurrences(
            case.start,
            case.frequency,
            case.anchor,
            case.start - dt.timedelta(days=400),
            case.start - _DAY,
        )
        == []
    )
    # `first_on_or_after` reads the same series: a chain date answers itself,
    # the day after a chain date answers the NEXT chain date.
    for index, day in enumerate(expected):
        assert first_on_or_after(case.start, case.frequency, case.anchor, day) == day
        following = first_on_or_after(case.start, case.frequency, case.anchor, day + _DAY)
        if index + 1 < len(expected):
            assert following == expected[index + 1]


@pytest.mark.parametrize("case", SCHEDULE_MATRIX, ids=_CASE_IDS)
def test_matrix_row_readers_answer_what_the_rule_answers(
    case: ScheduleCase, client: TestClient, session: Session
) -> None:
    """The row-level contract both OTHER surfaces call (M180 D2 wiring).

    `bill_occurrences` (the grid) and `bill_next_on_or_after` (the confirmed
    set) take a ROW, so the matrix is replayed through real `bill_reminders`
    rows: whatever the rule says for `(start, frequency, anchor)`, these two
    answer for the stored row — including the un-anchored legacy shape, whose
    anchor is the stored date's own day.
    """
    _, user_id = _auth(client)
    bill = _bill(
        session,
        user_id,
        name=case.name,
        due=case.start.isoformat(),
        frequency=case.frequency,
        anchor_day=case.anchor,
    )
    for span in ((case.start, case.start), (case.start, case.start + dt.timedelta(days=800))):
        assert bill_occurrences(bill, *span) == occurrences(
            case.start, case.frequency, case.anchor, *span
        )
    for probe in (case.start, case.start + _DAY, case.start + dt.timedelta(days=400)):
        assert bill_next_on_or_after(bill, probe) == (
            None
            if case.frequency not in ("monthly", "quarterly", "yearly") and case.start < probe
            else first_on_or_after(case.start, case.frequency, case.anchor, probe)
        )


@pytest.mark.parametrize("case", _CHAINED, ids=[case.name for case in _CHAINED])
def test_matrix_api_walk_lands_on_the_grid_in_every_month_of_the_chain(
    case: ScheduleCase, client: TestClient, session: Session
) -> None:
    """The write path and the grid, end to end, on the SAME bill row.

    Every month of the chain is read back from the route: the grid draws the
    bill on exactly the date `mark-paid` stored for it, so the two surfaces
    cannot clamp a schedule differently. The stored row is checked too — a
    real date, with the anchor kept.
    """
    headers, _ = _auth(client)
    bill_id = _create_bill(
        client, headers, due=case.start.isoformat(), frequency=case.frequency, name=case.name
    )["id"]
    assert _stored(session, bill_id) == (case.start.isoformat(), case.start.day)

    for expected in _chain_dates(case):
        assert _mark_paid(client, headers, bill_id)["next_due_date"] == expected.isoformat()
        assert [
            day for day, _event in _events(_get(client, headers, _month_of(expected)), "bill")
        ] == [expected.isoformat()]
        assert _stored(session, bill_id) == (expected.isoformat(), case.start.day)


@pytest.mark.parametrize("case", _CHAINED, ids=[case.name for case in _CHAINED])
def test_matrix_confirmed_set_reads_the_grids_next_date(
    case: ScheduleCase, client: TestClient, session: Session
) -> None:
    """Surface 3 — the confirmed set's next date IS the grid's next date.

    A `today` inside the chain, and one exactly ON a chain date (the boundary
    the confirmed set exists for): both answer the date the grid draws next.
    """
    headers, user_id = _auth(client)
    _create_bill(
        client, headers, due=case.start.isoformat(), frequency=case.frequency, name=case.name
    )
    probes = _chain_dates(case)
    for probe in (probes[0] - _DAY, probes[0], probes[0] + _DAY):
        entry = next(
            subscription
            for subscription in confirmed_subscriptions(session, user_id, today=probe)
            if subscription.payee == case.name
        )
        assert entry.next_expected == first_on_or_after(
            case.start, case.frequency, case.anchor, probe
        )
        # …and the same date is what the grid shows for it.
        assert entry.next_expected.isoformat() in [
            day
            for day, _event in _events(
                _get(client, headers, _month_of(entry.next_expected)), "bill"
            )
        ]


def test_matrix_one_time_bill_never_repeats_on_any_surface(
    client: TestClient, session: Session
) -> None:
    """`one_time` is the matrix's negative control: no cadence, no projection.

    The write path disables it (unchanged M40 behaviour), the grid keeps it on
    its stored date only, and the confirmed set drops it once the date passes.
    """
    headers, _user_id = _auth(client)
    bill_id = _create_bill(
        client, headers, due="2026-03-05", frequency="one_time", name="One-off tax"
    )["id"]
    stored_before = _stored(session, bill_id)
    body = _mark_paid(client, headers, bill_id)
    assert body["enabled"] is False
    assert body["next_due_date"] == stored_before[0]
    assert _stored(session, bill_id) == stored_before
    # Disabled bills leave the grid (the route reads enabled rows only).
    assert _events(_get(client, headers, "2026-03"), "bill") == []
    # …and the confirmed set has no next date for a non-repeating bill already past.
    assert (
        bill_next_on_or_after(session.get(BillReminder, bill_id), dt.date(2026, 3, 6)) is None  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# 3. DST — the clock seam and the 23/25-hour days
# --------------------------------------------------------------------------- #

#: `(instant, why, fixed offset, the date THAT offset answers)` — every instant
#: is UTC. `None` for the last two means the row is NOT a discriminator (a
#: fixed offset happens to agree there), which is itself worth pinning: the
#: hand-computed estimate is right by luck most of the year and wrong for the
#: hour around local midnight — which is why the zone database, not arithmetic,
#: is the only correct source.
DST_CLOCK_MATRIX: tuple[tuple[str, str, int | None, str | None], ...] = (
    (
        "2026-07-15T04:30:00Z",
        "EDT 00:30 — the EST offset (UTC-5) answers the PREVIOUS day",
        5,
        "2026-07-14",
    ),
    (
        "2026-12-15T04:30:00Z",
        "EST 23:30 — the EDT offset (UTC-4) answers the NEXT day",
        4,
        "2026-12-15",
    ),
    ("2026-03-08T06:30:00Z", "spring-forward morning, still EST 01:30 → 2026-03-08", None, None),
    ("2026-03-09T03:30:00Z", "after the switch, EDT 23:30 → still 2026-03-08", None, None),
    ("2026-11-01T05:30:00Z", "fall-back morning, EDT 01:30 → 2026-11-01", None, None),
    (
        "2026-11-01T06:30:00Z",
        "the same wall clock an hour later, EST 01:30 → 2026-11-01",
        None,
        None,
    ),
)


@pytest.mark.parametrize(
    ("iso", "why", "fixed_offset_hours", "fixed_wrong_date"),
    DST_CLOCK_MATRIX,
    ids=[row[0] for row in DST_CLOCK_MATRIX],
)
def test_matrix_the_clock_answers_the_real_et_date_at_every_dst_boundary(
    monkeypatch: pytest.MonkeyPatch,
    iso: str,
    why: str,
    fixed_offset_hours: int | None,
    fixed_wrong_date: str | None,
) -> None:
    """`today_et()` is zone-aware, so it tracks DST — it is not a fixed offset.

    The grid's whole notion of "today", "past" and "upcoming" hangs off this
    one function; a hand-computed UTC-4/UTC-5 estimate answers the wrong DATE
    for the hour around local midnight, which moves a bill a day early or a day
    late twice a year (the M117 precedent: the same bug class made the
    market-hours gate read a fixed offset). Nothing in the app may compute this
    by hand — and the rows that a fixed offset would get WRONG are asserted to
    be wrong here, so this test cannot pass vacuously.
    """
    instant = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    expected = _freeze_et_now(monkeypatch, instant)
    # The app's own function, on the frozen clock, answers this date.
    assert txn_filters.today_et() == expected, why
    assert txn_filters.today_et() == instant.astimezone(_ET).date()
    # The frozen clock is the app's clock: `_ET` is America/Toronto, not UTC.
    assert txn_filters._ET.key == "America/Toronto"
    if fixed_offset_hours is not None:
        naive = (instant - dt.timedelta(hours=fixed_offset_hours)).date()
        assert naive.isoformat() == fixed_wrong_date != expected.isoformat(), why


def test_matrix_a_23_hour_day_is_neither_dropped_nor_duplicated_on_the_grid(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, session: Session
) -> None:
    """Spring forward: 2026-03-08 is a 23-hour ET day and the grid is date-only.

    The month default, the day statuses and the day-list all come from the ET
    clock: the transition day is present exactly ONCE, the days either side
    keep their own status, and a 31st schedule in the same month still lands
    on the 31st.
    """
    headers, user_id = _auth(client)
    today = _freeze_et_now(monkeypatch, dt.datetime(2026, 3, 8, 6, 30, tzinfo=dt.UTC))
    assert today == dt.date(2026, 3, 8)
    for day, name in (
        (dt.date(2026, 3, 7), "Before"),
        (today, "On the transition day"),
        (dt.date(2026, 3, 9), "After"),
        (dt.date(2026, 3, 31), "Month end"),
    ):
        _bill(session, user_id, name=name, due=day.isoformat(), anchor_day=day.day)

    body = _get(client, headers)  # no `month` param: the clock picks the month
    assert (body["month"], body["today"]) == ("2026-03", today.isoformat())
    dates = [day["date"] for day in body["days"]]
    assert dates == sorted(set(dates)) and len(dates) == 31
    assert dates.count(today.isoformat()) == 1
    status = {day: event["status"] for day, event in _events(body, "bill")}
    assert status == {
        "2026-03-07": "past",
        "2026-03-08": "today",
        "2026-03-09": "upcoming",
        "2026-03-31": "upcoming",
    }


def test_matrix_a_25_hour_day_is_neither_dropped_nor_duplicated_on_the_grid(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, session: Session
) -> None:
    """Fall back: 2026-11-01 is a 25-hour ET day — still exactly one date.

    The repeated local hour (01:00-02:00 twice) must not turn into two grid
    days or shift the month default: `today` is the ET date, the month is its
    month, and an event on the transition day is statused against it.
    """
    headers, user_id = _auth(client)
    today = _freeze_et_now(monkeypatch, dt.datetime(2026, 11, 1, 6, 30, tzinfo=dt.UTC))
    assert today == dt.date(2026, 11, 1)
    _bill(session, user_id, name="On the fall-back day", due=today.isoformat(), anchor_day=1)

    body = _get(client, headers)
    assert (body["month"], body["today"]) == ("2026-11", today.isoformat())
    dates = [day["date"] for day in body["days"]]
    assert dates == sorted(set(dates)) and len(dates) == 30
    assert [(day, event["status"]) for day, event in _events(body, "bill")] == [
        (today.isoformat(), "today")
    ]


def test_matrix_the_month_default_is_the_et_month_across_a_month_boundary(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, session: Session
) -> None:
    """The ET clock also picks the MONTH — one hour from UTC can move the grid.

    At 2026-09-01T03:30Z the ET date is still 2026-08-31, so the default month
    is August (and a bill on the 31st is on TODAY, not next month's grid).
    """
    headers, user_id = _auth(client)
    today = _freeze_et_now(monkeypatch, dt.datetime(2026, 9, 1, 3, 30, tzinfo=dt.UTC))
    assert today == dt.date(2026, 8, 31)
    _bill(session, user_id, name="Last of August", due=today.isoformat(), anchor_day=31)

    body = _get(client, headers)
    assert (body["month"], body["today"]) == ("2026-08", "2026-08-31")
    assert len(body["days"]) == 31
    assert [(day, event["status"]) for day, event in _events(body, "bill")] == [
        ("2026-08-31", "today")
    ]


# --------------------------------------------------------------------------- #
# 4. Currency — the amount/currency pair, and where a figure is withheld
# --------------------------------------------------------------------------- #

#: `(case, rows, expected)` where `rows` is `(currency, amount)` per charge and
#: `expected` is the calendar's `(amount, currency)` pair for that group.
CURRENCY_MATRIX: tuple[
    tuple[str, tuple[tuple[str, str], ...], tuple[str | None, str | None]], ...
] = (
    ("usd", (("USD", "-11.99"), ("USD", "-11.99")), ("-11.99", "USD")),
    ("cad", (("CAD", "-15.99"), ("CAD", "-15.99")), ("-15.99", "CAD")),
    ("eur", (("EUR", "-9.99"), ("EUR", "-9.99")), ("-9.99", "EUR")),
    # A hand-written row cannot smuggle a token onto a surface that prints it:
    # the rule reads the code case-insensitively (docs/02 shape).
    ("usd-lowercase", (("usd", "-11.99"), ("usd", "-11.99")), ("-11.99", "USD")),
    ("usd-padded", ((" usd ", "-11.99"), ("USD", "-11.99")), ("-11.99", "USD")),
    # Mixed: the mean spans two currencies, so it is stated in neither.
    ("mixed", (("CAD", "-11.99"), ("USD", "-12.99")), (None, None)),
    # Unknown: one undemonstrable row makes the whole mean undemonstrable.
    ("unknown-blank", (("CAD", "-9.99"), ("", "-9.99")), (None, None)),
    ("unknown-two-letter", (("CAD", "-9.99"), ("US", "-9.99")), (None, None)),
    ("unknown-symbol", (("CAD", "-9.99"), ("US$", "-9.99")), (None, None)),
    ("unknown-and-mixed", (("USD", "-9.99"), ("", "-9.99")), (None, None)),
)


@pytest.mark.parametrize(
    ("case", "rows", "expected"),
    CURRENCY_MATRIX,
    ids=[row[0] for row in CURRENCY_MATRIX],
)
def test_matrix_group_currency_is_stated_or_withheld(
    client: TestClient,
    session: Session,
    case: str,
    rows: tuple[tuple[str, str], ...],
    expected: tuple[str | None, str | None],
) -> None:
    """The resolved pair, on BOTH surfaces, for every currency boundary.

    A figure is always accompanied by the code its rows agree on and is never
    relabelled: the group's amount is the mean of its `amount_native` values,
    so a mean over two currencies — or over one unknown token — is a number
    with no denomination, and `null`/`null` is how the client learns to print
    the app's "no honest figure" dash.
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    payee = f"MATRIX {case.upper()}"
    charges = [
        _txn(
            session,
            user_id,
            account,
            payee=payee,
            day=_today() - dt.timedelta(days=30 * (index + 1)),
            amount=amount,
            currency=currency,
            dedup=f"matrix-{case}-{index}",
        )
        for index, (currency, amount) in enumerate(rows)
    ]
    _accept(client, headers, charges)

    entry = next(
        subscription
        for subscription in confirmed_subscriptions(session, user_id)
        if subscription.payee == payee
    )
    assert entry.source == "recurring"
    assert (None if entry.amount is None else str(entry.amount), entry.currency) == expected

    # …and the grid states the SAME pair on the event it draws for the group.
    events = _events(_get(client, headers, _month_of(entry.next_expected)), "subscription")
    assert [(event["label"], event["amount"], event["currency"]) for _day, event in events] == [
        (payee, expected[0], expected[1])
    ]


def test_matrix_every_event_pairs_its_amount_with_its_currency(
    client: TestClient, session: Session
) -> None:
    """The invariant over EVERY kind at once, in one month: no bare figure.

    Bills (CAD by schema), a variable bill (no figure at all), a dividend (CAD
    by M29's convention), and three groups — CAD, USD and mixed — share the
    grid, so the pair is checked as a property of the payload as well as per
    source: `(amount is None) == (currency is None)` on every event, and the
    per-kind table below is exact (so a vacuous sweep cannot pass).
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    usd = _account(session, user_id, currency="USD")
    day = _today() + dt.timedelta(days=10)

    _bill(session, user_id, name="Matrix Rent", due=day.isoformat(), amount="1400.00")
    _bill(session, user_id, name="Matrix Hydro", due=day.isoformat(), amount=None)
    security = _security(session, user_id, "RY")
    for offset, amount in ((60, "25.00"), (30, "25.00")):
        _dividend(
            session, user_id, account, security, day=day - dt.timedelta(days=offset), amount=amount
        )

    def group(payee: str, rows: list[tuple[Account, str, str]]) -> None:
        charges = [
            _txn(
                session,
                user_id,
                card,
                payee=payee,
                day=day - dt.timedelta(days=30 * (index + 1)),
                amount=amount,
                currency=currency,
                dedup=f"matrix-all-{payee}-{index}",
            )
            for index, (card, amount, currency) in enumerate(rows)
        ]
        _accept(client, headers, charges)
        _repin_next(session, charges, day)

    group("MATRIX NETFLIX", [(account, "-15.99", "CAD"), (account, "-15.99", "CAD")])
    group("MATRIX SPOTIFY", [(usd, "-11.99", "USD"), (usd, "-11.99", "USD")])
    group("MATRIX DROPBOX", [(account, "-11.99", "CAD"), (usd, "-12.99", "USD")])

    projected = cashflow_calendar.compute_dividend_calendar(session, user_id)["upcoming"]
    months = sorted(
        {_month_of(day), *(_month_of(entry["next_expected_date"]) for entry in projected)}
    )

    events = [
        event
        for month in months
        for grid_day in _get(client, headers, month)["days"]
        for event in grid_day["events"]
    ]
    # The invariant, over every event of every kind on the grid.
    for event in events:
        assert (event["amount"] is None) == (event["currency"] is None), event
    # And the exact per-kind table, so the sweep cannot pass on an empty grid.
    assert {
        (event["kind"], event["label"]): (event["amount"], event["currency"]) for event in events
    } == {
        ("bill", "Matrix Rent"): ("-1400.00", "CAD"),
        ("bill", "Matrix Hydro"): (None, None),
        ("dividend", "RY dividend"): ("25.00", "CAD"),
        ("subscription", "MATRIX NETFLIX"): ("-15.99", "CAD"),
        ("subscription", "MATRIX SPOTIFY"): ("-11.99", "USD"),
        ("subscription", "MATRIX DROPBOX"): (None, None),
    }


def test_matrix_no_event_is_ever_converted_on_the_read_path(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A USD group renders its own number: the path has no fx read (docs/36 D6).

    The rate book is patched to raise, so if this endpoint ever starts
    converting — or merely asking for a rate — the request fails instead of
    quietly inventing a CAD figure for a foreign commitment.
    """
    headers, user_id = _auth(client)
    usd = _account(session, user_id, currency="USD")
    day = _today() + dt.timedelta(days=10)
    charges = [
        _txn(
            session,
            user_id,
            usd,
            payee="MATRIX NOFX",
            day=day - dt.timedelta(days=30 * (index + 1)),
            amount="-11.99",
            currency="USD",
            dedup=f"matrix-nofx-{index}",
        )
        for index in range(2)
    ]
    _accept(client, headers, charges)
    _repin_next(session, charges, day)

    from app.services import fx as fx_service

    def forbidden(*args, **kwargs):
        raise AssertionError("the calendar read consulted the fx rate book")

    monkeypatch.setattr(fx_service, "cached_cad_amount", forbidden)
    monkeypatch.setattr(fx_service, "latest_rate", forbidden)

    events = _events(_get(client, headers, _month_of(day)), "subscription")
    assert [(event["label"], event["amount"], event["currency"]) for _d, event in events] == [
        ("MATRIX NOFX", "-11.99", "USD")
    ]


def test_month_end_accepted_group_uses_the_shared_schedule_rule(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An accepted group preserves its newest charge's day across clamped months.

    The accept response remains a real, clamped date, while projection keeps
    the charge-day anchor and uses the same whole-month rule as bills.
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)

    # Half 1 — the schedule day is already lost at ACCEPT time: the newest
    # charge is the 31st and the stored next date is February's clamp.
    assert next_expected_date(dt.date(2026, 1, 31), RecurringCadence.monthly) == dt.date(
        2026, 2, 28
    )

    charges = [
        _txn(
            session,
            user_id,
            account,
            payee="MATRIX DRIFT",
            day=dt.date(2025, 12, 31) if index == 0 else dt.date(2026, 1, 31),
            amount="-20.00",
            dedup=f"matrix-drift-{index}",
        )
        for index in range(2)
    ]
    accepted = _accept(client, headers, charges)
    assert accepted["next_expected_date"] == "2026-02-28"
    # The stored base may be February's clamp (or an older un-clamped date);
    # both retain the newest charge's day when projected forward.
    _repin_next(session, charges, dt.date(2026, 1, 31))

    today = dt.date(2026, 4, 15)
    group = next(
        subscription
        for subscription in confirmed_subscriptions(session, user_id, today=today)
        if subscription.payee == "MATRIX DRIFT"
    )
    bill = next_due(dt.date(2026, 1, 31), "monthly", 31)
    assert group.next_expected == dt.date(2026, 4, 30)
    assert first_on_or_after(dt.date(2026, 1, 31), "monthly", 31, today) == dt.date(2026, 4, 30)
    assert bill == dt.date(2026, 2, 28)  # the shared rule's own first step

    # Both stored shapes drift the same way — the TRUE 31st above, and the
    # clamped `2026-02-28` a real accept of these charges actually writes.
    _repin_next(session, charges, dt.date(2026, 2, 28))
    drifted = next(
        subscription
        for subscription in confirmed_subscriptions(session, user_id, today=today)
        if subscription.payee == "MATRIX DRIFT"
    )
    assert drifted.next_expected == dt.date(2026, 4, 30)

    # The two series disagree on the same grid, an hour apart in the payload.
    _freeze_et_now(monkeypatch, dt.datetime(2026, 4, 15, 12, 0, tzinfo=dt.UTC))
    _bill(
        session,
        user_id,
        name="MATRIX DRIFT BILL",
        due="2026-01-31",
        amount="20.00",
        anchor_day=31,
    )
    grid = _get(client, headers, "2026-04")
    assert {day for day, _e in _events(grid, "subscription")} == {"2026-04-30"}
    assert {day for day, _e in _events(grid, "bill")} == {"2026-04-30"}


# --------------------------------------------------------------------------- #
# Consolidation: three surfaces, ONE rule
# --------------------------------------------------------------------------- #


def test_matrix_all_three_surfaces_call_the_one_rule_module() -> None:
    """The wiring the matrix depends on, pinned by identity.

    Each surface imports the rule's own function object, so a future edit that
    reintroduces a local projection has to break this line first.
    """
    from app.api import bill_reminders as bill_api
    from app.services import confirmed_subscriptions as confirmed_module

    assert cashflow_calendar.bill_occurrences is bill_schedule.bill_occurrences
    assert confirmed_module.bill_next_on_or_after is bill_schedule.bill_next_on_or_after
    assert bill_api.next_due is bill_schedule.next_due


def test_matrix_no_second_month_clamp_exists_on_the_bill_path() -> None:
    """No surface may re-derive a clamped month-day by hand (the M180 seam).

    `calendar.monthrange` — the thing that decides a clamped day — is allowed
    in `bill_schedule.py` alone on this path; the two readers and the bills
    router must not own a copy of it again (docs/36 decision 5).
    """
    services = Path(bill_schedule.__file__).resolve().parent
    readers = {
        "app/services/cashflow_calendar.py": services / "cashflow_calendar.py",
        "app/services/confirmed_subscriptions.py": services / "confirmed_subscriptions.py",
        "app/api/bill_reminders.py": services.parent / "api" / "bill_reminders.py",
    }
    for label, path in readers.items():
        source = path.read_text(encoding="utf-8")
        assert "monthrange" not in source, label
        assert not re.search(r"def advance_months", source), label
    assert "monthrange" in Path(bill_schedule.__file__).read_text(encoding="utf-8")

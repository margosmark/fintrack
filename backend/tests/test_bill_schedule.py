"""M180 — bill recurrence keeps a real schedule (docs/25 §M180, docs/36 decision 5).

Three defects this module pins shut, all of them the same root cause: the
schedule used to live only in `bill_reminders.next_due_date`, a column that
holds a REAL calendar date.

1. **Anchor drift.** `mark-paid` advanced from the previously CLAMPED date, so
   a bill due on the 31st went Jan 31 → Feb 28 → Mar 28 → Apr 28 … The 31st was
   gone after one month. Now the schedule's own day lives in
   `bill_reminders.anchor_day` and only the stored DATE is clamped.
2. **Impossible stored dates.** `_advance_due_date` formatted `{y}-{m}-{d}`
   with no month-length check, so a quarterly bill due on the 31st stored
   `2026-04-31` and a yearly leap-day bill stored `2029-02-29`. Neither is a
   date: the calendar silently dropped the bill and the NEXT `mark-paid`
   raised `ValueError` out of `date.fromisoformat` — a bare 500.
3. **A leap day that never came back.** Feb 29 → Feb 28 → Feb 28 … now returns
   to Feb 29 in the next leap year.

The rule itself is `app/services/bill_schedule.py`; every surface that projects
a bill forward (the write path, the cash-flow calendar, the confirmed-set
query) reads it, so this module tests it three ways: as pure arithmetic, as the
API's behaviour, and as the row-level contract the calendar and confirmed-set
readers call (`bill_occurrences` / `bill_next_on_or_after`).
"""

from __future__ import annotations

import calendar
import datetime as dt
import runpy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.bill_reminders import BillReminder
from app.services import bill_schedule
from app.services.bill_schedule import advance_months, first_on_or_after, next_due, occurrences

API = "/api/v1"
CAL = f"{API}/reports/cashflow-calendar"
BILLS = f"{API}/bills"

# The migration's own statements, executed here so the backfill the migration
# runs is the backfill under test (the M121 `BACKFILL_SQL` pattern).
_MIGRATION = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0054_bill_due_anchor.py"
)
BACKFILL_STATEMENTS: tuple[str, ...] = tuple(runpy.run_path(str(_MIGRATION))["BACKFILL_STATEMENTS"])


# --------------------------------------------------------------------------- #
# The rule itself — pure arithmetic, no database
# --------------------------------------------------------------------------- #


def test_monthly_advance_keeps_the_anchor_through_every_month_end() -> None:
    """A 31st schedule is a 31st schedule, in every month that has one."""
    due = dt.date(2026, 1, 31)
    chain: list[str] = []
    for _ in range(13):
        due = next_due(due, "monthly", 31)
        chain.append(due.isoformat())
    assert chain == [
        "2026-02-28",  # clamped: February has no 31st
        "2026-03-31",  # and the anchor brings it BACK (the drift defect)
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
    ]


def test_quarterly_advance_never_lands_on_a_missing_day() -> None:
    """Jan 31 + 3 months is April 30 — the pre-M180 code stored `2026-04-31`."""
    due = dt.date(2026, 1, 31)
    chain: list[str] = []
    for _ in range(4):
        due = next_due(due, "quarterly", 31)
        chain.append(due.isoformat())
    assert chain == ["2026-04-30", "2026-07-31", "2026-10-31", "2027-01-31"]


def test_yearly_leap_day_returns_in_the_next_leap_year() -> None:
    """Feb 29 → Feb 28 for three years, then Feb 29 again in 2032."""
    due = dt.date(2028, 2, 29)
    chain: list[str] = []
    for _ in range(5):
        due = next_due(due, "yearly", 29)
        chain.append(due.isoformat())
    assert chain == ["2029-02-28", "2030-02-28", "2031-02-28", "2032-02-29", "2033-02-28"]


def test_no_advance_can_ever_produce_a_stored_value_that_is_not_a_date() -> None:
    """The write path can only ever store a real date (defect 2, swept).

    Every anchor day a month end can name (28-31), every month of three years
    and all three cadences: each step is a `date`, and its ISO form parses back.
    """
    checked = 0
    for anchor in (28, 29, 30, 31):
        for year in (2026, 2027, 2028):
            for month in range(1, 13):
                for cadence in ("monthly", "quarterly", "yearly"):
                    # The stored date is CLAMPED (that is the point) while the
                    # anchor stays the day the schedule repeats on.
                    length = calendar.monthrange(year, month)[1]
                    due = dt.date(year, month, min(anchor, length))
                    for _ in range(14):
                        due = next_due(due, cadence, anchor)
                        assert dt.date.fromisoformat(due.isoformat()) == due
                        checked += 1
    assert checked == 4 * 3 * 12 * 3 * 14 == 6048


def test_one_time_and_an_unknown_cadence_never_advance() -> None:
    """No cadence, no next occurrence — the stored date itself."""
    due = dt.date(2026, 1, 31)
    assert next_due(due, "one_time", 31) == due
    assert next_due(due, "fortnightly", 31) == due
    assert occurrences(due, "one_time", 31, dt.date(2026, 1, 1), dt.date(2026, 1, 31)) == [due]
    assert occurrences(due, "one_time", 31, dt.date(2026, 2, 1), dt.date(2026, 2, 28)) == []
    assert first_on_or_after(due, "one_time", 31, dt.date(2026, 1, 1)) == due


def test_an_un_anchored_row_falls_back_to_the_stored_day() -> None:
    """`anchor_day IS NULL` = the pre-M180 behaviour, byte for byte."""
    due = dt.date(2026, 1, 15)
    assert advance_months(due, 1, None) == dt.date(2026, 2, 15)
    assert next_due(due, "monthly", None) == dt.date(2026, 2, 15)
    assert bill_schedule.anchor_day_for(due, None) == 15
    # A stored month-end date with no anchor still clamps like it always did.
    assert next_due(dt.date(2026, 2, 28), "monthly", None) == dt.date(2026, 3, 28)


def test_occurrences_read_the_anchor_and_never_replay_backwards() -> None:
    """The grid projects forward from the stored date, on the ANCHOR's day."""
    # A clamped February is what February's grid shows …
    feb = occurrences(
        dt.date(2026, 2, 28), "monthly", 31, dt.date(2026, 2, 1), dt.date(2026, 2, 28)
    )
    assert feb == [dt.date(2026, 2, 28)]
    # … and March gets the 31st back, not another 28th.
    mar = occurrences(
        dt.date(2026, 2, 28), "monthly", 31, dt.date(2026, 3, 1), dt.date(2026, 3, 31)
    )
    assert mar == [dt.date(2026, 3, 31)]
    apr = occurrences(
        dt.date(2026, 2, 28), "monthly", 31, dt.date(2026, 4, 1), dt.date(2026, 4, 30)
    )
    assert apr == [dt.date(2026, 4, 30)]
    # Months BEFORE the stored date are history the app does not track.
    assert (
        occurrences(dt.date(2026, 2, 28), "monthly", 31, dt.date(2026, 1, 1), dt.date(2026, 1, 31))
        == []
    )
    # A whole year of a 31st schedule, keyed exactly.
    year = occurrences(
        dt.date(2026, 2, 28), "monthly", 31, dt.date(2026, 2, 1), dt.date(2027, 2, 28)
    )
    assert [day.isoformat() for day in year] == [
        "2026-02-28",
        "2026-03-31",
        "2026-04-30",
        "2026-05-31",
        "2026-06-30",
        "2026-07-31",
        "2026-08-31",
        "2026-09-30",
        "2026-10-31",
        "2026-11-30",
        "2026-12-31",
        "2027-01-31",
        "2027-02-28",
    ]


def test_first_on_or_after_steps_the_anchor_not_the_clamped_day() -> None:
    """The confirmed-set read answers 2026-04-30 for a 31st bill started Jan 31."""
    due = dt.date(2026, 1, 31)
    assert first_on_or_after(due, "monthly", 31, dt.date(2026, 4, 15)) == dt.date(2026, 4, 30)
    assert first_on_or_after(due, "monthly", 31, dt.date(2026, 4, 30)) == dt.date(2026, 4, 30)
    assert first_on_or_after(due, "monthly", 31, dt.date(2026, 5, 1)) == dt.date(2026, 5, 31)
    # The clamp is only ever the current occurrence's problem, never the schedule's.
    assert first_on_or_after(dt.date(2026, 2, 28), "monthly", 31, dt.date(2026, 3, 1)) == dt.date(
        2026, 3, 31
    )


def test_stored_due_reads_the_column_or_answers_none() -> None:
    """One reader for the VARCHAR(10), so every surface skips the same rows."""
    assert bill_schedule.stored_due("2026-04-30") == dt.date(2026, 4, 30)
    assert bill_schedule.stored_due("2026-02-31") is None  # a pre-M180 leftover
    assert bill_schedule.stored_due("not-a-date") is None
    assert bill_schedule.stored_due(None) is None


# --------------------------------------------------------------------------- #
# The API — POST /bills, PATCH, POST /bills/{id}/mark-paid
# --------------------------------------------------------------------------- #


def _auth(client: TestClient) -> tuple[dict[str, str], int]:
    from datetime import UTC, datetime

    email = f"m180-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    user_id = client.get(f"{API}/users/me", headers=headers).json()["id"]
    return headers, user_id


def _create(client: TestClient, headers: dict[str, str], *, due: str, frequency: str) -> dict:
    response = client.post(
        BILLS,
        headers=headers,
        json={
            "name": "Mapleview Rent",
            "amount": 1400,
            "frequency": frequency,
            "next_due_date": due,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mark_paid(client: TestClient, headers: dict[str, str], bill_id: int) -> dict:
    response = client.post(f"{BILLS}/{bill_id}/mark-paid", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _stored(session: Session, bill_id: int) -> tuple[str, int | None]:
    """`(next_due_date, anchor_day)` straight from the row, not from the wire."""
    row = session.execute(
        text("SELECT next_due_date, anchor_day FROM bill_reminders WHERE id = :id"),
        {"id": bill_id},
    ).one()
    return row[0], row[1]


def _insert_raw(session: Session, user_id: int, name: str, due: str) -> int:
    """A pre-M180 row: `anchor_day` NULL, whatever the `next_due_date` string says.

    No API path can write either shape (`POST /bills` anchors the day and
    validates the date), which is the point: these rows stand for what the
    database held before M180, and for what a hand edit can still produce.
    """
    session.execute(
        text(
            "INSERT INTO bill_reminders "
            "(user_id, name, frequency, next_due_date, remind_days_before, enabled, "
            " match_mode, match_term, match_logic) "
            "VALUES (:uid, :name, 'monthly', :due, 3, true, 'contains', :name, 'any')"
        ),
        {"uid": user_id, "name": name, "due": due},
    )
    session.commit()
    return session.execute(
        text("SELECT id FROM bill_reminders WHERE user_id = :uid AND name = :name"),
        {"uid": user_id, "name": name},
    ).one()[0]


def test_mark_paid_advances_a_month_end_bill_without_losing_the_day(
    client: TestClient, session: Session
) -> None:
    """The reported defect: Jan 31 → Feb 28 → Mar 31 → Apr 30 (docs/25 §M180)."""
    headers, _ = _auth(client)
    bill = _create(client, headers, due="2026-01-31", frequency="monthly")
    bill_id = bill["id"]
    assert _stored(session, bill_id) == ("2026-01-31", 31)

    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-02-28"
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-03-31"
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-04-30"
    assert _stored(session, bill_id) == ("2026-04-30", 31)


def test_mark_paid_on_a_quarterly_month_end_bill_stores_a_valid_date(
    client: TestClient, session: Session
) -> None:
    """Pre-M180 this wrote `2026-04-31`, which made the NEXT mark-paid a 500."""
    headers, _ = _auth(client)
    bill_id = _create(client, headers, due="2026-01-31", frequency="quarterly")["id"]

    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-04-30"
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-07-31"
    assert _stored(session, bill_id) == ("2026-07-31", 31)


def test_mark_paid_on_a_leap_day_yearly_bill_returns_in_the_next_leap_year(
    client: TestClient, session: Session
) -> None:
    """Pre-M180 this wrote `2029-02-29` — a date that does not exist."""
    headers, _ = _auth(client)
    bill_id = _create(client, headers, due="2028-02-29", frequency="yearly")["id"]

    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2029-02-28"
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2030-02-28"
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2031-02-28"
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2032-02-29"
    assert _stored(session, bill_id) == ("2032-02-29", 29)


def test_patch_that_echoes_the_shown_date_keeps_the_schedule(
    client: TestClient, session: Session
) -> None:
    """The edit form posts back the CLAMPED date it was shown — no demotion.

    `Bills.tsx` always sends `next_due_date` on save, so an unchanged value (the
    February the 31st schedule was shown as) must not re-anchor the schedule to
    the 28th while the user edits the bill's name (docs/25 §M180 D1).
    """
    headers, _ = _auth(client)
    bill_id = _create(client, headers, due="2026-01-31", frequency="monthly")["id"]
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-02-28"

    patched = client.patch(
        f"{BILLS}/{bill_id}",
        headers=headers,
        json={"name": "Mapleview Rent (renamed)", "next_due_date": "2026-02-28"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["next_due_date"] == "2026-02-28"
    assert _stored(session, bill_id)[1] == 31, "an unchanged date re-anchored the schedule"

    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-03-31"


def test_patch_with_a_changed_date_re_anchors_the_schedule(
    client: TestClient, session: Session
) -> None:
    """A date the user actually CHANGED re-defines the schedule."""
    headers, _ = _auth(client)
    bill_id = _create(client, headers, due="2026-01-31", frequency="monthly")["id"]

    patched = client.patch(
        f"{BILLS}/{bill_id}", headers=headers, json={"next_due_date": "2026-03-15"}
    )
    assert patched.status_code == 200, patched.text
    assert _stored(session, bill_id) == ("2026-03-15", 15)
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-04-15"


def test_one_time_mark_paid_disables_the_bill_and_keeps_its_date(
    client: TestClient, session: Session
) -> None:
    """Unchanged M40 behaviour: a one-off has no next occurrence to advance to."""
    headers, _ = _auth(client)
    bill_id = _create(client, headers, due="2026-01-31", frequency="one_time")["id"]

    body = _mark_paid(client, headers, bill_id)
    assert body["enabled"] is False
    assert body["next_due_date"] == "2026-01-31"
    assert _stored(session, bill_id) == ("2026-01-31", 31)
    # Disabled bills leave the list (GET /bills filters `enabled`).
    assert client.get(BILLS, headers=headers).json() == []


def test_mark_paid_refuses_an_unreadable_stored_date_without_writing(
    client: TestClient, session: Session
) -> None:
    """A hand-edited row fails LOUD (422 `invalid_due_date`), never a 500.

    Migration `0054_bill_due_anchor` repaired every value the pre-M180 write
    path could store, so this shape only exists if something wrote the column
    outside the schema layer — the API must still answer an envelope, and must
    not half-apply the write.
    """
    headers, user_id = _auth(client)
    bill_id = _insert_raw(session, user_id, "Legacy", "2026-02-31")

    response = client.post(f"{BILLS}/{bill_id}/mark-paid", headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_due_date"
    assert _stored(session, bill_id) == ("2026-02-31", None)


def test_un_anchored_legacy_row_pins_its_anchor_on_the_first_advance(
    client: TestClient, session: Session
) -> None:
    """A pre-M180 row (NULL anchor) keeps its day and starts clamping correctly."""
    headers, user_id = _auth(client)
    bill_id = _insert_raw(session, user_id, "Legacy 31st", "2026-01-31")
    assert _stored(session, bill_id) == ("2026-01-31", None)

    # The first advance is byte-identical to the pre-M180 answer …
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-02-28"
    # … and the anchor is now pinned, so the 31st comes back.
    assert _stored(session, bill_id) == ("2026-02-28", 31)
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-03-31"


# --------------------------------------------------------------------------- #
# The row-level readers the OTHER surfaces call (docs/25 §M180 D2)
# --------------------------------------------------------------------------- #


def test_row_readers_read_the_anchor_off_a_row_the_api_wrote(
    client: TestClient, session: Session
) -> None:
    """`bill_occurrences` / `bill_next_on_or_after` — one call, row in, dates out.

    These are the two functions `services/cashflow_calendar.py` (the month grid)
    and `services/confirmed_subscriptions.py` (the confirmed set) call, so their
    contract is pinned here rather than in either caller: a mid-month query gets
    the ANCHOR's day, and an unreadable stored date is skipped instead of
    raising.
    """
    headers, user_id = _auth(client)
    bill_id = _create(client, headers, due="2026-01-31", frequency="monthly")["id"]
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-02-28"
    bill = session.get(BillReminder, bill_id)
    assert bill is not None
    assert bill.anchor_day == 31

    feb = dt.date(2026, 2, 1), dt.date(2026, 2, 28)
    mar = dt.date(2026, 3, 1), dt.date(2026, 3, 31)
    apr = dt.date(2026, 4, 1), dt.date(2026, 4, 30)
    assert bill_schedule.bill_occurrences(bill, *feb) == [dt.date(2026, 2, 28)]
    assert bill_schedule.bill_occurrences(bill, *mar) == [dt.date(2026, 3, 31)]
    assert bill_schedule.bill_occurrences(bill, *apr) == [dt.date(2026, 4, 30)]
    assert bill_schedule.bill_next_on_or_after(bill, dt.date(2026, 4, 15)) == dt.date(2026, 4, 30)

    broken = session.get(BillReminder, _insert_raw(session, user_id, "Broken", "2026-02-31"))
    assert broken is not None
    assert bill_schedule.bill_occurrences(broken, *feb) == []
    assert bill_schedule.bill_next_on_or_after(broken, dt.date(2026, 2, 1)) is None


def test_row_reader_drops_a_one_time_bill_once_its_date_has_passed(
    client: TestClient, session: Session
) -> None:
    """A non-repeating bill has no next occurrence — the confirmed-set contract."""
    headers, _ = _auth(client)
    bill_id = _create(client, headers, due="2026-03-05", frequency="one_time")["id"]
    bill = session.get(BillReminder, bill_id)
    assert bill is not None
    assert bill_schedule.bill_next_on_or_after(bill, dt.date(2026, 3, 5)) == dt.date(2026, 3, 5)
    assert bill_schedule.bill_next_on_or_after(bill, dt.date(2026, 3, 6)) is None
    # A recurring bill never expires, it just steps forward.
    assert bill_schedule.bill_next_on_or_after(
        session.get(
            BillReminder, _create(client, headers, due="2026-03-05", frequency="monthly")["id"]
        ),
        dt.date(2026, 3, 6),
    ) == dt.date(2026, 4, 5)


# --------------------------------------------------------------------------- #
# Calendar visibility (docs/36 decision 5)
# --------------------------------------------------------------------------- #


def test_calendar_skips_an_unreadable_date_and_never_500s(
    client: TestClient, session: Session
) -> None:
    """The grid keeps its skip policy: an unreadable row is silent, never a 500."""
    headers, user_id = _auth(client)
    _insert_raw(session, user_id, "Broken", "2026-02-31")
    body = client.get(CAL, headers=headers, params={"month": "2026-02"})
    assert body.status_code == 200, body.text
    assert [event["label"] for day in body.json()["days"] for event in day["events"]] == []


# --------------------------------------------------------------------------- #
# The migration — schema bound + the backfill it actually runs
# --------------------------------------------------------------------------- #


def test_backfill_anchors_and_repairs_pre_m180_rows(client: TestClient, session: Session) -> None:
    """The migration's own statements, run against the shapes it must fix.

    `2026-04-31` and `2029-02-29` are the two values the old write path could
    produce; `2026-02-15` is an ordinary row that must not move, and a row whose
    string is not ISO-shaped keeps it (nothing can invent a date from garbage).
    """
    _, user_id = _auth(client)
    ids = {
        "Quarterly April": _insert_raw(session, user_id, "Quarterly April", "2026-04-31"),
        "Yearly leap": _insert_raw(session, user_id, "Yearly leap", "2029-02-29"),
        "Ordinary": _insert_raw(session, user_id, "Ordinary", "2026-02-15"),
        "Garbage": _insert_raw(session, user_id, "Garbage", "not-a-date"),
    }

    for statement in BACKFILL_STATEMENTS:
        session.execute(text(statement))
    session.commit()

    def state(name: str) -> tuple[str, int | None]:
        return _stored(session, ids[name])

    assert state("Quarterly April") == ("2026-04-30", 31)  # clamped, day kept
    assert state("Yearly leap") == ("2029-02-28", 29)  # the leap day is remembered
    assert state("Ordinary") == ("2026-02-15", 15)  # untouched
    assert state("Garbage") == ("not-a-date", None)  # nothing invented

    # Idempotent: re-running the backfill changes nothing (a second
    # `alembic upgrade` on a re-seeded row cannot drift a schedule).
    for statement in BACKFILL_STATEMENTS:
        session.execute(text(statement))
    session.commit()
    assert state("Quarterly April") == ("2026-04-30", 31)
    assert state("Yearly leap") == ("2029-02-28", 29)
    assert state("Ordinary") == ("2026-02-15", 15)
    assert state("Garbage") == ("not-a-date", None)

    # And a repaired row now behaves: April 30 + 3 months is July 31.
    repaired = session.get(BillReminder, ids["Quarterly April"])
    assert repaired is not None
    assert next_due(dt.date(2026, 4, 30), "quarterly", repaired.anchor_day) == dt.date(2026, 7, 31)


def test_anchor_day_bound_lives_in_the_table(client: TestClient, session: Session) -> None:
    """`ck_bill_reminders_anchor_day` — a 0th or 32nd day is refused by the DB."""
    _, user_id = _auth(client)
    bill_id = _insert_raw(session, user_id, "Bound", "2026-02-10")

    for bad in (0, 32):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text("UPDATE bill_reminders SET anchor_day = :day WHERE id = :id"),
                {"day": bad, "id": bill_id},
            )

    row = session.execute(
        text("SELECT anchor_day FROM bill_reminders WHERE id = :id"), {"id": bill_id}
    ).one()
    assert row[0] is None  # the refused writes changed nothing (it is un-anchored)


def test_the_module_imports_no_network_client() -> None:
    """The rule is local arithmetic — no fx read, no HTTP (AGENTS §2, docs/11)."""
    source = Path(bill_schedule.__file__).read_text(encoding="utf-8")
    for forbidden in ("httpx", "requests", "yfinance", "urlopen", "app.services.fx"):
        assert forbidden not in source, forbidden

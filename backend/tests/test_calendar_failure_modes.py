"""The calendar/display-mask failure-mode matrix, end to end (t_742c9dc0).

Three failure modes are named on the card — month-end, currency, restore-null.
Each is pinned where it actually lives, and the two halves are JOINED by one
captured payload so the layer boundary itself is under test:

* **month-end x currency** (this module) — a 31st-anchored, all-USD group walked
  through the REAL `GET /reports/cashflow-calendar` route over the months that
  lack the day (Feb, Apr) and the month that has it back (Mar), the
  mixed-currency twin that must state NO figure in any of them, and the
  TAG-marked twin of the same series on the source that never runs the accept
  route (6c7b5ea's path — no stored `recurring_next_date` to project from). The
  two axes are each covered alone by
  `tests/test_calendar_recurrence_matrix.py` (month-end walk, currency
  pairing); what was never crossed is a FOREIGN currency group on a clamped
  month, which is where the read path would reach for a rate ("just convert
  it, the date is wrong anyway") without any single axis noticing, nor the two
  SOURCES of a confirmed subscription (accepted vs tag-marked) side by side on
  the same clamped chain.
* **restore-null** is client-only by design — docs/46 D5: the mask never
  touches the wire, so there is no server half to test. Its rows live in
  `frontend/tests/calendarFailureModes.test.tsx`, which boots the real
  `AuthProvider` with `fetch` stubbed at `/auth/refresh` (the shape that
  RESOLVES null, never rejects — c265cdd) and then renders the calendar.
* **the join** — `frontend/tests/fixtures/calendar-failure-modes.json` holds
  ONE payload captured from this route. This module byte-diffs the live route
  against it; the frontend file renders it. A field rename, a dropped currency,
  a flipped sign or a re-ordered day fails HERE (server stopped producing it)
  and THERE (client stopped reading it) instead of passing both suites that
  hand-author their own shapes.

Re-capture (only to record a deliberate contract change, never to go green):

    CALENDAR_GOLDEN=1 uv run pytest tests/test_calendar_failure_modes.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.test_calendar_recurrence_matrix import (
    _CALENDAR_CLOCK_BINDINGS,
    _account,
    _auth,
    _events,
    _get,
    _repin_next,
    _txn,
)
from tests.test_m174_recurring_intent_tags import _nominate, _tag, _tag_rows

ET = ZoneInfo("America/Toronto")

#: The instant the GOLDEN payload is read at: the 1st of the month that lacks
#: the 31st, so February's event is still `upcoming` and every field of the
#: response (including `today`) is a fixed, reproducible value.
NOW = dt.datetime(2027, 2, 1, 12, 0, tzinfo=ET)

#: The fixture both halves read. Captured from the route, never hand-written.
GOLDEN_PATH = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "tests"
    / "fixtures"
    / "calendar-failure-modes.json"
)

#: The month the fixture holds — the one that lacks the 31st.
GOLDEN_MONTH = "2027-02"

#: The command that re-captures it. Deliberate contract changes only — never a
#: way to make a red assertion green.
CAPTURE_COMMAND = "CALENDAR_GOLDEN=1 uv run pytest tests/test_calendar_failure_modes.py"

#: First-capture metadata. An existing file's own `meta` always wins, so a
#: re-capture can never rewrite what the fixture says about itself.
_DEFAULT_META = {
    "slice": "calendar-failure-modes",
    "base_commit": "captured from the route, never hand-written",
    "capture_command": CAPTURE_COMMAND,
    "notes": [
        "Cross-layer failure-mode matrix (t_742c9dc0): one CashflowCalendar "
        "payload — a 31st-anchored all-USD group on the month that lacks the "
        "31st, frozen to a reproducible clock. Captured by the backend route, "
        "byte-diffed by tests/test_calendar_failure_modes.py, rendered by "
        "frontend/tests/calendarFailureModes.test.tsx."
    ],
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every input here is a cached row; a fetch attempt is a test failure.

    Carried over from the matrix module: the currency rows are only meaningful
    if the read path answers from the row's own denomination, so a rate or
    price fetch during a calendar GET must blow up rather than answer.
    """

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network fetch attempted although all data is cached")

    from app.services import fx as fx_service
    from app.services import market_data

    monkeypatch.setattr(market_data, "fetch_latest_close", forbidden)
    monkeypatch.setattr(market_data, "fetch_history", forbidden)
    monkeypatch.setattr(fx_service, "fetch_valet_series", forbidden)


def _seed_group(
    client: TestClient,
    session: Session,
    headers: dict[str, str],
    user_id: int,
    *,
    payee: str,
    rows: tuple[tuple[str, str], ...],
    charge_days: tuple[dt.date, ...],
    tag: str,
) -> None:
    """An accepted recurring group, seeded exactly the way a user creates one.

    `rows` is `(account_currency, amount_native)` per charge, so an all-USD
    group and a mixed group differ only in their rows. The charges are dated on
    the 31st because that is where the projection takes its anchor from: the
    accept route writes `recurring_*` from the group's charges, and
    `_group_next_date` re-derives the NEWEST CHARGE'S day through the shared
    whole-month rule (the shape
    `test_month_end_accepted_group_uses_the_shared_schedule_rule` pins) — so a
    group whose newest charge is the 31st is a 31st-anchored group, whatever
    the clamped accept row stored.
    """
    assert len(charge_days) == len(rows)
    anchor = max(charge_days)
    charges = []
    for (currency, amount), day in zip(rows, charge_days, strict=True):
        account = _account(session, user_id, currency=currency)
        charges.append(
            _txn(
                session,
                user_id,
                account,
                payee=payee,
                day=day,
                amount=amount,
                currency=currency,
                dedup=f"failure-mode-{tag}-{day.isoformat()}",
            )
        )
    accepted = client.post(
        "/api/v1/transactions/recurring/accept",
        headers=headers,
        json={
            "group_key": f"{payee.replace(' ', '').upper()}|monthly",
            "cadence": "monthly",
            "transaction_ids": [txn.id for txn in charges],
        },
    )
    assert accepted.status_code == 200, accepted.text
    _repin_next(session, charges, anchor)


def _month(client: TestClient, headers: dict[str, str], month: str) -> dict:
    return _get(client, headers, month)


def _subscription(body: dict) -> list[tuple[str, dict]]:
    return _events(body, "subscription")


def _freeze(monkeypatch: pytest.MonkeyPatch, instant: dt.datetime) -> None:
    """Pin every clock binding the calendar route reads to `instant`.

    The mechanism is the matrix module's `_freeze_et_now` (replace the `dt`
    symbol the app's own `today_et` reads, then re-point the bindings), but
    restated here so ONE test can move the clock month by month for a walk:
    the bindings are pointed straight at the frozen date rather than chained
    through the freeze that was installed before them.
    """
    assert instant.tzinfo is not None, "freeze with an AWARE instant"

    class _FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return instant if tz is None else instant.astimezone(tz)

    from app.services import txn_filters

    monkeypatch.setattr(
        txn_filters,
        "dt",
        SimpleNamespace(datetime=_FrozenDatetime, date=dt.date, timedelta=dt.timedelta),
    )
    day = instant.astimezone(ET).date()
    for target in _CALENDAR_CLOCK_BINDINGS:
        monkeypatch.setattr(target, lambda frozen=day: frozen)


# --------------------------------------------------------------------------- #
# month-end x currency, through the route
# --------------------------------------------------------------------------- #


#: (queried month, the day the clock reads that month, the date the ONE shared
#: rule gives for a 31st anchor): the two clamped months and the month where
#: the 31st comes back (the M180 defect: advancing from the previously clamped
#: date used to lose it).
WALK: tuple[tuple[str, str, str], ...] = (
    ("2027-02", "2027-02-01", "2027-02-28"),
    ("2027-03", "2027-03-01", "2027-03-31"),
    ("2027-04", "2027-04-01", "2027-04-30"),
)


def _freeze_to(monkeypatch: pytest.MonkeyPatch, iso_day: str) -> None:
    day = dt.date.fromisoformat(iso_day)
    _freeze(monkeypatch, dt.datetime.combine(day, dt.time(12, 0), tzinfo=ET))


def test_month_end_usd_group_walks_the_rule_and_states_usd_on_every_month(
    client: TestClient,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 31st-anchored USD group lands on the rule's date AND stays USD.

    Feb 2027 (28 days) and Apr 2027 (30 days) are the clamped months; Mar is
    where the 31st comes back. The group's rows are USD, so every month's event
    must carry the pair `("-11.99", "USD")` — never a converted CAD figure,
    never a bare number, and never a date computed by a second rule. The clock
    is moved month by month because the confirmed set answers ONE next date:
    each month has to be read with a `today` inside it, exactly as a user
    browsing that month would.
    """
    _freeze(monkeypatch, NOW)
    headers, user_id = _auth(client)
    _seed_group(
        client,
        session,
        headers,
        user_id,
        payee="MATRIX FAILURE USD",
        rows=(("USD", "-11.99"), ("USD", "-11.99")),
        charge_days=(dt.date(2026, 12, 31), dt.date(2027, 1, 31)),
        tag="usd",
    )

    walked: dict[str, list[tuple[str, dict]]] = {}
    for month, today, expected_day in WALK:
        _freeze_to(monkeypatch, today)
        walked[month] = _subscription(_month(client, headers, month))
        assert [day for day, _event in walked[month]] == [expected_day], month
        assert next(event for _day, event in walked[month])["status"] == "upcoming", month

    for month, events in walked.items():
        for _day, event in events:
            assert (event["amount"], event["currency"]) == ("-11.99", "USD"), (month, event)
            # The pair is all-or-nothing: a stated figure without its code —
            # or a code without a figure — is the failure this row exists for.
            assert (event["amount"] is None) == (event["currency"] is None), (month, event)


def test_month_end_mixed_group_withholds_the_pair_on_every_month_of_the_walk(
    client: TestClient,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same walk, mixed rows: the grid states NO figure in any month.

    A mean over two currencies is a number with no denomination (docs/36
    §Currency), and a clamped month is exactly where a renderer under time
    pressure would fall back to `0.00` or the reporting currency. The event
    must still be drawn — on the rule's date — carrying `(None, None)`.
    """
    _freeze(monkeypatch, NOW)
    headers, user_id = _auth(client)
    _seed_group(
        client,
        session,
        headers,
        user_id,
        payee="MATRIX FAILURE MIXED",
        rows=(("CAD", "-11.99"), ("USD", "-12.99")),
        charge_days=(dt.date(2026, 12, 31), dt.date(2027, 1, 31)),
        tag="mixed",
    )

    for month, today, expected_day in WALK:
        _freeze_to(monkeypatch, today)
        events = _subscription(_month(client, headers, month))
        assert [day for day, _event in events] == [expected_day], month
        for _day, event in events:
            assert (event["amount"], event["currency"]) == (None, None), (month, event)


def test_tag_marked_group_keeps_the_month_end_anchor_and_states_its_currency(
    client: TestClient,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tag-marked twin of the walk: SOURCE_TAG shares the accepted group's rule.

    `_tag_intent_groups` never runs the accept route, so it has no stored
    `recurring_next_date` to project from — before 6c7b5ea it rolled the
    DETECTOR's own next date forward, and the detector fits a Jan-31 series to
    Feb-28, so the clamp leaked forward into every later month (Apr 28 instead
    of Apr 30). The rows here are the same 31st-anchored USD series on the
    other source, so this row flips on that fix and on a currency regression
    without needing the accept route at all.
    """
    _freeze(monkeypatch, NOW)
    headers, user_id = _auth(client)
    account = _account(session, user_id, currency="USD")
    intent = _tag(session, user_id, "subscription")
    rows = [
        _txn(
            session,
            user_id,
            account,
            payee="MATRIX FAILURE TAGGED",
            day=day,
            amount="-11.99",
            currency="USD",
            dedup=f"failure-mode-tagged-{day.isoformat()}",
        )
        # Same-day-of-month run ending on the 31st: a perfect monthly fit, so
        # the detector's math gate admits it and only the PROJECTION differs.
        for day in (dt.date(2026, 11, 30), dt.date(2026, 12, 31), dt.date(2027, 1, 31))
    ]
    assert intent.id is not None
    _tag_rows(session, [intent], rows)
    _nominate(session, user_id, [intent.id])

    for month, today, expected_day in WALK:
        _freeze_to(monkeypatch, today)
        events = _subscription(_month(client, headers, month))
        assert [day for day, _event in events] == [expected_day], month
        for _day, event in events:
            assert (event["amount"], event["currency"]) == ("-11.99", "USD"), (month, event)


# --------------------------------------------------------------------------- #
# the join: one payload, both halves
# --------------------------------------------------------------------------- #


def _golden_case() -> dict:
    assert GOLDEN_PATH.exists(), f"missing golden {GOLDEN_PATH}"
    payload = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    case = next(c for c in payload["cases"] if c["name"] == "month_end_usd_feb")
    return case


def test_the_captured_frontend_golden_is_still_what_the_route_answers(
    client: TestClient,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payload `frontend/tests/calendarFailureModes.test.tsx` renders.

    Seeded from the same inputs the fixture records, so the diff covers the
    WHOLE response — `days` (28 cells, zero-filled), `today`, `status` and
    `confirmed_subscription_count`, not just the events the assertions name.
    A field added, renamed, reordered or re-signed fails here; a field the
    client stops reading fails in the frontend file that renders it.
    """
    _freeze(monkeypatch, NOW)
    headers, user_id = _auth(client)
    _seed_group(
        client,
        session,
        headers,
        user_id,
        payee="MATRIX FAILURE USD",
        rows=(("USD", "-11.99"), ("USD", "-11.99")),
        charge_days=(dt.date(2026, 12, 31), dt.date(2027, 1, 31)),
        tag="usd",
    )

    live = _month(client, headers, GOLDEN_MONTH)

    if os.environ.get("CALENDAR_GOLDEN") == "1":
        existing = (
            json.loads(GOLDEN_PATH.read_text(encoding="utf-8")) if GOLDEN_PATH.exists() else {}
        )
        case = {
            "name": "month_end_usd_feb",
            "seed": {
                "frozen_now": "2027-02-01T12:00:00-05:00",
                "payee": "MATRIX FAILURE USD",
                "rows": [["USD", "-11.99"], ["USD", "-11.99"]],
                "charge_days": ["2026-12-31", "2027-01-31"],
            },
            "request": {"month": GOLDEN_MONTH},
            "response": live,
        }
        cases = [c for c in existing.get("cases", []) if c["name"] != case["name"]]
        cases.append(case)
        GOLDEN_PATH.write_text(
            json.dumps(
                {
                    "meta": existing.get("meta", _DEFAULT_META),
                    "cases": cases,
                },
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        pytest.skip(f"golden captured at {GOLDEN_PATH}")

    case = _golden_case()
    assert live == case["response"], (
        "the route's answer no longer matches the payload the frontend renders "
        f"(re-capture ONLY to record a deliberate contract change: {CAPTURE_COMMAND})"
    )

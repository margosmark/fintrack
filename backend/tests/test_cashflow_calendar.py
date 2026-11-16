"""M52 cash-flow calendar — `GET /reports/cashflow-calendar` (docs/36).

The endpoint composes three sources that already own their own math (docs/25
bills, M29 dividend calendar, M27 subscription detector), so these tests pin
the *composition* contract: zero-filled days, `today_et()` day boundary,
forward-only bill projection with day-of-month clamping, signed `amount` plus
the `currency` it is stated in, the per-kind `source_id`, the pinned
`invalid_month` envelope, tenancy and staging. The `autouse` fixture makes any
network attempt a failure — every input here is a cached row (docs/07,
docs/12).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.bill_reminders import BillReminder
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.models.transactions import Transaction
from app.services.confirmed_subscriptions import confirmed_subscriptions
from app.services.dividend_calendar import compute_dividend_calendar
from app.services.subscriptions import detect_subscriptions
from app.services.txn_filters import today_et

API = "/api/v1"
CAL = f"{API}/reports/cashflow-calendar"


def _today() -> dt.date:
    """The clock, read at CALL time — never captured at import.

    pytest imports every test module long before it runs any of them, so a
    date frozen when this module is imported is already a day stale by the
    time a suite run crosses local midnight, while the product re-reads
    ``today_et()`` on every request. The two disagreed by a day and
    ``test_status_is_relative_to_today_et`` reported ``past`` where the live
    clock said ``today`` (t_435c2d14).
    """
    return today_et()


def _auth(client: TestClient) -> tuple[dict[str, str], int]:
    from datetime import UTC, datetime

    email = f"m52cal-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    user_id = client.get(f"{API}/users/me", headers=headers).json()["id"]
    return headers, user_id


def _account(session: Session, user_id: int, *, currency: str = "CAD") -> Account:
    account = Account(
        user_id=user_id,
        name=f"Acct {user_id} {currency}",
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
    enabled: bool = True,
) -> BillReminder:
    bill = BillReminder(
        user_id=user_id,
        name=name,
        amount=None if amount is None else Decimal(amount),
        frequency=frequency,
        next_due_date=due,
        enabled=enabled,
        match_term=name,
    )
    session.add(bill)
    session.commit()
    session.refresh(bill)
    return bill


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


def _batch(session: Session, user_id: int, account_id: int, status: str) -> ImportBatch:
    batch = ImportBatch(
        user_id=user_id,
        account_id=account_id,
        institution="investment_csv",
        file_name="staged.csv",
        status=ImportBatchStatus(status),
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def _dividend(
    session: Session,
    user_id: int,
    account: Account,
    security: Security,
    *,
    day: dt.date,
    amount: str,
    batch_id: int | None = None,
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
        import_batch_id=batch_id,
    )
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def _txn(
    session: Session,
    user_id: int,
    account: Account,
    *,
    payee: str,
    day: dt.date,
    amount: str,
    currency: str = "CAD",
    batch_id: int | None = None,
    dedup: str | None = None,
) -> Transaction:
    txn = Transaction(
        user_id=user_id,
        account_id=account.id,
        date=day,
        raw_description=payee,
        normalized_payee=payee,
        amount_native=Decimal(amount),
        currency=currency,
        import_batch_id=batch_id,
        dedup_hash=dedup or f"m52-{user_id}-{payee}-{day.isoformat()}-{amount}",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _accept(client: TestClient, headers: dict[str, str], txns: list[Transaction]) -> None:
    """Accept a recurring group the way the user does (M96, docs/09).

    The REAL route, not a direct column write: `is_recurring` +
    `recurring_cadence` + `recurring_next_date` are the accept action's
    output, and M170 makes that output the calendar's subscription series —
    so the user-asserted path is what these tests have to exercise.
    """
    if not txns:
        return
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


def _repin_next(session: Session, txns: list[Transaction], day: dt.date) -> None:
    """Put an accepted group's `recurring_next_date` on `day`.

    The accept route writes that column (docs/09), and the calendar serves a
    stored date that is not in the past verbatim — so this is the state a real
    accept leaves when the next charge is due on `day`, used by the tests that
    need a subscription event on a SPECIFIC date rather than on the one the
    cadence happens to project.
    """
    for txn in txns:
        txn.recurring_next_date = day
        session.add(txn)
    session.commit()


def _get(client: TestClient, headers: dict[str, str], month: str | None = None):
    params = {} if month is None else {"month": month}
    response = client.get(CAL, headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _by_date(body: dict) -> dict[str, list[dict]]:
    return {day["date"]: day["events"] for day in body["days"]}


def _events(body: dict, kind: str) -> list[tuple[str, dict]]:
    return [
        (day["date"], event)
        for day in body["days"]
        for event in day["events"]
        if event["kind"] == kind
    ]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every input is a cached row; a fetch attempt is a test failure."""

    def forbidden(*args, **kwargs):
        raise AssertionError("network fetch attempted although all data is cached")

    from app.services import fx as fx_service
    from app.services import market_data

    monkeypatch.setattr(market_data, "fetch_latest_close", forbidden)
    monkeypatch.setattr(market_data, "fetch_history", forbidden)
    monkeypatch.setattr(fx_service, "fetch_valet_series", forbidden)


# ---------------------------------------------------------------------------
# Shape: zero-filled days, month echo, today_et boundary
# ---------------------------------------------------------------------------


def test_shape_zero_fills_every_day_of_the_month(client: TestClient) -> None:
    headers, _user_id = _auth(client)
    body = _get(client, headers, "2026-02")

    assert body["month"] == "2026-02"
    assert body["today"] == today_et().isoformat()
    assert len(body["days"]) == 28
    dates = [day["date"] for day in body["days"]]
    assert dates == sorted(dates)
    assert dates[0] == "2026-02-01" and dates[-1] == "2026-02-28"
    # zero-filled: every day present, every events list a list
    for day in body["days"]:
        assert day["events"] == []


def test_month_lengths_are_calendar_correct(client: TestClient) -> None:
    headers, _user_id = _auth(client)
    assert len(_get(client, headers, "2026-01")["days"]) == 31
    assert len(_get(client, headers, "2026-12")["days"]) == 31
    assert len(_get(client, headers, "2024-02")["days"]) == 29  # leap year
    assert len(_get(client, headers, "2026-04")["days"]) == 30


def test_absent_month_defaults_to_the_current_today_et_month(client: TestClient) -> None:
    headers, _user_id = _auth(client)
    body = _get(client, headers)
    assert body["month"] == today_et().strftime("%Y-%m")
    assert (
        len(body["days"])
        == (
            (today_et().replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            - dt.timedelta(days=1)
        ).day
    )


def test_requires_auth(client: TestClient) -> None:
    assert client.get(CAL, params={"month": "2026-09"}).status_code == 401


# ---------------------------------------------------------------------------
# Bills (docs/25) — due date, forward projection, clamping, filtering
# ---------------------------------------------------------------------------


def test_bill_event_lands_on_its_due_date(client: TestClient, session: Session) -> None:
    headers, user_id = _auth(client)
    bill = _bill(session, user_id, name="Fibrestream Internet", due="2026-09-05", amount="79.99")

    events = _by_date(_get(client, headers, "2026-09"))["2026-09-05"]
    assert [
        {
            "kind": e["kind"],
            "label": e["label"],
            "amount": e["amount"],
            "currency": e["currency"],
            "source_id": e["source_id"],
            "status": e["status"],
        }
        for e in events
    ] == [
        {
            "kind": "bill",
            "label": "Fibrestream Internet",
            "amount": "-79.99",  # signed: outflow (docs/36 decision 6)
            "currency": "CAD",  # bills are reporting-currency by schema
            "source_id": str(bill.id),
            "status": "past" if _today() > dt.date(2026, 9, 5) else "upcoming",
        }
    ]
    # the due day is the ONLY day with an event in that month
    assert {d for d, evs in _by_date(_get(client, headers, "2026-09")).items() if evs} == {
        "2026-09-05"
    }


def test_bill_projects_forward_with_day_of_month_clamping(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client)
    _bill(session, user_id, name="Rent", due="2026-01-31", amount="1400.00")
    _bill(
        session,
        user_id,
        name="Quarterly fee",
        due="2026-01-15",
        amount="10.00",
        frequency="quarterly",
    )
    _bill(session, user_id, name="Annual", due="2026-01-15", amount="60.00", frequency="yearly")
    _bill(session, user_id, name="One-off", due="2026-01-15", amount="5.00", frequency="one_time")

    feb = {d for d, _e in _events(_get(client, headers, "2026-02"), "bill")}
    assert feb == {"2026-02-28"}  # clamped: February has no 31st

    apr = {d for d, _e in _events(_get(client, headers, "2026-04"), "bill")}
    # 31st survives the short month: occurrences are anchored on the original
    # due date, so April gets the 30th (docs/36 decision 5).
    assert apr == {"2026-04-15", "2026-04-30"}

    assert {d for d, _e in _events(_get(client, headers, "2026-07"), "bill")} == {
        "2026-07-15",
        "2026-07-31",
    }
    jan27 = _events(_get(client, headers, "2027-01"), "bill")
    # quarterly + yearly + the 31st all roll into 2027; one_time never repeats
    assert {d for d, _e in jan27} == {"2027-01-15", "2027-01-31"}
    assert "One-off" not in [e["label"] for _d, e in jan27]


def test_bill_is_never_replayed_backwards(client: TestClient, session: Session) -> None:
    """Months before `next_due_date` are history the app does not track
    (docs/25 decision 4, docs/36 decision 5)."""
    headers, user_id = _auth(client)
    _bill(session, user_id, name="Mapleview Rent", due="2026-09-05", amount="1400.00")

    assert _events(_get(client, headers, "2026-08"), "bill") == []
    assert len(_events(_get(client, headers, "2026-09"), "bill")) == 1


def test_disabled_bill_is_excluded_and_a_variable_bill_has_null_amount(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client)
    _bill(session, user_id, name="Disabled", due="2026-09-05", amount="10.00", enabled=False)
    variable = _bill(session, user_id, name="Hydro (variable)", due="2026-09-12", amount=None)

    events = _events(_get(client, headers, "2026-09"), "bill")
    assert [(d, e["label"], e["amount"]) for d, e in events] == [
        ("2026-09-12", "Hydro (variable)", None)
    ]
    assert events[0][1]["source_id"] == str(variable.id)


def test_status_is_relative_to_today_et(client: TestClient, session: Session) -> None:
    headers, user_id = _auth(client)
    yesterday, today, tomorrow = (
        _today() - dt.timedelta(days=1),
        _today(),
        _today() + dt.timedelta(days=1),
    )
    _bill(session, user_id, name="Yesterday", due=yesterday.isoformat())
    _bill(session, user_id, name="Today", due=today.isoformat())
    _bill(session, user_id, name="Tomorrow", due=tomorrow.isoformat())

    for day, label, expected in (
        (yesterday, "Yesterday", "past"),
        (today, "Today", "today"),
        (tomorrow, "Tomorrow", "upcoming"),
    ):
        events = _by_date(_get(client, headers, day.strftime("%Y-%m")))[day.isoformat()]
        matching = [e for e in events if e["label"] == label]
        assert len(matching) == 1, f"expected exactly one {label!r} event on {day}"
        assert matching[0]["status"] == expected


def test_unparseable_stored_due_date_is_skipped_not_500(
    client: TestClient, session: Session
) -> None:
    """`bill_reminders.next_due_date` is a `VARCHAR(10)`, so a hand-edited row can
    hold a non-date (the write path itself is CLOSED by M180: `mark-paid` answers
    422 `invalid_due_date`) — the calendar keeps its skip policy, never a 500."""
    headers, user_id = _auth(client)
    _bill(session, user_id, name="Broken", due="2026-02-31")
    _bill(session, user_id, name="Fine", due="2026-02-10")

    body = _get(client, headers, "2026-02")
    assert [e["label"] for _d, e in _events(body, "bill")] == ["Fine"]


# ---------------------------------------------------------------------------
# M180 — the grid reads the schedule's ANCHOR (docs/25 §M180, docs/36 decision 5)
# ---------------------------------------------------------------------------

BILLS = f"{API}/bills"


def _create_bill(
    client: TestClient,
    headers: dict[str, str],
    *,
    due: str,
    frequency: str = "monthly",
    name: str = "Mapleview Rent",
) -> dict:
    """`POST /bills` — the API anchors the day it was given (docs/25 §M180 D1)."""
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


def test_the_grid_keeps_a_month_end_schedule_the_write_path_clamped(
    client: TestClient, session: Session
) -> None:
    """The reported defect, on this route: Jan 31 → Feb 28 → **Mar 31** → Apr 30.

    Before the readers called `services/bill_schedule.py`, `mark-paid` stored
    `2026-02-28` and the grid projected from that clamped value, rendering
    `2026-03-28` — the 31st survived exactly one advance (docs/25 §M180, docs/36
    decision 5).
    """
    headers, _user_id = _auth(client)
    bill_id = _create_bill(client, headers, due="2026-01-31")["id"]
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-02-28"
    row = session.get(BillReminder, bill_id)
    assert row is not None
    assert (row.next_due_date, row.anchor_day) == ("2026-02-28", 31)

    assert [d for d, _e in _events(_get(client, headers, "2026-02"), "bill")] == ["2026-02-28"]
    assert [d for d, _e in _events(_get(client, headers, "2026-03"), "bill")] == ["2026-03-31"]
    assert [d for d, _e in _events(_get(client, headers, "2026-04"), "bill")] == ["2026-04-30"]


def test_the_grid_returns_a_leap_day_schedule_in_the_next_leap_year(
    client: TestClient, session: Session
) -> None:
    """A yearly Feb-29 bill: Feb 28 for three years, then Feb 29 again."""
    headers, _user_id = _auth(client)
    bill_id = _create_bill(client, headers, due="2028-02-29", frequency="yearly")["id"]
    for expected in ("2029-02-28", "2030-02-28", "2031-02-28"):
        assert _mark_paid(client, headers, bill_id)["next_due_date"] == expected

    assert [d for d, _e in _events(_get(client, headers, "2031-02"), "bill")] == ["2031-02-28"]
    assert [d for d, _e in _events(_get(client, headers, "2032-02"), "bill")] == ["2032-02-29"]


def test_the_calendar_and_the_confirmed_query_agree_on_a_month_end_bill(
    client: TestClient, session: Session
) -> None:
    """ONE rule, two surfaces (docs/36 decision 5): the bill's next date in the
    confirmed set IS the date this grid draws for it.

    `today` is pinned so the assertion names a date instead of depending on the
    run date: from the stored, clamped `2026-02-28` the ANCHOR says April 30 —
    projecting the stored date again would answer April 28.
    """
    headers, user_id = _auth(client)
    bill_id = _create_bill(client, headers, due="2026-01-31")["id"]
    assert _mark_paid(client, headers, bill_id)["next_due_date"] == "2026-02-28"

    entry = next(
        subscription
        for subscription in confirmed_subscriptions(session, user_id, today=dt.date(2026, 4, 15))
        if subscription.payee == "Mapleview Rent"
    )
    assert entry.next_expected == dt.date(2026, 4, 30)
    assert [d for d, _e in _events(_get(client, headers, "2026-04"), "bill")] == [
        entry.next_expected.isoformat()
    ]


def test_an_un_anchored_row_keeps_the_pre_m180_answer(client: TestClient, session: Session) -> None:
    """`anchor_day IS NULL` = "the stored date's own day" (docs/25 §M180 D1), so a
    row written outside the API (a fixture, a seed, a hand edit) projects exactly
    as it did before M180 — this wiring changes nothing for legacy rows."""
    headers, user_id = _auth(client)
    legacy = _bill(session, user_id, name="Legacy 28th", due="2026-02-28")

    assert (legacy.next_due_date, legacy.anchor_day) == ("2026-02-28", None)
    assert [d for d, _e in _events(_get(client, headers, "2026-02"), "bill")] == ["2026-02-28"]
    assert [d for d, _e in _events(_get(client, headers, "2026-03"), "bill")] == ["2026-03-28"]


# ---------------------------------------------------------------------------
# Dividends (M29) and subscriptions (M27) — the source's own dates
# ---------------------------------------------------------------------------


def test_dividend_event_uses_the_m29_next_payment_date(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    security = _security(session, user_id, "RY")
    _dividend(
        session, user_id, account, security, day=_today() - dt.timedelta(days=100), amount="25.00"
    )
    _dividend(
        session, user_id, account, security, day=_today() - dt.timedelta(days=9), amount="35.00"
    )

    projected = compute_dividend_calendar(session, user_id)
    entry = projected["upcoming"][0]
    assert entry["symbol"] == "RY"
    month = entry["next_expected_date"].strftime("%Y-%m")

    events = _events(_get(client, headers, month), "dividend")
    assert [(d, e) for d, e in events] == [
        (
            entry["next_expected_date"].isoformat(),
            {
                "kind": "dividend",
                "label": "RY dividend",
                "amount": str(entry["avg_amount"]),  # +30.00
                "currency": "CAD",  # M29 projects the gross CAD amount
                "source_id": str(security.id),
                "status": "upcoming" if entry["next_expected_date"] > _today() else "past",
            },
        )
    ]
    assert entry["avg_amount"] == Decimal("30.00")


def test_subscription_event_is_the_accepted_group_next_occurrence(
    client: TestClient, session: Session
) -> None:
    """M170: the subscription series is CONFIRMED state — the group the user
    accepted — not the detector's read of the same rows.

    Both facts are seeded on purpose: `detect_subscriptions` DOES see these
    two Netflix charges (so the old feed would have drawn the same event), and
    the event that reaches the calendar comes from the accept action instead.
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    charges = [
        _txn(
            session,
            user_id,
            account,
            payee="NETFLIX.COM",
            day=_today() - dt.timedelta(days=60),
            amount="-15.99",
        ),
        _txn(
            session,
            user_id,
            account,
            payee="NETFLIX.COM",
            day=_today() - dt.timedelta(days=30),
            amount="-15.99",
        ),
    ]
    _accept(client, headers, charges)

    # The detector still sees it (it is a detector, not the fact source).
    detected = detect_subscriptions(session, user_id)
    assert [subscription["payee"] for subscription in detected] == ["NETFLIX.COM"]
    # The event sits on the CONFIRMED group's next occurrence — the shared
    # query's answer (docs/36 §M170), which is the accept route's stored date
    # rolled forward by the group's cadence until it reaches `today`.
    confirmed = [
        entry for entry in confirmed_subscriptions(session, user_id) if entry.source == "recurring"
    ]
    assert [entry.payee for entry in confirmed] == ["NETFLIX.COM"]
    expected = confirmed[0].next_expected
    assert expected >= _today()

    events = _events(_get(client, headers, expected.strftime("%Y-%m")), "subscription")
    assert [(d, e) for d, e in events] == [
        (
            expected.isoformat(),
            {
                "kind": "subscription",
                "label": "NETFLIX.COM",
                "amount": "-15.99",
                "currency": "CAD",  # the CAD control: a group whose rows are CAD
                "source_id": "NETFLIX.COM",
                "status": (
                    "past"
                    if expected < _today()
                    else "today"
                    if expected == _today()
                    else "upcoming"
                ),
            },
        )
    ]


def test_events_sort_by_kind_then_label_within_a_day(client: TestClient, session: Session) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    security = _security(session, user_id, "BMO")
    _bill(session, user_id, name="Zed bill", due=_today().isoformat(), amount="1.00")
    _bill(session, user_id, name="Alpha bill", due=_today().isoformat(), amount="2.00")
    # dividend + subscription on the same day as the bills
    # dividend projected onto today: quarterly payments 182 and 91 days ago
    _dividend(
        session, user_id, account, security, day=_today() - dt.timedelta(days=182), amount="10.00"
    )
    _dividend(
        session, user_id, account, security, day=_today() - dt.timedelta(days=91), amount="10.00"
    )
    aaa = [
        _txn(
            session,
            user_id,
            account,
            payee="AAA STORE",
            day=_today() - dt.timedelta(days=30),
            amount="-5.00",
        ),
        _txn(
            session,
            user_id,
            account,
            payee="AAA STORE",
            # The NEWEST charge is dated TODAY on purpose — not cosmetic. The
            # calendar serves `confirmed_subscriptions()[i].next_expected`, and
            # that projection anchors on the NEWEST charge's day-of-month
            # (docs/36 §M170/M180), so a seed whose newest charge is 30 days
            # back anchors on the 5th in a 31-day month and parks the event a
            # day past the one this test pins. Anchor == today's day is the
            # one seed that cannot drift.
            day=_today(),
            amount="-5.00",
        ),
    ]
    # Accepted, with the next occurrence pinned to TODAY — the state a real
    # accept leaves behind when the next charge is due today. The sort under
    # test is about the three kinds sharing one day, so the day has to be this
    # one; the projection math has its own tests.
    _accept(client, headers, aaa)
    _repin_next(session, aaa, _today())

    events = _by_date(_get(client, headers, _today().strftime("%Y-%m")))[_today().isoformat()]
    assert [(e["kind"], e["label"]) for e in events] == [
        ("bill", "Alpha bill"),
        ("bill", "Zed bill"),
        ("dividend", "BMO dividend"),
        ("subscription", "AAA STORE"),
    ]


# ---------------------------------------------------------------------------
# Validation, tenancy, staging
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "month",
    ["2026-13", "2026-00", "2026-1", "abc", "2026-09-31", "1899-01", "2201-01", "09-2026"],
)
def test_invalid_month_is_a_pinned_422_envelope(client: TestClient, month: str) -> None:
    headers, _user_id = _auth(client)
    response = client.get(CAL, headers=headers, params={"month": month})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_month"
    assert isinstance(body["detail"], str)
    assert month in body["detail"]


def test_tenancy_is_strict(client: TestClient, session: Session) -> None:
    headers_a, user_a = _auth(client)
    account = _account(session, user_a)
    security = _security(session, user_a, "ENB")
    _bill(session, user_a, name="A's rent", due="2026-09-05", amount="1400.00")
    _dividend(
        session, user_a, account, security, day=_today() - dt.timedelta(days=100), amount="25.00"
    )
    _dividend(
        session, user_a, account, security, day=_today() - dt.timedelta(days=9), amount="25.00"
    )
    gym = [
        _txn(
            session,
            user_a,
            account,
            payee="A'S GYM",
            day=_today() - dt.timedelta(days=60),
            amount="-40.00",
        ),
        _txn(
            session,
            user_a,
            account,
            payee="A'S GYM",
            day=_today() - dt.timedelta(days=30),
            amount="-40.00",
        ),
    ]
    # A ACCEPTED the gym group — a subscription is confirmed state now (M170),
    # so without the accept there would be no subscription event to leak.
    _accept(client, headers_a, gym)

    headers_b, _user_b = _auth(client)
    # every month in which A has an event: the bill due date, the projected
    # dividend payment and the confirmed subscription's next occurrence
    months = {"2026-09"}
    months.add(
        compute_dividend_calendar(session, user_a)["upcoming"][0]["next_expected_date"].strftime(
            "%Y-%m"
        )
    )
    months.add(
        max(
            entry.next_expected
            for entry in confirmed_subscriptions(session, user_a)
            if entry.source == "recurring"
        ).strftime("%Y-%m")
    )
    for month in sorted(months):
        body = _get(client, headers_b, month)
        assert [e for d in body["days"] for e in d["events"]] == []
        assert body["month"] == month


def test_staged_rows_never_reach_the_calendar(client: TestClient, session: Session) -> None:
    """Pending import rows are invisible to every derived reader (docs/03) —
    the calendar composes only committed sources."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    security = _security(session, user_id, "BNS")
    pending = _batch(session, user_id, account.id, "pending_review")

    _dividend(
        session, user_id, account, security, day=_today() - dt.timedelta(days=100), amount="25.00"
    )
    _dividend(
        session, user_id, account, security, day=_today() - dt.timedelta(days=9), amount="35.00"
    )
    _dividend(
        session,
        user_id,
        account,
        security,
        day=_today() - dt.timedelta(days=50),
        amount="999.00",
        batch_id=pending.id,
    )
    _dividend(
        session,
        user_id,
        account,
        security,
        day=_today() - dt.timedelta(days=10),
        amount="999.00",
        batch_id=pending.id,
    )

    spotify = [
        _txn(
            session,
            user_id,
            account,
            payee="SPOTIFY",
            day=_today() - dt.timedelta(days=60),
            amount="-11.99",
        ),
        _txn(
            session,
            user_id,
            account,
            payee="SPOTIFY",
            day=_today() - dt.timedelta(days=30),
            amount="-11.99",
        ),
    ]
    # Accepted — the subscription series is confirmed state (M170), and the
    # pending rows below are the ones that must never reach it.
    _accept(client, headers, spotify)
    _txn(
        session,
        user_id,
        account,
        payee="SPOTIFY",
        day=_today() - dt.timedelta(days=45),
        amount="-999.99",
        batch_id=pending.id,
    )
    _txn(
        session,
        user_id,
        account,
        payee="SPOTIFY",
        day=_today() - dt.timedelta(days=15),
        amount="-999.99",
        batch_id=pending.id,
    )

    dividend_month = compute_dividend_calendar(session, user_id)["upcoming"][0][
        "next_expected_date"
    ].strftime("%Y-%m")
    subscription_month = max(
        entry.next_expected
        for entry in confirmed_subscriptions(session, user_id)
        if entry.source == "recurring"
    ).strftime("%Y-%m")

    for month in sorted({dividend_month, subscription_month, _today().strftime("%Y-%m")}):
        raw = client.get(CAL, headers=headers, params={"month": month})
        assert raw.status_code == 200
        assert "999" not in raw.text

    dividends = _events(_get(client, headers, dividend_month), "dividend")
    assert [(d, e["amount"]) for d, e in dividends] == [
        (
            compute_dividend_calendar(session, user_id)["upcoming"][0][
                "next_expected_date"
            ].isoformat(),
            "30.00",
        )
    ]
    subscriptions = _events(_get(client, headers, subscription_month), "subscription")
    accepted = max(
        entry.next_expected
        for entry in confirmed_subscriptions(session, user_id)
        if entry.source == "recurring"
    )
    assert [(d, e["amount"]) for d, e in subscriptions] == [(accepted.isoformat(), "-11.99")]


# ---------------------------------------------------------------------------
# M170 — the subscription series is CONFIRMED state, never a detector guess
# ---------------------------------------------------------------------------


def _mark_fixture(client: TestClient, session: Session) -> tuple[dict[str, str], int, dt.date]:
    """Mark's day-detail, reduced: a YouTube BILL, the detector guessing the
    same subscription under two statement spellings, an accepted Netflix group
    and a raw `Electronic Funds Transfer` row.

    Every one of those rows is seeded so the OLD feed would have drawn them:
    this is the bug, reproduced as a fixture.
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    # A due date safely in the FUTURE (the 4th of next month), so the bill's
    # single occurrence is unambiguous in its own month — the projection and
    # the confirmed query then agree on it by construction.
    due = (_today().replace(day=28) + dt.timedelta(days=7)).isoformat()
    assert dt.date.fromisoformat(due) > _today()
    _bill(session, user_id, name="YouTube Premium", due=due, amount="25.98")

    google = [
        _txn(
            session,
            user_id,
            account,
            payee=payee,
            day=_today() - dt.timedelta(days=days),
            amount="-25.98",
        )
        # Two statement spellings of one payee — M165's normalization drops
        # the space before the `*`, so the detector sees ONE group and a bill
        # for "YouTube Premium" covers it.
        for payee, days in (("GOOGLE *YouTubePremium", 60), ("Google*Youtubepremium", 30))
    ]
    _accept(client, headers, google)

    # A third statement mangles the LETTERS ("Youtubeprmium") — a different
    # key under the shared normalizer on purpose (a normalize that guesses is
    # two normalizes again). Nobody accepted it, so it is exactly the guess
    # that used to render under Mark's bill.
    for days in (60, 30):
        _txn(
            session,
            user_id,
            account,
            payee="Google*Youtubeprmium",
            day=_today() - dt.timedelta(days=days),
            amount="-25.98",
        )

    netflix = [
        _txn(
            session,
            user_id,
            account,
            payee="NETFLIX.COM",
            day=_today() - dt.timedelta(days=days),
            amount="-15.99",
        )
        for days in (90, 60, 30)
    ]
    _accept(client, headers, netflix)

    for days in (90, 60, 30):
        _txn(
            session,
            user_id,
            account,
            payee="Electronic Funds Transfer",
            day=_today() - dt.timedelta(days=days),
            amount="-359.08",
        )
    return headers, user_id, dt.date.fromisoformat(due)


def test_m170_subscriptions_series_shows_only_confirmed_sources(
    client: TestClient, session: Session
) -> None:
    """The M170 verdict, end to end: accepted Netflix renders as a
    subscription; the bill-covered YouTube group renders ONCE, under Bills;
    the EFT row renders nowhere at all."""
    headers, user_id, due = _mark_fixture(client, session)

    months = sorted(
        {
            due.strftime("%Y-%m"),
            *(
                entry.next_expected.strftime("%Y-%m")
                for entry in confirmed_subscriptions(session, user_id)
            ),
        }
    )
    subscription_events: list[tuple[str, str, str]] = []
    bill_labels: list[str] = []
    raw = []
    for month in months:
        body = _get(client, headers, month)
        raw.append(body)
        subscription_events += [
            (day, event["label"], event["amount"]) for day, event in _events(body, "subscription")
        ]
        bill_labels += [event["label"] for _day, event in _events(body, "bill")]

    # Netflix — the group the user ACCEPTED — is the one subscription.
    assert subscription_events == [
        (
            max(
                entry.next_expected
                for entry in confirmed_subscriptions(session, user_id)
                if entry.source == "recurring"
            ).isoformat(),
            "NETFLIX.COM",
            "-15.99",
        )
    ]
    # YouTube appears exactly once, as the BILL (orange) — not twice, in two
    # colours, from two sources.
    assert bill_labels.count("YouTube Premium") == 1
    # The EFT is a detector-refusal class: the detector does not surface it…
    detected = {subscription["payee"] for subscription in detect_subscriptions(session, user_id)}
    assert not any(
        "Electronic Funds" in payee or payee.upper().startswith("EFT") for payee in detected
    )
    # The detector is unchanged and still guesses (including the mangled
    # spelling) — it is just not a fact source for this surface any more.
    assert {"NETFLIX.COM", "Google*Youtubeprmium"} <= detected
    # …and neither does any month of the calendar.
    assert not any("Electronic Funds" in str(body) for body in raw)
    # Two confirmed sources in total: the bill and the accepted Netflix group.
    # The accepted-but-bill-covered YouTube group does NOT add a third — the
    # same money answers once (docs/36 §M170).
    assert len(confirmed_subscriptions(session, user_id)) == 2


def test_m170_the_shared_confirmed_query_answers_both_surfaces(
    client: TestClient, session: Session
) -> None:
    """ONE query, two surfaces (docs/36 §M170): the calendar's subscription
    series IS `confirmed_subscriptions()`'s recurring-source set, event for
    event, and its bill series IS the same query's bill-source set."""
    headers, user_id, due = _mark_fixture(client, session)

    confirmed = confirmed_subscriptions(session, user_id)
    month = due.strftime("%Y-%m")
    body = _get(client, headers, month)

    expected_subscriptions = sorted(
        (
            entry.next_expected.isoformat(),
            entry.payee,
            str(entry.amount),
        )
        for entry in confirmed
        if entry.source == "recurring" and entry.next_expected.strftime("%Y-%m") == month
    )
    assert [
        (day, label, amount)
        for day, label, amount in (
            (d, e["label"], e["amount"]) for d, e in _events(body, "subscription")
        )
    ] == expected_subscriptions

    expected_bills = sorted(
        (entry.next_expected.isoformat(), entry.payee, str(entry.amount))
        for entry in confirmed
        if entry.source == "bill" and entry.next_expected.strftime("%Y-%m") == month
    )
    assert [(d, e["label"], e["amount"]) for d, e in _events(body, "bill")] == expected_bills


def test_m170_confirmed_count_distinguishes_nothing_yet_from_nothing_this_month(
    client: TestClient, session: Session
) -> None:
    """The payload's `confirmed_subscription_count` is what lets the client
    render an honest empty state instead of silence (docs/36 §M170)."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)

    # Nothing confirmed at all.
    body = _get(client, headers)
    assert body["confirmed_subscription_count"] == 0
    assert _events(body, "subscription") == []

    # A bill the user created IS confirmed — even though the detector has
    # never seen a single charge for it.
    _bill(session, user_id, name="YouTube Premium", due="2026-09-05", amount="25.98")
    body = _get(client, headers)
    assert body["confirmed_subscription_count"] == 1
    assert _events(body, "subscription") == []  # it renders under Bills, not here

    # A detector-only group (charges, no accept, no bill) is NOT confirmed:
    # still one, still no subscription event — the guess stays a prompt.
    for days in (60, 30):
        _txn(
            session,
            user_id,
            account,
            payee="NETFLIX.COM",
            day=_today() - dt.timedelta(days=days),
            amount="-15.99",
        )
    body = _get(client, headers)
    assert body["confirmed_subscription_count"] == 1
    assert detect_subscriptions(session, user_id)  # the detector does see it
    assert _events(body, "subscription") == []


# ---------------------------------------------------------------------------
# M170 follow-up — a subscription's amount is stated in ITS OWN currency
# (docs/36 §Currency)
# ---------------------------------------------------------------------------


def _accepted_charges(
    client: TestClient,
    session: Session,
    headers: dict[str, str],
    user_id: int,
    *,
    payee: str,
    rows: list[tuple[Account, str, str]],
) -> None:
    """Seed one payee's charges and accept them the way the user does.

    `rows` is `(account, amount, currency)` per charge — the account carries
    the currency a real import would have stamped, and the explicit
    `currency` keeps the two from drifting apart in a fixture.
    """
    charges = [
        _txn(
            session,
            user_id=user_id,
            account=account,
            payee=payee,
            day=_today() - dt.timedelta(days=30 * (index + 1)),
            amount=amount,
            currency=currency,
        )
        for index, (account, amount, currency) in enumerate(rows)
    ]
    _accept(client, headers, charges)


def _recurring_entry(session: Session, user_id: int):
    entries = [
        entry for entry in confirmed_subscriptions(session, user_id) if entry.source == "recurring"
    ]
    assert len(entries) == 1, f"expected one accepted group, got {entries}"
    return entries[0]


def test_usd_subscription_is_stated_in_usd_not_cad(client: TestClient, session: Session) -> None:
    """A USD group's figure is the row's own `amount_native`, labelled USD.

    This is the defect in one test: before M170's follow-up the calendar
    printed `-11.99` as `amount_cad`, i.e. it asserted a CAD figure it had
    never computed (docs/36 §Currency). Nothing here converts — the number is
    the mean of the native charges.
    """
    headers, user_id = _auth(client)
    usd = _account(session, user_id, currency="USD")
    _accepted_charges(
        client,
        session,
        headers,
        user_id,
        payee="SPOTIFY",
        rows=[(usd, "-11.99", "USD"), (usd, "-11.99", "USD")],
    )

    entry = _recurring_entry(session, user_id)
    assert (str(entry.amount), entry.currency) == ("-11.99", "USD")

    events = _events(_get(client, headers, entry.next_expected.strftime("%Y-%m")), "subscription")
    assert [(d, e["label"], e["amount"], e["currency"]) for d, e in events] == [
        (entry.next_expected.isoformat(), "SPOTIFY", "-11.99", "USD")
    ]


def test_mixed_currency_group_omits_the_amount(client: TestClient, session: Session) -> None:
    """A USD charge and a CAD charge under one payee: the mean is a number
    with no currency, so the calendar states NEITHER (docs/36 §Currency)."""
    headers, user_id = _auth(client)
    cad = _account(session, user_id, currency="CAD")
    usd = _account(session, user_id, currency="USD")
    _accepted_charges(
        client,
        session,
        headers,
        user_id,
        payee="DROPBOX",
        rows=[(cad, "-11.99", "CAD"), (usd, "-12.99", "USD")],
    )

    entry = _recurring_entry(session, user_id)
    assert entry.amount is None
    assert entry.currency is None

    events = _events(_get(client, headers, entry.next_expected.strftime("%Y-%m")), "subscription")
    # The event still exists — it is a commitment with a date. Only the
    # figure is withheld, and `null`/`null` is how the client learns to print
    # the app's "no honest figure" dash instead of a CAD number.
    assert [(d, e["label"], e["amount"], e["currency"]) for d, e in events] == [
        (entry.next_expected.isoformat(), "DROPBOX", None, None)
    ]


def test_unknown_currency_row_omits_the_amount(client: TestClient, session: Session) -> None:
    """One row the query cannot denominate is enough: the group's mean would
    be half-CAD/half-unknown, and an unknown is not CAD (docs/36 §Currency)."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    _accepted_charges(
        client,
        session,
        headers,
        user_id,
        payee="NOTION",
        rows=[(account, "-9.99", "CAD"), (account, "-9.99", "")],
    )

    entry = _recurring_entry(session, user_id)
    assert (entry.amount, entry.currency) == (None, None)

    events = _events(_get(client, headers, entry.next_expected.strftime("%Y-%m")), "subscription")
    assert [(e["amount"], e["currency"]) for _d, e in events] == [(None, None)]


def test_the_amount_is_the_native_figure_never_a_converted_one(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No fx read on this path (docs/36 decision 6, docs/07).

    The rate book is patched to raise: if this endpoint ever starts
    converting — or asking for a rate at all — the request fails instead of
    quietly inventing a CAD number for a USD commitment.
    """
    headers, user_id = _auth(client)
    usd = _account(session, user_id, currency="USD")
    _accepted_charges(
        client,
        session,
        headers,
        user_id,
        payee="SPOTIFY",
        rows=[(usd, "-11.99", "USD"), (usd, "-11.99", "USD")],
    )

    from app.services import fx as fx_service

    def forbidden(*args, **kwargs):
        raise AssertionError("the calendar read consulted the fx rate book")

    monkeypatch.setattr(fx_service, "cached_cad_amount", forbidden)
    monkeypatch.setattr(fx_service, "latest_rate", forbidden)

    entry = _recurring_entry(session, user_id)
    events = _events(_get(client, headers, entry.next_expected.strftime("%Y-%m")), "subscription")
    assert [(e["amount"], e["currency"]) for _d, e in events] == [("-11.99", "USD")]


def test_confirmed_query_states_the_currency_or_refuses_the_amount(
    client: TestClient, session: Session
) -> None:
    """The rule lives in the ONE confirmed query, so both surfaces inherit it.

    A figure is always accompanied by the currency it is stated in, and a
    figure the query cannot denominate is not stated at all — for every
    source: a bill (CAD by schema), a bill with no amount, a CAD group, a USD
    group and a mixed group.
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    usd = _account(session, user_id, currency="USD")
    _bill(session, user_id, name="Mapleview Rent", due="2026-09-05", amount="1400.00")
    _bill(session, user_id, name="Hydro (variable)", due="2026-09-12", amount=None)
    _accepted_charges(
        client,
        session,
        headers,
        user_id,
        payee="NETFLIX.COM",
        rows=[(account, "-15.99", "CAD"), (account, "-15.99", "CAD")],
    )
    _accepted_charges(
        client,
        session,
        headers,
        user_id,
        payee="SPOTIFY",
        rows=[(usd, "-11.99", "USD"), (usd, "-11.99", "USD")],
    )
    _accepted_charges(
        client,
        session,
        headers,
        user_id,
        payee="DROPBOX",
        rows=[(account, "-11.99", "CAD"), (usd, "-12.99", "USD")],
    )

    stated = {
        (entry.payee, entry.source): (
            None if entry.amount is None else str(entry.amount),
            entry.currency,
        )
        for entry in confirmed_subscriptions(session, user_id)
    }
    assert stated == {
        ("Mapleview Rent", "bill"): ("-1400.00", "CAD"),
        # A variable bill has no figure — and a currency with no figure
        # denominate nothing.
        ("Hydro (variable)", "bill"): (None, None),
        ("NETFLIX.COM", "recurring"): ("-15.99", "CAD"),
        ("SPOTIFY", "recurring"): ("-11.99", "USD"),
        ("DROPBOX", "recurring"): (None, None),
    }

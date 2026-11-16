"""M170 — the ONE confirmed-subscription query (docs/36 §M170, docs/09).

Two contracts live here, and the second exists because of the first:

1. **Confirmed state is the user's assertion, never the detector's read.**
   `confirmed_subscriptions()` answers "what recurring commitments does this
   user actually have?" from active `bill_reminders` rows and from the rows
   `POST /transactions/recurring/accept` tagged — nothing else. Mark's
   complaint (a guessed `Google*Youtubeprmium` under his real YouTube bill,
   plus a bare `Electronic Funds Transfer -$359.08`) is the defect this pins:
   a candidate the user has not touched is a PROMPT, so it belongs on /txns
   and the dashboard card, never on a surface that reads as fact.

2. **A statement-generic descriptor is a refusal class.**
   `is_statement_generic` is the deterministic input filter that keeps
   `Electronic Funds Transfer` / `INTERAC e-Transfer` / `TAX PAYMENT` /
   `PAYROLL …` out of the M27 detector however well their amounts and gaps
   fit — the money IS a transfer, and the merchant (when there is one) is the
   strip after it, which is precisely why none of the descriptor is a
   subscription identity to group on.

The query is deliberately the ONLY server answer to that question: the
cash-flow calendar's `subscription` series and its `confirmed_subscription_count`
both read it, so two surfaces cannot drift (M133's "one detector" ruling,
extended to "one confirmed-state query").
"""

from __future__ import annotations

import calendar
import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.bill_reminders import BillReminder
from app.models.transactions import RecurringCadence, Transaction
from app.services.confirmed_subscriptions import (
    SOURCE_BILL,
    SOURCE_RECURRING,
    confirmed_subscriptions,
)
from app.services.subscriptions import (
    detect_subscriptions,
    is_statement_generic,
    normalize_payee_key,
)
from app.services.txn_filters import today_et

API = "/api/v1"


def _today() -> dt.date:
    """The clock at CALL time — never captured at import (AGENTS §6)."""
    return today_et()


def _auth(client: TestClient) -> tuple[dict[str, str], int]:
    from datetime import UTC, datetime

    email = f"m170-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    user_id = client.get(f"{API}/users/me", headers=headers).json()["id"]
    return headers, user_id


def _account(session: Session, user_id: int) -> Account:
    account = Account(
        user_id=user_id,
        name=f"Acct {user_id}",
        type=AccountType.brokerage_non_registered,
        currency="CAD",
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _txn(
    session: Session,
    user_id: int,
    account: Account,
    *,
    payee: str,
    day: dt.date,
    amount: str,
    recurring: bool = False,
) -> Transaction:
    txn = Transaction(
        user_id=user_id,
        account_id=account.id,
        date=day,
        raw_description=payee,
        normalized_payee=payee,
        amount_native=Decimal(amount),
        currency="CAD",
        dedup_hash=f"m170-{user_id}-{payee}-{day.isoformat()}-{amount}",
        is_recurring=recurring,
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _bill(
    session: Session,
    user_id: int,
    *,
    name: str,
    due: str,
    amount: str | None = "16.99",
    enabled: bool = True,
    frequency: str = "monthly",
    anchor_day: int | None = None,
) -> BillReminder:
    bill = BillReminder(
        user_id=user_id,
        name=name,
        amount=None if amount is None else Decimal(amount),
        frequency=frequency,
        next_due_date=due,
        anchor_day=anchor_day,
        enabled=enabled,
        match_term=name,
    )
    session.add(bill)
    session.commit()
    session.refresh(bill)
    return bill


def _months_back(months: int) -> dt.date:
    """`months` whole calendar months before today, day-of-month kept (clamped
    to a short month) — the arithmetic a monthly schedule itself uses.

    NOT ``today - 30 * months``: 30-day arithmetic drifts against the calendar,
    and `confirmed_subscriptions` projects an accepted group on its NEWEST
    charge's day-of-month anchor (`bill_schedule.first_on_or_after`, docs/36 §
    M170/M180), so a drifted anchor answers the confirmed date a day off the
    pinned one in every month whose predecessor has 31 days. That is exactly
    how this file turned red on 2026-11-01 — three weeks after 91962b6 landed
    the anchor rule, in a month where `today - 30` is the 5th instead of the
    4th — while staying green through October.
    """
    today = _today()
    index = today.month - 1 - months
    year = today.year + index // 12
    month = index % 12 + 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def _monthly_charges(
    session: Session, user_id: int, account: Account, payee: str, amount: str
) -> list[Transaction]:
    """Three same-day-of-month charges — a perfect monthly fit, so anything
    that is refused here is refused by NAME, not by cadence or amount.

    The NEWEST lands on TODAY, which is what makes a test pinning the group's
    confirmed date to `_today()` date-proof: the projection's anchor day IS
    the newest charge's, and `first_on_or_after` answers the anchor's day in
    today's month — anchor == today's day is the one input that can never
    drift off the assertion.
    """
    return [
        _txn(
            session,
            user_id,
            account,
            payee=payee,
            day=_months_back(months),
            amount=amount,
        )
        for months in (2, 1, 0)
    ]


# ---------------------------------------------------------------------------
# The detector-refusal class (docs/24 §M27, docs/36 §M170)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payee",
    [
        "Electronic Funds Transfer",
        "INTERAC e-Transfer",
        "TAX PAYMENT",
        "PAYROLL DEPOSIT ACME",
    ],
)
def test_statement_generic_descriptors_are_never_subscriptions(
    client: TestClient, session: Session, payee: str
) -> None:
    """Whatever the amount and cadence fit: three identical monthly charges
    clear every OTHER detector rule, and the descriptor is still not a
    merchant."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    _monthly_charges(session, user_id, account, payee, "-359.08")

    assert detect_subscriptions(session, user_id) == []


def test_the_refusal_is_a_prefix_rule_over_the_normalized_key() -> None:
    refused = [
        "Electronic Funds Transfer 5551",
        "EFT 100",
        "INTERAC e-Transfer - 40",
        "Tax payment 2025",
        "Payroll",
        "PAYROLL",
    ]
    for payee in refused:
        assert is_statement_generic(normalize_payee_key(payee)), payee
    for payee in [
        "NETFLIX.COM",
        "Paymentus Utility",
        "Electronics Depot",
        "GOOGLE *YouTubePremium",
    ]:
        assert not is_statement_generic(normalize_payee_key(payee)), payee


def test_the_stoplist_is_the_detector_only_user_assertions_still_count(
    client: TestClient, session: Session
) -> None:
    """The boundary M170 draws: the refusal list filters the DETECTOR's input,
    not the user's own word. A row the user tagged recurring is asserted state
    and stays confirmed — which is also the only way an EFT-shaped descriptor
    can reach the calendar."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    rows = _monthly_charges(session, user_id, account, "Electronic Funds Transfer", "-359.08")
    for row in rows:
        row.is_recurring = True
        row.recurring_next_date = _today()
        session.add(row)
    session.commit()

    assert detect_subscriptions(session, user_id) == []  # the detector refuses it
    confirmed = confirmed_subscriptions(session, user_id)
    assert [entry.source for entry in confirmed] == [SOURCE_RECURRING]
    assert confirmed[0].next_expected == _today()


# ---------------------------------------------------------------------------
# The confirmed query (docs/36 §M170)
# ---------------------------------------------------------------------------


def test_a_mangled_statement_spelling_is_its_own_merchant_until_the_user_says(
    client: TestClient, session: Session
) -> None:
    """The M165 boundary, restated for the calendar: the shared normalizer
    folds case, whitespace and trailing branch numbers — nothing else. A
    statement that mangles the LETTERS (`Google*Youtubeprmium`) is a
    different merchant key, so it neither dedupes against the bill nor
    against the well-spelled group. What keeps it off the calendar is not a
    guess about letters: it is unconfirmed, and unconfirmed is a prompt."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    due = (_today().replace(day=28) + dt.timedelta(days=7)).isoformat()
    _bill(session, user_id, name="YouTube Premium", due=due, amount="25.98")
    mangled = _monthly_charges(session, user_id, account, "Google*Youtubeprmium", "-25.98")

    # The detector sees it as its own group — the guess Mark screenshot'd.
    assert [subscription["payee"] for subscription in detect_subscriptions(session, user_id)] == [
        "Google*Youtubeprmium"
    ]
    # Confirmed state is the bill alone: an untouched group is a prompt.
    assert [entry.payee for entry in confirmed_subscriptions(session, user_id)] == [
        "YouTube Premium"
    ]
    assert mangled  # seeded


def test_the_confirmed_set_is_bills_plus_accepted_groups(
    client: TestClient, session: Session
) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    due = (_today().replace(day=28) + dt.timedelta(days=7)).isoformat()
    _bill(session, user_id, name="YouTube Premium", due=due, amount="25.98")
    charges = _monthly_charges(session, user_id, account, "NETFLIX.COM", "-15.99")
    for row in charges:
        row.is_recurring = True
        row.recurring_cadence = RecurringCadence.monthly
        row.recurring_next_date = _today()
        session.add(row)
    session.commit()

    confirmed = confirmed_subscriptions(session, user_id)
    assert [(entry.source, entry.payee) for entry in confirmed] == [
        (SOURCE_RECURRING, "NETFLIX.COM"),
        (SOURCE_BILL, "YouTube Premium"),
    ]
    assert confirmed[0].amount == Decimal("-15.99")
    assert confirmed[1].amount == Decimal("-25.98")
    # Untouched charges are NOT confirmed, however well they fit.
    untouched = _monthly_charges(session, user_id, account, "SPOTIFY CANADA", "-11.99")
    assert untouched  # seeded
    assert [entry.payee for entry in confirmed_subscriptions(session, user_id)] == [
        "NETFLIX.COM",
        "YouTube Premium",
    ]


def test_a_bill_covers_the_accepted_group_for_every_spelling_of_its_payee(
    client: TestClient, session: Session
) -> None:
    """M170's dedup, with M165's normalization: the same subscription under
    two statement spellings is one merchant, and a bill for it answers ONCE."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    due = (_today().replace(day=28) + dt.timedelta(days=7)).isoformat()
    _bill(session, user_id, name="YouTube Premium", due=due, amount="25.98")

    # Two spellings the ONE normalizer collapses (M165): the space before the
    # `*` is not identity, so both keys are `GOOGLE*YOUTUBEPREMIUM`. A
    # statement that MANGLES the letters ("Youtubeprmium") is deliberately a
    # different key — a normalize that guesses is two normalizes again — and
    # that variant has its own test below.
    spellings = ("GOOGLE *YouTubePremium", "Google*Youtubepremium")
    rows = [
        _txn(
            session,
            user_id,
            account,
            payee=payee,
            day=_today() - dt.timedelta(days=days),
            amount="-25.98",
        )
        for payee, days in zip(spellings, (60, 30), strict=True)
    ]
    for row in rows:
        row.is_recurring = True
        row.recurring_cadence = RecurringCadence.monthly
        row.recurring_next_date = _today()
        session.add(row)
    session.commit()

    # The detector still sees the group (one merchant key — M165), which is
    # exactly the duplicate the old calendar drew. Which spelling labels it is
    # row order (both rows carry a NULL `posting_date`, so the scan's
    # `ORDER BY posting_date` cannot break the tie) — the assertion is about
    # ONE group, not about which of its two statements named it.
    detected = [subscription["payee"] for subscription in detect_subscriptions(session, user_id)]
    assert len(detected) == 1
    assert detected[0] in spellings
    confirmed = confirmed_subscriptions(session, user_id)
    assert [entry.source for entry in confirmed] == [SOURCE_BILL]
    assert confirmed[0].payee == "YouTube Premium"


def test_a_recurring_credit_is_not_a_subscription(client: TestClient, session: Session) -> None:
    """A payroll deposit clears the recurring rule exactly as happily as
    Netflix does; the calendar's subscription series is signed money-OUT, so
    a credit group is not one."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    rows = _monthly_charges(session, user_id, account, "ACME PAYROLL", "2400.00")
    for row in rows:
        row.is_recurring = True
        row.recurring_cadence = RecurringCadence.monthly
        row.recurring_next_date = _today()
        session.add(row)
    session.commit()

    assert confirmed_subscriptions(session, user_id) == []


def test_a_stale_accept_is_rolled_forward_to_today(client: TestClient, session: Session) -> None:
    """A stale stored base rolls forward on the accepted group's charge-day anchor."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    rows = _monthly_charges(session, user_id, account, "NETFLIX.COM", "-15.99")
    stale = _today() - dt.timedelta(days=95)
    for row in rows:
        row.is_recurring = True
        row.recurring_cadence = RecurringCadence.monthly
        row.recurring_next_date = stale
        session.add(row)
    session.commit()

    entry = confirmed_subscriptions(session, user_id)[0]
    assert entry.next_expected >= _today()
    # …and on the group's newest effective charge day.
    assert entry.next_expected.day == max(row.date for row in rows).day


def test_disabled_bills_and_other_tenants_are_not_confirmed(
    client: TestClient, session: Session
) -> None:
    headers_a, user_a = _auth(client)
    account = _account(session, user_a)
    due = (_today().replace(day=28) + dt.timedelta(days=7)).isoformat()
    _bill(session, user_a, name="Enabled bill", due=due, amount="10.00")
    _bill(session, user_a, name="Disabled bill", due=due, amount="11.00", enabled=False)
    charges = _monthly_charges(session, user_a, account, "NETFLIX.COM", "-15.99")
    for row in charges:
        row.is_recurring = True
        row.recurring_cadence = RecurringCadence.monthly
        row.recurring_next_date = _today()
        session.add(row)
    session.commit()

    assert [entry.payee for entry in confirmed_subscriptions(session, user_a)] == [
        "NETFLIX.COM",
        "Enabled bill",
    ]
    # B sees none of it — the query is user-scoped like every other reader.
    _headers_b, user_b = _auth(client)
    assert confirmed_subscriptions(session, user_b) == []
    # …and A's own API answer matches the service: the whole confirmed set
    # counted, each entry rendered in its own month, under its own kind.
    current = client.get(f"{API}/reports/cashflow-calendar", headers=headers_a).json()
    assert current["confirmed_subscription_count"] == 2
    assert [event["label"] for day in current["days"] for event in day["events"]] == ["NETFLIX.COM"]
    bill_month = client.get(
        f"{API}/reports/cashflow-calendar",
        headers=headers_a,
        params={"month": due[:7]},
    ).json()
    assert [event["label"] for day in bill_month["days"] for event in day["events"]] == [
        "Enabled bill"
    ]


# ---------------------------------------------------------------------------
# M180 — the confirmed set reads the schedule's ANCHOR day (docs/25 §M180,
# docs/36 decision 5), so a bill's next date here and its occurrence inside the
# cash-flow grid can never clamp differently.
# ---------------------------------------------------------------------------


def test_a_month_end_bill_projects_from_its_anchor_not_the_stored_clamp(
    client: TestClient, session: Session
) -> None:
    """`mark-paid` writes the CLAMPED date plus the day the schedule repeats on.

    `due = 2026-02-28` with `anchor_day = 31` is exactly the row that write
    leaves behind. Projecting the stored date answers the 28th forever (the
    M170-M180 drift); projecting the anchor answers the 31st in every month that
    has one — and the 30th in April, which is all April has.
    """
    _headers, user_id = _auth(client)
    _bill(session, user_id, name="Mapleview Rent", due="2026-02-28", anchor_day=31)

    assert confirmed_subscriptions(session, user_id, today=dt.date(2026, 3, 15))[
        0
    ].next_expected == dt.date(2026, 3, 31)
    assert confirmed_subscriptions(session, user_id, today=dt.date(2026, 4, 15))[
        0
    ].next_expected == dt.date(2026, 4, 30)


def test_a_leap_day_schedule_returns_in_the_next_leap_year(
    client: TestClient, session: Session
) -> None:
    """`Feb 29 → Feb 28` for three years, then the leap day comes back (M180).

    `due = 2029-02-28` with `anchor_day = 29` is the row one `mark-paid` leaves
    behind for a Feb-29 yearly bill. The anchor is what makes 2032 answer
    `2032-02-29` while 2030 and 2031 answer the 28th — projecting the stored day
    would answer `2032-02-28` and lose the leap day forever.
    """
    _headers, user_id = _auth(client)
    _bill(
        session,
        user_id,
        name="Leap-day renewal",
        due="2029-02-28",
        frequency="yearly",
        anchor_day=29,
    )

    assert confirmed_subscriptions(session, user_id, today=dt.date(2030, 1, 1))[
        0
    ].next_expected == dt.date(2030, 2, 28)
    assert confirmed_subscriptions(session, user_id, today=dt.date(2031, 1, 1))[
        0
    ].next_expected == dt.date(2031, 2, 28)
    assert confirmed_subscriptions(session, user_id, today=dt.date(2032, 1, 1))[
        0
    ].next_expected == dt.date(2032, 2, 29)


def test_the_confirmed_set_reads_what_mark_paid_wrote(client: TestClient, session: Session) -> None:
    """End to end: `POST /bills` on the 31st + one `mark-paid` → the set's date
    is the ANCHOR's 31st, never the clamped 28th the row now stores."""
    headers, user_id = _auth(client)
    created = client.post(
        f"{API}/bills",
        headers=headers,
        json={
            "name": "Mapleview Rent",
            "amount": 1400,
            "frequency": "monthly",
            "next_due_date": "2026-01-31",
        },
    )
    assert created.status_code == 201, created.text
    paid = client.post(f"{API}/bills/{created.json()['id']}/mark-paid", headers=headers)
    assert paid.status_code == 200, paid.text
    assert paid.json()["next_due_date"] == "2026-02-28"

    confirmed = confirmed_subscriptions(session, user_id, today=dt.date(2026, 4, 15))
    assert [(entry.source, entry.next_expected) for entry in confirmed] == [
        (SOURCE_BILL, dt.date(2026, 4, 30))
    ]


def test_a_one_time_bill_is_confirmed_once_and_never_repeats(
    client: TestClient, session: Session
) -> None:
    """No cadence, no next occurrence: the stored date, once — and only while it
    is still ahead of `today` (the M170 contract, kept through M180)."""
    _headers, user_id = _auth(client)
    _bill(
        session,
        user_id,
        name="One-off",
        due=_today().isoformat(),
        frequency="one_time",
        anchor_day=_today().day,
    )

    assert [entry.next_expected for entry in confirmed_subscriptions(session, user_id)] == [
        _today()
    ]
    assert confirmed_subscriptions(session, user_id, today=_today() + dt.timedelta(days=1)) == []
    # Recurring bills never expire — they step forward instead, on the anchor's
    # day (clamped to the target month's length when that month is shorter).
    _bill(session, user_id, name="Monthly", due=_today().isoformat(), anchor_day=_today().day)
    stepped = [
        entry.next_expected
        for entry in confirmed_subscriptions(
            session, user_id, today=_today() + dt.timedelta(days=1)
        )
        if entry.source == SOURCE_BILL
    ]
    assert len(stepped) == 1
    assert stepped[0] > _today()
    assert stepped[0].day == min(
        _today().day, calendar.monthrange(stepped[0].year, stepped[0].month)[1]
    )


def test_an_unreadable_stored_date_is_skipped_instead_of_raising(
    client: TestClient, session: Session
) -> None:
    """The READ half of docs/25 §M180 decision 4: a corrupted `VARCHAR(10)` is a
    skip here (the write path is the surface that answers 422 `invalid_due_date`),
    so one bad row can never take the whole confirmed set down."""
    _headers, user_id = _auth(client)
    _bill(session, user_id, name="Corrupted", due="2026-02-31")
    _bill(session, user_id, name="Fine", due=(_today() + dt.timedelta(days=5)).isoformat())

    assert [entry.payee for entry in confirmed_subscriptions(session, user_id)] == ["Fine"]


def test_the_confirmed_set_never_answers_a_past_date(client: TestClient, session: Session) -> None:
    """Forward projection only (docs/36 decision 5): the stored month is not
    replayed — the answer is the first occurrence at or after `today`, on the
    anchor's day."""
    _headers, user_id = _auth(client)
    _bill(session, user_id, name="Mapleview Rent", due="2026-02-10", anchor_day=10)

    entry = confirmed_subscriptions(session, user_id, today=dt.date(2026, 4, 15))[0]
    assert entry.next_expected == dt.date(2026, 5, 10)


def test_the_forward_projection_stays_inside_the_pinned_step_bound(
    client: TestClient, session: Session
) -> None:
    """The 1900-2200 request band (docs/36 decision 2) is at most ~3600 monthly
    steps, well inside the helper's 10 000-step bound: an old row still answers,
    and a corrupt one can never hang a request."""
    _headers, user_id = _auth(client)
    _bill(session, user_id, name="Ancient", due="1900-01-31", anchor_day=31)

    entry = confirmed_subscriptions(session, user_id, today=dt.date(2199, 12, 15))[0]
    assert entry.next_expected == dt.date(2199, 12, 31)

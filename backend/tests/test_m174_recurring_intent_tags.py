"""M174 — recurring-intent TAGS as a confirmed-subscription source (docs/09 §Tags
that mark recurring, docs/36 §M174).

Mark's ask: *"for recurring stuff can we make that based on tags since there can
be multiple"*. Four contracts live here, and each one exists because the
alternative is a silent wrong answer:

1. **The tag set is the user's OWN choice, and empty means OFF.** No magic tag
   name is interpreted (the PM's ruling): `users.recurring_intent_tag_ids` names
   the tags, `[]` turns the source off entirely, and ownership is enforced at
   the write seam — a nominated id the caller does not own is a `422`, never a
   silently-non-matching row.
2. **ALL of a group's rows must carry at least one nominated tag** (the M96
   consistency discipline). One untagged row in the run leaves it unconfirmed:
   a partly-marked run is ambiguity, and ambiguity is what turns a fact surface
   back into a guess.
3. **The detector's math gate stays.** A tag is INTENT, never arithmetic — the
   candidate set is `services/recurring.py::detect_recurring`, the same M96 fit
   the prompt cards run, so a tagged pair of charges with no cadence is not a
   subscription and a tagged payroll CREDIT is not one either.
4. **One merchant answers once.** The tag source unions into the same
   `confirmed_subscriptions()` result and is deduped by the shared
   `normalize_payee_key`, so an active bill wins, an accepted group wins, and a
   row carrying TWO nominated tags is still one group.

Plus the cross-language half of M174 §Create from a selection: the payee pattern
the browser derives from a multi-spelling selection (the M165 fixture) has to be
a real hit on BOTH matchers the editors use — the rule engine's `contains` and
the bill matcher's terms — because a pattern that only matches the first
spelling recreates M165's manufactured duplicate one layer up.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.bill_reminders import BillReminder
from app.models.categories import Category
from app.models.recurring_suppressions import RecurringSuppression
from app.models.rules import Rule, RuleMatchType
from app.models.tags import Tag
from app.models.transaction_tags import TransactionTag
from app.models.transactions import RecurringCadence, Transaction
from app.models.users import User
from app.services.bill_match import BillMatchRule, payee_matches
from app.services.cashflow_calendar import KIND_SUBSCRIPTION, build_cashflow_calendar
from app.services.confirmed_subscriptions import (
    SOURCE_BILL,
    SOURCE_RECURRING,
    SOURCE_TAG,
    confirmed_subscriptions,
)
from app.services.recurring import make_group_key
from app.services.rules_engine import rule_matches
from app.services.subscriptions import normalize_payee_key
from app.services.txn_filters import today_et

API = "/api/v1"

#: The pattern `frontend/src/lib/createFromSearch.ts::selectionCriteria` derives
#: from the M165 pair in the order (A, B) — parsed from the FIRST row's casing,
#: which is what makes the two sides of this contract one string.
M165_PATTERN = "YouTubePremium"
M165_SPELLINGS = ("GOOGLE *YouTubePremium", "Google*Youtubepremium")


def _today() -> dt.date:
    """The clock at CALL time — never captured at import (AGENTS §6)."""
    return today_et()


def _auth(client: TestClient) -> tuple[dict[str, str], int]:
    from datetime import UTC, datetime

    email = f"m174-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
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


def _tag(session: Session, user_id: int, name: str) -> Tag:
    tag = Tag(user_id=user_id, name=name)
    session.add(tag)
    session.commit()
    session.refresh(tag)
    return tag


def _nominate(session: Session, user_id: int, tag_ids: list[int]) -> None:
    """The Settings write, applied straight to the row (the API path is pinned
    separately, below)."""
    user = session.get(User, user_id)
    assert user is not None
    user.recurring_intent_tag_ids = list(tag_ids)
    session.add(user)
    session.commit()


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
        dedup_hash=f"m174-{user_id}-{payee}-{day.isoformat()}-{amount}",
        is_recurring=recurring,
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _tag_rows(session: Session, tags: list[Tag], rows: list[Transaction]) -> None:
    for row in rows:
        for tag in tags:
            session.add(TransactionTag(transaction_id=row.id, tag_id=tag.id))
    session.commit()


def _charges(
    session: Session,
    user_id: int,
    account: Account,
    payee: str,
    amount: str = "-15.99",
    days: tuple[int, ...] = (90, 60, 30),
) -> list[Transaction]:
    """Same-day-of-month charges — a perfect monthly fit, so anything refused
    here is refused by the TAG source's own rules, never by cadence."""
    return [
        _txn(
            session,
            user_id,
            account,
            payee=payee,
            day=_today() - dt.timedelta(days=offset),
            amount=amount,
        )
        for offset in days
    ]


def _bill(session: Session, user_id: int, name: str, due: str = "2030-01-05") -> BillReminder:
    bill = BillReminder(
        user_id=user_id,
        name=name,
        amount=Decimal("15.99"),
        frequency="monthly",
        next_due_date=due,
        enabled=True,
        match_term=name,
    )
    session.add(bill)
    session.commit()
    session.refresh(bill)
    return bill


def _calendar_event_months(
    session: Session, user_id: int, month_start: dt.date, *, today: dt.date | None = None
) -> list[dict]:
    calendar = build_cashflow_calendar(
        session,
        user_id,
        month_start=month_start,
        month_end=month_start,
        today=today or _today(),
    )
    return calendar["days"]


# ---------------------------------------------------------------------------
# The tag source itself
# ---------------------------------------------------------------------------


def test_a_fully_tagged_group_with_a_cadence_fit_is_confirmed(client: TestClient, session: Session):
    """Rows every one of which carries a nominated tag, fitting the M96 cadence
    and amount bands, ARE a confirmed subscription — and it is the third
    source, named `tag`."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    intent = _tag(session, user_id, "subscription")
    rows = _charges(session, user_id, account, "NETFLIX.COM")
    _tag_rows(session, [intent], rows)
    _nominate(session, user_id, [intent.id])

    confirmed = confirmed_subscriptions(session, user_id)

    assert [(entry.source, entry.payee, str(entry.amount)) for entry in confirmed] == [
        (SOURCE_TAG, "NETFLIX.COM", "-15.99")
    ]
    assert confirmed[0].next_expected >= _today()


def test_tagged_month_end_group_keeps_its_day_in_confirmed_set_and_calendar(
    client: TestClient, session: Session
):
    """A tag-marked Jan-31 series must not inherit the detector's Feb-28 clamp:
    it agrees with the accepted-group/bill schedule on April 30 in both readers."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    intent = _tag(session, user_id, "subscription")
    rows = [
        _txn(
            session,
            user_id,
            account,
            payee="MONTH END SERVICE",
            day=dt.date(2025, 11, 30),
            amount="-15.99",
        ),
        _txn(
            session,
            user_id,
            account,
            payee="MONTH END SERVICE",
            day=dt.date(2025, 12, 31),
            amount="-15.99",
        ),
        _txn(
            session,
            user_id,
            account,
            payee="MONTH END SERVICE",
            day=dt.date(2026, 1, 31),
            amount="-15.99",
        ),
    ]
    assert intent.id is not None
    _tag_rows(session, [intent], rows)
    _nominate(session, user_id, [intent.id])
    today = dt.date(2026, 4, 15)

    confirmed = confirmed_subscriptions(session, user_id, today=today)
    assert [(item.source, item.next_expected) for item in confirmed] == [
        (SOURCE_TAG, dt.date(2026, 4, 30))
    ]

    days = _calendar_event_months(session, user_id, dt.date(2026, 4, 30), today=today)
    events = [event for day in days for event in day["events"]]
    assert [
        (day["date"], event["kind"], event["label"]) for day in days for event in day["events"]
    ] == [(dt.date(2026, 4, 30), KIND_SUBSCRIPTION, "MONTH END SERVICE")]
    assert len(events) == 1


def test_one_untagged_row_in_the_group_leaves_it_unconfirmed(client: TestClient, session: Session):
    """ALL, not ANY (the M96 discipline): two tagged rows and one that is not
    is a partly-marked run — the user has not said the whole run is recurring."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    intent = _tag(session, user_id, "subscription")
    rows = _charges(session, user_id, account, "NETFLIX.COM")
    _tag_rows(session, [intent], rows[:2])
    _nominate(session, user_id, [intent.id])

    assert confirmed_subscriptions(session, user_id) == []


def test_a_row_nominated_by_an_earlier_tag_still_counts(client: TestClient, session: Session):
    """ANY nominated tag marks the ROW: which of the user's tags did it is not
    a distinction the query makes."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    intent = _tag(session, user_id, "abos")
    other = _tag(session, user_id, "vacation")
    rows = _charges(session, user_id, account, "NETFLIX.COM")
    _tag_rows(session, [intent], rows[:2])
    _tag_rows(session, [other], rows[2:])
    _nominate(session, user_id, [intent.id, other.id])

    assert [entry.source for entry in confirmed_subscriptions(session, user_id)] == [SOURCE_TAG]


def test_the_feature_is_off_until_the_user_names_a_tag(client: TestClient, session: Session):
    """`[]` is OFF — the same rows, tagged, are NOT confirmed while the set is
    empty, and become confirmed the moment the user names the tag. This is the
    Settings toggle's whole observable contract."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    intent = _tag(session, user_id, "subscription")
    rows = _charges(session, user_id, account, "NETFLIX.COM")
    _tag_rows(session, [intent], rows)

    assert confirmed_subscriptions(session, user_id) == []

    _nominate(session, user_id, [intent.id])
    assert [entry.source for entry in confirmed_subscriptions(session, user_id)] == [SOURCE_TAG]


def test_two_nominated_tags_on_one_row_are_still_one_group(client: TestClient, session: Session):
    """Multi-tag is one dimension, not many: a row carrying two nominated tags
    is still ONE row, its merchant group is still ONE group, and the union is
    over groups — so nothing is counted twice."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    first = _tag(session, user_id, "subscription")
    second = _tag(session, user_id, "recurring")
    rows = _charges(session, user_id, account, "NETFLIX.COM")
    _tag_rows(session, [first, second], rows)
    _nominate(session, user_id, [first.id, second.id])

    confirmed = confirmed_subscriptions(session, user_id)

    assert len(confirmed) == 1
    assert confirmed[0].source == SOURCE_TAG
    assert confirmed[0].merchant_key == normalize_payee_key("NETFLIX.COM")


def test_a_dismissed_tag_matched_group_stays_dismissed(client: TestClient, session: Session):
    """The suppression row wins over the tag: `recurring_suppressions` is read
    through the same `rekey_suppression` re-read the candidates route does, so
    a group the user answered "no" to never comes back as a fact because a tag
    also matches it."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    intent = _tag(session, user_id, "subscription")
    rows = _charges(session, user_id, account, "NETFLIX.COM")
    _tag_rows(session, [intent], rows)
    _nominate(session, user_id, [intent.id])

    assert len(confirmed_subscriptions(session, user_id)) == 1

    session.add(
        RecurringSuppression(
            user_id=user_id, group_key=make_group_key("NETFLIX.COM", RecurringCadence.monthly)
        )
    )
    session.commit()

    assert confirmed_subscriptions(session, user_id) == []


# ---------------------------------------------------------------------------
# The math gate is untouched by tagging
# ---------------------------------------------------------------------------


def test_a_tagged_group_without_a_cadence_fit_is_not_confirmed(
    client: TestClient, session: Session
):
    """A tag is intent, not arithmetic: three charges with broken gaps clear no
    cadence, so the M96 fit refuses the group and the tag cannot overrule it."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    intent = _tag(session, user_id, "subscription")
    rows = _charges(session, user_id, account, "NETFLIX.COM", days=(120, 61, 5))
    _tag_rows(session, [intent], rows)
    _nominate(session, user_id, [intent.id])

    assert confirmed_subscriptions(session, user_id) == []


def test_a_tagged_group_below_the_occurrence_floor_is_not_confirmed(
    client: TestClient, session: Session
):
    """Two charges are below `MIN_OCCURRENCES` — a tag on a pair is not a
    subscription, whatever it says."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    intent = _tag(session, user_id, "subscription")
    rows = _charges(session, user_id, account, "NETFLIX.COM", days=(60, 30))
    _tag_rows(session, [intent], rows)
    _nominate(session, user_id, [intent.id])

    assert confirmed_subscriptions(session, user_id) == []


def test_a_tagged_credit_group_is_not_a_subscription(client: TestClient, session: Session):
    """The calendar's `subscription` series is signed money-OUT by contract
    (docs/36 decision 6): the same rule the accepted path applies, so a tagged
    payroll deposit cannot render income in a bills colour."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    intent = _tag(session, user_id, "subscription")
    rows = _charges(session, user_id, account, "PAYROLL DEPOSIT ACME", amount="2500.00")
    _tag_rows(session, [intent], rows)
    _nominate(session, user_id, [intent.id])

    assert confirmed_subscriptions(session, user_id) == []


# ---------------------------------------------------------------------------
# One merchant answers once (the M170 dedup, widened)
# ---------------------------------------------------------------------------


def test_an_active_bill_still_wins_the_dedup(client: TestClient, session: Session):
    """A merchant an enabled bill tracks answers as the BILL only — the tag does
    not add a second colour for the same money."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    intent = _tag(session, user_id, "subscription")
    rows = _charges(session, user_id, account, "NETFLIX.COM")
    _tag_rows(session, [intent], rows)
    _nominate(session, user_id, [intent.id])
    _bill(session, user_id, "NETFLIX.COM")

    confirmed = confirmed_subscriptions(session, user_id)

    assert [entry.source for entry in confirmed] == [SOURCE_BILL]


def test_an_accepted_group_is_confirmed_once_as_the_accepted_one(
    client: TestClient, session: Session
):
    """Tagged AND accepted is still ONE entry, and it keeps the ACCEPTED source:
    the user's explicit accept is the stronger assertion, and two sources for one
    commitment is exactly the defect M170 fixes."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    intent = _tag(session, user_id, "subscription")
    rows = _charges(session, user_id, account, "NETFLIX.COM")
    _tag_rows(session, [intent], rows)
    for row in rows:
        row.is_recurring = True
        row.recurring_cadence = "monthly"
        row.recurring_next_date = _today() + dt.timedelta(days=5)
        session.add(row)
    session.commit()
    _nominate(session, user_id, [intent.id])

    confirmed = confirmed_subscriptions(session, user_id)

    assert len(confirmed) == 1
    assert confirmed[0].source == SOURCE_RECURRING


# ---------------------------------------------------------------------------
# The calendar reads it (docs/36 §M174)
# ---------------------------------------------------------------------------


def test_the_calendar_dates_a_tag_matched_group_it_never_billed(
    client: TestClient, session: Session
):
    """The acceptance path: a group the user TAGGED and never turned into a bill
    still earns a calendar dot, under the same `subscription` kind an accepted
    group uses, with the payload's count telling "nothing yet" from "nothing
    this month"."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    intent = _tag(session, user_id, "subscription")
    rows = _charges(session, user_id, account, "NETFLIX.COM")
    _tag_rows(session, [intent], rows)
    _nominate(session, user_id, [intent.id])

    expected = confirmed_subscriptions(session, user_id)[0].next_expected
    month = expected.strftime("%Y-%m")
    body = client.get(f"{API}/reports/cashflow-calendar?month={month}", headers=headers).json()

    assert body["confirmed_subscription_count"] == 1
    events = [
        (day["date"], event)
        for day in body["days"]
        for event in day["events"]
        if event["kind"] == KIND_SUBSCRIPTION
    ]
    assert [(day, event["label"]) for day, event in events] == [
        (expected.isoformat(), "NETFLIX.COM")
    ]
    assert events[0][1]["amount"] == "-15.99"
    # M170 follow-up: the figure travels with the currency its rows are
    # stated in (docs/36 §Currency) — a CAD group says CAD, never a bare
    # number the client has to assume.
    assert events[0][1]["currency"] == "CAD"


# ---------------------------------------------------------------------------
# The write seam (docs/08 §PATCH /users/me)
# ---------------------------------------------------------------------------


def test_reads_always_carry_the_field_and_default_to_off(client: TestClient):
    headers, _user_id = _auth(client)
    body = client.get(f"{API}/users/me", headers=headers).json()
    assert body["recurring_intent_tag_ids"] == []


def test_the_api_stores_the_whole_list_and_echoes_it(client: TestClient, session: Session):
    headers, user_id = _auth(client)
    first = _tag(session, user_id, "subscription")
    second = _tag(session, user_id, "recurring")

    response = client.patch(
        f"{API}/users/me",
        headers=headers,
        json={"recurring_intent_tag_ids": [first.id, second.id]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["recurring_intent_tag_ids"] == [first.id, second.id]
    assert client.get(f"{API}/users/me", headers=headers).json()["recurring_intent_tag_ids"] == [
        first.id,
        second.id,
    ]

    # `[]` is a real value — the feature turns off.
    cleared = client.patch(
        f"{API}/users/me", headers=headers, json={"recurring_intent_tag_ids": []}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["recurring_intent_tag_ids"] == []


def test_another_tenants_tag_id_is_refused(client: TestClient, session: Session):
    """The column is JSON, so the database carries no FK — ownership is enforced
    HERE or nowhere. A foreign id would silently never match, which is exactly
    the silent no-op AGENTS §3 refuses."""
    headers, _user_id = _auth(client)
    other_headers, other_id = _auth(client)
    foreign = _tag(session, other_id, "someone-elses-tag")

    response = client.patch(
        f"{API}/users/me",
        headers=headers,
        json={"recurring_intent_tag_ids": [foreign.id]},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_recurring_intent_tag_ids"
    # Nothing was written.
    assert client.get(f"{API}/users/me", headers=headers).json()["recurring_intent_tag_ids"] == []
    assert other_headers is not None


@pytest.mark.parametrize("value", [[0], [-3], ["7"]])
def test_malformed_tag_ids_are_a_validation_error(client: TestClient, value: list):
    headers, _user_id = _auth(client)
    response = client.patch(
        f"{API}/users/me", headers=headers, json={"recurring_intent_tag_ids": value}
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"


# ---------------------------------------------------------------------------
# §Create from a selection: the derived pattern is a real hit on BOTH matchers
# ---------------------------------------------------------------------------


def test_the_derived_pattern_matches_both_M165_spellings_on_the_rule_engine(
    client: TestClient, session: Session
):
    """The browser derives ONE pattern from a two-spelling selection; the rule it
    prefills has to match BOTH rows. `contains` lower-cases both sides in
    `services/rules_engine.py`, which is the relation
    `lib/createFromSearch.ts::selectionCriteria` computes the run over — the same
    relation, not a lookalike."""
    _headers, user_id = _auth(client)
    category = Category(user_id=user_id, name="Subscriptions")
    session.add(category)
    session.commit()
    session.refresh(category)
    rule = Rule(
        user_id=user_id,
        match_type=RuleMatchType.contains,
        pattern=M165_PATTERN,
        priority=1,
        category_id=category.id,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)

    assert normalize_payee_key(M165_SPELLINGS[0]) == normalize_payee_key(M165_SPELLINGS[1])
    for spelling in M165_SPELLINGS:
        assert rule_matches(rule, spelling, Decimal("-25.98")), spelling


def test_the_derived_pattern_matches_both_M165_spellings_on_the_bill_matcher():
    """The bill half: a term list of the same derived pattern hits both spellings
    through `services/bill_match.py::payee_matches`, so the ONE pattern
    `selectionCriteria` emits covers the selection whichever editor reads it."""
    rule = BillMatchRule.build(
        match_mode="contains",
        match_terms=[M165_PATTERN],
        category_id=None,
        bill_amount=None,
        amount_tolerance=None,
    )
    for spelling in M165_SPELLINGS:
        assert payee_matches(normalize_payee_key(spelling), rule), spelling

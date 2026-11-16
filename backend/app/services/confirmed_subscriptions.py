"""M170 — the ONE confirmed-subscription query (docs/36 §M170, docs/09).

"Confirmed" means **the user asserted it**, never the detector:

* an **active bill reminder** (`bill_reminders.enabled`, docs/25),
* an **accepted recurring group** — `transactions.is_recurring`, written by
  `POST /transactions/recurring/accept` (docs/09 §Recurring prompt). Its
  negative side is `recurring_suppressions`, and a suppressed group is never
  confirmed by anything here, and
* **M174 — a TAG-marked group**: every row of a detector group carries at least
  one of the tags the user nominated in Settings (`users.recurring_intent_tag_ids`,
  docs/09 §Tags that mark recurring). Mark's ask — *"for recurring stuff can we
  make that based on tags since there can be multiple"* — with the PM's ruling
  honoured: the tag set is the user's OWN choice, never a magic name pattern,
  and an empty set turns the source off entirely. `SOURCE_TAG` names it; the
  amount/cadence gate is still `services/recurring.py`'s M96 fit, so a tag is
  intent and never arithmetic.

Detector output is a PROMPT, not a fact — M133's film.ca verdict for the
dashboard cards, extended to the calendar by M170 (Mark's complaint: a
guessed `Google*Youtubeprmium` rendering directly under his real YouTube
BILL, plus a bare `Electronic Funds Transfer -$359.08`, because
`services/cashflow_calendar.py` used to feed the detector verbatim). A
candidate the user has not touched belongs on the surfaces whose job is
asking (`/txns`, the dashboard's "Bills you probably have" card), never on a
surface that reads as fact.

Every server surface that has to answer "what recurring commitments does this
user actually have?" reads THIS function, so two surfaces can never drift —
M133's "one detector" ruling, extended to "one confirmed-state query". The
cash-flow calendar's `subscription` series and the
`confirmed_subscription_count` its payload carries are the two readers today.

**Dedup is part of the answer, not a rendering detail.** A merchant an active
bill already tracks answers ONCE, as that bill: the accepted group's row is
the same money seen from the ledger side, and two colors for one subscription
is exactly the defect M170 fixes. The comparison is **payee-only** — amount
and category scope which PAYMENTS a bill counts (docs/25 §M116 D2); they do
not change the answer to "does a bill already exist for this payee", the same
line M165 draws on the prompt cards. The test is the shared
`services/bill_match.py` rule (`payee_matches`), i.e. the same matcher a bill
applies to its own payments, so the calendar can never disagree with /bills.

Local-only by construction: no LLM, no network client in the import list, no
fx read (the amount is the row's own `amount_native`, passed through exactly
as docs/36 decision 6 pins it, together with the currency those rows agree on
— M170 follow-up, §Currency there), and every query is user-scoped.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.bill_reminders import BillReminder
from app.models.import_batches import ImportBatch
from app.models.recurring_suppressions import RecurringSuppression
from app.models.transaction_tags import TransactionTag
from app.models.transactions import RecurringCadence, Transaction
from app.models.users import User
from app.services.bill_match import BillMatchRule, load_terms, payee_matches
from app.services.bill_schedule import STEP_MONTHS, bill_next_on_or_after, first_on_or_after
from app.services.recurring import (
    LOOKBACK_DAYS,
    LedgerRow,
    detect_recurring,
    next_expected_date,
    rekey_suppression,
)
from app.services.subscriptions import normalize_payee_key
from app.services.txn_filters import identity_tx_conditions, today_et

#: Which confirmed source an entry came from — the same words the cash-flow
#: calendar's series use, so a caller can map without inventing a second
#: vocabulary. M174 added the third: a group the user marked with one of their
#: own "recurring" tags (`SOURCE_TAG`) is confirmed exactly like an accepted
#: one, and the calendar renders both into `subscription`.
SOURCE_BILL = "bill"
SOURCE_RECURRING = "recurring"
SOURCE_TAG = "tag"

_MONEY = Decimal("0.01")

#: Hard bound on the GROUP roll-forward (`_roll_forward`, an accepted or
#: tag-marked group whose stored anchor was written once) — the same bound
#: `services/bill_schedule.py::MAX_STEPS` puts on the bill projection, for the
#: same reason: unreachable in practice, but a corrupt row can never hang a
#: request. Bill rows are bounded by the helper's own loop, never by this one.
_MAX_STEPS = 10_000


@dataclass(frozen=True)
class ConfirmedSubscription:
    """One user-asserted recurring commitment.

    `amount` is SIGNED the way docs/36 decision 6 signs the calendar:
    negative = money out, `None` only for a bill with no stored amount
    (variable bills). It is the row's own `amount_native` — M27's detector
    records no currency either, and a conversion would need the fx read
    docs/07 keeps out of this path.

    `currency` (M170 follow-up, docs/36 §Currency) is the ISO code the
    `amount` is STATED in, and it is all-or-nothing with the amount: a figure
    the query cannot denominate is not stated at all (`amount=None`,
    `currency=None`), because a native amount printed as CAD is a fabricated
    conversion. Bills are reporting-currency by schema, so a bill with an
    amount is CAD (docs/25, docs/36 decision 6); a recurring group is stated
    in a currency only when EVERY tagged row agrees on one well-formed code
    (`_group_currency`).
    """

    payee: str
    merchant_key: str
    source: str  # SOURCE_BILL | SOURCE_RECURRING
    next_expected: date
    amount: Decimal | None
    currency: str | None = None
    bill_id: int | None = None


#: The shape docs/02 / docs/21 pin for `transactions.currency` (ingestion
#: uppercases the token and falls back to the account's currency when it is
#: not `[A-Z]{3}`). Applied case-insensitively here so a hand-written row
#: cannot smuggle `usd` onto a surface that then prints it.
_ISO_CURRENCY = re.compile(r"[A-Z]{3}")


def _stated_currency(value: object) -> str | None:
    """`value` as an ISO-4217 code, or `None` when it is not one."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if _ISO_CURRENCY.fullmatch(code) else None


def _group_currency(rows: Sequence[Transaction]) -> str | None:
    """The ONE currency every row of an accepted group is stated in, or `None`.

    `None` is the honest answer to two different situations — a MIXED group (a
    USD account and a CAD account both charging the same payee) and an UNKNOWN
    one (a row whose `currency` is blank or not an ISO code) — because the
    group's amount is the mean of ALL its rows: one row the query cannot
    denominate makes that mean undemonstrable, and the calendar prints a
    figure only when it can say what the figure IS (docs/36 §Currency).
    """
    codes = {_stated_currency(row.currency) for row in rows}
    return codes.pop() if len(codes) == 1 else None


def _effective_date(row: Transaction) -> date | None:
    """`posting_date`, falling back to `date` — the M27 detector's rule."""
    for value in (row.posting_date, row.date):
        if isinstance(value, date):
            return value
    return None


def _group_cadence(rows: Sequence[Transaction]) -> RecurringCadence:
    """The cadence the group's tags agree on.

    The accept route writes one cadence across a whole group, so a vote is
    only doing real work for rows the user tagged by hand. Ties go to the
    cadence on the NEWEST row — the last thing the user said — and a group
    with no cadence at all reads as `monthly`, the default every other
    surface (the Create-bill hand-off, `readBillAutofill`) degrades to.
    """
    votes = Counter(row.recurring_cadence for row in rows if row.recurring_cadence is not None)
    if not votes:
        return RecurringCadence.monthly
    top = max(votes.values())
    winners = sorted((cadence for cadence, count in votes.items() if count == top), key=str)
    newest = next(
        row.recurring_cadence for row in reversed(rows) if row.recurring_cadence is not None
    )
    return newest if newest in winners else winners[0]


def _roll_forward(anchor: date, cadence: RecurringCadence, today: date) -> date:
    """Step a day-based cadence forward until it reaches `today`.

    Whole-month cadences use `bill_schedule.first_on_or_after` instead, with
    the group's charge-day anchor retained across clamped months.
    """
    steps = 0
    while anchor < today:
        anchor = next_expected_date(anchor, cadence)
        steps += 1
        if steps > _MAX_STEPS:
            break
    return anchor


def _group_next_date(rows: Sequence[Transaction], cadence: RecurringCadence, today: date) -> date:
    """The group's next occurrence on or after `today`.

    The latest stored `recurring_next_date` is the projection base when present
    (including a user-edited value); otherwise derive the first base from the
    newest charge. For whole-month cadences the newest charge's day is the
    schedule anchor, so a clamped February base cannot poison March onward.
    Day-based cadences retain their existing arithmetic rule.
    """
    dated = [day for day in (_effective_date(row) for row in rows) if day is not None]
    newest_charge = max(dated) if dated else None
    stored = [row.recurring_next_date for row in rows if row.recurring_next_date is not None]
    base = max(stored) if stored else None
    if cadence.value in STEP_MONTHS and newest_charge is not None:
        if base is None:
            base = next_expected_date(newest_charge, cadence)
        projected = first_on_or_after(base, cadence.value, newest_charge.day, today)
        return projected if projected is not None else base
    if base is None:
        if newest_charge is None:
            return today
        base = next_expected_date(newest_charge, cadence)
    return _roll_forward(base, cadence, today)


def _accepted_groups(session: Session, user_id: int) -> list[tuple[str, str, list[Transaction]]]:
    """`(merchant_key, payee, rows)` per merchant the user has ACCEPTED.

    Committed-only and parents-only come from the shared IDENTITY predicate,
    the same population both detectors scan (docs/03, docs/44 §2), so a
    pending import can never be read as a commitment.
    """
    rows = session.exec(
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(
            *identity_tx_conditions(user_id),
            Transaction.is_recurring.is_(True),
        )
        .order_by(Transaction.date, Transaction.id)
    ).all()

    grouped: dict[str, list[Transaction]] = {}
    for row in rows:
        key = normalize_payee_key(row.normalized_payee or row.raw_description or "")
        if key:
            grouped.setdefault(key, []).append(row)
    return [
        (key, group[0].normalized_payee or group[0].raw_description or key, group)
        for key, group in sorted(grouped.items())
    ]


def _tag_intent_groups(
    session: Session, user_id: int, today: date
) -> list[tuple[str, str, list[Transaction], RecurringCadence, date]]:
    """`(merchant_key, payee, rows, cadence, next_date)` per TAG-MARKED group.

    M174 (docs/09 §Tags that mark recurring, docs/36 §M174). Mark's ask —
    *"for recurring stuff can we make that based on tags since there can be
    multiple"* — answered with the PM's ruling: **no magic tag names**. Which
    tags mean "recurring" is `users.recurring_intent_tag_ids`, a list the user
    curates in Settings; an EMPTY list turns the feature off, and this function
    then does no work at all (no scan, no group) — the off state is a property
    of the query, not of a filter that happens to match nothing.

    Three rules, and none of them is negotiable:

    * **The detector's math gate stays.** The candidate set is
      `services/recurring.py::detect_recurring` — the SAME M96 pass the prompt
      cards run, over the same 400-day window and the same committed/identity
      population — so a tag cannot make a row recurring whose cadence and
      amount do not fit. Tagging is INTENT; the numbers still have to agree.
    * **ALL of a group's rows must carry at least one nominated tag** (the M96
      consistency discipline). One untagged row in the group leaves it
      unconfirmed: the user has not said the whole run is recurring, and a
      partially-marked run is exactly the ambiguity that turns a fact surface
      back into a guess.
    * **A suppression still wins.** `recurring_suppressions` is read through the
      same `rekey_suppression` re-read the candidates route does, so a group the
      user dismissed ("no, this is not recurring") never comes back as confirmed
      just because a tag also matches it.

    Multi-tag is one dimension, not many: a row carrying two nominated tags is
    still ONE row and its merchant group is still ONE group — the union below is
    over GROUPS, deduped by the shared `normalize_payee_key`, so nothing is
    counted twice.
    """
    user = session.get(User, user_id)
    intent_ids = (
        {int(tag_id) for tag_id in (user.recurring_intent_tag_ids or [])} if user else set()
    )
    if not intent_ids:
        return []

    # The id list is the user's own by construction (the write seam rejects a
    # foreign id), but the join below is scoped to `user_id` regardless, so a
    # stale id left behind by a deleted tag simply matches nothing.
    tagged_tx_ids: set[int] = {
        int(tx_id)
        for tx_id in session.exec(
            select(TransactionTag.transaction_id)
            .join(Transaction, Transaction.id == TransactionTag.transaction_id)
            .where(
                Transaction.user_id == user_id,
                TransactionTag.tag_id.in_(sorted(intent_ids)),
            )
        ).all()
    }
    if not tagged_tx_ids:
        return []

    window_start = today - timedelta(days=LOOKBACK_DAYS)
    rows = session.exec(
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(
            *identity_tx_conditions(user_id),
            Transaction.date >= window_start,
        )
        .order_by(Transaction.date, Transaction.id)
    ).all()
    if not rows:
        return []

    by_id = {int(row.id): row for row in rows if row.id is not None}
    suppressed = {
        str(key)
        for key in session.exec(
            select(RecurringSuppression.group_key).where(RecurringSuppression.user_id == user_id)
        ).all()
    }
    suppressed |= {rekey_suppression(key) for key in suppressed}

    ledger = [
        LedgerRow(
            id=int(row.id),
            date=row.date,
            payee=row.normalized_payee or row.raw_description or "",
            amount=row.amount_native,
        )
        for row in rows
        if row.id is not None
    ]

    groups: list[tuple[str, str, list[Transaction], RecurringCadence, date]] = []
    for group in detect_recurring(ledger):
        if group.group_key in suppressed:
            continue
        if not all(tx_id in tagged_tx_ids for tx_id in group.transaction_ids):
            continue
        member_rows = [by_id[tx_id] for tx_id in group.transaction_ids if tx_id in by_id]
        if len(member_rows) != len(group.transaction_ids):
            continue
        groups.append(
            (
                group.merchant_key,
                group.payee,
                member_rows,
                group.cadence,
                group.next_expected_date,
            )
        )
    groups.sort(key=lambda entry: (entry[4], entry[1]))
    return groups


def _all_credited(rows: Sequence[Transaction]) -> bool:
    """True when every row is money IN.

    A recurring CREDIT is not a subscription — the calendar's `subscription`
    series is signed money-OUT by contract (docs/36 decision 6), and a payroll
    deposit clears the recurring rule exactly as happily as Netflix does. The
    rule is applied to every confirmed source that is not a bill, so a tagged
    `PAYROLL` group cannot render an income line in a bills colour.
    """
    return all(row.amount_native >= 0 for row in rows)


def confirmed_subscriptions(
    session: Session, user_id: int, *, today: date | None = None
) -> list[ConfirmedSubscription]:
    """The user's confirmed recurring set: active bills + accepted groups + tag-marked groups.

    Deduped by the shared payee rule — a merchant an enabled bill tracks is
    answered by the BILL only, and a merchant already answered by an accepted
    group is never answered again by its tag (see the module docstring). Sorted
    by `next_expected`, then payee, so two surfaces listing the same set list it
    in the same order.
    """
    today = today or today_et()

    bills = list(
        session.exec(
            select(BillReminder).where(
                BillReminder.user_id == user_id,
                BillReminder.enabled.is_(True),
            )
        ).all()
    )
    terms = load_terms(session, [bill.id for bill in bills if bill.id is not None])
    bill_rules = [
        (bill, BillMatchRule.from_bill(bill, terms.get(bill.id) if bill.id else None))
        for bill in bills
    ]

    confirmed: list[ConfirmedSubscription] = []
    #: Merchant keys already answered — the accepted groups and the tag-marked
    #: ones share this set, so ONE merchant is confirmed ONCE no matter how many
    #: of the three sources recognise it (M170's dedup, widened by M174).
    emitted: set[str] = set()
    for bill, _rule in bill_rules:
        # `services/bill_schedule.py::bill_next_on_or_after` — the ONE
        # bill-recurrence rule (M180), shared with `mark-paid` and the cash-flow
        # calendar, so this surface reads the schedule's ANCHOR day-of-month
        # instead of the possibly-clamped stored date. Forward projection only:
        # months before `next_due_date` are never reconstructed (bills carry no
        # payment history, docs/25 decision 4, docs/36 decision 5). An
        # unreadable stored date yields `None`, and a `one_time` bill past its
        # date yields `None` too — it has no next occurrence.
        day = bill_next_on_or_after(bill, today)
        if day is None:
            continue
        amount = None if bill.amount is None else -bill.amount
        confirmed.append(
            ConfirmedSubscription(
                payee=bill.name,
                merchant_key=normalize_payee_key(bill.name),
                source=SOURCE_BILL,
                next_expected=day,
                amount=amount,
                # A bill carries no currency column: bills are
                # reporting-currency by schema (docs/25, docs/36 decision 6).
                currency="CAD" if amount is not None else None,
                bill_id=bill.id,
            )
        )

    for key, payee, rows in _accepted_groups(session, user_id):
        if _all_credited(rows):
            continue
        if any(payee_matches(key, rule) for _bill, rule in bill_rules):
            # Already carried by a bill — rendered once, under Bills.
            continue
        amounts = [row.amount_native for row in rows]
        mean = sum(abs(amount) for amount in amounts) / len(amounts)
        emitted.add(key)
        # M170 follow-up (docs/36 §Currency): the amount is the rows' own
        # `amount_native`, so it is stated ONLY together with the currency
        # those rows agree on. A mixed or unknown group yields no figure at
        # all — never the mean relabelled CAD.
        currency = _group_currency(rows)
        confirmed.append(
            ConfirmedSubscription(
                payee=payee,
                merchant_key=key,
                source=SOURCE_RECURRING,
                next_expected=_group_next_date(rows, _group_cadence(rows), today),
                amount=None if currency is None else -mean.quantize(_MONEY),
                currency=currency,
            )
        )

    for key, payee, rows, cadence, _detector_next_date in _tag_intent_groups(
        session, user_id, today
    ):
        if key in emitted:
            # The same money, already confirmed by an accepted group.
            continue
        if _all_credited(rows):
            continue
        if any(payee_matches(key, rule) for _bill, rule in bill_rules):
            continue
        amounts = [row.amount_native for row in rows]
        mean = sum(abs(amount) for amount in amounts) / len(amounts)
        emitted.add(key)
        # M170 follow-up (docs/36 §Currency): a tag-marked group is
        # denominated by the SAME rule as an accepted one — one currency the
        # rows agree on, or no figure at all.
        currency = _group_currency(rows)
        confirmed.append(
            ConfirmedSubscription(
                payee=payee,
                merchant_key=key,
                source=SOURCE_TAG,
                # Tag-marked groups share the accepted-group projection: whole-
                # month cadences retain the newest charge's day even when the
                # detector's first expected date was clamped (Jan 31 -> Feb 28).
                next_expected=_group_next_date(rows, cadence, today),
                amount=None if currency is None else -mean.quantize(_MONEY),
                currency=currency,
            )
        )

    confirmed.sort(key=lambda entry: (entry.next_expected, entry.payee))
    return confirmed

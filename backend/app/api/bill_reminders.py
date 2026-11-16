"""bill_reminders — upcoming bills (docs/25) + M116 match rules & drill-down.

Four families of route live here:

* the M40 CRUD (docs/25 §API) — unchanged in shape, except that `GET /bills`
  now answers with `BillStatPublic`: each row carrying how many payments its
  rule found, the rolling average of its last 12 periods (M116 B4) and its
  calendar-YTD spend (M122 T4);
* `PUT /bills/{id}/rule` — full replacement of `match_mode` / `match_term` /
  `amount_tolerance` (M116 D1);
* `GET /bills/{id}/detail`, `POST /bills/{id}/find-past` and
  `POST /bills/find-past` — the derived view (M116 B1-B3): the rule is applied
  over the committed ledger on every read, never copied anywhere. M122 adds
  the dry-run `stats` block on the find-past responses and the all-time/YTD
  `stats` block on the detail;
* `GET /bills/suggest-terms` (M122 T1) — term suggestions grouped off the
  caller's OWN committed payees, excluding what the bill's current rule
  already matches.

Tenancy is the §2 invariant everywhere: every statement below binds
`current_user.id`, and a foreign id is a 404 before anything is computed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, TypedDict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import Pagination, get_current_user, get_db, get_pagination
from app.api.transactions import _transaction_page
from app.core.errors import api_error
from app.models.accounts import Account
from app.models.bill_reminders import (
    BillMatchLogic,
    BillMatchMode,
    BillReminder,
    BillReminderMatchTerm,
)
from app.models.categories import Category
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.bill_reminders import (
    BillAllTimeStats,
    BillChart,
    BillChartPoint,
    BillDetail,
    BillFindPast,
    BillFindPastAccountCount,
    BillFindPastGroup,
    BillFindPastItem,
    BillFindPastRequest,
    BillFindPastStats,
    BillMatchModeLiteral,
    BillReminderCreate,
    BillReminderPublic,
    BillReminderUpdate,
    BillRolling,
    BillRollingWindow,
    BillRulePreviewRequest,
    BillRuleUpdate,
    BillSpendingStats,
    BillStatPublic,
    BillSuggestTerm,
    BillYtdStats,
    resolve_terms,
)
from app.schemas.transactions import TransactionPage
from app.services.bill_match import (
    MAX_PREVIEW_ITEMS,
    MONEY,
    BillHistory,
    BillMatchRow,
    BillMatchRule,
    bucket_label,
    bucket_of,
    build_history,
    load_rows,
    load_terms,
    match_rows,
    terms_of,
)
from app.services.bill_schedule import anchor_day_for, next_due, stored_due
from app.services.match_terms import MAX_TERM_LENGTH, as_is, match_terms, validate_terms
from app.services.subscriptions import detect_subscriptions, normalize_payee_key

router = APIRouter(prefix="/bills", tags=["bills"])


class _SubscriptionDraft(TypedDict):
    """The four fields M55 copies out of a detected subscription (docs/25)."""

    name: str
    amount: Decimal | None
    frequency: str
    next_due_date: str


# docs/25 M55 decision 9: weekly/biweekly detections have no bill cadence and
# must fail loudly (422) instead of silently degrading to one_time.
_SUBSCRIPTION_BILL_CADENCE = {
    "monthly": "monthly",
    "quarterly": "quarterly",
    "yearly": "yearly",
}


def _resolve_subscription(db: Session, user_id: int, subscription_id: str) -> _SubscriptionDraft:
    """Draft fields for a subscription detected FOR THIS USER (docs/25 M55).

    Resolved through the one canonical detector
    (`app.services.subscriptions.detect_subscriptions` — the reference the
    browser port `s3Engine.subscriptionsReport` is pinned to), over this user's
    own committed rows in the shared 13-month window: no second detection path,
    no new logic, no network. An id that is not in this user's detected set is a
    404, so a foreign `from_subscription_id` fails closed.
    """
    for subscription in detect_subscriptions(db, user_id):
        if subscription["payee"] != subscription_id:
            continue
        cadence = _SUBSCRIPTION_BILL_CADENCE.get(subscription["frequency"])
        if cadence is None:
            raise api_error(
                422,
                "Subscription cadence has no bill reminder equivalent",
                "unsupported_bill_cadence",
            )
        next_expected = subscription["next_expected"]
        if next_expected is None:
            # Unreachable with the current detector (it always projects one
            # from >=2 occurrences); pinned so it can never surface as a 500.
            raise api_error(
                422,
                "Subscription has no next expected date",
                "subscription_next_due_missing",
            )
        return _SubscriptionDraft(
            name=subscription["payee"],
            amount=subscription["avg_amount"],
            frequency=cadence,
            next_due_date=next_expected.isoformat(),
        )
    raise api_error(404, "Subscription not found", "subscription_not_found")


def _resolve_category(db: Session, user_id: int, category_id: int | None) -> int | None:
    """404 category_not_found unless the category exists and belongs to the user."""
    if category_id is None:
        return None
    cat = db.get(Category, category_id)
    if cat is None or cat.user_id != user_id:
        raise api_error(404, "Category not found", "category_not_found")
    return cat.id


def _require_match_terms(payload: BillReminderCreate, name: str | None) -> tuple[list[str], str]:
    """The term list a NEW bill ships with (M116 D1: always has a rule).

    Explicit terms win (`match_terms`, or the pre-M121 `match_term`); when the
    request carries none the bill's `name` is the obvious default — an M55
    autofill sets `name` to the detector's payee, so the two agree out of the
    box. Blank and absent are the SAME statement (the form starts blank).

    Every term — the derived one included — must name a payee after the shared
    normalizer runs, so a name of `"123"` is refused here rather than shipping
    a rule that matches everything or nothing (422 `validation_error`), and the
    LIST is bounded (≤ 10 terms) by the same validator the other rule routes
    use (docs/25 §M121 D7).
    """
    terms = resolve_terms(payload.match_terms, payload.match_term, required=False)
    if not terms:
        terms = [(name or "").strip()]
    try:
        terms = validate_terms(terms)
    except ValueError as exc:
        raise api_error(422, str(exc), "validation_error") from exc
    return terms, payload.match_logic


def _write_terms(db: Session, bill: BillReminder, terms: Sequence[str]) -> None:
    """Replace this bill's rows in `bill_reminder_match_terms` (the ONLY writer).

    Delete-then-insert, in position order: `PUT /bills/{id}/rule` REPLACES the
    rule (docs/25 §M116 D1), so the stored term list is never left holding a
    term the new rule did not ask for. `bill.match_term` is kept equal to
    terms[0] by the caller — the legacy field must never lie about terms[0].
    """
    db.execute(
        sa_delete(BillReminderMatchTerm).where(BillReminderMatchTerm.bill_reminder_id == bill.id)
    )
    for position, term in enumerate(terms):
        db.add(BillReminderMatchTerm(bill_reminder_id=bill.id, position=position, term=term))


def _public(bill: BillReminder, terms: Sequence[str]) -> BillReminderPublic:
    """The wire shape for one bill WITH its term list.

    `match_terms` lives in a child table, so `from_attributes` alone cannot
    see it: `model_validate` reads every column (including `match_logic`) and
    the loaded terms replace the empty default in one `model_copy`. Passing
    the list in explicitly is deliberate — a response can then never claim
    terms the row does not have.
    """
    return BillReminderPublic.model_validate(bill).model_copy(update={"match_terms": list(terms)})


def _terms_for(db: Session, bill: BillReminder) -> list[str]:
    """`[terms…]` for one bill, with the pre-M121 fallback (docs/25 §M121 D6)."""
    return terms_of(bill, load_terms(db, [bill.id]))


def _require_tolerance_affordable(tolerance: Decimal | None, amount: Decimal | None) -> None:
    """A tolerance with no amount to measure against is a 422, never a no-op."""
    if tolerance is not None and amount is None:
        raise api_error(
            422,
            "amount_tolerance needs the bill's amount to measure against",
            "validation_error",
        )


def _bill_history(
    db: Session,
    user_id: int,
    bill: BillReminder,
    rows: list[BillMatchRow],
    terms: Sequence[str],
) -> BillHistory:
    rule = BillMatchRule.from_bill(bill, terms)
    return build_history(match_rows(rows, rule), bill.frequency)


def _stats_for(
    db: Session,
    user_id: int,
    bills: list[BillReminder],
    rows: list[BillMatchRow],
    terms_map: dict[int, list[str]],
) -> list[BillStatPublic]:
    """`BillStatPublic` for each bill over ONE scan of the ledger (M116 B4).

    `load_rows` precomputes each row's normalized payee once and `load_terms`
    reads every bill's terms in ONE query (M121), so N bills cost two queries
    and N cheap string tests — not N queries and N full scans.
    """
    out: list[BillStatPublic] = []
    for bill in bills:
        terms = terms_of(bill, terms_map)
        history = _bill_history(db, user_id, bill, rows, terms)
        out.append(_with_stats(bill, history, terms))
    return out


def _with_stats(bill: BillReminder, history: BillHistory, terms: Sequence[str]) -> BillStatPublic:
    plain = _public(bill, terms).model_dump()
    return BillStatPublic(
        **plain,
        payment_count=history.payment_count,
        rolling_average=history.rolling["12"].mean,
        # M122 T4: the list row's YTD figure — same matched set, same scan.
        ytd_total=history.spend.ytd.total,
    )


def _load_bill(db: Session, user_id: int, bill_id: int) -> BillReminder:
    bill = db.get(BillReminder, bill_id)
    if bill is None or bill.user_id != user_id:
        raise api_error(404, "Bill not found", "not_found")
    return bill


def _stored_due(bill: BillReminder) -> date:
    """`bill.next_due_date` as a real date, or 422 `invalid_due_date` (M180).

    The column is a `VARCHAR(10)` written through the schema layer since M40,
    and migration `0054_bill_due_anchor` repaired every impossible value the
    pre-M180 `_advance_due_date` could store (`2026-04-31` quarterly,
    `2029-02-29` yearly) — so this is defensive, not expected: a row that was
    hand-edited in the database fails LOUD with the code docs/25 already pins
    for it instead of a bare 500 out of `date.fromisoformat`.
    """
    due = stored_due(bill.next_due_date)
    if due is None:
        raise api_error(
            422,
            f"Invalid due date: {bill.next_due_date!r} — expected YYYY-MM-DD",
            "invalid_due_date",
        )
    return due


# --------------------------------------------------------------------------- #
# M40 CRUD
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[BillStatPublic])
def list_bills(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    month: str | None = Query(default=None, description="YYYY-MM for calendar view"),
) -> list[BillStatPublic]:
    """List enabled bills with the two numbers M116 B4 puts on each row.

    One scan of the caller's committed expense rows feeds every bill's
    `payment_count` and `rolling_average` (docs/25 §M116 B4) — a variable bill
    answers `rolling_average: null`, and a rule that has found nothing answers
    `payment_count: 0` so the UI can say "0 payments yet" honestly.
    """
    stmt = select(BillReminder).where(
        BillReminder.user_id == current_user.id,
        BillReminder.enabled.is_(True),
    )
    if month:
        stmt = stmt.where(BillReminder.next_due_date.startswith(month))
    bills = list(db.exec(stmt.order_by(BillReminder.next_due_date)).all())
    if not bills:
        return []
    rows = load_rows(db, current_user.id)
    # M121: every bill's terms in ONE query, so the list stays two queries.
    terms_map = load_terms(db, [bill.id for bill in bills if bill.id is not None])
    return _stats_for(db, current_user.id, bills, rows, terms_map)


@router.post("", response_model=BillReminderPublic, status_code=201)
def create_bill(
    payload: BillReminderCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BillReminderPublic:
    # M55 (docs/25): resolve the draft first, so a foreign/unknown subscription
    # id is a 404 before anything else is written. Check order is pinned there.
    name = payload.name
    amount = payload.amount
    frequency = payload.frequency
    next_due = payload.next_due_date
    if payload.from_subscription_id is not None:
        draft = _resolve_subscription(db, current_user.id, payload.from_subscription_id)
        # Explicit request values win (decision 10); null/absent means "derive".
        if name is None:
            name = draft["name"]
        if "amount" not in payload.model_fields_set:
            amount = draft["amount"]
        if frequency is None:
            frequency = draft["frequency"]
        if next_due is None:
            next_due = date.fromisoformat(draft["next_due_date"])

    # Unreachable: the schema validator guarantees all three without a draft and
    # the draft supplies them with one — pinned so a future contract change
    # surfaces as an envelope, never a NOT NULL IntegrityError (docs/25 M55).
    if name is None or frequency is None or next_due is None:
        raise api_error(422, "Missing bill draft field", "validation_error")

    # M116 D1: the rule exists from the first write — explicit terms win,
    # otherwise `name` is term 0 (the M55 autofill already IS the payee).
    # M121: the request may carry a whole TERM LIST with its AND/OR mode.
    terms, match_logic = _require_match_terms(payload, name)
    _require_tolerance_affordable(payload.amount_tolerance, amount)

    category_id = _resolve_category(db, current_user.id, payload.category_id)
    bill = BillReminder(
        user_id=current_user.id,
        name=name,
        amount=amount,
        category_id=category_id,
        frequency=frequency,
        next_due_date=next_due.isoformat(),
        # M180: the schedule's own day, so a 31st bill stays a 31st bill
        # through February (docs/25 §M180 D1). Set here rather than derived at
        # read time because the stored date is CLAMPED — by the time February
        # says the 28th, only this column remembers what the user chose.
        anchor_day=next_due.day,
        remind_days_before=payload.remind_days_before,
        enabled=payload.enabled,
        match_mode=BillMatchMode(payload.match_mode),
        # terms[0] mirrored into the legacy column: it must never disagree
        # with the list a pre-M121 reader will fall back to (docs/25 §M121 D5).
        match_term=terms[0],
        match_logic=BillMatchLogic(match_logic),
        amount_tolerance=payload.amount_tolerance,
    )
    db.add(bill)
    db.flush()  # bill.id, so the term rows can key off it in ONE transaction
    _write_terms(db, bill, terms)
    db.commit()
    db.refresh(bill)
    return _public(bill, terms)


@router.patch("/{bill_id}", response_model=BillReminderPublic)
def update_bill(
    bill_id: int,
    payload: BillReminderUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BillReminderPublic:
    bill = _load_bill(db, current_user.id, bill_id)
    changes = payload.model_dump(exclude_unset=True)
    if "category_id" in changes:
        changes["category_id"] = _resolve_category(db, current_user.id, changes["category_id"])
    if "next_due_date" in changes:
        new_due = changes["next_due_date"]
        changes["next_due_date"] = new_due.isoformat() if new_due else None
        # M180: a due date the caller CHANGED re-defines the schedule, so the
        # anchor follows it. An UNCHANGED value keeps the stored anchor — the
        # edit form posts back the date it was shown, and that date is the
        # CLAMPED one, so re-anchoring on a name edit would silently demote a
        # 31st schedule to the 28th February clamped it to (docs/25 §M180 D1).
        if new_due is not None and changes["next_due_date"] != bill.next_due_date:
            changes["anchor_day"] = new_due.day
    # M116: nulling the amount would leave a tolerance measuring nothing. The
    # rule edit path can clear the band; this one must not strand it.
    if "amount" in changes and changes["amount"] is None:
        _require_tolerance_affordable(bill.amount_tolerance, None)
    for field, value in changes.items():
        setattr(bill, field, value)
    db.add(bill)
    db.commit()
    db.refresh(bill)
    # The rule is untouched by PATCH — the terms are read back for the shape.
    return _public(bill, _terms_for(db, bill))


@router.delete("/{bill_id}", status_code=204)
def delete_bill(
    bill_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    bill = _load_bill(db, current_user.id, bill_id)
    db.delete(bill)
    db.commit()


@router.post("/{bill_id}/mark-paid", response_model=BillReminderPublic)
def mark_paid(
    bill_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BillReminderPublic:
    """Mark bill as paid and advance to next due date.

    The advance is the ONE bill-recurrence rule (`services/bill_schedule.py`,
    docs/25 §M180 D1): the next occurrence is computed from the schedule's
    ANCHOR day-of-month, so a bill due on the 31st goes Jan 31 → Feb 28 →
    **Mar 31** instead of staying pinned to the 28th February clamped it to, and
    a leap-day bill returns to Feb 29 in the next leap year. `one_time` has no
    next occurrence and is disabled instead (docs/25 §API — unchanged).
    """
    bill = _load_bill(db, current_user.id, bill_id)
    if bill.frequency == "one_time":
        bill.enabled = False
    else:
        due = _stored_due(bill)
        try:
            following = next_due(due, bill.frequency, bill.anchor_day)
        except ValueError:
            # `date` has no year 10000: a schedule at the end of the
            # representable range fails loud, with the code docs/25 pins for a
            # due date it cannot use, instead of a bare 500.
            raise api_error(
                422,
                f"Due date {due.isoformat()} cannot advance any further",
                "invalid_due_date",
            ) from None
        # An un-anchored row (pre-M180, or written outside the API) pins the
        # anchor on its FIRST advance — the anchor IS the stored day, so this
        # first step is byte-identical to the pre-M180 answer (docs/25 §M180 D1).
        bill.anchor_day = anchor_day_for(due, bill.anchor_day)
        bill.next_due_date = following.isoformat()
    db.add(bill)
    db.commit()
    db.refresh(bill)
    return _public(bill, _terms_for(db, bill))


# --------------------------------------------------------------------------- #
# M116 B1 — the match rule
# --------------------------------------------------------------------------- #


@router.put("/{bill_id}/rule", response_model=BillReminderPublic)
def update_bill_rule(
    bill_id: int,
    payload: BillRuleUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BillReminderPublic:
    """Replace the bill's match rule (docs/25 §M116 D1 + §M121 D1/D2).

    Nothing is written about the transactions the rule finds: the matched set
    IS the rule re-applied over the ledger on every read (D3), so saving this
    immediately re-shapes the detail page, the counts on the list and the
    chart. `amount_tolerance` is refused while the bill has no `amount` to be
    measured against — a band that can never be evaluated is a misconfiguration,
    not a preference.
    """
    bill = _load_bill(db, current_user.id, bill_id)
    _require_tolerance_affordable(payload.amount_tolerance, bill.amount)
    terms = resolve_terms(payload.match_terms, payload.match_term, required=True)
    bill.match_mode = BillMatchMode(payload.match_mode)
    bill.match_logic = BillMatchLogic(payload.match_logic)
    # terms[0] mirrors into the legacy column in the SAME write as the rows,
    # so `match_term` can never lag the list (docs/25 §M121 D5).
    bill.match_term = terms[0]
    bill.amount_tolerance = payload.amount_tolerance
    _write_terms(db, bill, terms)
    db.add(bill)
    db.commit()
    db.refresh(bill)
    return _public(bill, terms)


# --------------------------------------------------------------------------- #
# M116 B1/B2 — the derived view
# --------------------------------------------------------------------------- #


def _rolling_public(frequency: str, history: BillHistory) -> BillRolling:
    def window(key: str) -> BillRollingWindow:
        window_ = history.rolling[key]
        return BillRollingWindow(
            periods=window_.periods,
            window_periods=window_.window_periods,
            payments=window_.payments,
            total=window_.total,
            mean=window_.mean,
            median=window_.median,
        )

    return BillRolling(frequency=frequency, six=window("6"), twelve=window("12"))


def _spending_public(history: BillHistory) -> BillSpendingStats:
    """The M122 T4 stats block off a LIFETIME `BillHistory`."""
    all_time = history.spend.all_time
    ytd = history.spend.ytd
    return BillSpendingStats(
        all_time=BillAllTimeStats(
            total=all_time.total,
            payments=all_time.payments,
            average=all_time.average,
            years=all_time.years,
            yearly_average=all_time.yearly_average,
        ),
        ytd=BillYtdStats(year=ytd.year, total=ytd.total, payments=ytd.payments),
    )


def _chart_public(history: BillHistory) -> BillChart:
    chart = history.chart
    return BillChart(
        frequency=chart.frequency,
        unit=chart.unit,  # type: ignore[arg-type]
        granularity=chart.unit,  # type: ignore[arg-type]
        first_period=chart.first_period,
        last_period=chart.last_period,
        points=[
            BillChartPoint(
                period=point.period,
                label=point.label,
                total=point.total,
                count=point.count,
                missed=point.missed,
                rolling_six=point.rolling_six,
            )
            for point in chart.points
        ],
    )


@router.get("/{bill_id}/detail", response_model=BillDetail)
def bill_detail(
    bill_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
) -> BillDetail:
    """The drill-down (docs/25 §M116 B2): header numbers, matched rows, chart.

    `from` / `to` bound the WHOLE response — the transaction page, the chart and
    the rolling readout all describe the same window; omitted, the window is the
    whole committed ledger, which is what makes "every matched transaction" true
    by default. `bill.payment_count` / `bill.rolling_average` stay LIFETIME, so
    narrowing the picture never changes the row the list shows.
    """
    bill = _load_bill(db, current_user.id, bill_id)
    # One scan, category-scoped (the rule requires category equality, so this
    # narrows without changing the answer); the window is applied in Python.
    rows = load_rows(db, current_user.id, category_id=bill.category_id)
    # M121: the term list rides with the same read, so the header prose, the
    # counts, the chart and the page below all describe ONE rule.
    terms = _terms_for(db, bill)
    rule = BillMatchRule.from_bill(bill, terms)
    matched = match_rows(rows, rule)
    lifetime = build_history(matched, bill.frequency)

    windowed = matched
    if from_date is not None or to_date is not None:
        windowed = [
            row
            for row in matched
            if (from_date is None or row.date >= from_date)
            and (to_date is None or row.date <= to_date)
        ]
    history = build_history(windowed, bill.frequency, from_date=from_date, to_date=to_date)

    ids = [row.id for row in windowed]  # already date desc, id desc
    page_ids = ids[pagination.offset : pagination.offset + pagination.limit]
    if page_ids:
        # The matched SET is already known (the rule ran in Python), so the page
        # only has to hydrate its own slice: at most `limit` ids in the IN list,
        # never the whole match. `_transaction_page` is the SAME builder the
        # ledger list uses — tags, receipts and split headers come along instead
        # of being forked here (docs/44 §6.4).
        stmt = (
            select(Transaction)
            .where(
                Transaction.user_id == current_user.id,
                Transaction.id.in_(page_ids),
            )
            .order_by(Transaction.date.desc(), Transaction.id.desc())
        )
        transactions = _transaction_page(
            db,
            stmt,
            owner_id=current_user.id,
            pagination=Pagination(limit=len(page_ids), offset=0),
        )
    else:
        transactions = TransactionPage(
            items=[], total=0, limit=pagination.limit, offset=pagination.offset
        )
    transactions.total = len(ids)
    transactions.limit = pagination.limit
    transactions.offset = pagination.offset
    return BillDetail(
        bill=_with_stats(bill, lifetime, terms),
        matched_count=len(ids),
        transactions=transactions,
        rolling=_rolling_public(bill.frequency, history),
        chart=_chart_public(history),
        # M122 T4: LIFETIME stats — the `from`/`to` window narrows the chart
        # and the rolling readout above, never the all-time/YTD numbers.
        stats=_spending_public(lifetime),
    )


@router.post("/{bill_id}/find-past", response_model=BillFindPast)
def find_past(
    bill_id: int,
    payload: BillFindPastRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BillFindPast:
    """Run the bill's rule over the whole ledger, grouped by period (M116 B1).

    Read-only: no linkage row is written because there is nothing to write —
    the matched set is derived on every read (D3).
    """
    bill = _load_bill(db, current_user.id, bill_id)
    changes = payload.model_dump(exclude_unset=True)
    # Absent = run the SAVED rule (the whole point of the optional body);
    # anything sent replaces it wholesale, exactly like `PUT …/rule`.
    override = resolve_terms(payload.match_terms, payload.match_term, required=False)
    logic = (
        BillMatchLogic(payload.match_logic) if payload.match_logic is not None else bill.match_logic
    )
    rule = BillMatchRule.build(
        match_mode=changes.get("match_mode", bill.match_mode),
        match_terms=override if override else _terms_for(db, bill),
        match_logic=logic,
        # `exclude_unset` already tells "not sent" (run the saved scope) from an
        # explicit `null` (preview with NO category scope).
        category_id=(
            _resolve_category(db, current_user.id, changes["category_id"])
            if "category_id" in changes
            else bill.category_id
        ),
        bill_amount=bill.amount,
        amount_tolerance=changes.get("amount_tolerance", bill.amount_tolerance),
    )
    _require_tolerance_affordable(rule.amount_tolerance, rule.bill_amount)
    return _find_past_response(db, current_user.id, rule, bill.frequency)


@router.post("/find-past", response_model=BillFindPast)
def find_past_draft(
    payload: BillRulePreviewRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BillFindPast:
    """The same machinery for a rule that has not been saved yet.

    The create form has no `bill_id` to hang `/bills/{id}/find-past` on, so the
    body carries every input the saved rule would have read off the row
    (reported as an addition to the docs/08 wire — the pinned `{id}` route is
    untouched and both share `_find_past_response`).
    """
    category_id = _resolve_category(db, current_user.id, payload.category_id)
    rule = BillMatchRule.build(
        match_mode=payload.match_mode,
        match_terms=resolve_terms(payload.match_terms, payload.match_term, required=True),
        match_logic=payload.match_logic,
        category_id=category_id,
        bill_amount=payload.amount,
        amount_tolerance=payload.amount_tolerance,
    )
    _require_tolerance_affordable(rule.amount_tolerance, rule.bill_amount)
    return _find_past_response(db, current_user.id, rule, payload.frequency)


def _find_past_response(
    db: Session, user_id: int, rule: BillMatchRule, frequency: str
) -> BillFindPast:
    """Group the rule's matches newest-period-first, bounded by a preview cap.

    Groups report their TRUE `count`/`total` (what the bill actually holds) even
    when `items` is cut short by `MAX_PREVIEW_ITEMS`, so a truncated preview can
    never be read as "this period only had two payments".
    """
    rows = load_rows(db, user_id, category_id=rule.category_id)
    matched = match_rows(rows, rule)

    by_period: dict[str, list[BillMatchRow]] = {}
    order: list[str] = []
    for row in matched:
        key = bucket_of(row.date, frequency)
        if key not in by_period:
            by_period[key] = []
            order.append(key)
        by_period[key].append(row)

    remaining = MAX_PREVIEW_ITEMS
    selected: list[tuple[str, list[BillMatchRow]]] = []
    for key in order:
        if remaining <= 0:
            break
        members = by_period[key]
        selected.append((key, members[:remaining]))
        remaining -= len(members)

    account_ids: set[int] = {row.account_id for _key, chunk in selected for row in chunk}
    names = _account_names(db, user_id, account_ids)

    groups = []
    for key, shown in selected:
        members = by_period[key]
        groups.append(
            BillFindPastGroup(
                period=key,
                label=bucket_label(key, frequency),
                count=len(members),
                total=sum((row.bill_amount for row in members), Decimal("0")).quantize(
                    Decimal("0.01")
                ),
                items=[_past_item(row, names) for row in shown],
            )
        )
    return BillFindPast(
        match_mode=rule.match_mode.value,
        match_term=rule.match_term,
        match_terms=list(rule.terms),
        match_logic=rule.match_logic.value,
        frequency=frequency,
        total_count=len(matched),
        truncated=len(matched) > MAX_PREVIEW_ITEMS,
        groups=groups,
        stats=_find_past_stats(db, user_id, matched, frequency),
    )


def _find_past_stats(
    db: Session, user_id: int, matched: Sequence[BillMatchRow], frequency: str
) -> BillFindPastStats:
    """M122 T2 — the dry-run summary over the WHOLE matched set.

    The previewed `items` may be cut at `MAX_PREVIEW_ITEMS`, so every number
    here is counted off `matched` itself (the M91 anti-fabrication rule); the
    12-period sum reuses `build_history`, so the panel's "last 12 periods" and
    the detail page's rolling stat are the same arithmetic.
    """
    if not matched:
        return BillFindPastStats(
            first_date=None, last_date=None, last_12_total=None, account_counts=[]
        )
    counts: dict[int, int] = {}
    for row in matched:
        counts[row.account_id] = counts.get(row.account_id, 0) + 1
    names = _account_names(db, user_id, set(counts))
    ordered = sorted(
        counts.items(),
        key=lambda item: (-item[1], names.get(item[0], ""), item[0]),
    )
    return BillFindPastStats(
        first_date=min(row.date for row in matched),
        last_date=max(row.date for row in matched),
        last_12_total=build_history(matched, frequency).rolling["12"].total,
        account_counts=[
            BillFindPastAccountCount(
                account_id=account_id,
                account_name=names.get(account_id, ""),
                count=count,
            )
            for account_id, count in ordered
        ],
    )


def _account_names(db: Session, user_id: int, account_ids: set[int]) -> dict[int, str]:
    """One statement for every account the preview needs (no N+1).

    Explicit `Account.user_id` predicate: `accounts` is a tenant table, so the
    ownership is expressed in the statement itself rather than inherited from
    the driving query (the M93 tripwire rule).
    """
    if not account_ids:
        return {}
    return {
        account.id: account.name
        for account in db.exec(
            select(Account).where(
                Account.user_id == user_id,
                Account.id.in_(account_ids),
            )
        ).all()
    }


def _past_item(row: BillMatchRow, names: dict[int, str]) -> BillFindPastItem:
    return BillFindPastItem(
        id=row.id,
        date=row.date,
        amount_native=row.amount_native,
        currency=row.currency,
        cad_amount=row.cad_amount,
        normalized_payee=row.normalized_payee,
        account_id=row.account_id,
        account_name=names.get(row.account_id, ""),
    )


# --------------------------------------------------------------------------- #
# M122 T1 — term suggestions from the caller's own ledger
# --------------------------------------------------------------------------- #

# The editor asks for a handful of chips, not a catalogue (docs/25 §M122 D1).
_SUGGEST_LIMIT_DEFAULT = 8
_SUGGEST_LIMIT_MAX = 25


@dataclass(frozen=True)
class _PayeeGroup:
    """One distinct payee over the scan: newest spelling, count, CAD sum."""

    normalized_payee: str
    count: int
    total: Decimal
    last_date: date


@router.get("/suggest-terms", response_model=list[BillSuggestTerm])
def suggest_terms(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str, Query(max_length=MAX_TERM_LENGTH)] = "",
    limit: Annotated[int, Query(ge=1, le=_SUGGEST_LIMIT_MAX)] = _SUGGEST_LIMIT_DEFAULT,
    bill_id: Annotated[int | None, Query()] = None,
    match_mode: BillMatchModeLiteral = "contains",
) -> list[BillSuggestTerm]:
    """Suggest match terms from this user's OWN committed payees (M122 T1).

    One scan groups the tenant's committed expense rows by
    `normalize_payee_key(normalized_payee)` — the SAME key the matcher
    compares — keeping the newest spelling of each group as its label, then
    ranks by frequency, recency, and answers the top `limit`.

    * `q` empty → the top payee groups overall (the "opening the editor"
      candidates); `q` set → only groups the draft would match, under the SAME
      `match_mode` the editor is using, through the shared `match_terms`
      matcher. A `q` that normalizes to nothing (`"123"`) answers `[]` — the
      same answer its committed chip would get (docs/25 §M121 D7).
    * `bill_id` set → groups whose rows are ALREADY inside that bill's current
      matched set are excluded: a suggestion that would change nothing is
      noise. The exclusion reuses the rule's own matcher over the same scan
      (a payee group is "already matched" when ANY of its rows is).

    Privacy (docs/11): a DB query over the caller's own rows — no LLM, no
    network client in this module, nothing external. `privacy_mode` changes
    nothing here because there is nothing to privatize.
    """
    rows = load_rows(db, current_user.id)
    already_matched: set[str] = set()
    if bill_id is not None:
        # 404 before any suggestion is computed (the tenancy invariant).
        bill = _load_bill(db, current_user.id, bill_id)
        rule = BillMatchRule.from_bill(bill, _terms_for(db, bill))
        already_matched = {row.payee_key for row in match_rows(rows, rule)}

    groups: dict[str, _PayeeGroup] = {}
    for row in rows:
        key = row.payee_key
        # Rows are loaded newest-first, so the FIRST row seen per key carries
        # the newest spelling — that is the label the chip will be created
        # from, a real string out of the user's own ledger.
        if not key or key in already_matched:
            continue
        existing = groups.get(key)
        if existing is None:
            groups[key] = _PayeeGroup(row.normalized_payee, 1, row.bill_amount, row.date)
        else:
            groups[key] = _PayeeGroup(
                existing.normalized_payee,
                existing.count + 1,
                existing.total + row.bill_amount,
                max(existing.last_date, row.date),
            )

    if q.strip() and not normalize_payee_key(q):
        return []
    draft_key = normalize_payee_key(q)
    if draft_key:
        groups = {
            key: group
            for key, group in groups.items()
            if match_terms([draft_key], "any", as_is, key, match_mode)
        }

    ranked = sorted(
        groups.values(),
        key=lambda group: (-group.count, -group.last_date.toordinal(), group.normalized_payee),
    )[:limit]
    return [
        BillSuggestTerm(
            normalized_payee=group.normalized_payee,
            count=group.count,
            total=group.total.quantize(MONEY),
            last_date=group.last_date,
        )
        for group in ranked
    ]

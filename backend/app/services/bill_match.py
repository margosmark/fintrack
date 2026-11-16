"""M116/M121 — bill ↔ transaction match rules and the bill's derived history.

Everything a bill "contains" is computed HERE, on demand, over the committed
ledger (docs/25 §M116 D3): there is no bill-link table, so editing a rule
retroactively changes which rows the bill holds and the answer can never go
stale. Three responsibilities, in order:

1. **The rule** — `BillMatchRule` (mode + TERM LIST + AND/OR + category +
   amount band). Every term is normalized with
   `subscriptions.normalize_payee_key`, the ONE grouping normalization in this
   codebase (M27/M96's merchant normalizer), so a bill and the recurring
   detector can never disagree about identity. The comparison itself is NOT
   implemented here: `payee_matches` calls the shared
   `services/match_terms.py::match_terms`, so bills, the categorization rules
   and the recurring family can never drift apart on what "matches" means
   (docs/25 §M121 D3).
2. **The scan** — `load_rows` pulls the tenant's committed *expense* rows once
   (ownership + staging + `amount_native < 0` in SQL, everything else in
   Python) and precomputes each row's payee key, so a list of N bills costs one
   query and one normalization per ROW rather than one per (row, bill).
3. **The history** — `build_chart` / `rolling_windows` bucket matched rows into
   calendar periods keyed by the period's FIRST DATE, the same bucket-key shape
   `GET /reports` hands the frontend (`fmtBucket`), so the bill chart reuses the
   existing chart primitives instead of inventing a second time axis.

Why the payee test is in Python and not in the WHERE clause: `normalize_payee_key`
is a Python rule (``str.upper()`` + ``\\s*#?\\d+$``) and Postgres's case folding
is NOT the same function — ``'straße'.upper()`` is ``STRASSE`` while
``upper('straße')`` is ``STRAßE``, so no ``ILIKE``/``upper()`` predicate over the
raw column is a proven *superset* of the Python rule, and a prefilter that can be
narrower than the rule silently loses rows. The scan is therefore bounded by the
tenant + staging + sign + date predicates — the indexes ``docs/02`` already
lists — measured against the 10k-row budget (docs/25 §M116 verification).

Privacy (docs/11): local aggregation only. No LLM, no network client in this
module's import list, no external service.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.bill_reminders import (
    BillMatchLogic,
    BillMatchMode,
    BillReminder,
    BillReminderMatchTerm,
)
from app.models.import_batches import ImportBatch
from app.models.transactions import Transaction
from app.services.match_terms import as_is, match_terms
from app.services.subscriptions import normalize_payee_key
from app.services.txn_filters import identity_tx_conditions, today_et

MONEY = Decimal("0.01")
ZERO = Decimal("0")
MAX_MATCH_TERM = 120
# The chart never grows without bound: a decade-old monthly bill is 120 bars.
MAX_PERIODS = 120
# The two windows docs/25 §M116 B2 pins for the rolling readout.
ROLLING_WINDOWS: tuple[int, ...] = (6, 12)
# M122 T3 (docs/25 §M122): the rolling-average LINE drawn over the bars uses
# the SAME 6-period window as the card's "Last 6 periods" stat, so the line's
# last point and the stat can never disagree.
ROLLING_LINE_WINDOW = 6
# `find-past` is a PREVIEW (docs/25 §M116 B1) — bounded so a rule that matches
# five thousand rows still answers in one round trip.
MAX_PREVIEW_ITEMS = 500

_MONTH_NAMES = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


# --------------------------------------------------------------------------- #
# 1. The rule
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BillMatchRule:
    """One bill's matching rule, fully resolved (M116 D1 + M121 D1/D2).

    `terms` is what the user typed, in order (`terms[0]` is also the bill's
    legacy `match_term`); `term_keys` is `normalize_payee_key(term)` per term —
    the values actually compared, so the stored terms stay exactly as typed
    while BOTH sides of every comparison go through the shared normalizer.
    Normalizing here, once per rule, is what keeps the scan at one
    normalization per LEDGER ROW (see `load_rows`).
    """

    match_mode: BillMatchMode
    match_logic: BillMatchLogic
    terms: tuple[str, ...]
    term_keys: tuple[str, ...]
    category_id: int | None
    bill_amount: Decimal | None
    amount_tolerance: Decimal | None

    @property
    def match_term(self) -> str:
        """The legacy single-term view: terms[0] (never empty)."""
        return self.terms[0] if self.terms else ""

    @classmethod
    def build(
        cls,
        *,
        match_mode: BillMatchMode | str,
        match_terms: Sequence[str] | None = None,
        match_term: str | None = None,
        match_logic: BillMatchLogic | str = BillMatchLogic.any,
        category_id: int | None,
        bill_amount: Decimal | None,
        amount_tolerance: Decimal | None,
    ) -> BillMatchRule:
        """Resolve a rule from the wire. `match_term` (singular) is the
        pre-M121 field and still accepted — it becomes a ONE-term list, which
        answers identically under both logics.
        """
        mode = match_mode if isinstance(match_mode, BillMatchMode) else BillMatchMode(match_mode)
        logic = (
            match_logic if isinstance(match_logic, BillMatchLogic) else BillMatchLogic(match_logic)
        )
        raw = list(match_terms) if match_terms is not None else [match_term or ""]
        terms = tuple(term.strip() for term in raw if term and term.strip())
        if not terms:
            terms = ((match_term or "").strip(),)
        return cls(
            match_mode=mode,
            match_logic=logic,
            terms=terms,
            term_keys=tuple(normalize_payee_key(term) for term in terms),
            category_id=category_id,
            bill_amount=bill_amount,
            amount_tolerance=amount_tolerance,
        )

    @classmethod
    def from_bill(cls, bill: BillReminder, terms: Sequence[str] | None = None) -> BillMatchRule:
        """The rule as stored. `terms` comes from `load_terms`; omitted, the
        bill's own `match_term` is the whole list — which is exactly what a
        pre-M121 row (or a fixture that never wrote term rows) reads back as.
        """
        return cls.build(
            match_mode=bill.match_mode,
            match_terms=list(terms) if terms is not None else None,
            match_term=bill.match_term,
            match_logic=bill.match_logic,
            category_id=bill.category_id,
            bill_amount=bill.amount,
            amount_tolerance=bill.amount_tolerance,
        )


def payee_matches(row_key: str, rule: BillMatchRule) -> bool:
    """The rule's term test over one already-normalized payee key (M121 D3).

    Delegates WHOLESALE to the shared `services/match_terms.py::match_terms`
    — the same function the categorization rules and the recurring family
    call, so the three cannot drift. `as_is` (the identity normalizer) is the
    signal that both sides arrive pre-normalized: `row_key` was computed once
    per ledger row by `load_rows`, `rule.term_keys` once per rule here.

    One term is the pre-M121 world and answers exactly as it always did under
    either logic (docs/25 §M121 D6 — backward compatibility).
    """
    return match_terms(
        rule.term_keys,
        rule.match_logic,
        as_is,
        row_key,
        rule.match_mode,
    )


def bill_row_amount(row: BillMatchRow) -> Decimal:
    """CAD-denominated magnitude used for the amount band and the chart total.

    `cad_amount` is the row's CAD value; a row that never got one (manual and
    demo rows, same-currency imports) falls back to `amount_native` — the best
    figure the ledger holds, rather than silently dropping the payment.
    """
    value = row.cad_amount if row.cad_amount is not None else row.amount_native
    return abs(value)


def amount_matches(row: BillMatchRow, rule: BillMatchRule) -> bool:
    """Band test against the bill's own `amount` (docs/25 §M116 D2).

    No `bill.amount` means a variable bill — there is nothing to be within, so
    the amount clause simply does not apply. With an amount but no tolerance
    the comparison is exact (that is what `tolerance = None` means); with a
    tolerance it is `±` that many dollars, both directions.
    """
    if rule.bill_amount is None:
        return True
    delta = abs(bill_row_amount(row) - rule.bill_amount)
    if rule.amount_tolerance is None:
        return delta == ZERO
    return delta <= rule.amount_tolerance


def row_matches(row: BillMatchRow, rule: BillMatchRule) -> bool:
    """Payee AND category AND amount — the whole rule (docs/25 §M116 D2)."""
    if not payee_matches(row.payee_key, rule):
        return False
    if rule.category_id is not None and row.category_id != rule.category_id:
        return False
    return amount_matches(row, rule)


# --------------------------------------------------------------------------- #
# 2. The scan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BillMatchRow:
    """The projection of a committed expense row the matcher needs."""

    id: int
    date: date
    account_id: int
    category_id: int | None
    amount_native: Decimal
    currency: str
    cad_amount: Decimal | None
    normalized_payee: str
    # `normalize_payee_key(normalized_payee)`, computed ONCE per row so a list
    # of N bills never runs the normalizer N times per row.
    payee_key: str

    @property
    def bill_amount(self) -> Decimal:
        return bill_row_amount(self)


def _rows_statement(
    user_id: int,
    *,
    category_id: int | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
):
    """Ownership + committed staging + sign (+ optional category/date window).

    M107 IDENTITY (docs/44 §2): split PARENTS are the statement lines and stay,
    CHILDREN are excluded — the same population `detect_subscriptions` scans, so
    a bill and the recurring detector count the same payment the same way.
    """
    stmt = (
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(*identity_tx_conditions(user_id), Transaction.amount_native < 0)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )
    if category_id is not None:
        stmt = stmt.where(Transaction.category_id == category_id)
    if from_date is not None:
        stmt = stmt.where(Transaction.date >= from_date)
    if to_date is not None:
        stmt = stmt.where(Transaction.date <= to_date)
    return stmt


def load_rows(
    db: Session,
    user_id: int,
    *,
    category_id: int | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[BillMatchRow]:
    """Every committed expense row of ONE user, newest first (see module doc)."""
    rows = db.exec(
        _rows_statement(user_id, category_id=category_id, from_date=from_date, to_date=to_date)
    ).all()
    return [
        BillMatchRow(
            id=tx.id,
            date=tx.date,
            account_id=tx.account_id,
            category_id=tx.category_id,
            amount_native=tx.amount_native,
            currency=tx.currency,
            cad_amount=tx.cad_amount,
            normalized_payee=tx.normalized_payee,
            payee_key=normalize_payee_key(tx.normalized_payee or ""),
        )
        for tx in rows
    ]


def match_rows(rows: Sequence[BillMatchRow], rule: BillMatchRule) -> list[BillMatchRow]:
    """The rows of one bill, in the order they were loaded (newest first)."""
    return [row for row in rows if row_matches(row, rule)]


def load_terms(db: Session, bill_ids: Sequence[int]) -> dict[int, list[str]]:
    """The terms of N bills in ONE query, in `position` order (M121 D1).

    A list of N bills must not cost N queries: the terms ride alongside the
    same scan that already feeds every row's `payment_count`.
    """
    if not bill_ids:
        return {}
    rows = db.exec(
        select(BillReminderMatchTerm)
        .where(BillReminderMatchTerm.bill_reminder_id.in_(list(dict.fromkeys(bill_ids))))
        .order_by(BillReminderMatchTerm.bill_reminder_id, BillReminderMatchTerm.position)
    ).all()
    out: dict[int, list[str]] = {}
    for row in rows:
        out.setdefault(row.bill_reminder_id, []).append(row.term)
    return out


def terms_of(bill: BillReminder, loaded: dict[int, list[str]] | None) -> list[str]:
    """This bill's term list from a `load_terms` result.

    FALLBACK: a bill with no term rows at all — every row that predates
    migration 0046, and any fixture that constructs `BillReminder(match_term=…)`
    directly — reads back as `[match_term]`, so the pre-M121 shape survives
    without a data fix-up (docs/25 §M121 D6).
    """
    if bill.id is None:
        return [bill.match_term]
    return (loaded or {}).get(bill.id) or [bill.match_term]


# --------------------------------------------------------------------------- #
# 3. Periods and history
# --------------------------------------------------------------------------- #


def bucket_unit(frequency: str) -> str:
    """Calendar unit a cadence's "period" is (docs/25 §M116 B2.3).

    `one_time` has no cadence at all, so it buckets by month — the least
    surprising unit for "the handful of times this happened".
    """
    if frequency == "quarterly":
        return "quarter"
    if frequency == "yearly":
        return "year"
    return "month"


def _bucket_index(d: date, unit: str) -> int:
    if unit == "quarter":
        return d.year * 4 + (d.month - 1) // 3
    if unit == "year":
        return d.year
    return d.year * 12 + (d.month - 1)


def _bucket_from_index(index: int, unit: str) -> str:
    """Bucket key = the period's FIRST DATE (the `GET /reports` bucket shape)."""
    if unit == "quarter":
        year, quarter = divmod(index, 4)
        return date(year, quarter * 3 + 1, 1).isoformat()
    if unit == "year":
        return date(index, 1, 1).isoformat()
    year, month = divmod(index, 12)
    return date(year, month + 1, 1).isoformat()


def bucket_of(d: date, frequency: str) -> str:
    unit = bucket_unit(frequency)
    return _bucket_from_index(_bucket_index(d, unit), unit)


def bucket_label(key: str, frequency: str) -> str:
    """Human label for a bucket key — `Sep 2026` / `Q3 2026` / `2026`."""
    parsed = date.fromisoformat(key)
    unit = bucket_unit(frequency)
    if unit == "quarter":
        return f"Q{(parsed.month - 1) // 3 + 1} {parsed.year}"
    if unit == "year":
        return str(parsed.year)
    return f"{_MONTH_NAMES[parsed.month - 1]} {parsed.year}"


@dataclass(frozen=True)
class ChartPoint:
    """One bar: its period, its matched total, and whether it is a miss."""

    period: str  # bucket key = first date of the period
    label: str
    total: Decimal
    count: int
    # M122 T3: mean of the trailing ≤6 period totals ENDING at this point
    # (missed periods count as $0.00, exactly like the rolling readout). The
    # LAST point's value is therefore identical to
    # `rolling_windows(chart)["6"].mean` — the line and the card's stat are
    # the same number by construction, not by coincidence.
    rolling_six: Decimal | None = None

    @property
    def missed(self) -> bool:
        return self.count == 0


def rolling_line(
    points: Sequence[ChartPoint], window: int = ROLLING_LINE_WINDOW
) -> tuple[Decimal | None, ...]:
    """One trailing-window mean per chart point (M122 T3).

    Point i's value is the mean of points [i-window+1 .. i] — the window
    TRUNCATES at the front (early points average what exists), and missed
    periods participate as `0.00` so the line dips exactly where the card's
    stat would. A chart with no points answers an empty tuple.
    """
    out: list[Decimal | None] = []
    for index in range(len(points)):
        chunk = points[max(0, index - window + 1) : index + 1]
        total = sum((point.total for point in chunk), ZERO)
        out.append((total / len(chunk)).quantize(MONEY))
    return tuple(out)


@dataclass(frozen=True)
class Chart:
    frequency: str
    unit: str
    points: tuple[ChartPoint, ...]

    @property
    def first_period(self) -> str | None:
        return self.points[0].period if self.points else None

    @property
    def last_period(self) -> str | None:
        return self.points[-1].period if self.points else None


def build_chart(
    rows: Sequence[BillMatchRow],
    frequency: str,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    max_periods: int = MAX_PERIODS,
) -> Chart:
    """One bar per calendar period, misses included as explicit zero bars.

    The span is the requested window when one is given (so an empty window
    still renders its bars and every one of them is honestly `missed`), and
    otherwise the bill's own first→last paid period. Gaps inside the span are
    never skipped — a skipped month is exactly the fact the chart exists to
    show.
    """
    unit = bucket_unit(frequency)
    totals: dict[str, tuple[Decimal, int]] = {}
    for row in rows:
        key = _bucket_from_index(_bucket_index(row.date, unit), unit)
        total, count = totals.get(key, (ZERO, 0))
        totals[key] = (total + row.bill_amount, count + 1)

    lo = _bucket_index(from_date, unit) if from_date is not None else None
    hi = _bucket_index(to_date, unit) if to_date is not None else None
    if lo is None and totals:
        lo = min(_bucket_index(date.fromisoformat(key), unit) for key in totals)
    if hi is None and totals:
        hi = max(_bucket_index(date.fromisoformat(key), unit) for key in totals)
    if lo is None or hi is None or lo > hi:
        return Chart(frequency=frequency, unit=unit, points=())
    if hi - lo + 1 > max_periods:
        lo = hi - max_periods + 1

    points = []
    for index in range(lo, hi + 1):
        key = _bucket_from_index(index, unit)
        total, count = totals.get(key, (ZERO, 0))
        points.append(
            ChartPoint(
                period=key,
                label=bucket_label(key, frequency),
                total=total.quantize(MONEY),
                count=count,
            )
        )
    # M122 T3: the rolling-average line is computed from the SAME quantized
    # point totals the bars and the rolling readout use.
    line = rolling_line(points)
    return Chart(
        frequency=frequency,
        unit=unit,
        points=tuple(
            replace(point, rolling_six=value) for point, value in zip(points, line, strict=True)
        ),
    )


@dataclass(frozen=True)
class RollingWindow:
    periods: int
    """How many periods the window ASKS for (`6` or `12`)."""

    window_periods: int
    """How many the chart actually has (≤ `periods`)."""

    payments: int
    # M122 T4/T2: the window's SUM (missed periods as `0.00`) — the same
    # population `mean` divides, so `total = mean x window_periods` exactly.
    total: Decimal | None
    mean: Decimal | None
    median: Decimal | None


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid].quantize(MONEY)
    return ((ordered[mid - 1] + ordered[mid]) / 2).quantize(MONEY)


def rolling_windows(chart: Chart) -> dict[str, RollingWindow]:
    """Mean + median + sum of the last N period totals, N ∈ {6, 12}.

    Missed periods count as `0.00` — a bill that was skipped for three months
    has a lower average than one that was paid, and averaging only the months
    that happened would hide exactly that.
    """
    out: dict[str, RollingWindow] = {}
    for size in ROLLING_WINDOWS:
        window = chart.points[-size:] if chart.points else ()
        if not window:
            out[str(size)] = RollingWindow(
                periods=size, window_periods=0, payments=0, total=None, mean=None, median=None
            )
            continue
        totals = [point.total for point in window]
        total = sum(totals, ZERO).quantize(MONEY)
        out[str(size)] = RollingWindow(
            periods=size,
            window_periods=len(window),
            payments=sum(point.count for point in window),
            total=total,
            mean=(total / len(totals)).quantize(MONEY),
            median=_median(totals),
        )
    return out


# --------------------------------------------------------------------------- #
# M122 T4 — all-time + YTD spend over the matched set
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AllTimeSpend:
    """The matched set summed over its whole life (docs/25 §M122 D4)."""

    total: Decimal
    payments: int
    average: Decimal | None
    """`total / payments` — mean per payment; `None` while nothing matched."""
    years: int
    """Distinct calendar years holding ≥ 1 payment (the honest divisor)."""
    yearly_average: Decimal | None
    """`total / years` — spend per YEAR-WITH-PAYMENTS, quantized to cents.

    Missed years are not years-with-payments, so they never inflate the
    divisor; a bill paid in 3 of 5 calendar years divides by 3.
    """


@dataclass(frozen=True)
class YtdSpend:
    """Calendar-year-to-date slice of the matched set (Jan 1 → `as_of`)."""

    year: int
    as_of: date
    total: Decimal
    payments: int


@dataclass(frozen=True)
class SpendStats:
    all_time: AllTimeSpend
    ytd: YtdSpend


def spend_stats(rows: Sequence[BillMatchRow], *, as_of: date | None = None) -> SpendStats:
    """All-time + YTD totals over ONE matched set (M122 T4).

    Only real payments count — a MISSED period is spend that never happened,
    so it never enters `all_time.total` (the rolling windows above keep their
    missed-$0.00 semantics; this is the other, deliberately different,
    population). Amounts are the same CAD magnitudes the chart totals use
    (`bill_row_amount`), so "all-time total" always equals the sum of the
    bars the chart would draw over the whole life.

    YTD is the CALENDAR year (Jan 1 → `as_of`, default `today_et()`) — the
    app's budget/calendar periods, never the bill's anniversary.
    """
    today = as_of or today_et()
    total = sum((bill_row_amount(row) for row in rows), ZERO).quantize(MONEY)
    payments = len(rows)
    years = len({row.date.year for row in rows})
    ytd_rows = [row for row in rows if date(today.year, 1, 1) <= row.date <= today]
    return SpendStats(
        all_time=AllTimeSpend(
            total=total,
            payments=payments,
            average=(total / payments).quantize(MONEY) if payments else None,
            years=years,
            yearly_average=(total / years).quantize(MONEY) if years else None,
        ),
        ytd=YtdSpend(
            year=today.year,
            as_of=today,
            total=sum((bill_row_amount(row) for row in ytd_rows), ZERO).quantize(MONEY),
            payments=len(ytd_rows),
        ),
    )


@dataclass(frozen=True)
class BillHistory:
    chart: Chart
    rolling: dict[str, RollingWindow]
    # Counted from the MATCHED ROWS, never summed off the chart: `max_periods`
    # can trim old bars off the front, and "how many payments did I find" must
    # not shrink because the picture got shorter.
    payment_count: int
    # M122 T4: all-time + YTD spend over the SAME matched rows — recomputed on
    # every read from the rule, never a cached snapshot (M91 anti-fabrication).
    spend: SpendStats


def build_history(
    rows: Sequence[BillMatchRow],
    frequency: str,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    as_of: date | None = None,
) -> BillHistory:
    """Chart + both rolling windows + the lifetime count + spend stats."""
    chart = build_chart(rows, frequency, from_date=from_date, to_date=to_date)
    return BillHistory(
        chart=chart,
        rolling=rolling_windows(chart),
        payment_count=len(rows),
        spend=spend_stats(rows, as_of=as_of),
    )

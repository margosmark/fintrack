"""Deterministic net-worth forecasting (M109, docs/45).

Two halves, deliberately separated:

* ``collect_inputs`` — the ONLY part that reads the database. It reuses the
  shipped report machinery instead of re-deriving it: baseline balances come
  from ``reports.net_worth_report`` (docs/12 §4) and the historical flow from
  ``reports.cashflow_report`` (docs/12 §3), so every number in the assumptions
  panel is the same number the Cash-flow and Net-worth tabs already show.
* ``run_forecast`` — a pure function. No clock, no randomness, no network, no
  LLM: same inputs ⇒ byte-identical output (test F3).

Money and rates are ``Decimal`` end-to-end (AGENTS §2), rounded through the
canonical ``app.tax.acb.cents`` (ROUND_HALF_UP, docs/45 §Numerics). Values are
quantized to cents ONLY when they are emitted, never during accumulation, so a
`day` run and a `week` run agree exactly on the dates they share.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.config import get_settings
from app.models.import_batches import ImportBatch
from app.models.transactions import RecurringCadence, Transaction
from app.schemas.forecast import (
    CareerBreakEvent,
    ForecastEvent,
    HousePurchaseEvent,
    OneOffCashEvent,
    RecurringCostDeltaEvent,
    RunInputs,
    RunParams,
    RunResponse,
    RunSeries,
    SalaryChangeEvent,
)
from app.services.recurring import (
    AVERAGE_MONTH_DAYS,
    merchant_key,
    monthly_total,
    next_expected_date,
)
from app.services.reports import bucket_end, bucket_starts, cashflow_report, net_worth_report
from app.services.txn_filters import committed_tx_conditions, transfer_exclusion_conditions
from app.tax.acb import cents

HUNDRED = Decimal("100")
TWELFTH = Decimal(1) / Decimal(12)
YEAR_DAYS = Decimal("365.25")
ZERO = Decimal("0")
_DISCLAIMER = "Projection, not a prediction."
# A runaway cadence would otherwise spin forever; no real cadence gets near it.
_MAX_OCCURRENCES = 5000


def money_str(value: Decimal) -> str:
    return str(cents(value))


# --------------------------------------------------------------------------
# date helpers — calendar months with the day clamped (services/recurring rule)
# --------------------------------------------------------------------------


def add_months(day: dt.date, months: int) -> dt.date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecurringStream:
    """One M96 recurring group as the projection sees it.

    Rows are grouped by ``merchant_key(payee) + cadence`` — exactly the
    ``make_group_key`` identity M96 tags with — because a tagged group stores
    SEVERAL rows: generating one projected stream per row would turn a single
    Netflix subscription into three.
    """

    cadence: RecurringCadence
    amount: Decimal  # mean occurrence, signed, CAD (M96's monthly_total mean)
    first_future: dt.date  # first occurrence strictly after `as_of`


@dataclass(frozen=True)
class ForecastInputs:
    cash: Decimal
    positions: Decimal
    history_from: dt.date
    history_to: dt.date
    income_monthly: Decimal
    expense_monthly: Decimal
    recurring_income_monthly: Decimal
    recurring_expense_monthly: Decimal
    residual_income_monthly: Decimal
    residual_expense_monthly: Decimal
    recurring_rows: int
    streams: tuple[RecurringStream, ...]


def collect_inputs(
    session: Session, user_id: int, as_of: dt.date, flow_window_months: int
) -> ForecastInputs:
    """Baseline balances + trailing-window flow + scheduled recurring streams."""
    history_from = add_months(as_of, -flow_window_months)

    # docs/12 §4 reused verbatim: one bucket ending exactly on `as_of`.
    net_worth = net_worth_report(session, user_id, as_of, as_of, "day")
    bucket = net_worth["buckets"][-1]
    cash = Decimal(bucket["cash_cad"])
    positions = Decimal(bucket["positions_cad"])

    # docs/12 §3 definitions reused verbatim: trade principal and
    # transfer_in/transfer_out are already excluded there, and unresolved-CAD
    # rows are excluded from both sides — so these figures equal the
    # Cash-flow tab's over the same window.
    flow = cashflow_report(session, user_id, history_from, as_of, "month")
    income_total = sum((Decimal(b["income_cad"]) for b in flow["buckets"]), ZERO)
    expense_total = sum((Decimal(b["expenses_cad"]) for b in flow["buckets"]), ZERO)
    window_months = Decimal((as_of - history_from).days) / AVERAGE_MONTH_DAYS
    if window_months <= 0:  # pragma: no cover — flow_window_months >= 1
        window_months = Decimal(1)
    income_monthly = income_total / window_months
    expense_monthly = expense_total / window_months

    rows = session.exec(
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(
            *committed_tx_conditions(user_id),
            # docs/12 §Shared population rules: a recurring TRANSFER (the
            # monthly TFSA move) is neither income nor expense, so it must not
            # appear as a recurring expense stream beside the ones the
            # Cash-flow tab now excludes.
            *transfer_exclusion_conditions(user_id),
            Transaction.is_recurring.is_(True),  # type: ignore[arg-type]
            Transaction.recurring_cadence.is_not(None),  # type: ignore[arg-type]
            Transaction.date >= history_from,
            Transaction.date <= as_of,
        )
        .order_by(Transaction.date, Transaction.id)  # type: ignore[attr-defined]
    ).all()

    grouped: dict[tuple[str, str], list[tuple[Decimal, dt.date, dt.date | None]]] = defaultdict(
        list
    )
    for row in rows:
        if row.cad_amount is None:  # unresolved CAD — the docs/12 §3 exclusion
            continue
        cadence = row.recurring_cadence
        if cadence is None:  # pragma: no cover — guarded by the WHERE clause
            continue
        merchant = merchant_key(row.normalized_payee or row.raw_description or "")
        if not merchant:
            continue
        grouped[(merchant, cadence.value)].append(
            (Decimal(str(row.cad_amount)), row.date, row.recurring_next_date)
        )

    recurring_income_monthly = ZERO
    recurring_expense_monthly = ZERO
    streams: list[RecurringStream] = []
    row_count = 0
    for (_merchant, cadence_value), group in sorted(grouped.items()):
        cadence = RecurringCadence(cadence_value)
        amounts = [amount for amount, _, _ in group]
        row_count += len(amounts)
        monthly = monthly_total(amounts, cadence)  # M96's own normalization
        if monthly > 0:
            recurring_income_monthly += monthly
        else:
            recurring_expense_monthly += -monthly
        last_date = max(d for _, d, _ in group)
        next_dates = [n for _, _, n in group if n is not None]
        anchor = max(next_dates) if next_dates else next_expected_date(last_date, cadence)
        while anchor <= as_of:
            anchor = next_expected_date(anchor, cadence)
        streams.append(RecurringStream(cadence=cadence, amount=amounts[-1], first_future=anchor))

    return ForecastInputs(
        cash=cash,
        positions=positions,
        history_from=history_from,
        history_to=as_of,
        # Published figures are quantized FIRST and the residual is derived
        # from the quantized pair, so the assumptions panel's own identity —
        # `income = recurring + residual` — holds to the cent (docs/45 §1.2).
        income_monthly=cents(income_monthly),
        expense_monthly=cents(expense_monthly),
        recurring_income_monthly=cents(recurring_income_monthly),
        recurring_expense_monthly=cents(recurring_expense_monthly),
        # NOT clamped: if recurring rows over-explain a short window the
        # residual goes negative, and the user SEES that rather than a quietly
        # adjusted figure.
        residual_income_monthly=cents(income_monthly) - cents(recurring_income_monthly),
        residual_expense_monthly=cents(expense_monthly) - cents(recurring_expense_monthly),
        recurring_rows=row_count,
        streams=tuple(streams),
    )


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------


@dataclass
class _State:
    cash: Decimal
    positions: Decimal
    assets: Decimal = ZERO
    liabilities: Decimal = ZERO

    @property
    def net(self) -> Decimal:
        return self.cash + self.positions + self.assets - self.liabilities


def _monthly_growth(annual_return_pct: Decimal) -> Decimal:
    """Compound monthly rate. Short-circuits to an exact zero so the DEFAULT
    run performs no fractional power at all — same math as docs/12 §7."""
    if annual_return_pct == 0:
        return ZERO
    return (Decimal(1) + annual_return_pct / HUNDRED) ** TWELFTH - Decimal(1)


def mortgage_payment(principal: Decimal, annual_rate_pct: Decimal, years: int) -> Decimal:
    """Standard amortization ``L·i / (1 - (1+i)^-n)`` with ``i = annual/12``.

    A FLAT monthly rate by construction — no Canadian semi-annual compounding,
    no stress test. The assumptions panel discloses exactly this (docs/45 §1.5).
    """
    periods = years * 12
    monthly = annual_rate_pct / HUNDRED / Decimal(12)
    if monthly == 0:
        return cents(principal / Decimal(periods))
    payment = principal * monthly / (Decimal(1) - (Decimal(1) + monthly) ** Decimal(-periods))
    return cents(payment)


def _occurrences(start: dt.date, last: dt.date, cadence: RecurringCadence) -> list[dt.date]:
    out: list[dt.date] = []
    cursor = start
    while cursor <= last and len(out) < _MAX_OCCURRENCES:
        out.append(cursor)
        cursor = next_expected_date(cursor, cadence)
    return out


def _mortgage_schedule(
    principal: Decimal, payment: Decimal, annual_rate_pct: Decimal, start: dt.date, last: dt.date
) -> list[tuple[dt.date, Decimal, Decimal]]:
    """``(date, cash_out, principal_part)`` through the horizon.

    Precomputed rather than closed over a mutable balance, so the schedule is
    decided BEFORE any effect runs and the result cannot depend on effect
    ordering. Interest and payment are cents-quantized per period, exactly as
    a real amortization schedule is.
    """
    monthly = annual_rate_pct / HUNDRED / Decimal(12)
    balance = principal
    schedule: list[tuple[dt.date, Decimal, Decimal]] = []
    k = 1
    while balance > 0 and len(schedule) < _MAX_OCCURRENCES:
        date = add_months(start, k)
        if date > last:
            break
        interest = cents(balance * monthly)
        if payment >= balance + interest:
            cash_out = balance + interest
            principal_part = balance
            balance = ZERO
        else:
            cash_out = payment
            principal_part = payment - interest
            balance -= principal_part
        schedule.append((date, cash_out, principal_part))
        k += 1
    return schedule


def _add_events(
    events: list[ForecastEvent],
    state: _State,
    add: Callable[[dt.date, Callable[[], None]], None],
    broken: Callable[[dt.date], bool],
    last: dt.date,
) -> None:
    for event in events:
        if isinstance(event, HousePurchaseEvent):
            principal = event.home_price_cad - event.down_payment_cad
            add(
                event.date,
                lambda s=state, e=event: _buy(s, e),
            )
            payment = mortgage_payment(principal, event.mortgage_rate_pct, event.amortization_years)
            for date, cash_out, principal_part in _mortgage_schedule(
                principal, payment, event.mortgage_rate_pct, event.date, last
            ):
                add(
                    date,
                    lambda s=state, c=cash_out, p=principal_part: _pay_mortgage(s, c, p),
                )

        elif isinstance(event, CareerBreakEvent):
            # A modifier, not an effect: `_add_events` never touches income
            # here — `broken()` is consulted by the anchor and income effects.
            continue

        elif isinstance(event, SalaryChangeEvent):
            cursor = event.date
            while cursor <= last:
                if not broken(cursor):  # a break suppresses income, not pay cuts after it
                    add(
                        cursor,
                        lambda s=state, d=event.monthly_delta_cad: setattr(s, "cash", s.cash + d),
                    )
                cursor = add_months(cursor, 1)

        elif isinstance(event, OneOffCashEvent):
            add(
                event.date,
                lambda s=state, d=event.amount_cad: setattr(s, "cash", s.cash + d),
            )

        elif isinstance(event, RecurringCostDeltaEvent):
            stop = min(event.end_date, last) if event.end_date is not None else last
            cursor = event.start_date
            while cursor <= stop:
                # Costs are never suppressed by a career break — you still eat.
                add(
                    cursor,
                    lambda s=state, d=event.monthly_amount_cad: setattr(s, "cash", s.cash + d),
                )
                cursor = add_months(cursor, 1)


def _buy(state: _State, event: HousePurchaseEvent) -> None:
    """Cash becomes equity + a mortgage: net worth does NOT move on day one."""
    state.cash -= event.down_payment_cad
    state.assets += event.home_price_cad
    state.liabilities += event.home_price_cad - event.down_payment_cad


def _pay_mortgage(state: _State, cash_out: Decimal, principal_part: Decimal) -> None:
    state.cash -= cash_out
    state.liabilities -= principal_part
    # net effect = -interest, the only part that is actually spent.


def run_forecast(
    as_of: dt.date,
    inputs: ForecastInputs,
    params: RunParams,
    scenarios: list[tuple[str, str, list[ForecastEvent]]],
) -> RunResponse:
    """Project the baseline plus every scenario over the same horizon.

    `scenarios` is `[(id, name, events), …]`; the baseline is always rendered
    first and never comes from a caller, so it cannot be shadowed.
    """
    horizon_end = add_months(as_of, params.horizon_months)
    # Granularity changes SAMPLING only; state evolution is granularity-free.
    starts = bucket_starts(as_of, horizon_end, params.granularity)
    buckets = [min(bucket_end(s, params.granularity), horizon_end) for s in starts]

    growth = _monthly_growth(params.annual_return_pct)
    inflation = params.inflation_pct
    # The deflator depends on the bucket alone, so it is computed once per
    # bucket and shared by every series — and `inflation 0` short-circuits to
    # an exact 1 so `real_values == values` byte-for-byte (test F9).
    deflators = [
        Decimal(1)
        if inflation == 0
        else (Decimal(1) + inflation / HUNDRED) ** (Decimal((d - as_of).days) / YEAR_DAYS)
        for d in buckets
    ]

    # The baseline is rendered FIRST and never comes from a caller, so it
    # cannot be shadowed by an id the client picked.
    base_values, base_real, base_final = _project(
        as_of, buckets, deflators, inputs, [], growth, params.horizon_months
    )
    baseline = RunSeries(
        id="baseline",
        name="Baseline",
        values=base_values,
        real_values=base_real,
        final_cad=money_str(base_final),
        delta_cad="0.00",
    )
    scenario_series: list[RunSeries] = []
    for sid, name, events in scenarios:
        values, real_values, final = _project(
            as_of, buckets, deflators, inputs, events, growth, params.horizon_months
        )
        scenario_series.append(
            RunSeries(
                id=sid,
                name=name,
                values=values,
                real_values=real_values,
                final_cad=money_str(final),
                delta_cad=money_str(final - base_final),
            )
        )

    return RunResponse(
        as_of=str(as_of),
        horizon_months=params.horizon_months,
        granularity=params.granularity,
        inflation_pct=str(cents(inflation)),
        annual_return_pct=str(cents(params.annual_return_pct)),
        flow_window_months=params.flow_window_months,
        reporting_currency=get_settings().reporting_currency,
        buckets=[str(d) for d in buckets],
        inputs=RunInputs(
            cash_cad=money_str(inputs.cash),
            positions_cad=money_str(inputs.positions),
            net_worth_cad=money_str(inputs.cash + inputs.positions),
            history_from=str(inputs.history_from),
            history_to=str(inputs.history_to),
            income_monthly_cad=money_str(inputs.income_monthly),
            expense_monthly_cad=money_str(inputs.expense_monthly),
            recurring_income_monthly_cad=money_str(inputs.recurring_income_monthly),
            recurring_expense_monthly_cad=money_str(inputs.recurring_expense_monthly),
            residual_income_monthly_cad=money_str(inputs.residual_income_monthly),
            residual_expense_monthly_cad=money_str(inputs.residual_expense_monthly),
            recurring_rows=inputs.recurring_rows,
        ),
        baseline=baseline,
        scenarios=scenario_series,
        disclaimer=_DISCLAIMER,
    )


def _project(
    as_of: dt.date,
    buckets: list[dt.date],
    deflators: list[Decimal],
    inputs: ForecastInputs,
    events: list[ForecastEvent],
    growth: Decimal,
    horizon_months: int,
) -> tuple[list[str], list[str], Decimal]:
    """One full pass: build every effect, walk the buckets in date order, emit."""
    state = _State(cash=inputs.cash, positions=inputs.positions)
    breaks = [(e.start_date, e.end_date) for e in events if isinstance(e, CareerBreakEvent)]

    def broken(day: dt.date) -> bool:
        # INCLUSIVE of end_date: "no income from A to B" means B too (docs/45).
        return any(start <= day <= end for start, end in breaks)

    effects: list[tuple[dt.date, int, Callable[[], None]]] = []
    seq = 0

    def add(day: dt.date, fn: Callable[[], None]) -> None:
        nonlocal seq
        if day <= buckets[-1]:
            effects.append((day, seq, fn))
            seq += 1

    # 1. Baseline monthly anchors — growth FIRST, then the residual flow: the
    #    order docs/45 §1.4 pins and docs/12 §7 already uses.
    residual_income = inputs.residual_income_monthly
    residual_expense = inputs.residual_expense_monthly
    for k in range(1, horizon_months + 1):
        anchor = add_months(as_of, k)

        def _anchor(
            s: _State = state,
            day: dt.date = anchor,
            inc: Decimal = residual_income,
            exp: Decimal = residual_expense,
        ) -> None:
            if growth != 0:
                s.positions *= Decimal(1) + growth
            if not broken(day):  # a career break suppresses income only
                s.cash += inc
            s.cash -= exp

        add(anchor, _anchor)

    # 2. Recurring streams on their own cadence dates (the double-count rule:
    #    these replace, not add to, the residual above).
    for stream in inputs.streams:
        is_income = stream.amount > 0
        for occurrence in _occurrences(stream.first_future, buckets[-1], stream.cadence):
            if is_income and broken(occurrence):
                continue
            add(
                occurrence,
                lambda s=state, amount=stream.amount: setattr(s, "cash", s.cash + amount),
            )

    # 3. Life events (a CareerBreakEvent is a modifier, handled by `broken`).
    _add_events(events, state, add, broken, buckets[-1])

    effects.sort(key=lambda item: (item[0], item[1]))

    values: list[str] = []
    real_values: list[str] = []
    cursor = 0
    for bucket_date, deflator in zip(buckets, deflators, strict=True):
        while cursor < len(effects) and effects[cursor][0] <= bucket_date:
            effects[cursor][2]()
            cursor += 1
        net = state.net
        values.append(money_str(net))
        real_values.append(money_str(net / deflator))
    return values, real_values, state.net

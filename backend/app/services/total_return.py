"""Total-return metrics for the Investments page (M77, docs/05 §Total return).

Two returns over one window, both **period** (not annualized) so they line up
with the benchmark comparison's own period return:

* **TWR (time-weighted)** — daily chain-linked: ``r_d = (V_d - CF_d) / V_{d-1}``
  with flows treated as occurring at the END of their date, so external money
  cannot inflate the result. The product is carried as an exact integer ratio
  (cents numerator / cents denominator) and rounded ONCE to 8 decimal places.
* **MWR (money-weighted, Modified Dietz)** — one exact rational over the same
  window: ``r = (V_end - V_start - CF) / (V_start + sum(w_i * CF_i))`` with
  ``w_i = days_remaining_i / window_days``.

Portfolio value ``V(d) = positions(d) + cash(d)`` where positions are valued at
the newest cached price <= d (manual wins, earliest-price fallback, docs/12)
and cash is the account ledger **plus trade-generated cash**: this schema keeps
account balances on the `transactions` table only, so dividends, commissions,
buys and sells would otherwise never move cash and the value would jump on
every ex-dividend date. Trade cash effects are therefore derived here:

========================  ==================================================
action                    cash effect (the ROW's currency, M132)
========================  ==================================================
``dividend|distribution`` ``+price_native``
``return_of_capital``     ``+price_native``
``contribution``          ``+price_native``
``withdrawal|fee``        ``-abs(price_native)`` (always money out — the row
                          stores the broker's native sign, negative; M131)
``buy|drip``              ``-(|quantity| * price_native) - commission``
``sell``                  ``+(|quantity| * price_native) - commission``
``transfer_in``           ``+|price_native|`` when ``quantity == 0`` (cash row,
                          docs/02), ``0`` when it moves shares (docs/06)
``transfer_out``          ``-|price_native|`` when ``quantity == 0``, ``0`` for shares
``corporate_action_…``    ``0``
========================  ==================================================

That makes a cash dividend neutral (value drops with the price, cash rises) and
a DRIP neutral (the reinvested shares give back what the price gave up), which
is exactly what "total return including reinvested dividends" has to mean.

**Multi-currency (M132, docs/05 §Portfolio value).** Every effect in the table
is stated in the ROW's own currency (``trades.currency``; a row with none reads
as its account's, docs/02). Two different conversions then apply, because cash
and flows are different questions:

* trade cash is a **balance** — it accumulates per currency and converts at
  the day it is *valued* (``d`` in ``V(d)``), the same day positions and
  starting balances convert. Valuing it at each row's own trade date instead
  would price a balance at a basket of historical rates: real account 731's
  +6,124.38 and -6,124.18 (both USD) net to +0.20 USD, and per-row-date
  conversion turned that near-zero balance into -147 CAD of rate drift;
* external flows and income are **events** — they convert at the *trade date*
  (and at the row's currency: a USD dividend in a CAD account is USD income).

Both are cache-only ``FxCache`` reads (docs/12): a missing rate excludes the
figure, never a fetch.

**External flows** (money crossing the portfolio boundary) are
``contribution``/``withdrawal``/``transfer_in``/``transfer_out`` plus every
committed transaction on an investment account. Everything else — buys, sells,
drips, dividends, fees, commissions — is internal and stays inside the return.

Scope: accounts whose type is in ``INVESTMENT_ACCOUNT_TYPES`` (docs/02).
Reads are cache-only: no path here ever fetches (docs/12).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.config import get_settings
from app.models.accounts import INVESTMENT_ACCOUNT_TYPES, Account
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.models.transactions import Transaction
from app.services.reports import _ZERO, FxCache, PriceCache, _cents
from app.services.trade_filters import committed_trade_conditions

_PCT_FRACTION_DIGITS = 8
_PCT_SCALE = 10**_PCT_FRACTION_DIGITS

# Cash actions that move money ACROSS the portfolio boundary (signed by
# direction). transfer_in/transfer_out are included on purpose: the demo seed
# funds margin accounts with `transfer_in` cash rows, and docs/06 treats a
# quantity-carrying transfer as an in-kind acquisition — both are value that
# arrived from outside the portfolio.
_FLOW_DIRECTION: dict[TradeAction, int] = {
    TradeAction.contribution: 1,
    TradeAction.transfer_in: 1,
    TradeAction.withdrawal: -1,
    TradeAction.transfer_out: -1,
}
_INCOME_ACTIONS = (
    TradeAction.dividend,
    TradeAction.distribution,
    TradeAction.return_of_capital,
)


def ratio_pct(num: int, den: int) -> str | None:
    """``(num / den) * 100`` as a fixed 8-decimal-place string, HALF_UP.

    One rounding, done on integers: ``num * 100 * 10**8 / den`` with a
    half-up tie break, then explicit integer/fraction formatting. The fixed
    format is deliberate — a ``Decimal`` below 1e-6 would serialize in
    scientific notation (``1E-8``), and the contract pins a plain
    ``0.00000001``. The client mirror (`lib/totalReturnEngine.ts`) runs the
    same integer division and the same format, so both sides emit
    byte-identical strings. Returns ``None`` when ``den`` is zero.
    """
    if den == 0:
        return None
    sign = 1
    if den < 0:
        num, den = -num, -den
    if num < 0:
        sign, num = -1, -num
    scaled, remainder = divmod(num * 100 * _PCT_SCALE, den)
    if remainder * 2 >= den:
        scaled += 1
    whole, frac = divmod(scaled, _PCT_SCALE)
    return f"{'-' if sign < 0 else ''}{whole}.{frac:0{_PCT_FRACTION_DIGITS}d}"


def _as_cents(value: Decimal) -> int:
    """Exact integer cents from a 2dp Decimal (every money value here is)."""
    return int(value * 100)


def _trade_cash_effect(trade: Trade) -> Decimal:
    """Signed native-cash change this trade causes inside its account.

    The result is stated in the ROW's own currency (``trades.currency``,
    M132) — a USD buy inside a CAD account returns USD here. Consumers
    convert it; see docs/05 §Portfolio value for which day each conversion
    uses.
    """
    action = trade.action
    commission = trade.commission or _ZERO
    if action in (TradeAction.buy, TradeAction.drip):
        return -(abs(trade.quantity) * trade.price_native) - commission
    if action == TradeAction.sell:
        return abs(trade.quantity) * trade.price_native - commission
    if action in (
        TradeAction.dividend,
        TradeAction.distribution,
        TradeAction.return_of_capital,
        TradeAction.contribution,
    ):
        return trade.price_native
    if action in (TradeAction.fee, TradeAction.withdrawal):
        # ALWAYS money out (M131, docs/05). The STORED sign is the broker's
        # native sign — a fee/withdrawal row is imported negative — so the old
        # `-price_native` would turn a correctly-signed row into a credit, and
        # `-abs` is the only direction that holds for both the repaired rows
        # and any legacy row stored positive.
        return -abs(trade.price_native)
    if action in _FLOW_DIRECTION:
        # quantity == 0 -> the row IS cash (docs/02 input convention);
        # quantity != 0 -> shares moved, no cash changed (docs/06).
        # The MAGNITUDE goes through abs() and the DIRECTION comes from the
        # table, exactly like `_trade_external_flow` — otherwise a
        # `transfer_out` would add cash while being reported as money leaving,
        # and V_end and net_flow would disagree (breaking MWR).
        if trade.quantity == 0:
            return abs(trade.price_native) * _FLOW_DIRECTION[action]
        return _ZERO
    return _ZERO  # corporate_action_adjustment: no cash


def _trade_external_flow(trade: Trade) -> Decimal:
    """Signed native value crossing the portfolio boundary, or ``_ZERO``."""
    direction = _FLOW_DIRECTION.get(trade.action)
    if direction is None:
        return _ZERO
    magnitude = (
        abs(trade.quantity) * trade.price_native if trade.quantity != 0 else abs(trade.price_native)
    )
    return magnitude * direction


def _trade_currency(trade: Trade, account_currency: str) -> str:
    """The currency a row's own amounts are STATED in (M132, docs/02 §trades).

    ``trades.currency`` is the broker's per-row currency, captured at import
    (or by the M132 repair). A row written before M132 has none and reads in
    the ACCOUNT's currency — exactly what every consumer did before the
    column existed, never a fabricated code.
    """
    return trade.currency or account_currency


def _committed_transactions_upto(
    session: Session, user_id: int, account_ids: set[int], upto: dt.date
) -> list[Transaction]:
    stmt = (
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.account_id.in_(account_ids),
            (ImportBatch.status == ImportBatchStatus.committed)
            | (Transaction.import_batch_id.is_(None)),
            Transaction.date <= upto,
        )
        .order_by(Transaction.date, Transaction.id)
    )
    return list(session.exec(stmt).all())


def total_return_report(
    session: Session, user_id: int, from_: dt.date, to: dt.date
) -> dict[str, object]:
    """TWR + MWR over ``[from_, to]`` (docs/05 §Total return)."""
    reporting = get_settings().reporting_currency
    fx = FxCache(session, reporting)
    prices = PriceCache(session, user_id)

    accounts = [
        account
        for account in session.exec(select(Account).where(Account.user_id == user_id)).all()
        if account.type in INVESTMENT_ACCOUNT_TYPES
    ]
    account_ids = {account.id for account in accounts}
    currency_of = {account.id: account.currency for account in accounts}

    trades: list[Trade] = (
        list(
            session.exec(
                select(Trade)
                .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
                .where(
                    *committed_trade_conditions(user_id),
                    Trade.account_id.in_(account_ids),
                    Trade.date <= to,
                )
                .order_by(Trade.date, Trade.id)  # type: ignore[attr-defined]
            ).all()
        )
        if account_ids
        else []
    )
    transactions = (
        _committed_transactions_upto(session, user_id, account_ids, to) if account_ids else []
    )
    security_ids = {trade.security_id for trade in trades}
    listing = (
        {
            security.id: security.listing_currency
            for security in session.exec(
                select(Security).where(Security.id.in_(security_ids))
            ).all()
        }
        if security_ids
        else {}
    )

    def rate_for(currency: str, day: dt.date) -> Decimal | None:
        return fx.rate(currency, reporting, day)

    # --- external flows per day (window only) --------------------------------
    flows: dict[dt.date, Decimal] = {}
    income_total = _ZERO
    for txn in transactions:
        if txn.cad_amount is None or not (from_ <= txn.date <= to):
            continue
        flows[txn.date] = flows.get(txn.date, _ZERO) + txn.cad_amount
    for trade in trades:
        # FLOWS and INCOME are events, so they convert at the TRADE DATE — but
        # at the ROW's currency (M132), not the account's: a USD dividend in a
        # CAD account is USD income, and converting it at a CAD rate face-values
        # it into the CAD total.
        currency = _trade_currency(trade, currency_of.get(trade.account_id, reporting))
        rate = rate_for(currency, trade.date)
        flow = _trade_external_flow(trade)
        if flow != _ZERO and rate is not None and from_ <= trade.date <= to:
            flows[trade.date] = flows.get(trade.date, _ZERO) + _cents(flow * rate)
        if trade.action in _INCOME_ACTIONS and from_ <= trade.date <= to and rate is not None:
            income_total += _cents(trade.price_native * rate)

    # --- replay history in date order up to the opening day -----------------
    positions: dict[int, Decimal] = {}
    txn_cad = _ZERO
    # M132 (docs/05 §Portfolio value): trade cash is a BALANCE, kept per ROW
    # currency and converted at the day it is VALUED — never face-summed into
    # one number and never converted once at the account's rate. A USD buy in
    # a CAD account is USD cash, and valuing a balance at a basket of the
    # rates its rows happened on is a different (wrong) question: real account
    # 731's +6,124.38 and -6,124.18 (both USD) net to +0.20 USD, and
    # per-row-date conversion turned that near-zero balance into -147 CAD of
    # pure rate drift.
    trade_cash: dict[str, Decimal] = {}
    account_currency = {account.id: account.currency for account in accounts}

    def apply_transaction(txn: Transaction) -> None:
        nonlocal txn_cad
        if txn.cad_amount is not None:
            txn_cad += txn.cad_amount

    def apply_trade(trade: Trade) -> None:
        effect = _trade_cash_effect(trade)
        if effect != _ZERO:
            currency = _trade_currency(trade, account_currency.get(trade.account_id, reporting))
            trade_cash[currency] = trade_cash.get(currency, _ZERO) + _cents(effect)
        if trade.action == TradeAction.corporate_action_adjustment:
            if trade.quantity != 0:
                positions[trade.security_id] = trade.quantity
        else:
            positions[trade.security_id] = positions.get(trade.security_id, _ZERO) + (
                trade.quantity
            )

    def value_on(day: dt.date) -> Decimal:
        cash = txn_cad
        for currency, amount in trade_cash.items():
            rate = rate_for(currency, day)
            if rate is not None:
                cash += _cents(amount * rate)
        for account in accounts:
            rate = rate_for(account.currency, day)
            if rate is None:
                continue
            cash += _cents((account.starting_balance or _ZERO) * rate)
        positions_cad = _ZERO
        for security_id, quantity in positions.items():
            if quantity == 0:
                continue
            price = prices.price(security_id, day)
            rate = rate_for(listing.get(security_id, reporting), day)
            if price is None or rate is None:
                continue  # unresolved price/fx excluded, never fetched (docs/12)
            positions_cad += quantity * price * rate
        return _cents(cash + positions_cad)

    # (stream rank, date, id, row) — date leads so the replay is chronological;
    # on ONE date TRADES apply before the transaction ledger (rank 0 first —
    # note "tr" also sorts before "tx", so the old tag sort agreed by luck);
    # the row id gives a stable order inside a stream. `lib/totalReturnEngine.ts`
    # must use the identical (date, rank, id) order or the two implementations
    # diverge on `corporate_action_adjustment`, which ASSIGNS rather than adds.
    merged: list[tuple[int, dt.date, int, object]] = sorted(
        [(1, txn.date, txn.id or 0, txn) for txn in transactions]
        + [(0, trade.date, trade.id or 0, trade) for trade in trades],
        key=lambda item: (item[1], item[0], item[2]),
    )

    open_day = from_ - dt.timedelta(days=1)
    cursor = 0
    while cursor < len(merged) and merged[cursor][1] <= open_day:
        row = merged[cursor][3]
        if merged[cursor][0] == 1:
            apply_transaction(row)  # type: ignore[arg-type]
        else:
            apply_trade(row)  # type: ignore[arg-type]
        cursor += 1
    start_value = value_on(open_day)

    # --- daily walk over the window -----------------------------------------
    twr_num = 1
    twr_den = 1
    active_days = 0
    previous = start_value
    day = from_
    one_day = dt.timedelta(days=1)
    while day <= to:
        while cursor < len(merged) and merged[cursor][1] <= day:
            if merged[cursor][0] == 1:
                apply_transaction(merged[cursor][3])  # type: ignore[arg-type]
            else:
                apply_trade(merged[cursor][3])  # type: ignore[arg-type]
            cursor += 1
        current = value_on(day)
        today_flow = flows.get(day, _ZERO)
        if previous > _ZERO:
            active_days += 1
            numerator = current - today_flow
            if numerator != previous:
                twr_num *= _as_cents(numerator)
                twr_den *= _as_cents(previous)
        previous = current
        day += one_day

    end_value = previous
    window_days = (to - from_).days
    flow_total = sum(flows.values(), _ZERO)
    flow_days = sum(1 for amount in flows.values() if amount != _ZERO)

    twr_pct = ratio_pct(twr_num - twr_den, twr_den) if active_days > 0 else None

    mwr_pct: str | None = None
    if window_days > 0:
        delta = _as_cents(end_value - start_value - flow_total)
        weighted = _as_cents(start_value) * window_days
        for flow_day, amount in flows.items():
            if amount != _ZERO:
                weighted += _as_cents(amount) * (to - flow_day).days
        if weighted > 0:
            mwr_pct = ratio_pct(delta * window_days, weighted)

    return {
        "from_": from_,
        "to": to,
        "status": "ok" if active_days > 0 else "no_data",
        "start_value_cad": start_value,
        "end_value_cad": end_value,
        "net_flow_cad": _cents(flow_total),
        "income_cad": _cents(income_total),
        "twr_pct": twr_pct,
        "mwr_pct": mwr_pct,
        "valuation_days": (to - from_).days + 1,
        "active_days": active_days,
        "flow_days": flow_days,
        "account_count": len(accounts),
    }

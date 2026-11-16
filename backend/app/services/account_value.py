"""Per-account value history (M118, docs/08 §Account value history).

One account's ``V(bucket_end)``, bucketed with docs/12 §Time bucketing — the
SAME ``bucket_starts``/``bucket_end``/``FxCache``/``PriceCache`` the reports
family uses, imported rather than reimplemented — and valued with docs/05
§Portfolio value's chain::

    V(end)   = cash(end) + positions(end)
    cash     = (starting_balance + Σ committed transactions
                + Σ trade-generated cash) x fx(account → reporting, end)
    positions = Σ signed_qty x newest price ≤ end x fx(listing → reporting, end)

**Why this is not ``net_worth_report`` with an ``account_id`` filter.** docs/12
§4 pins net-worth ``cash_cad`` to ``starting_balance + Σ transactions`` ONLY,
and ``lib/reportEngine.ts::netWorthReport`` has to match that to the string, so
the net-worth chain must not change. It is also the wrong chain HERE: this
schema keeps account balances on ``transactions`` only, so an investment
account's dollars live on ``trades`` (real account 731 has ZERO transactions —
every contribution, fee and buy is a trade row). A transactions-only cash line
would draw a false ``0`` between a contribution and the buy that spends it.
Trade cash therefore enters through the ONE table docs/05 pins
(``total_return._trade_cash_effect``) — imported, never copied.

Bucketing, the committed-only predicates, the price/fx caches and the position
replay are all shared with the reports family; the ONLY new arithmetic is this
per-account walk. Honesty rules below are docs/08 §Honesty; reads are
cache-only — no path here ever fetches (docs/12 Principles).

Money rounding is the canonical `app.tax.acb.cents` (ROUND_HALF_UP, docs/12 §money),
imported under the historical `_cents` name — the ONE rounding helper app-wide
(AGENTS §2). `_ZERO`, `_apply_trade_to_position` and `_committed_transactions`
are still private reports helpers; they are shared with `services.total_return`
so they stay put (renaming them is a wider change than this one).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from typing import Literal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.config import get_settings
from app.core.errors import api_error
from app.models.accounts import INVESTMENT_ACCOUNT_TYPES, Account
from app.models.categories import Category
from app.models.import_batches import ImportBatch
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.services.reports import (
    _ZERO,
    FxCache,
    Granularity,
    PriceCache,
    _apply_trade_to_position,
    _committed_transactions,
    bucket_end,
    bucket_key,
    bucket_starts,
)
from app.services.total_return import _trade_cash_effect, _trade_currency
from app.services.trade_filters import committed_trade_conditions
from app.tax.acb import cents as _cents

# The chart's own roofline (docs/08): a detail chart never renders more than
# this many points, so the endpoint coarsens the granularity instead.
MAX_POINTS = 400
# House staleness rule (docs/07 / M115): a price or fx row carried forward by
# MORE than this is beyond the accepted carry-forward window. A price that old
# makes the bucket `estimated`; an fx rate that old makes the bucket unvalued —
# there is no honest CAD figure to draw.
STALE_DAYS = 7
# docs/08: the significance floor, in reporting currency, when the caller does
# not pass `min_event_value`.
DEFAULT_MIN_EVENT_VALUE = Decimal("1000")

#: WHY a bucket has no valuation (M144, docs/08 §Honesty — the two-reason
#: empty state). `valued: false` used to be one fact with one sentence, and
#: that sentence said "no valuation for this period" even when nothing was
#: wrong with the data — the account simply held nothing yet. The two reasons
#: are different facts and the UI must be able to say which one it has.
#:
#: * `no_price` — a held position has no cached close on or before this day
#:   (a DATA gap: the deep backfill or a manual price closes it);
#: * `fx` — a rate this bucket needs is missing or past docs/07's ≤7d
#:   carry-forward window (also a DATA gap);
#: * `not_held` — the account held NO cash and NO position on this day, and
#:   had held nothing earlier in the window either: a HOLDINGS gap, not a
#:   hole in the cache.
GapReason = Literal["no_price", "fx", "not_held"]

_COARSEN: dict[str, Granularity] = {"day": "week", "week": "month", "month": "year"}

# The significant-action set (docs/08 §Event markers): money crossing the
# account boundary. Buys/sells/dividends/fees are INTERNAL — they move value
# between cash and holdings without moving money in or out, so marking them
# would annotate every trading day.
_FLOW_ACTIONS = frozenset(
    {
        TradeAction.contribution,
        TradeAction.withdrawal,
        TradeAction.transfer_in,
        TradeAction.transfer_out,
    }
)


def resolve_granularity(from_: dt.date, to: dt.date, requested: Granularity) -> Granularity:
    """The finest granularity that stays inside ``MAX_POINTS`` buckets.

    Coarsening only ever moves one way (day → week → month → year), so a caller
    asking for `day` over two years gets weeks, not a 730-point chart. The
    response echoes both the ask and the answer (docs/08).
    """
    granularity: Granularity = requested
    while len(bucket_starts(from_, to, granularity)) > MAX_POINTS:
        nxt = _COARSEN.get(granularity)
        if nxt is None:  # pragma: no cover — `year` has no coarser bucket
            break
        granularity = nxt
    return granularity


def _event_value(trade: Trade) -> Decimal:
    """Signed native value this flow moved ACROSS the account boundary.

    Cash rows are docs/05's cash effect verbatim (``_trade_cash_effect``); an
    in-kind ``transfer_in``/``transfer_out`` moves NO cash but moves a lot of
    VALUE, and it is exactly the "value arrived" jump the chart has to explain,
    so it counts at ``|quantity| x price_native`` with the flow's direction.
    """
    effect = _trade_cash_effect(trade)
    if effect != _ZERO:
        return effect
    if trade.quantity != 0:
        direction = 1 if trade.action == TradeAction.transfer_in else -1
        return abs(trade.quantity * trade.price_native) * direction
    return _ZERO


def _flow_kind(trade: Trade, value: Decimal) -> str:
    """Marker label from the ACTION (M131); value only covers legacy rows.

    A `withdrawal` row IS money leaving — real account 731's 2025-12-19 row
    used to arrive as a `contribution` of -24318.76 (an EFT "Withdrawal" the
    old importer could not tell from a deposit) and the kind was inferred from
    the sign. M131's importer refuses a negative contribution outright, so the
    action is now authoritative; the sign check stays only for rows written
    before that fix, which are still in the database.
    """
    if trade.action == TradeAction.transfer_in:
        return "transfer_in"
    if trade.action == TradeAction.transfer_out:
        return "transfer_out"
    if trade.action == TradeAction.withdrawal:
        return "withdrawal"
    return "contribution" if value >= 0 else "withdrawal"


def account_value_history(
    session: Session,
    account: Account,
    from_: dt.date,
    to: dt.date,
    requested: Granularity,
    min_event_value: Decimal,
) -> dict[str, object]:
    """Buckets + significant-event markers for ONE account (docs/08).

    Every figure is derived from committed rows on each call — markers are
    never extracted into a cache table (docs/08 §Event markers).
    """
    user_id = account.user_id
    reporting = get_settings().reporting_currency
    if from_ is not None and from_ > to:
        raise api_error(422, "from must be on or before to", "invalid_date_range")

    # Cumulative reads, UNBOUNDED: a balance at bucket end is every row up to
    # that end (docs/12 §4 "cumulative sums"), and the window floor below needs
    # the earliest row the account has — however far back it goes.
    txns = _committed_transactions(session, user_id, account_id=account.id)
    trades = list(
        session.exec(
            select(Trade)
            .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
            .where(*committed_trade_conditions(user_id), Trade.account_id == account.id)
            .order_by(Trade.date, Trade.id)  # type: ignore[attr-defined]
        ).all()
    )

    # docs/08 §Defaults: the account's own life — but `created_at` is when the
    # ROW was written, not when the account started. Real account 731 was
    # created 2026-10-08 with trades back to 2025-11-13 (an import of years of
    # history), so flooring at `created_at` alone would draw ONE point and
    # throw away the entire timeline the chart exists to show. The floor is
    # the earliest of the account row and its first committed row: never
    # before either, so no bucket is fabricated, and never after real data.
    floor = account.created_at.date() if account.created_at is not None else None
    for row in (*txns, *trades):
        if floor is None or row.date < floor:
            floor = row.date
    start = min(max(from_ if from_ is not None else (floor or to), floor or to), to)

    granularity = resolve_granularity(start, to, requested)
    starts = bucket_starts(start, to, granularity)
    ends = [bucket_end(bstart, granularity) for bstart in starts]
    start_to_end = dict(zip(starts, ends, strict=True))

    fx = FxCache(session, reporting)
    prices = PriceCache(session, user_id)

    # ONE securities read — listings (valuation) and symbols (marker copy).
    security_ids = {trade.security_id for trade in trades}
    securities: dict[int, Security] = (
        {s.id: s for s in session.exec(select(Security).where(Security.id.in_(security_ids))).all()}
        if security_ids
        else {}
    )
    listings = {sid: s.listing_currency for sid, s in securities.items()}
    categories = {
        c.id: c.name for c in session.exec(select(Category).where(Category.user_id == user_id))
    }

    def cad_rate(currency: str, day: dt.date) -> Decimal | None:
        """Cache-only rate, refused when it is past the M115 ≤7d window."""
        info = fx.rate_info(currency, reporting, day)
        if info is None or (day - info[1]).days > STALE_DAYS:
            return None
        return info[0]

    cash_native = account.starting_balance or _ZERO
    # M132 (docs/05 §Portfolio value, docs/08 §Account value history): trade
    # cash is a BALANCE kept per ROW currency — a USD buy inside this CAD
    # account is USD cash, not CAD — while the account's own ledger
    # (`starting_balance` + transactions) stays in the account's currency.
    # Each currency converts at the bucket end, the same day positions and the
    # account's own ledger convert: a balance is worth today's rate, not a
    # basket of the rates its rows happened on.
    foreign_cash: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    positions: dict[tuple[int, int], Decimal] = defaultdict(lambda: _ZERO)
    buckets: list[dict[str, object]] = []
    account_rate_at: dict[dt.date, Decimal | None] = {}

    ti = tj = 0
    # M144 (docs/08 §Honesty): a bucket in which the account holds NO cash and
    # NO position is not a valuation, it is an absence — recorded per bucket so
    # the leading days before the account held anything can say "not held yet"
    # instead of implying a hole in the price cache.
    money_present: list[bool] = []
    for end in ends:
        while ti < len(txns) and txns[ti].date <= end:
            cash_native += txns[ti].amount_native
            ti += 1
        while tj < len(trades) and trades[tj].date <= end:
            effect = _trade_cash_effect(trades[tj])
            if effect != _ZERO:
                currency = _trade_currency(trades[tj], account.currency)
                if currency == account.currency:
                    cash_native += effect
                else:
                    foreign_cash[currency] += effect
            _apply_trade_to_position(positions, trades[tj])
            tj += 1

        money_present.append(
            cash_native != _ZERO
            or any(amount != _ZERO for amount in foreign_cash.values())
            or any(
                owner_id == account.id and quantity != _ZERO
                for (owner_id, _), quantity in positions.items()
            )
        )

        rate = cad_rate(account.currency, end)
        account_rate_at[end] = rate
        if rate is None:
            # No honest CAD cash figure (fx gap past the carry-forward window):
            # the whole bucket is unknown, never a fabricated zero.
            buckets.append(
                {
                    "bucket_end": end,
                    "cash_cad": None,
                    "positions_cad": None,
                    "value_cad": None,
                    "estimated": False,
                    "valued": False,
                    "gap_reason": "fx",
                }
            )
            continue

        # Sum the LEGS raw and quantize ONCE, so this cell and the header's
        # `investment_cash_cad` are the same number by construction (docs/05:
        # "the header's total and the chart's last point mean the same thing").
        # Rounding each leg first drops real cents on a multi-currency account:
        # real 731 reads 0.02 + 0.24 = 0.26 per leg, but 0.27 rounded once.
        cash_total: Decimal | None = cash_native * rate
        fx_gap = False
        for currency, amount in foreign_cash.items():
            foreign_rate = cad_rate(currency, end)
            if foreign_rate is None:
                # A row currency with no honest rate at this bucket end: the
                # CASH cell is unknown (docs/08 §Honesty — its own cell is null
                # when its fx fails), while the holdings side may still be
                # valued on its own below.
                cash_total = None
                fx_gap = True
                break
            if cash_total is not None:
                cash_total += amount * foreign_rate
        cash_cad: Decimal | None = None if cash_total is None else _cents(cash_total)
        positions_cad = _ZERO
        estimated = False
        valued = True
        gap_reason: GapReason | None = "fx" if fx_gap else None
        for (owner_id, security_id), quantity in positions.items():
            if owner_id != account.id or quantity == 0:
                continue
            priced = prices.price_on(security_id, end)
            if priced is None:
                valued = False  # no price at all → GAP (docs/08 §Honesty)
                gap_reason = "no_price"
                break
            price, price_date = priced
            if price_date > end or (end - price_date).days > STALE_DAYS:
                estimated = True  # carried forward past docs/07's staleness
            listing_rate = cad_rate(listings.get(security_id, reporting), end)
            if listing_rate is None:
                valued = False
                gap_reason = "fx"
                break
            positions_cad += quantity * price * listing_rate

        if not valued:
            buckets.append(
                {
                    "bucket_end": end,
                    "cash_cad": cash_cad,
                    "positions_cad": None,
                    "value_cad": None,
                    "estimated": estimated,
                    "valued": False,
                    "gap_reason": gap_reason,
                }
            )
            continue
        positions_cad = _cents(positions_cad)
        if cash_cad is None:
            # Holdings are honest, cash is not: the VALUE is unknown, never a
            # holdings-only total wearing the account's name.
            buckets.append(
                {
                    "bucket_end": end,
                    "cash_cad": None,
                    "positions_cad": positions_cad,
                    "value_cad": None,
                    "estimated": estimated,
                    "valued": False,
                    "gap_reason": gap_reason,
                }
            )
            continue
        buckets.append(
            {
                "bucket_end": end,
                "cash_cad": cash_cad,
                "positions_cad": positions_cad,
                "value_cad": _cents(cash_cad + positions_cad),
                "estimated": estimated,
                "valued": True,
                "gap_reason": None,
            }
        )

    # --- M144: the leading days before the account held anything -------------
    # They were drawn as a flat $0.00 line, which reads as "this account was
    # worth nothing" — the same fabrication docs/08 §Empty state refuses at
    # series level ("the three-part empty contract instead of a flat $0 line").
    # The first bucket that holds money ends the run; everything before it is
    # `not_held`, and only ever at the FRONT: a later day that legitimately
    # bottoms out at zero is a real zero and stays valued.
    first_money = next((i for i, present in enumerate(money_present) if present), None)
    if first_money:
        for bucket in buckets[:first_money]:
            bucket["valued"] = False
            bucket["gap_reason"] = "not_held"
            bucket["value_cad"] = None

    # --- event markers (docs/08 §Event markers) ------------------------------
    # The relative leg of the significance rule measures the window's own move,
    # so a $300 deposit is significant inside an account that moved $2,000.
    valued_points = [b["value_cad"] for b in buckets if b["value_cad"] is not None]
    window_delta = valued_points[-1] - valued_points[0] if len(valued_points) >= 2 else None

    def significant(amount: Decimal) -> bool:
        if abs(amount) >= min_event_value:  # `0` → every event (docs/08)
            return True
        if window_delta is None or window_delta == _ZERO:
            return False
        return abs(amount) >= abs(window_delta) * Decimal("0.10")

    def event_cad(currency: str, day: dt.date, native: Decimal) -> Decimal | None:
        if native == _ZERO:
            return _ZERO
        rate = cad_rate(currency, day)
        if rate is None:  # fall back to the bucket's own account rate
            rate = account_rate_at.get(bucket_for(day) or day)
        return _cents(native * rate) if rate is not None else None

    def bucket_for(day: dt.date) -> dt.date | None:
        return start_to_end.get(bucket_key(day, granularity))

    events: list[dict[str, object]] = []
    for trade in trades:
        if trade.action not in _FLOW_ACTIONS:
            continue
        end = bucket_for(trade.date)
        if end is None:  # outside the drawn range
            continue
        native = _event_value(trade)
        if native == _ZERO:
            continue
        # M132: the marker's native amount is in the ROW's currency (the cash
        # effect or the in-kind value), so it converts at that currency — not
        # at the account's, which face-values a USD flow into the CAD total.
        marker_currency = _trade_currency(trade, account.currency)
        amount_cad = event_cad(marker_currency, trade.date, native)
        if amount_cad is None or not significant(amount_cad):
            continue
        security = securities.get(trade.security_id)
        events.append(
            {
                "id": f"trade:{trade.id}",
                "source": "trade",
                "trade_id": trade.id,
                "transaction_id": None,
                "date": trade.date,
                "bucket_end": end,
                "kind": _flow_kind(trade, native),
                "direction": 1 if native > 0 else -1,
                "amount_cad": amount_cad,
                "amount_native": native,
                "currency": marker_currency,
                "payee": None,
                "category_name": None,
                "symbol": security.symbol if security else None,
                "security_id": trade.security_id,
                "quantity": trade.quantity,
                "action": trade.action.value,
            }
        )

    for txn in txns:
        end = bucket_for(txn.date)
        if end is None or txn.amount_native == _ZERO:
            continue
        amount_cad = (
            _cents(txn.cad_amount)
            if txn.cad_amount is not None
            else event_cad(txn.currency, txn.date, txn.amount_native)
        )
        if amount_cad is None or not significant(amount_cad):
            continue
        events.append(
            {
                "id": f"transaction:{txn.id}",
                "source": "transaction",
                "trade_id": None,
                "transaction_id": txn.id,
                "date": txn.date,
                "bucket_end": end,
                "kind": "deposit" if txn.amount_native > 0 else "withdrawal",
                "direction": 1 if txn.amount_native > 0 else -1,
                "amount_cad": amount_cad,
                "amount_native": txn.amount_native,
                "currency": txn.currency,
                "payee": txn.normalized_payee,
                "category_name": categories.get(txn.category_id)
                if txn.category_id is not None
                else None,
                "symbol": None,
                "security_id": None,
                "quantity": None,
                "action": None,
            }
        )
    events.sort(key=lambda event: (event["date"], event["id"]))

    return {
        "account_id": account.id,
        # The EFFECTIVE window (after the floor), echoing the request the way
        # every other report does — the first bucket may start earlier through
        # docs/12 §Time bucketing alignment, and `buckets` carries the dates
        # that are actually drawn.
        "from": start,
        "to": to,
        "granularity": granularity,
        "requested_granularity": requested,
        "currency": reporting,
        "native_currency": account.currency,
        "min_event_value": min_event_value,
        # docs/08 §Empty state: an account with nothing recorded by `to`
        # renders the three-part empty contract instead of a flat $0 line.
        # Scoped to `to` (not "has rows ever"), so a window that ends before
        # the account's history starts is honestly EMPTY too.
        "has_data": any(txn.date <= to for txn in txns)
        or any(trade.date <= to for trade in trades)
        or (account.starting_balance is not None and account.starting_balance != _ZERO),
        "buckets": buckets,
        "events": events,
    }


# --- M130: the PORTFOLIO scope (docs/08 §Portfolio value history) -------------
#
# The per-account engine above is the whole of the arithmetic; the portfolio is
# that engine run over every investment account and SUMMED, never a second
# implementation. Two properties the merge has to guarantee:
#
#   * ONE granularity. Buckets are calendar-aligned (docs/12 §Time bucketing),
#     so two accounts resolve the same granularity to the same dates — but they
#     may not RESOLVE to the same granularity, because the account that starts
#     earlier has more buckets and coarsens first. The merge therefore picks the
#     coarsest answer any account produced and re-asks the others with it, so
#     the summed series has one bucket size.
#   * a bucket an account does not have is that account contributing ZERO, not
#     an unknown — an account that did not exist yet held nothing. An account
#     that IS in the bucket and reports `None` makes the sum `None`: the whole
#     point of docs/08 §Honesty is that one unvaluable account cannot be papered
#     over with a zero.

#: The coarsening ladder, coarsest last — used to reconcile per-account answers.
_GRANULARITY_ORDER: dict[str, int] = {"day": 0, "week": 1, "month": 2, "year": 3}


def investment_accounts(session: Session, user_id: int) -> list[Account]:
    """Every account of the user that may hold securities (docs/02 §accounts).

    Ordered by id so the portfolio's account list — and therefore the marker
    attribution order — is stable across calls.
    """
    return [
        account
        for account in session.exec(
            select(Account).where(Account.user_id == user_id).order_by(Account.id)
        ).all()
        if account.type in INVESTMENT_ACCOUNT_TYPES
    ]


def _cash_legs(
    session: Session, user_id: int, accounts: list[Account], to: dt.date
) -> tuple[dict[int, Decimal], dict[int, dict[str, Decimal]]]:
    """Per-account cash LEGS, batched: (native, foreign) keyed by account id.

    `native[aid]` is the account-currency balance (starting_balance + Σ
    committed transactions + Σ account-currency trade effects); `foreign[aid]`
    keeps each OTHER row currency's trade effects separate (M132 — a USD buy
    inside a CAD account is USD cash, not CAD at face value).

    The ONE accumulation every cash surface reads (M145): the value-history
    bucket walk replays this same arithmetic incrementally, and both the
    detail card and the portfolio header call through here — one committed
    predicate, one trade-cash table (`total_return._trade_cash_effect`), one
    `date <= to` rule (a future-dated row has not moved money yet).
    Cumulative and UNBOUNDED below `to`: a balance at a date is every row up
    to it (docs/12 §4).
    """
    native: dict[int, Decimal] = {
        account.id: account.starting_balance or _ZERO for account in accounts
    }
    account_currency = {account.id: account.currency for account in accounts}
    foreign: dict[int, dict[str, Decimal]] = {account.id: {} for account in accounts}
    account_ids = [account.id for account in accounts]
    # ONE transactions statement for the whole batch (M145): the /accounts
    # list must not pay one query per account. Grouped in Python — the rows
    # arrive date-ordered and each lands on its own account's leg.
    for txn in _committed_transactions(session, user_id, account_ids=account_ids, to=to):
        native[txn.account_id] += txn.amount_native
    trades = list(
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
    for trade in trades:
        effect = _trade_cash_effect(trade)
        if effect == _ZERO:
            continue
        currency = _trade_currency(trade, account_currency[trade.account_id])
        if currency == account_currency[trade.account_id]:
            native[trade.account_id] += effect
        else:
            held = foreign[trade.account_id]
            held[currency] = held.get(currency, _ZERO) + effect
    return native, foreign


def _account_cash_raw(
    native: Decimal,
    foreign: dict[str, Decimal],
    account_currency: str,
    fx_cache: FxCache,
    reporting: str,
    today: dt.date,
) -> Decimal | None:
    """ONE account's legs converted to the reporting currency, UNquantized.

    The account-currency leg first, then the foreign legs SORTED, so the same
    rows always sum in the same order (byte-identical for a client mirror
    that walks the same legs). A zero leg needs no rate — an empty account
    holds no cash, which IS zero, not an unknown. `None` when a non-zero leg
    has no honest rate (absent, or past the M115 ≤7d carry-forward window):
    never a CAD-only half-truth (M132).

    The caller quantizes ONCE with `_cents` (docs/05: sum the legs raw, round
    once — rounding each leg first drops real cents on a multi-currency
    account).
    """
    legs: list[tuple[str, Decimal]] = [(account_currency, native)]
    legs += sorted(foreign.items())
    total = _ZERO
    for currency, amount in legs:
        if amount == _ZERO:
            continue
        info = fx_cache.rate_info(currency, reporting, today)
        if info is None or (today - info[1]).days > STALE_DAYS:
            return None
        total += amount * info[0]
    return total


def accounts_cash(
    session: Session, user_id: int, accounts: list[Account], today: dt.date
) -> dict[int, Decimal | None]:
    """The cash contract (M145), PER ACCOUNT, in the reporting currency.

    This is THE definition every cash surface reads — the detail card's
    `cash_balance`, the portfolio header's `cash_cad` (via
    `investment_cash_cad`, which is the SUM of these values) — built on the
    same legs the value-history engine walks::

        cash = starting_balance + Σ committed transactions
               + Σ trade-generated cash          (docs/05 cash-effect table)

    so "the card, the chart's last point and the header" mean the same number
    by construction (a trade row is an account's cash, docs/02 — real account
    459 has ZERO transactions, every dollar is a trade row; the transactions
    ledger alone draws a false $0 for it).

    Cache-only (docs/12 Principles): rates come from `FxCache` and are
    refused past the M115 ≤7d carry-forward window. A value of `None` means
    "no honest figure for this account" — never a zero stand-in. Each account
    quantizes its OWN total once (a card shows one account's money); the
    header sums the raw legs across accounts and quantizes once (M130's
    single-sum rule) — see `investment_cash_cad`.
    """
    if not accounts:
        return {}
    reporting = get_settings().reporting_currency
    native, foreign = _cash_legs(session, user_id, accounts, today)
    fx_cache = FxCache(session, reporting)
    cash: dict[int, Decimal | None] = {}
    for account in accounts:
        raw = _account_cash_raw(
            native[account.id], foreign[account.id], account.currency, fx_cache, reporting, today
        )
        cash[account.id] = None if raw is None else _cents(raw)
    return cash


def account_cash(session: Session, account: Account, today: dt.date) -> Decimal | None:
    """ONE account's cash at `today`, reporting currency (M145, docs/08).

    The single-account face of `accounts_cash` — the account Detail card's
    `cash_balance` source for investment accounts, so the card and the
    account's own value chart cannot disagree (M91/M130's totals-agree rule,
    now covering the cash line).
    """
    return accounts_cash(session, account.user_id, [account], today)[account.id]


def investment_cash_cad(
    session: Session, user_id: int, accounts: list[Account], today: dt.date
) -> Decimal | None:
    """The in-scope accounts' CASH, summed in the reporting currency.

    The docs/05 §Portfolio value cash leg, read at `today` — the same chain
    `account_value_history` walks and `account_cash` reports per account. The
    header sums the accounts' RAW legs and quantizes ONCE (M130's one-sum
    rule: rounding per account first would drop real cents the way per-leg
    rounding did — b9067cb), so the header's total and the chart's last
    point mean the same thing by construction.

    Cache-only (docs/12 Principles): the rate comes from `FxCache` and is
    refused past the M115 ≤7d carry-forward window, exactly like the buckets'.
    `None` means "no honest figure for at least one account" — never a zero
    stand-in. An empty scope holds no cash at all, which IS zero.
    """
    if not accounts:
        return _ZERO
    reporting = get_settings().reporting_currency
    native, foreign = _cash_legs(session, user_id, accounts, today)
    fx_cache = FxCache(session, reporting)
    total = _ZERO
    for account in accounts:
        raw = _account_cash_raw(
            native[account.id], foreign[account.id], account.currency, fx_cache, reporting, today
        )
        if raw is None:
            return None
        total += raw
    return _cents(total)


def _empty_portfolio(
    from_: dt.date | None, to: dt.date, requested: Granularity, min_event_value: Decimal
) -> dict[str, object]:
    """The documented empty contract for a user with no investment account.

    Echoes the request the way a populated response does, so the chart's
    loading -> empty transition never changes the SHAPE it renders from.
    """
    reporting = get_settings().reporting_currency
    return {
        "account_ids": [],
        "from": from_ if from_ is not None else to,
        "to": to,
        "granularity": requested,
        "requested_granularity": requested,
        "currency": reporting,
        "native_currency": reporting,
        "min_event_value": min_event_value,
        "has_data": False,
        "buckets": [],
        "events": [],
        "stats": {
            "from_bucket": None,
            "to_bucket": None,
            "from_value_cad": None,
            "to_value_cad": None,
            "change_cad": None,
            "change_pct": None,
        },
    }


def _merge_buckets(results: list[dict[str, object]]) -> list[dict[str, object]]:
    """Sum the per-account bucket series into ONE, keyed by `bucket_end`.

    Accounts are absent from the buckets before their own floor — they held
    nothing then, so absence contributes zero. Present-but-unvalued contributes
    `None`: the sum of an unknown is unknown (docs/08 §Honesty). The ONE
    exception is M144's `not_held` bucket: an account that held nothing at all
    on that day contributes ZERO, not an unknown — it is the same fact as
    absence, stated for a day the account already existed. Treating it as
    unknown would let one account's empty opening weeks blank every other
    account's real numbers for those days.
    """
    merged: dict[dt.date, dict[str, object]] = {}
    for result in results:
        for bucket in result["buckets"]:  # type: ignore[index]
            end = bucket["bucket_end"]
            if bucket.get("gap_reason") == "not_held":
                continue
            slot = merged.get(end)
            if slot is None:
                # First account in this bucket seeds it verbatim; every field
                # carries that account's own honesty flags.
                merged[end] = {
                    "bucket_end": end,
                    "cash_cad": bucket["cash_cad"],
                    "positions_cad": bucket["positions_cad"],
                    "value_cad": bucket["value_cad"],
                    "estimated": bucket["estimated"],
                    "valued": bucket["valued"],
                    "gap_reason": bucket.get("gap_reason"),
                }
                continue
            slot["valued"] = slot["valued"] and bucket["valued"]
            slot["estimated"] = slot["estimated"] or bucket["estimated"]
            if not bucket["valued"] and slot["gap_reason"] is None:
                slot["gap_reason"] = bucket.get("gap_reason")
            for field in ("cash_cad", "positions_cad", "value_cad"):
                mine, theirs = slot[field], bucket[field]
                slot[field] = (
                    theirs if mine is None else (mine if theirs is None else mine + theirs)
                )
    return [merged[end] for end in sorted(merged)]


def _merge_events(
    accounts: list[Account], results: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Dedup the per-account markers BY EVENT ID, carrying every account that
    produced it (docs/08 §Event markers).

    `trade:<id>` / `transaction:<id>` is globally unique, so one row yields one
    marker however many account series mention it — the guard that keeps a
    portfolio chart from pinning the same contribution twice, and the reason a
    marker that does span accounts names all of them in its tooltip.
    """
    by_id: dict[str, dict[str, object]] = {}
    for account, result in zip(accounts, results, strict=True):
        for event in result["events"]:  # type: ignore[index]
            existing = by_id.get(event["id"])
            if existing is None:
                merged = dict(event)
                merged["account_ids"] = [account.id]
                merged["account_names"] = [account.name]
                by_id[merged["id"]] = merged
            elif account.id not in existing["account_ids"]:  # type: ignore[operator]
                existing["account_ids"].append(account.id)  # type: ignore[union-attr]
                existing["account_names"].append(account.name)  # type: ignore[union-attr]
    return sorted(by_id.values(), key=lambda event: (event["date"], event["id"]))


def _range_stats(buckets: list[dict[str, object]]) -> dict[str, object]:
    """The window's own move, computed from the SAME series the chart draws.

    Server-side on purpose (docs/09): the header's "over <range>" figure and
    the line under it can never disagree, because the only source is this
    walk — the client renders a number it was handed, never one it subtracted.
    """
    valued = [bucket for bucket in buckets if bucket["valued"] and bucket["value_cad"] is not None]
    if not valued:
        empty: dict[str, object] = {
            "from_bucket": None,
            "to_bucket": None,
            "from_value_cad": None,
            "to_value_cad": None,
            "change_cad": None,
            "change_pct": None,
        }
        return empty
    first, last = valued[0], valued[-1]
    first_value = first["value_cad"]
    last_value = last["value_cad"]
    change: Decimal | None = None
    pct: float | None = None
    if len(valued) >= 2 and first_value is not None and last_value is not None:
        change = _cents(last_value - first_value)
        if first_value != _ZERO:
            pct = round(float(change / first_value) * 100, 2)
    return {
        "from_bucket": first["bucket_end"],
        "to_bucket": last["bucket_end"],
        "from_value_cad": first_value,
        "to_value_cad": last_value,
        "change_cad": change,
        "change_pct": pct,
    }


def portfolio_value_history(
    session: Session,
    user_id: int,
    from_: dt.date | None,
    to: dt.date,
    requested: Granularity,
    min_event_value: Decimal,
) -> dict[str, object]:
    """Every investment account's value series, SUMMED (docs/08 §Portfolio
    value history) — one chart for the whole portfolio.

    Each account runs the SAME ``account_value_history`` walk as the account
    page (no fork, no cache table, every figure derived from committed rows on
    each call); the only new arithmetic is the sum below plus the marker
    dedup. Scope: the user's investment accounts (docs/02); a user with none
    answers the empty contract rather than a flat $0 line.
    """
    if from_ is not None and from_ > to:
        raise api_error(422, "from must be on or before to", "invalid_date_range")

    accounts = investment_accounts(session, user_id)
    if not accounts:
        return _empty_portfolio(from_, to, requested, min_event_value)

    results = [
        account_value_history(session, account, from_, to, requested, min_event_value)
        for account in accounts
    ]
    # ONE bucket size for the summed series: take the coarsest answer any
    # account reached (the earliest floor has the most buckets and coarsens
    # first) and re-ask only the accounts that resolved finer.
    coarsest = max((str(r["granularity"]) for r in results), key=_GRANULARITY_ORDER.__getitem__)
    if any(str(r["granularity"]) != coarsest for r in results):
        results = [
            account_value_history(session, account, from_, to, coarsest, min_event_value)  # type: ignore[arg-type]
            if str(result["granularity"]) != coarsest
            else result
            for account, result in zip(accounts, results, strict=True)
        ]

    buckets = _merge_buckets(results)
    return {
        "account_ids": [account.id for account in accounts],
        "from": min(result["from"] for result in results),
        "to": to,
        "granularity": coarsest,
        "requested_granularity": requested,
        "currency": get_settings().reporting_currency,
        # A portfolio has no single native currency: every figure is already in
        # the reporting one, so echoing it keeps the chart's "values in <cur>"
        # badge off instead of claiming an account's currency it does not have.
        "native_currency": get_settings().reporting_currency,
        "min_event_value": min_event_value,
        "has_data": any(bool(result["has_data"]) for result in results),
        "buckets": buckets,
        "events": _merge_events(accounts, results),
        "stats": _range_stats(buckets),
    }

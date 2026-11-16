"""Portfolio valuation: signed positions + currency exposure (docs/05).

Positions = signed sum of trade quantities per (account, security); zero-qty
rows are skipped. Each position is valued at the newest usable price <= today
(manual wins over yfinance, docs/07) with source + age + stale indicator
(stale iff age_days > 7), shown in native currency and CAD. NO fetch
timestamp is exposed: the prices table records no fetch event — created_at is
row insertion time and provider no-ops/manual writes leave it meaningless as
provenance — so `fetched N ago` would be an unprovable claim; market-date age
is the only freshness fact (t_29881f89). CAD conversion
uses the newest cached fx_rates(listing_currency -> CAD, <= today); missing
rates degrade to market_value_cad=None rather than failing the endpoint. Reads
are cache-only; refresh is handled by explicit/background refresh paths.

Each position also carries its CAD book cost (w12 pin 1): the persisted
acb_pools value for non-registered accounts, or a pure average-cost replay of
the account's trades for registered ones (never persisted); plus `spark` — up
to the last 30 closes <= today ascending per security; plus the M129 delta
block, all server-computed from the same cached closes and fx_rates rows:
`previous_close_native` / `previous_close_date` (the move vs the prior close
— the DATE is what lets the M130 portfolio header label the baseline with the
real market day instead of "yesterday") / `day_change_native` /
`day_change_pct`, `day_change_cad` (qty x Δ x rate — the move's impact on
THIS holder's shares), `fx_rate_to_cad` (the exact rate used, so a live-tick
re-evaluation multiplies by the server's number), and
`unrealized_gain_cad` / `unrealized_gain_native` / `unrealized_gain_pct`
(market value vs book cost). Every one of them is None when its inputs are
missing — never a fabricated zero (docs/09 honesty).
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import Integer, column, true, values
from sqlalchemy.orm import Session, aliased
from sqlmodel import select

from app.core.config import get_settings
from app.models.acb_pools import AcbPool
from app.models.accounts import Account
from app.models.fx_rates import FxRate
from app.models.import_batches import ImportBatch
from app.models.prices import Price
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.services import market_data
from app.services.trade_filters import committed_trade_conditions
from app.tax.acb import CARRY_FORWARD_MAX_DAYS, cents, fx_rate_cad

_SPARK_POINTS = 30


def _today() -> date:
    return datetime.now(UTC).date()


def _cents(value: Decimal) -> Decimal:
    """Position-level money rounding — ROUND_HALF_EVEN, DELIBERATELY.

    NOT drift, and NOT the canonical money mode: docs/08 §Portfolio value
    history (F-M130-1) pins the positions pass as "round EACH security row to
    cents, then sum (Σ round(xᵢ))" against the value-history walk's
    "sum raw per account, round once per bucket", and says explicitly that
    "sharing one rounding order would break the single-source rules both
    endpoints pin". Banker's rounding is what makes the two independent;
    `tests/test_m130_portfolio_value.py::test_f_m130_1_...` hand-computes the
    half-even result (1 sh @ 10.006 → 10.01; Σ rows 20.02 vs walk 20.01), and
    the client mirrors the mode in `lib/positionGroups.ts` / `lib/impact.ts`.

    The canonical `app.tax.acb.cents` is called with the mode stated, so there
    is ONE rounding implementation app-wide. Do NOT "fix" this to half-up: it
    would move pinned money. The ACB pool math in `replay_registered_book_cost`
    below uses the canonical default (half-up) and is unaffected.
    """
    return cents(value, rounding=ROUND_HALF_EVEN)


def compute_positions(
    session: Session,
    user_id: int,
    today: date | None = None,
    account_id: int | None = None,
    *,
    account_ids: list[int] | None = None,
) -> list[dict]:
    """Positions for `user_id`, optionally narrowed to ONE account
    (`account_id`) or an ALLOWLIST (`account_ids`) — the M90 partner
    projection passes its scope so a hidden account's trades never enter
    the computation (docs/43 §2). Both default to unrestricted, which is
    byte-identical to the pre-M90 query every existing caller depends on.
    """
    today = today or _today()
    # Committed (or manual) trades only — a staged import batch must not move
    # positions before the user commits it (docs/03 staging model).
    query = (
        select(Trade, Account, Security)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .join(Account, Trade.account_id == Account.id)
        .join(Security, Trade.security_id == Security.id)
        .where(*committed_trade_conditions(user_id))
    )
    if account_id is not None:
        query = query.where(Trade.account_id == account_id)
    if account_ids is not None:
        query = query.where(Trade.account_id.in_(account_ids))
    rows = session.exec(
        query.order_by(Trade.account_id, Trade.security_id, Trade.date, Trade.id)
    ).all()

    quantities: dict[tuple[int, int], Decimal] = defaultdict(lambda: Decimal("0"))
    meta: dict[tuple[int, int], tuple[Account, Security]] = {}
    history: dict[tuple[int, int], list[Trade]] = defaultdict(list)
    for trade, account, security in rows:
        key = (account.id, security.id)
        if trade.action == TradeAction.corporate_action_adjustment:
            # quantity>0 is the RESULTING absolute share count (docs/06);
            # quantity=0 carries an ACB delta with no share effect.
            if trade.quantity != 0:
                quantities[key] = trade.quantity
        else:
            quantities[key] += trade.quantity  # sells are stored negative
        meta[key] = (account, security)
        history[key].append(trade)

    reporting_currency = get_settings().reporting_currency
    # Non-registered book cost reads the persisted pool (docs/06); registered
    # accounts have no pools and replay their trades instead (w12 pin 1).
    pool_stmt = select(AcbPool).where(AcbPool.user_id == user_id)
    if account_ids is not None:
        # Scope-filtered like the trades above (docs/43 §2): pool rows of a
        # hidden account are not read for a partner projection at all.
        pool_stmt = pool_stmt.where(AcbPool.account_id.in_(account_ids))
    pools: dict[tuple[int, int], Decimal] = {
        (pool.account_id, pool.security_id): pool.total_acb_cad
        for pool in session.exec(pool_stmt).all()
    }
    # Cache-only newest price/history reads, batched for every held security.
    # The same bounded series read supplies the current close, sparkline, and
    # previous-close baseline; no provider call occurs in this valuation pass.
    held: dict[int, Security] = {}
    for key in meta:
        security = meta[key][1]
        if quantities[key] != 0 and security.id is not None:
            held[security.id] = security
    held_ids = sorted(held)
    # The read path is cache-only. Price refresh remains discoverable through
    # the explicit refresh routes and portfolio background refresh worker.
    series = _price_series(session, user_id, held_ids, today)
    latest_rows: dict[int, Price | None] = {
        security_id: (rows[-1] if rows else None) for security_id, rows in series.items()
    }
    sparks = {
        security_id: [str(row.price) for row in rows[-_SPARK_POINTS:]]
        for security_id, rows in series.items()
    }
    # The close the newest row moved FROM: the newest cached close strictly
    # before the row the position is valued at (docs/08 — the same baseline
    # `market_data.previous_close` reads for the security page). One cached
    # row → no previous close → every day-Δ field stays None, which is how a
    # first-day listing renders honestly instead of a fabricated zero move.
    # M130 also keeps the DATE of that baseline: the portfolio header has to
    # say "vs Oct 7 close" with the real date, never "yesterday".
    previous_closes: dict[int, Decimal] = {}
    previous_close_dates: dict[int, date] = {}
    for security_id in held_ids:
        latest = latest_rows.get(security_id)
        if latest is None:
            continue
        older = [row for row in series.get(security_id, []) if row.date < latest.date]
        if older:
            previous_closes[security_id] = older[-1].price
            previous_close_dates[security_id] = older[-1].date
    foreign_currencies = {
        security.listing_currency.upper()
        for _, security in meta.values()
        if security.listing_currency.upper() != reporting_currency.upper()
    }
    fx_by_currency: dict[str, FxRate] = {}
    if foreign_currencies:
        for row in session.exec(
            select(FxRate)
            .where(
                FxRate.base.in_(foreign_currencies),
                FxRate.quote == reporting_currency.upper(),
                FxRate.date <= today,
            )
            .order_by(FxRate.base, FxRate.date.desc())
        ).all():
            fx_by_currency.setdefault(row.base, row)
    registered_rate_keys = {
        ((trade.currency or security.listing_currency).upper(), trade.date)
        for key, trade_rows in history.items()
        for account, security in [meta[key]]
        if not account.taxable
        for trade in trade_rows
        if trade.action
        in (
            TradeAction.buy,
            TradeAction.drip,
            TradeAction.transfer_in,
            TradeAction.return_of_capital,
        )
        and (trade.currency or security.listing_currency).upper() != "CAD"
    }
    registered_rates = _preload_registered_rates(session, registered_rate_keys)
    positions: list[dict] = []
    for key in sorted(meta):
        account, security = meta[key]
        quantity = quantities[key]
        if quantity == 0:
            continue  # fully-closed positions drop out of the view

        price_row = latest_rows.get(security.id) if security.id is not None else None
        if account.taxable:
            book_cost_cad = pools.get(key)
            if book_cost_cad is not None:
                book_cost_cad = _cents(book_cost_cad)
        else:
            book_cost_cad = replay_registered_book_cost(
                session, history[key], security, registered_rates
            )
        position: dict = {
            "account_id": account.id,
            "account_name": account.name,
            "security_id": security.id,
            "symbol": security.symbol,
            "listing_currency": security.listing_currency,
            "quantity": quantity,
            "book_cost_cad": book_cost_cad,
            "spark": sparks.get(security.id, []),
            "price_date": None,
            "price": None,
            "price_source": None,
            "price_age_days": None,
            "price_fetched_at": None,
            "price_stale": None,
            "market_value_native": None,
            "market_value_cad": None,
            # M129 delta block — every field server-computed (docs/08,
            # docs/09 §Day change): None means "no data", never a zero.
            "fx_rate_to_cad": None,
            "previous_close_native": None,
            "previous_close_date": None,
            "day_change_native": None,
            "day_change_pct": None,
            "day_change_cad": None,
            "unrealized_gain_cad": None,
            "unrealized_gain_native": None,
            "unrealized_gain_pct": None,
        }
        if price_row is not None:
            age_days = (today - price_row.date).days
            price = price_row.price
            native = _cents(quantity * price)
            # The reporting-currency rate for this listing: 1 without a query
            # when the two currencies match, else the newest fx_rates row
            # (docs/07 precedence). Kept on the row as `fx_rate_to_cad` so the
            # M129 live-tick re-evaluation on the security page multiplies by
            # the SERVER'S rate, never one it invented.
            rate: Decimal | None = Decimal("1")
            if security.listing_currency.upper() != reporting_currency.upper():
                fx_row = fx_by_currency.get(security.listing_currency.upper())
                rate = fx_row.rate if fx_row is not None else None
            position.update(
                price_date=price_row.date,
                price=price,
                price_source=price_row.source.value,
                price_age_days=age_days,
                # Option A (t_bd5ec95f): the REAL provider-fetch evidence,
                # straight off the row — NULL when no fetch ever stamped it.
                price_fetched_at=price_row.fetched_at,
                # Pinned boundary: stale iff age_days > 7; exactly 7 is fresh.
                price_stale=age_days > market_data.PRICE_STALE_DAYS,
                market_value_native=native,
                fx_rate_to_cad=str(rate) if rate is not None else None,
            )
            market_value_cad: Decimal | None = None
            if rate is not None:
                market_value_cad = (
                    native
                    if security.listing_currency.upper() == reporting_currency.upper()
                    else _cents(native * rate)
                )
                position["market_value_cad"] = market_value_cad
            security_id = security.id
            previous_close = previous_closes.get(security_id) if security_id is not None else None
            if previous_close is not None and security_id is not None:
                change = price - previous_close
                position.update(
                    previous_close_native=str(previous_close),
                    previous_close_date=previous_close_dates.get(security_id),
                    day_change_native=str(change),
                    day_change_pct=(
                        round(float(change / previous_close) * 100, 2)
                        if previous_close != 0
                        else None
                    ),
                )
                if rate is not None:
                    # "The delta for your own shares" (M129): one rounding,
                    # cents(qty x Δ x rate) — pinned in docs/08 so tests can
                    # hand-recompute it exactly.
                    position["day_change_cad"] = _cents(quantity * change * rate)
            if book_cost_cad is not None and market_value_cad is not None:
                gain = market_value_cad - book_cost_cad
                position["unrealized_gain_cad"] = _cents(gain)
                position["unrealized_gain_pct"] = (
                    round(float(gain / book_cost_cad) * 100, 2) if book_cost_cad != 0 else None
                )
                if rate is not None:
                    position["unrealized_gain_native"] = _cents(native - book_cost_cad / rate)
        positions.append(position)
    return positions


def replay_registered_book_cost(
    session: Session,
    trades: list[Trade],
    security: Security,
    preloaded_rates: dict[tuple[str, date], Decimal] | None = None,
) -> Decimal:
    """Pure average-cost replay for REGISTERED accounts (w12 pin 1): docs/06
    pool mechanics minus the registered-only exclusions — no superficial-loss
    logic, no addbacks; ACB-delta and ROC reductions floor at zero. Reads
    fx_rates only (docs/06 FX precedence); NEVER persisted.

    M132: an in-kind ``transfer_in`` is an ACQUISITION and an in-kind
    ``transfer_out`` is a DISPOSITION at average cost (docs/06) — the same two
    branches ``tax/acb.py`` runs for the taxable side. Without them a
    registered position that ARRIVED by transfer was priced at its DRIPs
    alone: real account 731's MSFT (40 shares @ 486.35 USD, in on 2025-12-02)
    read ``book_cost_cad = 152.08`` instead of ≈27,430 CAD, and every
    unrealized gain against it was fiction. A quantity-0 transfer row is CASH
    (docs/02) and moves no cost. The fx base is the ROW's currency
    (``trades.currency``, M132) falling back to the security's listing — the
    row is the authority for what its own price is stated in.
    """
    quantity = Decimal("0")
    acb_cad = Decimal("0")

    def trade_rate(base: str, trade: Trade) -> Decimal:
        if preloaded_rates is not None:
            cached = preloaded_rates.get((base.upper(), trade.date))
            if cached is not None:
                return cached
        return fx_rate_cad(session, base, trade.date, security.symbol)

    for trade in sorted(trades, key=lambda t: (t.date, t.id)):
        base = trade.currency or security.listing_currency
        if trade.action in (TradeAction.buy, TradeAction.drip):
            bought = abs(trade.quantity)
            rate = trade_rate(base, trade)
            acb_cad += cents(bought * trade.price_native * rate) + cents(trade.commission * rate)
            quantity += bought
        elif trade.action == TradeAction.transfer_in:
            moved = abs(trade.quantity)
            if moved:  # quantity 0 = a CASH transfer row, no cost basis (docs/02)
                rate = trade_rate(base, trade)
                acb_cad += cents(moved * trade.price_native * rate) + cents(trade.commission * rate)
                quantity += moved
        elif trade.action == TradeAction.transfer_out:
            moved = abs(trade.quantity)
            if moved and quantity > 0:
                acb_cad -= cents(acb_cad * moved / quantity)
                quantity -= moved
        elif trade.action == TradeAction.sell:
            sold = abs(trade.quantity)
            if quantity > 0:
                acb_cad -= cents(acb_cad * sold / quantity)
                quantity -= sold
        elif trade.action == TradeAction.return_of_capital:
            rate = trade_rate(base, trade)
            acb_cad = max(Decimal("0"), acb_cad - cents(trade.price_native * rate))
        elif trade.action == TradeAction.corporate_action_adjustment:
            if trade.quantity > 0:
                # Resulting absolute share count; total ACB preserved.
                quantity = trade.quantity
            elif trade.quantity == 0:
                # SIGNED CAD ACB delta applied directly (already CAD, no fx).
                acb_cad = max(Decimal("0"), acb_cad + cents(trade.price_native))
        # Cash-only actions (dividend, contribution, …) carry no cost basis.
    return cents(acb_cad)


def _preload_registered_rates(
    session: Session, required: set[tuple[str, date]]
) -> dict[tuple[str, date], Decimal]:
    """Resolve registered replay trade-date FX keys from one bounded query."""
    if not required:
        return {}
    bases = {base.upper() for base, _ in required}
    earliest = min(day for _, day in required)
    latest = max(day for _, day in required)
    rows = session.exec(
        select(FxRate)
        .where(
            FxRate.base.in_(bases),
            FxRate.quote == "CAD",
            FxRate.date >= earliest - timedelta(days=CARRY_FORWARD_MAX_DAYS),
            FxRate.date <= latest,
        )
        .order_by(FxRate.base, FxRate.date)
    ).all()
    dates_by_base: dict[str, list[date]] = defaultdict(list)
    rates_by_base: dict[str, list[Decimal]] = defaultdict(list)
    for row in rows:
        dates_by_base[row.base].append(row.date)
        rates_by_base[row.base].append(row.rate)
    resolved: dict[tuple[str, date], Decimal] = {}
    for base, day in required:
        normalized = base.upper()
        dates = dates_by_base.get(normalized, [])
        index = bisect_right(dates, day) - 1
        if index >= 0 and dates[index] >= day - timedelta(days=CARRY_FORWARD_MAX_DAYS):
            resolved[(normalized, day)] = rates_by_base[normalized][index]
    return resolved


def _price_series(
    session: Session, user_id: int, security_ids: list[int], today: date
) -> dict[int, list[Price]]:
    """Newest 31 closes <= today per security, ascending by date.

    The single scan behind the sparkline (last <=30, w12 pin 5) AND the M129
    previous close: one query serves both, so the two can never disagree
    about which row is newest. Manual wins by construction: prices is
    one-row-per-(security, date).
    """
    if not security_ids:
        return {}
    held = values(column("security_id", Integer), name="held_security_ids").data(
        [(security_id,) for security_id in security_ids]
    )
    recent = (
        select(Price)
        .where(
            Price.user_id == user_id,
            Price.security_id == held.c.security_id,
            Price.date <= today,
        )
        .order_by(Price.date.desc())
        .limit(_SPARK_POINTS + 1)
        .lateral("recent_prices")
    )
    price_row = aliased(Price, recent)
    rows = session.exec(
        select(price_row)
        .select_from(held)
        .join(recent, true())
        .order_by(price_row.security_id, price_row.date)
    ).all()
    series: dict[int, list[Price]] = {}
    for row in rows:
        if row.security_id is None:
            continue  # unreachable: filtered to `security_id.in_(...)`
        series.setdefault(row.security_id, []).append(row)
    return series


def compute_exposure(session: Session, user_id: int, account_id: int | None = None) -> list[dict]:
    """Holdings grouped by listing_currency, sorted desc by native value."""
    totals: dict[str, dict] = {}
    for position in compute_positions(session, user_id, account_id=account_id):
        value = position["market_value_native"]
        if value is None:
            continue
        bucket = totals.setdefault(
            position["listing_currency"],
            {"listing_currency": position["listing_currency"], "holdings_count": 0},
        )
        bucket["total_market_value_native"] = (
            bucket.get("total_market_value_native", Decimal("0")) + value
        )
        bucket["holdings_count"] += 1
    return sorted(totals.values(), key=lambda row: row["total_market_value_native"], reverse=True)

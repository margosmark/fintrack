"""securities — CRUD + yfinance symbol lookup (docs/05, docs/08).

Lookup failures degrade to 404 {"code": "lookup_unavailable"} (docs/07
degrade-to-no-data discipline); manual creation never requires a lookup.
"""

import logging
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func
from sqlalchemy.orm import Session
from sqlmodel import Session as SQLModelSession
from sqlmodel import select

from app.api._price_stream import price_stream_generator
from app.api.deps import (
    Pagination,
    get_current_user,
    get_current_user_with_query_token,
    get_db,
    get_engine,
    get_pagination,
    owned_or_404,
    paginate,
)
from app.core.errors import api_error
from app.core.ratelimit import limiter
from app.models.acb_pools import AcbPool
from app.models.dividend_history import DividendHistory, DividendHistoryRefresh
from app.models.etf_holdings import EtfAssetClass, EtfHolding, EtfHoldingSource
from app.models.fundamentals import Fundamentals, FundamentalsSource
from app.models.import_batches import ImportBatch
from app.models.prices import Price
from app.models.securities import QuoteOutcome, Security, SecurityKind
from app.models.trades import Trade
from app.models.users import User
from app.models.watchlist import WatchlistItem
from app.schemas.securities import (
    EtfHoldingRead,
    EtfHoldingsBlock,
    EtfHoldingsUpdate,
    FundamentalsRead,
    FundamentalsUpdate,
    HistoryPoint,
    LatestPrice,
    OverviewTotals,
    QuoteCheck,
    RecentTrade,
    SecurityCreate,
    SecurityLookup,
    SecurityOverview,
    SecurityPage,
    SecurityRead,
    SecurityStats,
    SecurityUpdate,
    SplitEventRead,
    SplitRepairRead,
)
from app.services import market_data, split_repair, valuation
from app.services.trade_filters import committed_trade_conditions
from app.tax.acb import cents

router = APIRouter(prefix="/securities", tags=["securities"])


# Historical backfill span for newly created securities (W11): one year of
# daily closes, fetched via yfinance; manual price rows always win.
BACKFILL_DAYS = 365


def _run_backfill(security: Security, *, probe_dead: bool = False) -> None:
    """BackgroundTask body — opens its own Session because request sessions
    are closed before background tasks run. Failures degrade silently
    (docs/07): a failed (or privacy-blocked) fetch never breaks creation,
    and manual-source price rows are never touched.

    ``probe_dead`` (M141) is the manual "Try refresh" path: it is the only
    caller that probes a ``dead_symbol`` row, so the flag can be disproved.
    Every automatic queue leaves it False and the row is skipped entirely.
    """
    with SQLModelSession(get_engine(), expire_on_commit=False) as session:
        try:
            # Re-read: the row may have been flagged (or fixed) since the
            # request loaded it, and `_run_backfill` must act on the CURRENT
            # state, not the snapshot the HTTP response was built from.
            current = session.get(Security, security.id) if security.id else None
            target = current if current is not None else security
            # M155 (docs/07 §Price-path sessions): `session.get` autobegins and
            # `backfill_range`'s first act is a year of bars over the network.
            # This session is the task's own, and nothing has been written on
            # it, so the release hands the pool connection back before the wait
            # and commits nothing of the caller's (docs/01 §Sessions).
            market_data.release_before_provider_wait(session, target)
            market_data.backfill_range(
                session,
                target,
                date.today() - timedelta(days=BACKFILL_DAYS),
                date.today(),
                probe_dead=probe_dead,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Historical backfill failed for %s; continuing without it", security.symbol
            )


def queue_backfill(
    background_tasks: BackgroundTasks, security: Security, *, probe_dead: bool = False
) -> None:
    """Dispatch the historical-price backfill as a BackgroundTask (W11)."""
    background_tasks.add_task(_run_backfill, security, probe_dead=probe_dead)


def _run_deepen(fund: Security, symbols: list[str]) -> None:
    """BackgroundTask body - opens its own Session because request sessions
    are closed before background tasks run. Eager sub-fund enrichment
    (M19, docs/05): failures degrade silently (docs/07)."""
    with SQLModelSession(get_engine(), expire_on_commit=False) as session:
        try:
            market_data.ensure_sub_fund_composition(session, fund, symbols)
        except Exception:
            logging.getLogger(__name__).warning(
                "Sub-fund enrichment failed for security %s; continuing without it",
                fund.symbol,
            )


def queue_deepen(background_tasks: BackgroundTasks, fund: Security, symbols: list[str]) -> None:
    """Dispatch eager sub-fund composition enrichment after a holdings write."""
    background_tasks.add_task(_run_deepen, fund, symbols)


# NOTE: registered BEFORE /{security_id} so "lookup" is not captured as an id.
@router.get("/lookup", response_model=SecurityLookup)
@limiter.limit("30/minute")
def lookup_symbol(
    request: Request,
    symbol: Annotated[str, Query(min_length=1)],
    _: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SecurityLookup:
    """yfinance autofill (docs/05); M142 adds the name→ticker search fallback.

    `market_data.resolve_symbol` answers in three ways (docs/07 §Symbol
    search fallback): the typed TICKER resolves (`direct`/`suffix`), a typed
    NAME resolves to the ONE listing Yahoo's search returns (`search`, and
    `symbol` is the canonical ticker to persist), or several listings match
    (`ambiguous` — the pick-one chooser, never a silent guess). No answer at
    all still degrades to 404 `lookup_unavailable`, which is the state
    M141's dead-symbol guard consumes.

    M143 widens `ambiguous` to the case that bit: BOTH the bare symbol and
    its `.TO` sibling name a real listing (`VGRO` on NYSEArca and `VGRO.TO`
    on the TSX), so the answer is a choice — the candidates carry symbol,
    name, kind, currency and venue, and picking one applies it directly
    (never a second lookup, which would only re-ask the same question).
    """
    # M175 (docs/07 §Provider-wait ledger): `get_current_user` already ran its
    # one query on THIS request session (deps.get_db), so the connection is
    # `idle in transaction` when the lookup goes out — a `.info` read plus a
    # search fallback, each on the 5 s market-data deadline. Read-only route:
    # the release commits the auth read and nothing else.
    market_data.release_before_provider_wait(db)
    resolution = market_data.resolve_symbol(symbol)
    match = resolution["match"]
    result = resolution["result"]
    if match == "ambiguous":
        return SecurityLookup(match="ambiguous", candidates=resolution["candidates"])
    if result is None or match == "none":
        raise api_error(
            404, f"No market data available for symbol '{symbol}'", "lookup_unavailable"
        )
    return SecurityLookup.model_validate(
        {**result, "match": match}  # `match` is direct|suffix|search here
    )


# NOTE: registered BEFORE /{security_id} so "quote-check" is not captured as an id.
@router.get("/quote-check", response_model=QuoteCheck)
@limiter.limit("30/minute")
def check_quote(
    request: Request,
    symbol: Annotated[str, Query(min_length=1)],
    _: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> QuoteCheck:
    """M141 add-time symbol validation (docs/07 §Symbol check, docs/08).

    One yfinance probe — the same call the price fan-out makes — answering
    "does Yahoo know this ticker at all?". Deliberately NOT a hard block: a
    never-fetched symbol is a legitimate thing to watch (a future listing, a
    cash equivalent), so the router always answers 200 and the FORM decides
    how loudly to warn. ``quote_found: null`` means we could not ask — a
    network failure is never reported as "no quote".

    Privacy mode raises the standard 403 from the seam guard; the form treats
    any non-``false`` answer as "no warning".
    """
    clean = symbol.strip().upper()
    # M175 (docs/07 §Provider-wait ledger): same shape as the lookup route
    # above — the auth read has the request session `idle in transaction`, and
    # `probe_quote` is a yfinance call on the 5 s deadline.
    market_data.release_before_provider_wait(db)
    outcome, reason = market_data.probe_quote(clean)
    if outcome is QuoteOutcome.ok:
        return QuoteCheck(symbol=clean, quote_found=True)
    if outcome is QuoteOutcome.not_found:
        return QuoteCheck(symbol=clean, quote_found=False, reason=reason)
    return QuoteCheck(symbol=clean, quote_found=None, reason=reason)


def _cents(value: Decimal) -> Decimal:
    """Rounding for this module's overview totals — ROUND_HALF_EVEN, PRESERVED.

    These totals are the arithmetic Σ of the SAME per-position fields
    `compute_positions` published (docs/08 §`GET /securities/{id}/overview`:
    "the card headline and the holdings table can never disagree"), so they keep
    the positions pass's mode. That pass is half-even by docs/08 §Portfolio
    value history (F-M130-1).

    Only `quantity`, `total_weight_pct` and the asset-class bucket percentages
    are actually mode-SENSITIVE here (they sum RAW `Numeric(18,6)` /
    `Numeric(7,4)` columns); the money totals are sums of values valuation
    already quantized, so re-quantizing them is a no-op either way. No doc
    states a mode for the three sensitive ones, so this PRESERVES the current
    bytes rather than silently moving money — see the module's rounding note.

    Delegates to the canonical `app.tax.acb.cents` (one implementation app-wide,
    AGENTS §2); it is no longer a reach into `valuation._cents`'s private name.
    """
    return cents(value, rounding=ROUND_HALF_EVEN)


@router.get("", response_model=SecurityPage)
def list_securities(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
) -> SecurityPage:
    rows, total = paginate(
        db,
        select(Security).where(Security.user_id == current_user.id).order_by(Security.id),
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return SecurityPage(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.post("", response_model=SecurityRead, status_code=201)
def create_security(
    payload: SecurityCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> Security:
    """Create a security and queue its 365-day historical-price backfill
    (W11): the fetch happens after the response, degrades silently, and
    never overwrites manual prices.

    M143 (docs/07 §Unresolved symbols): an EMPTY `listing_currency` is the
    "unresolved" marker, not a value to fill in. The form only sends it when
    the lookup found nothing — so the row is created with the name the user
    actually typed, NO invented venue/currency, the M141 dead-symbol guard
    set up front (the client already established no listing exists, so there
    is nothing to fetch and no reason to spend three 404s learning it
    again), and the backfill is not queued. The UI renders the state from
    `listing_currency == ""` + `dead_symbol`; "Try refresh" (probe_dead) is
    still the way to disprove it later.
    """
    symbol = payload.symbol.strip().upper()
    exchange = payload.exchange.strip().upper()
    listing_currency = payload.listing_currency.strip().upper()
    duplicate = db.exec(
        select(Security).where(
            Security.user_id == current_user.id,
            Security.symbol == symbol,
            Security.exchange == exchange,
        )
    ).first()
    if duplicate is not None:
        raise api_error(
            409, "A security with this symbol/exchange already exists", "duplicate_security"
        )
    unresolved = not listing_currency
    security = Security(
        user_id=current_user.id,
        symbol=symbol,
        name=payload.name,
        kind=payload.kind,
        listing_currency=listing_currency,
        exchange=exchange,
        dead_symbol=unresolved,
        dead_symbol_reason=(
            f"No market data for {symbol} when the row was created" if unresolved else None
        ),
    )
    db.add(security)
    db.commit()
    db.refresh(security)
    if not unresolved:
        queue_backfill(background_tasks, security)
    return security


# NOTE: declared BEFORE /{security_id} so "overview" is not captured as an id.
@router.get("/{security_id}/overview", response_model=SecurityOverview)
def security_overview(
    security_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SecurityOverview:
    """One payload for the per-security dashboard page (M13): cached price
    history/stats + reused position math. Auto-refreshes price and fundamentals
    on each call to ensure live data."""
    security = owned_or_404(Security, security_id, current_user, db)
    today = date.today()

    # Auto-refresh price data (network call, degrades silently)
    try:
        market_data.ensure_recent_price(db, security, today)
    except Exception:  # silent degradation
        logging.getLogger(__name__).warning(
            "Auto-refresh price failed for %s; continuing", security.symbol
        )

    window_start = today - timedelta(days=BACKFILL_DAYS)
    in_window = (
        Price.security_id == security.id,
        Price.date >= window_start,
        Price.date <= today,
    )

    latest_row = market_data.newest_price(db, security.id, today)
    latest = None
    if latest_row is not None:
        age_days = (today - latest_row.date).days
        # M129: the daily delta's baseline — the close strictly BEFORE this
        # row, so the price card renders ▲/▼ vs the prior session and a live
        # tick can re-evaluate the delta from server primitives. One cached
        # row (first-day listing) → None, and the card honestly shows no
        # delta line rather than a fabricated zero.
        prev_row = market_data.previous_close(db, security.id, latest_row.date)
        day_change = None
        day_pct = None
        if prev_row is not None:
            day_change = latest_row.price - prev_row.price
            if prev_row.price != 0:
                day_pct = round(float(day_change / prev_row.price) * 100, 2)
        latest = LatestPrice(
            date=latest_row.date,
            price=latest_row.price,
            source=latest_row.source,
            age_days=age_days,
            stale=age_days > market_data.PRICE_STALE_DAYS,
            previous_close=str(prev_row.price) if prev_row is not None else None,
            day_change_native=str(day_change) if day_change is not None else None,
            day_change_pct=day_pct,
        )

    high, low, cached_days = db.exec(
        select(func.max(Price.price), func.min(Price.price), func.count()).where(*in_window)
    ).one()
    change_pct: float | None = None
    first_close = None
    if cached_rows := int(cached_days or 0):
        first_close = db.exec(
            select(Price.price).where(*in_window).order_by(Price.date.asc()).limit(1)
        ).first()
    if latest_row is not None and first_close is not None and cached_rows >= 2 and first_close != 0:
        change_pct = round(float((latest_row.price - first_close) / first_close) * 100, 2)

    history = [
        HistoryPoint(date=row_date, price=str(row_price))
        for row_date, row_price in db.exec(
            select(Price.date, Price.price).where(*in_window).order_by(Price.date.asc())
        ).all()
    ]

    # Single source of gain/book-cost math (AGENTS pin): reuse verbatim.
    positions = [
        row
        for row in valuation.compute_positions(db, current_user.id)
        if row["security_id"] == security.id
    ]
    quantity = sum((row["quantity"] for row in positions), Decimal("0"))
    book_costs = [row["book_cost_cad"] for row in positions if row["book_cost_cad"] is not None]
    market_values = [
        row["market_value_cad"] for row in positions if row["market_value_cad"] is not None
    ]
    # M129 impact totals: sums of the per-position server fields, so the
    # "You hold …" headline is provably the same numbers as the table below
    # it (anti-fabrication: nothing is recomputed here, only added).
    gains_cad = [
        row["unrealized_gain_cad"] for row in positions if row["unrealized_gain_cad"] is not None
    ]
    gains_native = [
        row["unrealized_gain_native"]
        for row in positions
        if row["unrealized_gain_native"] is not None
    ]
    day_cad = [row["day_change_cad"] for row in positions if row["day_change_cad"] is not None]
    gain_book = [
        row["book_cost_cad"]
        for row in positions
        if row["unrealized_gain_cad"] is not None and row["book_cost_cad"] is not None
    ]
    total_gain_cad = sum(gains_cad, Decimal("0"))
    total_gain_book = sum(gain_book, Decimal("0"))

    # Committed (or manual) trades only — recent_trades is a LIVE view, not the
    # import review surface; staged rows are reviewed via /trades/import.
    recent_trades = [
        RecentTrade(
            id=trade.id,
            account_id=trade.account_id,
            action=trade.action.value,
            date=trade.date,
            quantity=str(trade.quantity),
            price_native=str(trade.price_native),
        )
        for trade in db.exec(
            select(Trade)
            .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
            .where(*committed_trade_conditions(current_user.id), Trade.security_id == security.id)
            .order_by(Trade.date.desc(), Trade.id.desc())
            .limit(10)
        ).all()
    ]
    # Fundamentals refresh (network call, degrades silently)
    fundamentals_row = None
    try:
        fundamentals_row = market_data.ensure_fundamentals(db, security, today)
    except Exception:  # silent degradation
        logging.getLogger(__name__).debug("fundamentals refresh failed", exc_info=True)

    return SecurityOverview(
        security=SecurityRead.model_validate(security),
        latest=latest,
        stats=SecurityStats(
            high_52w=str(high) if high is not None else None,
            low_52w=str(low) if low is not None else None,
            change_pct=change_pct,
            cached_days=int(cached_days or 0),
        ),
        history=history,
        positions=positions,
        totals=OverviewTotals(
            quantity=str(_cents(quantity)),
            book_cost_cad=(str(_cents(sum(book_costs, Decimal("0")))) if book_costs else None),
            market_value_cad=(
                str(_cents(sum(market_values, Decimal("0")))) if market_values else None
            ),
            unrealized_gain_cad=(str(_cents(total_gain_cad)) if gains_cad else None),
            unrealized_gain_native=(
                str(_cents(sum(gains_native, Decimal("0")))) if gains_native else None
            ),
            unrealized_gain_pct=(
                round(float(total_gain_cad / total_gain_book) * 100, 2)
                if gain_book and total_gain_book != 0
                else None
            ),
            day_change_cad=(str(_cents(sum(day_cad, Decimal("0")))) if day_cad else None),
        ),
        watchlisted=db.exec(
            select(WatchlistItem).where(
                WatchlistItem.user_id == current_user.id,
                WatchlistItem.security_id == security.id,
            )
        ).first()
        is not None,
        recent_trades=recent_trades,
        fundamentals=(
            FundamentalsRead.model_validate(fundamentals_row) if fundamentals_row else None
        ),
        # Composition is a cache-only read (M17): no fetch here — import or
        # manual editing populates it; null when the fund has no rows.
        holdings=(
            _holdings_block(db, security)
            if db.exec(
                select(EtfHolding.id).where(EtfHolding.security_id == security.id).limit(1)
            ).first()
            else None
        ),
    )


# NOTE: declared BEFORE /{security_id} so "fundamentals" is not captured as an id.
@router.put("/{security_id}/fundamentals", response_model=FundamentalsRead)
def update_fundamentals(
    security_id: int,
    payload: FundamentalsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> FundamentalsRead:
    """Manual-wins upsert (M16): only provided fields change; source=manual
    rows are never overwritten by later yfinance refreshes."""
    security = owned_or_404(Security, security_id, current_user, db)
    row = db.exec(select(Fundamentals).where(Fundamentals.security_id == security.id)).first()
    if row is None:
        row = Fundamentals(user_id=current_user.id, security_id=security.id)
        db.add(row)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    row.source = FundamentalsSource.manual
    row.fetched_at = date.today()
    db.add(row)
    db.commit()
    db.refresh(row)
    return FundamentalsRead.model_validate(row)


def _holdings_block(
    db: Session, security: Security, *, recursive: bool = False
) -> EtfHoldingsBlock:
    """Cache-only composition payload (M17): rows sorted by weight desc,
    cents-quantized weight total, majority-row source (null when empty).
    When recursive=True, fund holdings are enriched with asset_class_breakdown."""
    rows = list(
        db.exec(
            select(EtfHolding)
            .where(EtfHolding.security_id == security.id)
            .order_by(EtfHolding.weight_pct.desc(), EtfHolding.id.asc())
        ).all()
    )
    if not rows:
        return EtfHoldingsBlock(items=[], total_weight_pct="0.00", source=None)
    total = _cents(sum((row.weight_pct for row in rows), Decimal("0")))
    manual_count = sum(1 for row in rows if row.source == EtfHoldingSource.manual)
    if manual_count * 2 >= len(rows):
        source: str | None = EtfHoldingSource.manual.value
    else:
        # M147 + M147-followup: provider rows may come from the issuer CSV,
        # Vanguard's own APIs or yfinance — report whichever non-manual
        # provider wrote the MAJORITY of the block (row order is weight-desc,
        # so a tie resolves to the provider behind the heaviest row).
        counts: dict[EtfHoldingSource, int] = {}
        for row in rows:
            if row.source != EtfHoldingSource.manual:
                counts[row.source] = counts.get(row.source, 0) + 1
        source = max(counts, key=counts.__getitem__).value if counts else None

    items: list[EtfHoldingRead] = []
    if recursive:
        # Preload data for recursive breakdown computation
        holdings_by_fund: dict[int, list[EtfHolding]] = {}
        securities_by_id: dict[int, Security] = {}
        for h in db.exec(select(EtfHolding).where(EtfHolding.user_id == security.user_id)).all():
            holdings_by_fund.setdefault(h.security_id, []).append(h)
        for sec in db.exec(select(Security).where(Security.user_id == security.user_id)).all():
            securities_by_id[sec.id] = sec

        for row in rows:
            item = EtfHoldingRead.model_validate(row)
            # Enrich with recursive breakdown if this holding is a fund
            if row.holding_security_id is not None and row.holding_security_id in securities_by_id:
                sub_sec = securities_by_id[row.holding_security_id]
                if sub_sec.kind in (SecurityKind.etf, SecurityKind.mutual_fund):
                    sub_holdings = holdings_by_fund.get(sub_sec.id, [])
                    if sub_holdings:
                        breakdown = _compute_asset_class_breakdown(
                            sub_holdings,
                            holdings_by_fund,
                            securities_by_id,
                            depth=0,
                            visited={sub_sec.id},
                        )
                        item.asset_class_breakdown = breakdown
            items.append(item)
    else:
        items = [EtfHoldingRead.model_validate(row) for row in rows]

    return EtfHoldingsBlock(
        items=items,
        total_weight_pct=str(total),
        source=source,
    )


def _compute_asset_class_breakdown(
    holdings: list[EtfHolding],
    holdings_by_fund: dict[int, list[EtfHolding]],
    securities_by_id: dict[int, Security],
    depth: int,
    visited: set[int],
) -> dict[str, str]:
    """Recursively compute asset class breakdown for a fund's holdings.
    Returns dict mapping asset_class → weight_pct (as string). Cycle-safe
    via visited set; max depth 3 to prevent runaway recursion."""
    from app.models.etf_holdings import EtfAssetClass

    MAX_DEPTH = 3
    buckets: dict[str, Decimal] = {}
    total_weight = sum((h.weight_pct for h in holdings), Decimal("0"))
    if total_weight <= 0:
        return {}
    denominator = max(total_weight, Decimal("100"))

    for holding in holdings:
        weight = holding.weight_pct / denominator
        # Check if this holding is a fund we can recurse into
        sub_sec = (
            securities_by_id.get(holding.holding_security_id)
            if holding.holding_security_id
            else None
        )
        sub_holdings = holdings_by_fund.get(sub_sec.id, []) if sub_sec else []
        if (
            sub_sec is not None
            and sub_sec.kind in (SecurityKind.etf, SecurityKind.mutual_fund)
            and sub_holdings
            and depth < MAX_DEPTH
            and sub_sec.id not in visited
        ):
            # Recurse into sub-fund
            sub_breakdown = _compute_asset_class_breakdown(
                sub_holdings,
                holdings_by_fund,
                securities_by_id,
                depth + 1,
                visited | {sub_sec.id},
            )
            for ac, pct_str in sub_breakdown.items():
                pct = Decimal(pct_str)
                buckets[ac] = buckets.get(ac, Decimal("0")) + weight * pct
        else:
            # Leaf: use the holding's own asset_class
            if isinstance(holding.asset_class, EtfAssetClass):
                ac = holding.asset_class.value
            else:
                ac = str(holding.asset_class)
            buckets[ac] = buckets.get(ac, Decimal("0")) + weight * Decimal("100")

    # Convert to string percentages
    return {k: str(_cents(v)) for k, v in buckets.items()}


# NOTE: declared BEFORE /{security_id} so "holdings" is not captured as an id.
@router.get("/{security_id}/holdings", response_model=EtfHoldingsBlock)
def get_holdings(
    security_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    recursive: bool = Query(
        False,
        description="Enable recursive asset class breakdown for fund holdings",
    ),
) -> EtfHoldingsBlock:
    """Cached fund composition — never triggers a market-data fetch.
    When recursive=true, fund holdings are enriched with asset_class_breakdown."""
    security = owned_or_404(Security, security_id, current_user, db)
    return _holdings_block(db, security, recursive=recursive)


# NOTE: declared BEFORE /{security_id} so "holdings" is not captured as an id.
@router.put("/{security_id}/holdings", response_model=EtfHoldingsBlock)
def update_holdings(
    security_id: int,
    payload: EtfHoldingsUpdate,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> EtfHoldingsBlock:
    """Manual bulk replace (M17): deletes only source=manual rows and
    re-inserts the payload as manual; yfinance rows survive untouched.
    Weights under 100 are legal — the residual is simply uncategorized."""
    security = owned_or_404(Security, security_id, current_user, db)
    total = sum((item.weight_pct for item in payload.items), Decimal("0"))
    if total > Decimal("100.01"):
        raise api_error(422, "Holdings weights exceed 100%", "holdings_weight_exceeded")
    for row in db.exec(
        select(EtfHolding).where(
            EtfHolding.security_id == security.id,
            EtfHolding.source == EtfHoldingSource.manual,
        )
    ).all():
        db.delete(row)
    # M154: the SAME index the provider path matches through
    # (`market_data.security_symbol_index`) — exact spelling first, then an
    # unambiguous bare stem — so a manually typed bare ticker links to a
    # suffixed security exactly like an imported one does.
    index = market_data.security_symbol_index(
        db.exec(
            select(Security).where(Security.user_id == current_user.id).order_by(Security.id)
        ).all()
    )
    for item in payload.items:
        matched = index.get(item.holding_symbol.strip().upper()) if item.holding_symbol else None
        db.add(
            EtfHolding(
                user_id=current_user.id,
                security_id=security.id,
                holding_security_id=matched.id if matched is not None else None,
                holding_symbol=item.holding_symbol,
                holding_name=item.holding_name,
                asset_class=EtfAssetClass(item.asset_class),
                region=item.region,
                country=item.country,
                sector=item.sector,
                weight_pct=item.weight_pct,
                source=EtfHoldingSource.manual,
            )
        )
    db.commit()
    queue_deepen(background_tasks, security, [item.holding_symbol for item in payload.items])
    return _holdings_block(db, security)


# NOTE: declared BEFORE /{security_id} so "holdings" is not captured as an id.
@router.post("/{security_id}/holdings/import", response_model=EtfHoldingsBlock)
def import_holdings(
    security_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
) -> EtfHoldingsBlock:
    """Provider composition import (M17 + M147): runs the provider chain
    (issuer full CSV first, Vanguard full holdings second, yfinance
    top-holdings fallback) through the
    single write path — `market_data.import_fund_composition` — which
    replaces ONLY non-manual rows (manual rows are permanent). The privacy
    guard's 403 passes through uncaught; a None fetch degrades to a 503
    envelope."""
    security = owned_or_404(Security, security_id, current_user, db)
    # M175 (docs/07 §Provider-wait ledger): this route runs the provider chain
    # INLINE (`import_fund_composition` → issuer CSV, then Vanguard, then
    # yfinance — up to four network calls) and its first act is the fetch, so
    # `owned_or_404`'s read transaction is what the request would park for the
    # whole chain. Nothing is written before the fetch: the delete+insert
    # happens after it, inside the same call, and commits itself.
    market_data.release_before_provider_wait(db, security)
    written = market_data.import_fund_composition(db, security)
    if written is None:
        raise api_error(503, "Market data unavailable", "market_data_unavailable")
    queue_deepen(
        background_tasks,
        security,
        [str(row.holding_symbol) for row in written if row.holding_symbol],
    )
    return _holdings_block(db, security)


# NOTE: declared BEFORE /{security_id} so "refresh" is not captured as an id.
@router.post("/{security_id}/refresh", response_model=SecurityOverview)
def refresh_security(
    security_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SecurityOverview:
    """Force refresh market data for this security (price + fundamentals).

    M141: this is the manual "Try refresh" — the ONE path that still probes a
    ``dead_symbol`` row (``force=True``), so a flag that Yahoo has since
    disproved can be cleared without anyone editing the database. A security
    that stays dead simply re-arms the flag on the next miss.
    """
    security = owned_or_404(Security, security_id, current_user, db)

    # M155 (docs/07 §Price-path sessions): `owned_or_404` autobegins and the
    # next act is a 365-day fetch. Nothing has been written on this request
    # session yet, so the release commits the read only — `backfill_range`'s
    # own writes still commit themselves, `ensure_fundamentals` still
    # releases (M153), and `db.commit()` below still closes the deal.
    market_data.release_before_provider_wait(db, security)

    # Force full price history backfill (not just recent price check).
    # probe_dead=True bypasses the dead_symbol gate — the manual retry is the
    # one escape hatch from the M141 flag.
    try:
        market_data.backfill_range(
            db, security, date.today() - timedelta(days=365), date.today(), probe_dead=True
        )
    except Exception:
        logging.getLogger(__name__).warning(
            "Price refresh failed for %s; continuing", security.symbol
        )

    # Re-read: backfill_range may have flipped dead_symbol on this very call.
    db.refresh(security)

    # Fundamentals: the flag may just have been set, so this read is honest.
    try:
        market_data.ensure_fundamentals(db, security, date.today())
    except Exception:
        logging.getLogger(__name__).warning(
            "Fundamentals refresh failed for %s; continuing", security.symbol
        )

    db.commit()

    # Return updated overview
    return security_overview(security_id, current_user, db)


# NOTE: declared BEFORE /{security_id} so "split-repair" is not captured as an id.
@router.post("/{security_id}/split-repair", response_model=SplitRepairRead)
def repair_security_split(
    security_id: int,
    start_date: Annotated[date, Query(description="First day of the window to re-cache")],
    end_date: Annotated[date, Query(description="Last day of the window to re-cache")],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SplitRepairRead:
    """Re-cache one security's closes for a window in the ledger's own frame (M149).

    The escape hatch for the split/cache mismatch (docs/07 §Price frame): Yahoo
    only ever serves split-ADJUSTED closes, and `POST /securities/{id}/refresh`
    writes through the seam that un-adjusts them, but a window cached BEFORE
    that seam existed keeps its wrong rows forever — the sweep inserts only
    missing dates. This re-fetches the window and rewrites the machine rows in
    place, so a position held across a split is valued at what it was actually
    worth (prod security 86 `VGT`, 8:1 on 2026-04-21: 51 shares held
    2025-11-18 → 2026-01-08 read $4.7k instead of $37.4k).

    `source=manual` rows are counted and left alone (manual wins); the M141
    verdict of the refetch is booked exactly like any other fetch. Idempotent
    by construction — it re-derives the window rather than scaling rows, so a
    second run reports `updated= 0` instead of double-scaling. Owner-scoped
    like every other securities route; the demo tenant's write guard rejects it.
    Privacy mode raises the standard 403 from the fetch seam and nothing is
    written.
    """
    security = owned_or_404(Security, security_id, current_user, db)
    result = split_repair.repair_split_window(db, security, start_date, end_date)
    logging.getLogger(__name__).info("%s", result.summary())
    return SplitRepairRead(
        security_id=result.security_id,
        symbol=result.symbol,
        start=result.start,
        end=result.end,
        fetched=result.fetched,
        splits=[SplitEventRead(date=day, ratio=str(ratio)) for day, ratio in result.splits],
        updated=result.updated,
        inserted=result.inserted,
        unchanged=result.unchanged,
        skipped_manual=result.skipped_manual,
        refused=result.refused,
    )


@router.get("/stream/prices")
async def stream_prices(
    security_ids: str = Query(..., description="Comma-separated security IDs"),
    current_user: Annotated[User, Depends(get_current_user_with_query_token)] = None,
):
    """SSE endpoint for streaming price updates every 5 seconds."""
    try:
        ids = [int(x.strip()) for x in security_ids.split(",") if x.strip()]
    except ValueError:
        raise api_error(422, "security_ids must contain integer IDs", "validation_error") from None

    return StreamingResponse(
        price_stream_generator(ids, current_user.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@router.get("/{security_id}", response_model=SecurityRead)
def get_security(
    security_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Security:
    return owned_or_404(Security, security_id, current_user, db)


@router.patch("/{security_id}", response_model=SecurityRead)
def update_security(
    security_id: int,
    payload: SecurityUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Security:
    security = owned_or_404(Security, security_id, current_user, db)
    data = payload.model_dump(exclude_unset=True)
    for field in ("symbol", "listing_currency"):
        if field in data and data[field] is not None:
            data[field] = str(data[field]).strip().upper()
    if "exchange" in data and data["exchange"] is not None:
        # Same normalization as POST: exchange is part of the unique key, so a
        # mixed-case PATCH must not be able to store `nms` next to `NMS`.
        data["exchange"] = str(data["exchange"]).strip().upper()
    for field, value in data.items():
        setattr(security, field, value)
    db.add(security)
    db.commit()
    db.refresh(security)
    return security


@router.delete("/{security_id}", status_code=204)
def delete_security(
    security_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
) -> None:
    security = owned_or_404(Security, security_id, current_user, db)
    uid = current_user.id
    # docs/29 F3: no ON DELETE rules in the schema — every referencing table is
    # pre-checked (user_id-scoped) before the delete. Watchlist rows are the
    # exception: watch semantics is "not held", so deletion of the security
    # silently clears its watchlist entries below.
    reference_checks = (
        (Trade, Trade.security_id),
        (AcbPool, AcbPool.security_id),
        (Price, Price.security_id),
        (Fundamentals, Fundamentals.security_id),
        (DividendHistory, DividendHistory.security_id),
        (DividendHistoryRefresh, DividendHistoryRefresh.security_id),
    )
    for _model, column in reference_checks:
        referenced = db.exec(
            select(column).where(column == security.id, _model.user_id == uid).limit(1)
        ).first()
        if referenced is not None:
            raise api_error(409, "Security is in use; remove references first", "security_in_use")
    # EtfHolding references the security through either FK column.
    holding_ref = db.exec(
        select(EtfHolding.id)
        .where(
            (EtfHolding.security_id == security.id)
            | (EtfHolding.holding_security_id == security.id),
            EtfHolding.user_id == uid,
        )
        .limit(1)
    ).first()
    if holding_ref is not None:
        raise api_error(409, "Security is in use; remove references first", "security_in_use")
    # Watchlist entries are cleared silently (watch semantics: "not held").
    db.exec(
        delete(WatchlistItem).where(
            WatchlistItem.security_id == security.id, WatchlistItem.user_id == uid
        )
    )
    db.delete(security)
    db.commit()

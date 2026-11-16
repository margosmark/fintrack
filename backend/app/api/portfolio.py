"""portfolio — positions, currency exposure, and price refresh (docs/05, docs/08)."""

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response
from sqlalchemy.orm import Session
from sqlmodel import Session as SQLModelSession

from app.api.deps import get_current_user, get_db, get_engine
from app.api.securities import queue_backfill
from app.core.errors import api_error
from app.models.securities import Security
from app.models.users import User
from app.schemas.accounts import PortfolioValueHistory, ValueGranularity
from app.services import (
    account_value,
    benchmarks,
    exposure_breakdown,
    market_data,
    price_refresh,
    valuation,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

#: The exchange calendar the background refresh keys off (TSX/NYSE/NASDAQ all
#: settle on US/Eastern wall-clock time).
_ET = ZoneInfo("America/New_York")


def _markets_are_open() -> bool:
    """Market-hours gate for the proactive background refresh (docs/18).

    The window itself lives in ``services.price_refresh.markets_are_open`` —
    one rule shared with the scheduled refresh worker (M187), with the clock
    read here so the request path keeps its own seam.

    Weekdays 09:30-20:00 America/New_York. Outside it no forced refresh can
    produce a bar dated *today*: weekends have no session at all, and before
    09:30 yfinance has not printed today's bar yet — a fetch in either window
    could only return the previous close, which is already cached. That is
    precisely the wasted call this gate exists to prevent, and with the
    refresh now forced (it used to sit behind a 7-day freshness window) the
    window would otherwise be polled once per positions request.

    Uses the real tz database rather than a fixed UTC offset: the old
    `6 <= (now.hour - 4) % 24 < 20` estimate drifted an hour either side of
    the DST change.
    """
    return price_refresh.markets_are_open(datetime.now(_ET))


def _run_ensure_recent_price(security: Security, *, force: bool = False) -> None:
    """BackgroundTask body — own Session (request sessions close before
    background tasks run); the caller passes the loaded Security so the task
    never re-fetches it. Failures degrade silently (docs/07): under privacy
    mode the yfinance seam guard blocks the fetch and the security simply
    keeps its cached prices.

    ``force=True`` is what makes the automatic refresh actually refresh:
    without it `PRICE_STALE_DAYS` hands back the cached close untouched
    whenever the newest row is 1-7 days old, so positions would sit a day
    behind except on the one day in eight when the window expires.

    M141: this is the market-hours AUTOMATIC fan-out, so it must honour the
    dead-symbol gate even though it asks for ``force=True`` (a freshness
    knob, not a bypass — the gate has its own ``probe_dead`` parameter). The
    row is re-loaded below so the gate reads the CURRENT flag, not the
    snapshot the request held (docs/07 §Dead symbols).
    """
    with SQLModelSession(get_engine(), expire_on_commit=False) as session:
        try:
            # Re-read: the row may have been flagged dead since the request
            # loaded it, and the flag must be honoured on the CURRENT state.
            current = session.get(Security, security.id) if security.id else None
            target = current if current is not None else security
            market_data.ensure_recent_price(session, target, date.today(), force=force)
        except Exception:
            logging.getLogger(__name__).warning(
                "Price refresh failed for %s; keeping cached prices", security.symbol
            )


def _run_ensure_fundamentals(security: Security) -> None:
    """BackgroundTask body — own Session (request sessions close before
    background tasks run); mirrors _run_ensure_recent_price. Failures
    degrade silently (docs/07): under privacy mode the seam guard blocks the
    fetch and the security keeps its cached/manual fundamentals.

    M141: re-reads the row so a security flagged dead since the request
    loaded it is served from cache instead of another 404."""
    with SQLModelSession(get_engine(), expire_on_commit=False) as session:
        try:
            current = session.get(Security, security.id) if security.id else None
            target = current if current is not None else security
            market_data.ensure_fundamentals(session, target, date.today())
        except Exception:
            logging.getLogger(__name__).warning(
                "Fundamentals refresh failed for %s; keeping cached values",
                security.symbol,
            )


@router.post("/refresh", status_code=202)
def refresh_prices(
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, int]:
    """Queue a forced refresh for every DISTINCT security held OR watched by
    the caller (W11); returns 202 {"queued": n, "skipped_dead": m}.

    Prices are queued before fundamentals: positions render from prices, and
    fundamentals is the slow seam (`Ticker.info`), so ordering lands the rows
    the user is watching first (docs/18).

    Each price task runs `queue_backfill` — the same 365-day backfill
    `POST /securities/{id}/refresh` runs. One `.history()` call either way (a
    wider window only widens the payload), and it shares the per-security
    refresh's contract instead of the `ensure_recent_price` freshness window
    that made this endpoint a no-op for a quote fetched within the last week.

    M141 (docs/07 §Dead symbols): rows already flagged ``dead_symbol`` are
    NOT queued — this sweep is the fan-out whose 404 noise the flag exists to
    stop, and re-queuing them here would make the flag decorative. They are
    counted in ``skipped_dead`` so the UI can say what it did not do instead
    of implying every row was refreshed. A dead row comes back through the
    per-security "Try refresh" (`POST /securities/{id}/refresh`), which is the
    one path that still probes it.
    """
    # The held + watched selection is ONE implementation (M187): the same
    # `price_refresh.securities_to_refresh` the scheduled worker sweeps, so
    # the two paths can never drift apart on what "refreshable" means.
    securities = price_refresh.securities_to_refresh(db, current_user.id, include_dead=True)
    live = [security for security in securities if not security.dead_symbol]
    for security in live:
        queue_backfill(background_tasks, security)
    for security in live:
        background_tasks.add_task(_run_ensure_fundamentals, security)
    return {"queued": len(live), "skipped_dead": len(securities) - len(live)}


def _positions_summary(
    db: Session, user_id: int, items: list[dict], account_id: int | None
) -> dict:
    """The M130 header card's figures, all derived from the ONE
    `compute_positions` call the table itself rendered (docs/08 §Portfolio
    summary).

    Anti-fabrication is structural, not aspirational: `total_value_cad` IS the
    sum of the rows above it (there is no second valuation pass anywhere), so
    the header can never show a number the positions table disagrees with, and
    `total_day_change_cad` is the sum of the SAME per-row `day_change_cad` the
    `Day Δ` column prints. Cash is docs/05's cash leg read at today (the chain
    `account_value_history` walks), so `portfolio_value_cad` — the headline — is
    `total + cash` server-side; the client adds nothing.
    """
    zero = Decimal("0")
    holdings = sum(
        (row["market_value_cad"] for row in items if row["market_value_cad"] is not None), zero
    )
    day_values = [row["day_change_cad"] for row in items if row["day_change_cad"] is not None]
    # Same rule as `total_unrealized_cad`: rows that cannot compute one are
    # excluded rather than counted as a zero move, and no row at all → None.
    day_change = sum(day_values, zero) if day_values else None
    # The baseline date(s) the Σ is measured from. One date → the honest
    # "vs Oct 7 close" label; several → the LATEST is named and the flag below
    # says the rest closed earlier, instead of implying one market day.
    baselines = {
        row["previous_close_date"]
        for row in items
        if row["day_change_cad"] is not None and row["previous_close_date"] is not None
    }
    day_pct: float | None = None
    if day_change is not None:
        yesterday = holdings - day_change
        if yesterday != zero:
            day_pct = round(float(day_change / yesterday) * 100, 2)

    scope = account_value.investment_accounts(db, user_id)
    if account_id is not None:
        scope = [account for account in scope if account.id == account_id]
    cash = account_value.investment_cash_cad(db, user_id, scope, datetime.now(UTC).date())
    return {
        "total_value_cad": holdings,
        "cash_cad": cash,
        "portfolio_value_cad": holdings + cash if cash is not None else None,
        "total_day_change_cad": day_change,
        "total_day_change_pct": day_pct,
        "day_change_as_of": max(baselines) if baselines else None,
        "day_change_mixed_baseline": len(baselines) > 1,
    }


@router.get("/positions", response_model=dict)
def get_positions(
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    account_id: Annotated[int | None, Query()] = None,
) -> dict:
    items = valuation.compute_positions(db, current_user.id, account_id=account_id)
    # Auto-refresh prices in background only when markets are open.
    # After hours (or on a weekend) yfinance returns the same cached data, so
    # skip the network calls entirely. Manual "Refresh prices" still works
    # anytime. `force=True` advances a close dated before today up to today —
    # without it the 7-day freshness window leaves positions a day behind for
    # most of the week. `ensure_recent_price` stops at a close already dated
    # today, so this costs at most one fetch per security per day.
    if _markets_are_open():
        # ONE selection implementation (M187), shared with the scheduled
        # worker: held + watched, staged-only rows excluded, dead symbols
        # filtered out (docs/03 staging model, docs/07 §Dead symbols).
        securities = price_refresh.securities_to_refresh(db, current_user.id)
        for security in securities:
            background_tasks.add_task(_run_ensure_recent_price, security, force=True)
            background_tasks.add_task(_run_ensure_fundamentals, security)
    # M129: the Σ row of the positions table reads THIS, server-computed the
    # same way as the per-row gains (sum of `unrealized_gain_cad`, so a row
    # whose book cost is unknown contributes nothing rather than a lie).
    gains = [row["unrealized_gain_cad"] for row in items if row["unrealized_gain_cad"] is not None]
    total_unrealized = str(valuation._cents(sum(gains, Decimal("0")))) if gains else None
    # M130: the header card's block, computed from THIS `items` list — see
    # `_positions_summary`. `total_unrealized_cad` stays a string (the Σ row
    # has always rendered it as one); the summary fields ride the same way.
    summary = _positions_summary(db, current_user.id, items, account_id)
    return {
        "items": items,
        "total": len(items),
        "total_unrealized_cad": total_unrealized,
        **summary,
    }


@router.get("/value-history", response_model=PortfolioValueHistory)
def value_history(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    granularity: ValueGranularity = "day",
    min_event_value: Annotated[Decimal | None, Query(ge=0)] = None,
) -> PortfolioValueHistory:
    """The WHOLE portfolio's value timeline + its markers (M130, docs/08
    §Portfolio value history).

    One series summed across every investment account of the caller, built by
    running M118's `account_value_history` per account and adding the buckets —
    no second valuation chain, no cache table, derived from committed rows on
    every call. Response contract mirrors `/accounts/{id}/value-history`
    (defaults `from` = the portfolio's own floor, `to` = today, `granularity=day`
    coarsened to the ~400-point roofline, `min_event_value=1000`, `from > to` →
    `422 invalid_date_range`) plus `account_ids` and the server-computed
    `stats` block the header's "over <range>" figure is rendered from.

    Read-only and derived, so the response carries
    `Cache-Control: private, max-age=60` (docs/08 §Caching).
    """
    window_to = to or datetime.now(UTC).date()
    threshold = (
        min_event_value if min_event_value is not None else account_value.DEFAULT_MIN_EVENT_VALUE
    )
    response.headers["Cache-Control"] = "private, max-age=60"
    return PortfolioValueHistory(
        **account_value.portfolio_value_history(
            db, current_user.id, from_, window_to, granularity, threshold
        )
    )


@router.get("/exposure", response_model=dict)
def get_exposure(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    account_id: Annotated[int | None, Query()] = None,
) -> dict:
    items = valuation.compute_exposure(db, current_user.id, account_id=account_id)
    return {"items": items, "total": len(items)}


@router.get("/breakdown", response_model=dict)
def get_breakdown(
    dimension: Annotated[Literal["sector", "geography", "asset_class", "company"], Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    account_id: Annotated[int | None, Query()] = None,
    drill: Annotated[
        str | None,
        Query(
            description=(
                "M169: drill ONE bucket — the exact `key` of an item in this "
                "response (e.g. `Canada`), URL-encoded. Adds a `drill` block "
                "naming the positions, funds and holdings that bucket is made "
                "of. Bucket keys are unique WITHIN a dimension, so "
                "(dimension, drill) is the address; an unknown key is a 404 "
                "envelope, never an empty drill."
            )
        ),
    ] = None,
) -> dict:
    """Look-through exposure (M18): positions distributed into sector /
    geography / asset-class buckets over cached composition + fundamentals.
    Invalid dimension → 422 envelope via the standard validation handler.

    M169: ``drill`` names one bucket key and the SAME walk then reports that
    bucket's contributors (docs/16 §Exposure drill). It is an optional
    projection of one response, not a second endpoint, so a caller that never
    passes it gets a payload byte-identical to the pre-M169 one."""
    result = exposure_breakdown.breakdown(
        db, current_user.id, dimension, account_id=account_id, drill=drill
    )
    if drill is not None and not any(item["key"] == drill for item in result["items"]):
        # The bucket does not exist in THIS dimension (a stale client, or a
        # name from another tab). An empty drill would read as "nothing holds
        # this", which is a different — and false — claim.
        raise api_error(
            404,
            f"No {dimension} bucket named {drill!r} in this breakdown.",
            "drill_bucket_not_found",
        )
    return {"dimension": dimension, **result}


# ---------------------------------------------------------------------------
# Benchmarks — compare portfolio vs market indexes
# ---------------------------------------------------------------------------


@router.get("/benchmarks")
def benchmark_comparison(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    start_date: date = Query(description="Start date"),
    end_date: date = Query(description="End date"),
    symbol: str = Query(default="SPY", description="Benchmark symbol"),
) -> dict:
    return benchmarks.compute_benchmark_comparison(
        db, current_user.id, start_date, end_date, symbol
    )

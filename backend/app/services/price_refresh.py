"""Scheduled price refresh (M187, docs/07 §Scheduled price refresh).

The request fan-out (docs/07 §Market-hours optimization) advances a close
dated *before* today, but it deliberately stops at a close already dated
today: `ensure_recent_price`'s once-per-day rule is what keeps a positions
page load from polling yfinance per security per request. The consequence is
that a held security's bar freezes at whatever the FIRST market-hours page
load of the day happened to see. This worker is the other half: an in-process
tick at fixed wall-clock times of the trading session that ADVANCES today's
close through the same seam — opt-in only, via
``ensure_recent_price(intraday_update=True)``.

Discipline (docs/07's, restated because this is a new CALLER of it):

- **The opt-in is the whole safety.** No request path passes
  ``intraday_update``; the default stays the once-per-day rule, so a page
  load still costs at most one fetch per security per calendar day.
- **Manual wins (docs/02 / docs/07 §Manual price overrides):** the advance
  may move only a ``source=yfinance`` row; a manual close dated today is
  never touched, overwritten or deleted by any tick.
- **Privacy is PER-USER here (docs/11), unlike the FX/benchmark workers.**
  ``fx_rates`` and ``benchmark_prices`` are SHARED caches with no
  ``user_id``, which is why those ticks skip entirely while ANY tenant has
  privacy mode on. ``prices`` carries ``user_id`` on every row, so this tick
  skips only the privacy-mode OWNER's securities and refreshes everyone
  else's. The gate keys on the security's OWNER — a watchlist row may name
  another tenant's security, and that tenant's symbols must never leave the
  machine either.
- **Dead symbols stay dead (M141):** ``probe_dead`` is never passed, so
  ``force``/``intraday_update`` remain freshness knobs, not gate bypasses.
- **Sessions (M155):** one session per tick; ``ensure_recent_price`` releases
  it before every provider wait, so no path here holds a transaction across
  a wait.
- **Failures are per-security and soft**: one symbol's outage logs and the
  rest still get their attempt; the loop outlives any tick (the M59
  contract).

The schedule is CLOCK-based, not interval-based: wall-clock ticks in
America/New_York anchored to the session (default
``09:35,12:00,15:00,16:15`` — one just after the open, one after the 16:00
close), so the loop sleeps until the next tick time rather than a fixed
interval. Weekends and out-of-hours ticks are skipped by the same window the
request fan-out gates on (:func:`markets_are_open`), which costs zero
provider calls.

Started from `app.main.lifespan` exactly like the M59/M150/M173/M184 workers:
one in-process task, ``price_refresh_enabled`` / ``price_refresh_times`` in
settings, and tests force it off (`PRICE_REFRESH_ENABLED=false` in conftest)
so no background tick can fetch while a fixture asserts what the cache holds.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlmodel import Session as SQLModelSession
from sqlmodel import select

from app.core.config import get_settings
from app.models.import_batches import ImportBatch
from app.models.securities import Security
from app.models.trades import Trade
from app.models.users import User
from app.models.watchlist import WatchlistItem
from app.services import market_data
from app.services.trade_filters import committed_trade_conditions

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: The exchange clock every tick is anchored to (TSX/NYSE/NASDAQ all settle
#: on US/Eastern wall-clock time). Real tz database, never a fixed offset.
ET = ZoneInfo("America/New_York")

#: A tick's datetime is minute-resolution; the loop may wake up to this long
#: after the scheduled minute and still count as "the tick" (event-loop
#: scheduling jitter, a provider wait that overran).
TICK_GRACE = timedelta(minutes=1)

#: The sleep is capped and the schedule re-read every wake, so a settings typo
#: or a clock jump can never park the worker on a stale target for days.
MAX_SLEEP_SECONDS = 3600.0

#: When the schedule itself cannot be read, wait this long before retrying.
SCHEDULE_RETRY_SECONDS = 60


def tick_times(raw: str | None = None) -> list[time]:
    """The configured wall-clock ticks in ET, parsed from ``HH:MM``.

    A malformed entry is DROPPED with a warning rather than raising: a typo
    must not kill the worker, and the remaining valid ticks are still
    honoured. An empty result is a configuration error and raises — the loop
    logs it and retries — because running no ticks at all must be loud, never
    a silently healthy-looking worker that never refreshes.
    """
    text = get_settings().price_refresh_times if raw is None else raw
    times: list[time] = []
    for chunk in text.split(","):
        value = chunk.strip()
        if not value:
            continue
        try:
            hour, minute = (int(part) for part in value.split(":"))
            times.append(time(hour, minute))
        except ValueError:
            logger.warning("price refresh: ignoring malformed tick time %r (expected HH:MM)", value)
    if not times:
        raise ValueError(f"price_refresh_times parsed to no valid ticks: {text!r}")
    return sorted(set(times))


def markets_are_open(now: datetime) -> bool:
    """Weekdays 09:30-20:00 America/New_York — the trading-session window.

    The exact window `api.portfolio._markets_are_open` gates the request
    fan-out on (docs/07 §Market-hours optimization), with the clock passed IN
    so a tick's decision is testable. Every default tick sits inside it,
    16:15 (after the close, to capture the final bar) included.

    Before 09:30 yfinance has not printed today's bar — a fetch could only
    return the previous close, which is already cached — and weekends have no
    TSX/NYSE/NASDAQ session at all, so a tick outside the window is a pure
    wasted call. The fixed-UTC-offset estimate is documented as wrong across
    DST; this reads the real tz database.
    """
    if now.weekday() >= 5:  # Saturday / Sunday — no TSX, NYSE or NASDAQ session
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 20 * 60


def next_tick(
    now: datetime, times: Iterable[time], *, last_fired: datetime | None = None
) -> datetime:
    """The earliest configured tick the loop has not fired yet, in ET.

    Candidates are today's ticks then the next days', each built as a
    WALL-CLOCK datetime in America/New_York, so DST is the tz database's
    problem and not arithmetic here. Two candidates are skipped:

    - one whose minute has already fully elapsed at ``now`` — it was fired
      already, or missed while the process was down, and re-firing it now
      could only fetch a bar that has not moved (``last_fired`` additionally
      makes the just-fired tick ineligible, so the loop can never fire the
      same tick twice);
    - one :func:`markets_are_open` would reject — a weekend day, or a
      configured time outside the session. Such a tick cannot produce a bar
      dated today, so waking for it would be a guaranteed no-op; the week is
      scanned so a Saturday morning lands on Monday's first tick instead of
      raising.
    """
    today = now.astimezone(ET).date()
    for day_offset in range(8):  # today + a full weekend + slack
        day = today + timedelta(days=day_offset)
        for tick in sorted(times):
            candidate = datetime(day.year, day.month, day.day, tick.hour, tick.minute, tzinfo=ET)
            if last_fired is not None and candidate <= last_fired:
                continue
            if candidate + TICK_GRACE <= now:
                continue
            if not markets_are_open(candidate):
                continue
            return candidate
    raise ValueError(
        f"no session-anchored tick in the next week — check price_refresh_times ({times!r})"
    )


@dataclass
class PriceRefreshStats:
    """What ONE tick did — the unit a test asserts and the log line reports."""

    #: Symbols a fetch was attempted for (whatever its outcome).
    refreshed: list[str] = field(default_factory=list)
    #: Symbols whose cached close actually moved (inserted or advanced).
    updated: list[str] = field(default_factory=list)
    #: Symbols whose fetch failed; the tick continued past them.
    failed: list[str] = field(default_factory=list)
    #: Symbols NOT requested because their owner has privacy_mode on (docs/11).
    privacy_skipped: list[str] = field(default_factory=list)
    #: Symbols NOT probed because they carry `dead_symbol` (M141).
    dead_skipped: list[str] = field(default_factory=list)
    #: Set when the tick was a no-op outside the trading session.
    skipped: str | None = None


def held_security_ids(db: Session, user_id: int) -> set[int]:
    """Distinct securities with COMMITTED (or manual) trades.

    A security held only by a staged import batch is not held yet, so it must
    not be price-refreshed or streamed for a position the user cannot see
    (docs/03 staging model).
    """
    return set(
        db.exec(
            select(Trade.security_id)
            .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
            .where(*committed_trade_conditions(user_id))
        ).all()
    )


def securities_to_refresh(
    db: Session, user_id: int, *, include_dead: bool = False
) -> list[Security]:
    """The held + watched selection BOTH price-refresh paths use (docs/05/18).

    One implementation behind the request fan-out (`GET /portfolio/positions`,
    `POST /portfolio/refresh`) and this worker: a staged-only security is not
    held, a ``dead_symbol`` row is excluded unless the caller counts it, and
    the order is stable by id. ``db`` only needs ``.exec`` — a SQLModel
    session, which is what both the request path and the worker hold.
    """
    held = held_security_ids(db, user_id)
    watched = set(
        db.exec(select(WatchlistItem.security_id).where(WatchlistItem.user_id == user_id)).all()
    )
    securities = db.exec(
        select(Security).where(Security.id.in_(sorted(held | watched))).order_by(Security.id)
    ).all()
    if include_dead:
        return list(securities)
    return [security for security in securities if not security.dead_symbol]


def _newest_pair(session: Session, security_id: int, today: date) -> tuple[date, Decimal] | None:
    """(date, price) of the newest cached close <= today — a cache-only read."""
    row = market_data.newest_price(session, security_id, today)
    return None if row is None else (row.date, row.price)


def run_once(session: Session, *, now: datetime | None = None) -> PriceRefreshStats:
    """One intraday refresh pass over every live, non-privacy held/watched row.

    Never raises on a symbol failure (the M59 contract), and makes ZERO
    provider calls outside the trading session. ``now`` is passed in tests so
    a tick's gate is deterministic.
    """
    now = now or datetime.now(ET)
    stats = PriceRefreshStats()
    if not markets_are_open(now):
        stats.skipped = "markets_closed"
        return stats

    today = now.astimezone(ET).date()
    privacy_by_user = {
        user.id: bool(user.privacy_mode) for user in session.exec(select(User)).all()
    }

    # One pass over every tenant's selection. A security held or watched by
    # several users is refreshed ONCE — the price row keys on its OWNER.
    candidates: dict[int, Security] = {}
    for user_id in privacy_by_user:
        for security in securities_to_refresh(session, user_id, include_dead=True):
            candidates.setdefault(security.id, security)

    for security in candidates.values():
        if security.dead_symbol:
            stats.dead_skipped.append(security.symbol)
            continue
        owner_is_private = privacy_by_user.get(security.user_id, False)
        if owner_is_private:
            # docs/11: a privacy-mode user's SYMBOLS must never be requested.
            # Skipped BEFORE any seam is reached, so the guard never has to
            # catch it — and only this owner's rows are skipped, not the tick.
            stats.privacy_skipped.append(security.symbol)
            continue
        # Belt and braces: bind the OWNER's flag to this context anyway, so
        # the seam's own `_guard_external_fetch` would refuse the fetch even
        # if a future caller forgot the check above.
        market_data.bind_privacy_lock(owner_is_private)
        before = _newest_pair(session, security.id, today)
        stats.refreshed.append(security.symbol)
        try:
            market_data.ensure_recent_price(
                session,
                security,
                today,
                force=True,
                # The whole point of the worker — and the ONLY path that
                # passes it (docs/07 §Scheduled price refresh).
                intraday_update=True,
            )
        except Exception:  # one symbol's outage is not the tick's
            stats.failed.append(security.symbol)
            logger.warning(
                "price refresh: %s failed — keeping cached prices",
                security.symbol,
                exc_info=True,
            )
            continue
        if _newest_pair(session, security.id, today) != before:
            stats.updated.append(security.symbol)

    if stats.refreshed or stats.privacy_skipped or stats.dead_skipped:
        logger.info(
            "price refresh: %d advanced (%s); failed: %s; privacy-skipped: %s; dead-skipped: %d",
            len(stats.updated),
            ", ".join(stats.updated) or "none",
            ", ".join(stats.failed) or "none",
            ", ".join(stats.privacy_skipped) or "none",
            len(stats.dead_skipped),
        )
    return stats


# --- worker (the M59 shape, docs/37 §The worker) -------------------------------


def _open_session() -> SQLModelSession:
    """The worker's own DB session — MUST be SQLModel's (it uses `.exec()`)."""
    from app.api.deps import get_engine

    return SQLModelSession(get_engine(), expire_on_commit=False)


def _run_once_thread() -> None:
    with _open_session() as session:
        run_once(session)


async def worker_loop() -> None:
    """Sleeps until the next configured tick, runs it, repeats (M59 contract).

    Clock-based rather than interval-based: the target is recomputed from the
    real clock every wake, so drift is impossible and a tick that overran its
    slot cannot skip the NEXT one — `last_fired` only ever prevents the SAME
    tick from firing twice. The capped sleep means a long wait (overnight,
    weekend) is slept in ≤1 h slices and re-aimed, not accumulated.
    """
    last_fired: datetime | None = None
    while True:
        try:
            now = datetime.now(ET)
            target = next_tick(now, tick_times(), last_fired=last_fired)
            delay = (target - now).total_seconds()
        except Exception:  # a bad schedule must not kill the loop
            logger.exception(
                "price refresh: schedule unreadable; retrying in %ds", SCHEDULE_RETRY_SECONDS
            )
            await asyncio.sleep(SCHEDULE_RETRY_SECONDS)
            continue
        await asyncio.sleep(min(max(delay, 0.0), MAX_SLEEP_SECONDS))
        if datetime.now(ET) < target:
            continue  # capped slice of a long wait — re-aim at the same target
        last_fired = target
        try:
            # to_thread: a yfinance call must not block the event loop.
            await asyncio.to_thread(_run_once_thread)
        except Exception:  # the loop must outlive any tick
            logger.exception("price refresh tick failed")


def start_worker() -> asyncio.Task | None:
    """Started from `lifespan` only when PRICE_REFRESH_ENABLED (default true)."""
    if not get_settings().price_refresh_enabled:
        return None
    return asyncio.create_task(worker_loop())


async def stop_worker(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "ET",
    "SCHEDULE_RETRY_SECONDS",
    "TICK_GRACE",
    "PriceRefreshStats",
    "held_security_ids",
    "markets_are_open",
    "next_tick",
    "run_once",
    "securities_to_refresh",
    "start_worker",
    "stop_worker",
    "tick_times",
    "worker_loop",
]

"""Scheduled benchmark-series refresh (M173, docs/07 §Benchmark series).

The vs-Benchmark panel reads ``benchmark_prices`` and never fetches (docs/28),
so SOMETHING has to keep the four series current. This worker is that
something: on each tick it fetches only the benchmarks whose newest cached
close is missing or older than :data:`REFRESH_MIN_AGE_DAYS`, through
``services/benchmarks.py::backfill_all`` — which owns the venue table, the
window and the store path.

Discipline (docs/07's, restated because this is a new CALLER of it):

- **Privacy (docs/11): the whole tick is skipped while ANY user has
  ``privacy_mode`` on.** ``benchmark_prices`` is a SHARED cache with no
  ``user_id``, so a per-tenant flag cannot be honoured symbol-by-symbol — the
  promise is that nothing leaves the machine, and the honest staleness the
  panel renders is what keeps that visible instead of silent. The decision
  lives in ``benchmarks.privacy_blocks`` (one home for both this worker and
  the operator script).
- **A current series costs zero network calls.** A benchmark is fetched only
  when its newest row is older than the min-age window, so a 6-hour tick that
  finds yesterday's close does nothing.
- **The store path writes EMPTY dates only** (``benchmarks._cache_closes``),
  so a re-run adds nothing and a manual row is never overwritten.
- **Failures are per-symbol and soft**: one symbol's outage logs and the other
  three still get their attempt; the loop outlives any tick (the M59
  contract).

Started from `app.main.lifespan` exactly like the M59/M150 workers: one
in-process task, ``benchmark_refresh_enabled`` /
``benchmark_refresh_interval_seconds`` in settings, and tests force it off
(``BENCHMARK_REFRESH_ENABLED=false`` in conftest) so no background tick can
fetch while a fixture asserts what the cache holds.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlmodel import Session as SQLModelSession  # has .exec — the worker must use this one

from app.core.config import get_settings
from app.services import benchmarks

logger = logging.getLogger(__name__)

#: A symbol is refreshed only when its newest cached close is older than this
#: (or missing entirely). Exchanges publish one bar per trading day, so this is
#: the difference between "yesterday's close" and "the series is behind".
REFRESH_MIN_AGE_DAYS = 1


@dataclass
class BenchmarkRefreshStats:
    """What ONE tick did — the unit a test asserts and the log line reports."""

    #: Symbols a fetch was attempted for (whatever its outcome).
    refreshed: list[str] = field(default_factory=list)
    #: Symbols whose fetch landed closes.
    updated: list[str] = field(default_factory=list)
    #: Symbols already inside the min-age window — deliberately not fetched.
    current: list[str] = field(default_factory=list)
    #: Symbols whose fetch failed; the tick continued past them.
    failed: list[str] = field(default_factory=list)
    rows_added: int = 0
    #: A privacy-mode user is present, so the tick made zero packets.
    privacy_blocked: bool = False


def symbols_behind(
    session: SQLModelSession, today: date, *, min_age_days: int = REFRESH_MIN_AGE_DAYS
) -> list[str]:
    """Benchmarks whose newest cached close is missing or older than min_age_days.

    A pure cache read — deciding whether to fetch must never itself fetch.
    """
    behind: list[str] = []
    for symbol in benchmarks.BENCHMARK_SYMBOLS:
        newest = benchmarks.newest_cached_date(session, symbol)
        if newest is None or (today - newest).days > min_age_days:
            behind.append(symbol)
    return behind


def run_once(session: SQLModelSession, today: date | None = None) -> BenchmarkRefreshStats:
    """One refresh pass over the four picks. Never raises on a symbol failure."""
    today = today or datetime.now(UTC).date()
    stats = BenchmarkRefreshStats()

    behind = symbols_behind(session, today)
    behind_set = set(behind)
    stats.current = [s for s in benchmarks.BENCHMARK_SYMBOLS if s not in behind_set]
    if not behind:
        return stats

    results = benchmarks.backfill_all(session, today=today, symbols=behind)
    if results and all(result.skipped == "privacy_blocked" for result in results):
        stats.privacy_blocked = True
        logger.info("benchmark refresh: skipped — a user has privacy_mode on (docs/11)")
        return stats

    for result in results:
        stats.refreshed.append(result.symbol)
        stats.rows_added += result.inserted
        if result.skipped is not None:
            stats.failed.append(result.symbol)
            logger.warning("benchmark refresh: %s failed — %s", result.symbol, result.describe())
        elif result.inserted:
            stats.updated.append(result.symbol)

    if stats.rows_added:
        logger.info(
            "benchmark refresh: %d row(s) cached for %s (%d already current; failed: %s)",
            stats.rows_added,
            ", ".join(stats.updated) or "none",
            len(stats.current),
            ", ".join(stats.failed) or "none",
        )
    return stats


# --- worker (the M59 shape, docs/37 §The worker) -------------------------------


def _open_session() -> SQLModelSession:
    """The worker's own DB session — MUST be SQLModel's (it uses `.exec()`)."""
    from app.api.deps import get_engine

    return SQLModelSession(get_engine())


def _run_once_thread() -> None:
    with _open_session() as session:
        run_once(session)


async def worker_loop() -> None:
    """Ticks until cancelled. Errors are logged, never fatal (docs/37)."""
    while True:
        settings = get_settings()
        try:
            # to_thread: a yfinance call must not block the event loop.
            await asyncio.to_thread(_run_once_thread)
        except Exception:  # the loop must outlive any tick
            logger.exception("benchmark refresh tick failed")
        await asyncio.sleep(max(int(settings.benchmark_refresh_interval_seconds), 1))


def start_worker() -> asyncio.Task | None:
    """Started from `lifespan` only when BENCHMARK_REFRESH_ENABLED (default true)."""
    if not get_settings().benchmark_refresh_enabled:
        return None
    return asyncio.create_task(worker_loop())


async def stop_worker(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "REFRESH_MIN_AGE_DAYS",
    "BenchmarkRefreshStats",
    "run_once",
    "start_worker",
    "stop_worker",
    "symbols_behind",
    "worker_loop",
]

"""Scheduled FX rate-book refresh (M150, docs/07 §Scheduled refresh).

The cache used to fill itself on a TRUE miss only — `fx.latest_rate` fetches
when nothing exists on or before the target date, and `fx.fx_rate_cad` never
fetches at all — and the ≤7-day carry-forward (M115) means a book that stopped
being fetched still *resolves* every date: Wednesday's USD cash leg reads
Tuesday's rate, nothing errors, nothing logs. The gap re-prices silently.

This worker closes that hole by keeping the book current on a timer, so the
carry-forward window is used for what it was built for (BoC's publication
calendar — weekends, holidays) and for nothing else.

Discipline (all of it docs/07's, restated because this is a new CALLER of them):

- The pair list is `fx.CURATED_FX_BASES` — a STATIC constant, never derived
  from tenant rows — so a tick's egress carries no tenant data (docs/11).
  Membership is "BoC publishes a live `FX{base}CAD` series for it" (M151,
  docs/07 §Curated board membership), which is why a base that can never
  answer is not in the list at all: `pairs_behind` therefore never names a
  pair whose only possible outcome is a 404, and the loop spends one call
  per REAL gap and none on a series that does not exist.
- The network call is `fx.refresh_pair`, which lives in the fx module: this
  file decides WHICH pair and WHEN, and never talks to Valet itself (AGENTS
  §2 keeps every outbound call behind the two market-data seams).
- A pair is fetched only when its newest cached row is missing or older than
  `REFRESH_MIN_AGE_DAYS`; a current book costs zero network calls. BoC
  publishes once a business day, so a 6-hour tick finds a same-day row and
  does nothing.
- The store path writes EMPTY dates only, so a tick can never overwrite a
  BoC row or a manual gap-fill, and re-running a cached window adds nothing.
- Failures are per-pair and soft: one currency's outage logs and the others
  still get their attempt; the loop outlives any tick (the M59 contract).
- Privacy (docs/11): the tick is skipped entirely while ANY user has
  `privacy_mode` on. `fx_rates` is shared with no `user_id`, so a per-user
  flag cannot be honoured pair-by-pair, and the promise is that nothing
  leaves the machine — privacy wins, and the M150 staleness chip
  (`GET /fx/status`) is what makes the resulting staleness VISIBLE instead
  of silent.

Started from `lifespan` (`app.main`) exactly like the M59 delivery worker:
one in-process task, `fx_refresh_enabled` / `fx_refresh_interval_seconds` in
settings, and tests force it off (`FX_REFRESH_ENABLED=false` in conftest) so
no background tick can race a fixture.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlmodel import Session as SQLModelSession  # has .exec — the worker must use this one
from sqlmodel import select

from app.core.config import get_settings
from app.models.users import User
from app.services import fx

logger = logging.getLogger(__name__)

#: A pair is refreshed only when its newest cached row is older than this (or
#: missing entirely). BoC publishes one observation per business day, so this
#: is the difference between "yesterday's rate" and "the book is behind".
REFRESH_MIN_AGE_DAYS = 1


@dataclass
class RefreshStats:
    """What ONE tick did — the unit a test asserts and the log line reports."""

    #: Pairs a fetch was attempted for and landed.
    refreshed: list[str] = field(default_factory=list)
    #: Pairs already inside the min-age window — deliberately not fetched.
    current: list[str] = field(default_factory=list)
    #: Pairs whose fetch failed (ValetUnavailable); the tick continued past them.
    failed: list[str] = field(default_factory=list)
    rows_added: int = 0
    #: A privacy-mode user is present, so the tick made zero packets.
    privacy_blocked: bool = False


def pairs_behind(
    session: SQLModelSession, today: date, *, min_age_days: int = REFRESH_MIN_AGE_DAYS
) -> list[str]:
    """Curated bases whose newest cached row is missing or older than min_age_days.

    A pure cache read — deciding whether to fetch must never itself fetch.

    The iteration set IS the guarantee (M151): because membership of
    ``fx.CURATED_FX_BASES`` requires a live BoC series, a base that can only
    ever answer 404 is never named here, so the caller cannot spend a tick's
    call on it. ``DKK`` is the worked example (docs/07 §Curated board
    membership) — it is absent from the constant, not special-cased here,
    which keeps this function a pure "which of the board's pairs are behind"
    read with no unavailability state to carry.
    """
    behind: list[str] = []
    for base in fx.CURATED_FX_BASES:
        newest = fx.newest_cached_date(session, base, "CAD")
        if newest is None or (today - newest).days > min_age_days:
            behind.append(base)
    return behind


def privacy_blocks(session: SQLModelSession) -> bool:
    """True when ANY user has privacy_mode on — the tick then sends nothing.

    `fx_rates` is a shared cache with no `user_id` (docs/02), so there is no
    honest per-pair answer here: the fetch either leaves the machine or it
    does not. docs/11's promise wins over freshness.
    """
    row = session.exec(select(User.id).where(User.privacy_mode.is_(True)).limit(1)).first()  # type: ignore[arg-type]
    return row is not None


def run_once(session: SQLModelSession, today: date | None = None) -> RefreshStats:
    """One refresh pass over the curated board. Never raises on a pair failure."""
    today = today or datetime.now(UTC).date()
    stats = RefreshStats()

    if privacy_blocks(session):
        stats.privacy_blocked = True
        logger.info("fx refresh: skipped — a user has privacy_mode on (docs/11)")
        return stats

    behind = pairs_behind(session, today)
    behind_set = set(behind)
    stats.current = [base for base in fx.CURATED_FX_BASES if base not in behind_set]

    # M175 (docs/07 §Provider-wait ledger): `privacy_blocks` and `pairs_behind`
    # READ above, and the loop below is a provider wait PER PAIR (`refresh_pair`
    # → a Valet series call, 10 s x 3 retries). Without the release the worker's
    # one session carried an open transaction across the first fetch — and
    # across every pair whose window turned out to add no rows, since
    # `_store_fetched` only commits when it wrote something.
    fx.release_before_provider_wait(session)
    for base in behind:
        try:
            stats.rows_added += fx.refresh_pair(session, base, "CAD", today)
        except fx.ValetUnavailable as exc:
            stats.failed.append(base)
            logger.warning("fx refresh: %s/CAD failed — %s", base, exc)
        else:
            stats.refreshed.append(base)

    if stats.rows_added:
        logger.info(
            "fx refresh: %d row(s) cached for %s (%d pair(s) already current; failed: %s)",
            stats.rows_added,
            ", ".join(f"{base}/CAD" for base in stats.refreshed),
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
            # to_thread: a BoC HTTP call must not block the event loop.
            await asyncio.to_thread(_run_once_thread)
        except Exception:  # the loop must outlive any tick
            logger.exception("fx refresh tick failed")
        await asyncio.sleep(max(int(settings.fx_refresh_interval_seconds), 1))


def start_worker() -> asyncio.Task | None:
    """Started from `lifespan` only when FX_REFRESH_ENABLED (default true).

    Tests force it off so no background tick can fetch while a fixture asserts
    what the cache holds.
    """
    if not get_settings().fx_refresh_enabled:
        return None
    return asyncio.create_task(worker_loop())


async def stop_worker(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "REFRESH_MIN_AGE_DAYS",
    "RefreshStats",
    "pairs_behind",
    "privacy_blocks",
    "run_once",
    "start_worker",
    "stop_worker",
    "worker_loop",
]

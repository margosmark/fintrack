"""Tenant-scoped, cache-first dividend history refresh (docs/07)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from app.api.deps import get_engine
from app.core.config import get_settings
from app.models.dividend_history import (
    DividendHistory,
    DividendHistoryRefresh,
    DividendHistoryState,
)
from app.models.securities import Security
from app.models.users import User
from app.services import market_data

logger = logging.getLogger(__name__)
REFRESH_AGE = timedelta(hours=24)
RETRY_AGE = timedelta(minutes=15)
# M186 bounded retry (docs/07 §M184): a failure streak backs off
# 15m → 30m → 1h → … capped at 6h. The cap is the bound — a symbol that stops
# resolving is retried four times a day instead of every 15 minutes forever,
# and `securities.dead_symbol` (three hard misses) still stops the fan-out
# entirely. Recovery is one success: `consecutive_failures` resets to 0.
MAX_RETRY_AGE = timedelta(hours=6)
# 15m * 2**7 = 32h > MAX_RETRY_AGE, so any streak ≥ 7 saturates at the cap;
# the clamp keeps a pathological counter from overflowing timedelta math.
_MAX_BACKOFF_DOUBLINGS = 7
STALE_JOB_AGE = timedelta(minutes=5)
WORKER_INTERVAL_SECONDS = 2
MAX_JOBS_PER_TICK = 8


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def retry_age(consecutive_failures: int | None) -> timedelta:
    """Backoff before the next attempt after ``consecutive_failures`` misses.

    ``0`` (or a never-failed row) stays at the flat :data:`RETRY_AGE`; each
    consecutive failure doubles it, capped at :data:`MAX_RETRY_AGE`.
    """
    failures = max(0, consecutive_failures or 0)
    if failures == 0:
        return RETRY_AGE
    # Clamp the exponent BEFORE multiplying: `timedelta * 2**10_000` raises
    # OverflowError long before the min() below could cap it.
    doubling = min(failures, _MAX_BACKOFF_DOUBLINGS)
    return min(RETRY_AGE * (2**doubling), MAX_RETRY_AGE)


def enqueue_due(
    session: Session, user_id: int, security_ids: set[int]
) -> dict[int, DividendHistoryRefresh]:
    """Batch-persist due jobs, single-flight per tenant/security, without provider waits."""
    if not security_ids:
        return {}
    now = _now()
    current = session.exec(
        select(DividendHistoryRefresh).where(
            DividendHistoryRefresh.user_id == user_id,
            DividendHistoryRefresh.security_id.in_(security_ids),
        )
    ).all()
    by_security = {row.security_id: row for row in current}
    missing = security_ids - by_security.keys()
    if missing:
        session.exec(
            pg_insert(DividendHistoryRefresh)
            .values(
                [
                    {
                        "user_id": user_id,
                        "security_id": security_id,
                        "last_attempt_at": now,
                        "state": DividendHistoryState.pending,
                    }
                    for security_id in sorted(missing)
                ]
            )
            .on_conflict_do_nothing(index_elements=["user_id", "security_id"])
        )
    due_ids = [
        security_id
        for security_id, row in by_security.items()
        if row.state not in (DividendHistoryState.pending, DividendHistoryState.refreshing)
        and (row.last_success_at is None or row.last_success_at < now - REFRESH_AGE)
        and (
            row.last_attempt_at is None
            or row.last_attempt_at < now - retry_age(row.consecutive_failures)
        )
    ]
    if due_ids:
        session.exec(
            update(DividendHistoryRefresh)
            .where(
                DividendHistoryRefresh.user_id == user_id,
                DividendHistoryRefresh.security_id.in_(due_ids),
                ~DividendHistoryRefresh.__table__.c.state.in_(
                    (DividendHistoryState.pending, DividendHistoryState.refreshing)
                ),
            )
            .values(
                last_attempt_at=now,
                state=DividendHistoryState.pending,
                error_code=None,
            )
        )
    session.flush()
    refreshed = session.exec(
        select(DividendHistoryRefresh).where(
            DividendHistoryRefresh.user_id == user_id,
            DividendHistoryRefresh.security_id.in_(security_ids),
        )
    ).all()
    return {row.security_id: row for row in refreshed}


def refresh_one(user_id: int, security_id: int) -> bool:
    """Fetch and persist one history in a fresh session; return success status."""
    with Session(get_engine()) as session:
        refresh = session.exec(
            select(DividendHistoryRefresh)
            .where(
                DividendHistoryRefresh.user_id == user_id,
                DividendHistoryRefresh.security_id == security_id,
            )
            .with_for_update(skip_locked=True)
        ).first()
        if refresh is None or refresh.state == DividendHistoryState.refreshing:
            return False
        user = session.get(User, user_id)
        security = session.get(Security, security_id)
        if user is None or user.privacy_mode or security is None or security.user_id != user_id:
            refresh.state = DividendHistoryState.unavailable
            refresh.error_code = "privacy_or_ownership"
            session.add(refresh)
            session.commit()
            return False
        if security.dead_symbol:
            refresh.state = DividendHistoryState.unavailable
            refresh.error_code = "dead_symbol"
            # M186: a flag that will not clear by itself still gets the bounded
            # backoff, so the worker stops re-poking a dead row every 15 min.
            refresh.consecutive_failures = (refresh.consecutive_failures or 0) + 1
            session.add(refresh)
            session.commit()
            return False
        symbol = security.symbol
        # Close the read transaction before provider I/O. Tenant ownership and
        # persisted privacy_mode were checked immediately above.
        refresh.state = DividendHistoryState.refreshing
        refresh.last_attempt_at = _now()
        session.add(refresh)
        session.commit()

    payload_error: str | None = None
    quote_outcome = market_data.QuoteOutcome.unknown
    quote_reason: str | None = None
    try:
        market_data.reset_quote_verdict()
        history = market_data.fetch_dividend_history(symbol)
        quote_outcome, quote_reason = market_data.last_quote_verdict()
    except Exception:  # provider failures preserve last-good rows
        logger.warning("Dividend history provider failed for security %s", security_id)
        history = None
        normalized = None
        payload_error = "provider_unavailable"
    else:
        normalized = None
        if history is not None:
            try:
                normalized = []
                for item in history:
                    ex_date = date.fromisoformat(str(item["date"]))
                    amount = Decimal(str(item["amount"]))
                    if not amount.is_finite() or amount <= 0:
                        raise ValueError("dividend amount must be finite and positive")
                    normalized.append((ex_date, amount))
            except Exception:  # malformed provider data is not a successful empty
                logger.warning("Dividend history payload invalid for security %s", security_id)
                history = None
                normalized = None
                payload_error = "invalid_provider_payload"
    if history is None and payload_error is None:
        payload_error = "provider_unavailable"
    with Session(get_engine()) as session:
        refresh = session.exec(
            select(DividendHistoryRefresh).where(
                DividendHistoryRefresh.user_id == user_id,
                DividendHistoryRefresh.security_id == security_id,
            )
        ).first()
        security = session.exec(
            select(Security).where(Security.id == security_id, Security.user_id == user_id)
        ).first()
        user = session.get(User, user_id)
        if refresh is None or security is None:
            session.rollback()
            return False
        if user is None or user.privacy_mode:
            refresh.state = DividendHistoryState.unavailable
            refresh.error_code = "privacy_or_ownership"
            session.add(refresh)
            session.commit()
            return False
        if history is None:
            market_data.record_quote_outcome(session, security, quote_outcome, quote_reason)
            refresh.state = DividendHistoryState.unavailable
            refresh.error_code = payload_error or "provider_unavailable"
            if quote_outcome is market_data.QuoteOutcome.not_found:
                # M186 observability: a hard Yahoo miss is a verdict about the
                # SYMBOL (docs/07 §Dead symbols), not the network — surface it
                # distinctly so the UI can say "possibly delisted" instead of
                # a generic "unavailable" that implies retry will help.
                refresh.error_code = "quote_not_found"
            refresh.consecutive_failures = (refresh.consecutive_failures or 0) + 1
            session.add(refresh)
            session.commit()
            return False
        # Re-check ownership/privacy after the network wait; no stale write may
        # cross a tenant deletion or privacy toggle.
        session.exec(
            delete(DividendHistory).where(
                DividendHistory.user_id == user_id,
                DividendHistory.security_id == security_id,
                DividendHistory.source == "yfinance",
            )
        )
        market_data.record_quote_outcome(session, security, quote_outcome, quote_reason)
        for provider_order, (ex_date, amount) in enumerate(normalized or []):
            session.add(
                DividendHistory(
                    user_id=user_id,
                    security_id=security_id,
                    ex_date=ex_date,
                    amount=amount,
                    provider_order=provider_order,
                    source="yfinance",
                )
            )
        refresh.last_success_at = _now()
        refresh.error_code = None
        # M186: one success ends the failure streak — recovery returns the row
        # to the flat 15-minute cadence immediately, not the capped backoff.
        refresh.consecutive_failures = 0
        refresh.state = DividendHistoryState.available if history else DividendHistoryState.empty
        session.add(refresh)
        session.commit()
        return True


def _run_pending_batch() -> int:
    with Session(get_engine()) as session:
        session.exec(
            update(DividendHistoryRefresh)
            .where(
                DividendHistoryRefresh.state == DividendHistoryState.refreshing,
                DividendHistoryRefresh.__table__.c.last_attempt_at.is_not(None),
                DividendHistoryRefresh.last_attempt_at < _now() - STALE_JOB_AGE,
            )
            .values(state=DividendHistoryState.pending)
        )
        session.commit()
        keys = session.exec(
            select(DividendHistoryRefresh.user_id, DividendHistoryRefresh.security_id)
            .where(DividendHistoryRefresh.state == DividendHistoryState.pending)
            .order_by(DividendHistoryRefresh.last_attempt_at)
            .limit(MAX_JOBS_PER_TICK)
        ).all()
    processed = 0
    for user_id, security_id in keys:
        try:
            processed += int(refresh_one(user_id, security_id))
        except Exception:  # one bad record must not kill worker
            logger.exception("Dividend refresh worker failed for security %s", security_id)
    return processed


async def worker_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_run_pending_batch)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Dividend history worker tick failed")
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)


def start_worker() -> asyncio.Task | None:
    if not get_settings().dividend_refresh_enabled:
        return None
    return asyncio.create_task(worker_loop())


async def stop_worker(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

"""SSE price-stream generator, extracted from ``securities.py`` for leanness.

Every tick is a cache-only read (docs/18, docs/29 F5): the newest usable
``Price`` row on or before today per requested security in a single DB query —
no yfinance, no network, no market-hours logic on the stream path. The
request-scoped privacy lockdown
(docs/11 step 6) closes the stream immediately when the requesting user has
``privacy_mode`` enabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from datetime import date

from sqlalchemy import func
from sqlmodel import Session as SQLModelSession
from sqlmodel import select

from app.api.deps import get_engine
from app.models.prices import Price
from app.models.securities import Security
from app.services.market_data import PRICE_STALE_DAYS, _privacy_lock

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5


def _latest_cached_prices(db: SQLModelSession, security_ids: list[int], user_id: int) -> list[dict]:
    """Newest usable close on or before today per security in ONE query.

    Joins ``Security`` for the symbol in the same round-trip; every row is
    scoped to the requesting user (``Price.user_id`` + ``Security.user_id``).
    Future-dated rows are never selected or valued as current. Securities with
    no usable cached close are omitted.
    """
    today = date.today()
    latest_date = (
        select(Price.security_id, func.max(Price.date).label("latest_date"))
        .where(
            Price.security_id.in_(security_ids),
            Price.user_id == user_id,
            Price.date <= today,
        )
        .group_by(Price.security_id)
        .subquery()
    )
    rows = db.exec(
        select(Price.security_id, Security.symbol, Price.price, Price.date, Price.source)
        .join(
            latest_date,
            (Price.security_id == latest_date.c.security_id)
            & (Price.date == latest_date.c.latest_date),
        )
        .join(Security, Security.id == Price.security_id)
        .where(
            Price.user_id == user_id,
            Security.user_id == user_id,
            Price.date <= today,
        )
    ).all()
    return [
        {
            "security_id": security_id,
            "symbol": symbol,
            "price": str(price),
            "date": price_date.isoformat(),
            "source": source.value,
            "age_days": (today - price_date).days,
            "stale": (today - price_date).days > PRICE_STALE_DAYS,
        }
        for security_id, symbol, price, price_date, source in rows
    ]


async def price_stream_generator(
    security_ids: list[int], user_id: int
) -> AsyncGenerator[str, None]:
    """Yield SSE price-update events every ``POLL_INTERVAL_SECONDS``.

    Each tick reads only the cached ``prices`` table — the stream never
    triggers an external fetch (docs/18 decision "cache-only prices on the
    stream"). Under privacy mode the stream closes immediately with no
    network access at all.
    """
    # Privacy lockdown (docs/11 step 6): never open an external network seam
    # from the endpoint. The request-scoped lock is read here.
    if _privacy_lock.get():
        logger.info("Closing price stream: privacy_mode blocks external fetches")
        return
    while True:
        # M148 (docs/01 § "Sessions and the event loop"): the session lives
        # only as long as the two queries. The `await` and the `yield` below
        # are OUTSIDE the `with`, because a session that spans them holds a
        # pooled connection `idle in transaction` for the whole 5 s tick —
        # and this stream is open for as long as a tab is. One connection per
        # open stream is 15 open streams = the entire pool, which is the same
        # exhaustion that took the API down on 2026-10-11.
        price_updates: list[dict] = []
        try:
            with SQLModelSession(get_engine()) as db:
                securities = db.exec(
                    select(Security).where(
                        Security.id.in_(security_ids),
                        Security.user_id == user_id,
                    )
                ).all()
                if securities:
                    price_updates = _latest_cached_prices(
                        db, [security.id for security in securities], user_id
                    )
        except Exception as e:  # keep the stream alive on errors
            logger.warning(f"SSE stream error: {e}")
        if price_updates:
            yield f"data: {json.dumps(price_updates)}\n\n"
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

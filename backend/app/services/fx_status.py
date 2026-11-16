"""Rate-book status — the read behind the M150 staleness chip (docs/07).

**Why this exists.** Every exact-date FX consumer resolves in two steps (the
date's row, else the newest published row within `fx.CARRY_FORWARD_MAX_DAYS`),
so a book that stopped being fetched keeps answering — off an older rate, with
nothing erroring and nothing logging. The refresh worker (`services/fx_refresh`)
closes that hole; this module is the *reporting* half: it says how far behind
the book actually is for the currencies that price THIS tenant's money, so the
UI can show it instead of the numbers quietly drifting.

**Cache-only, always** (docs/12): nothing here fetches, so the endpoint can
never 403 on privacy and can never become a network call from a page load.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.config import get_settings
from app.models.accounts import Account
from app.models.fx_rates import FxRate
from app.models.import_batches import ImportBatch
from app.models.securities import Security
from app.models.trades import Trade
from app.schemas.fx import FxPairStatus, FxStatusRead
from app.services import fx
from app.services.trade_filters import committed_trade_conditions


def currencies_in_use(session: Session, user_id: int, reporting_currency: str) -> list[str]:
    """Every non-reporting currency that prices this tenant's money (docs/07).

    Three readers, matching the three places an FX rate is actually spent:

    - ``trades.currency`` — the M132 row currency behind the trade-cash leg
      (docs/05 §Portfolio value);
    - ``accounts.currency`` — what a **pre-M132** trade row reads in, because
      its ``currency`` is NULL and falls back to its account's code (docs/02);
    - ``securities.listing_currency`` — the listing currency positions convert
      at.

    Staged rows are excluded (``committed_trade_conditions``), consistent with
    every other report input. The reporting currency itself is excluded: CAD
    never needs a rate. Sorted, so the response is stable.
    """
    reporting = reporting_currency.strip().upper()
    trade_rows = session.exec(
        select(Trade.currency)
        # The caller owns the outerjoin (services.trade_filters contract) —
        # without it the committed-only condition reads as a cartesian product.
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(*committed_trade_conditions(user_id))
    ).all()
    account_rows = session.exec(select(Account.currency).where(Account.user_id == user_id)).all()
    security_rows = session.exec(
        select(Security.listing_currency).where(Security.user_id == user_id)
    ).all()

    codes: set[str] = set()
    for raw in (*trade_rows, *account_rows, *security_rows):
        code = (raw or "").strip().upper()
        if code and code != reporting:
            codes.add(code)
    return sorted(codes)


def _newest_rate(session: Session, base: str, quote: str, today: dt.date) -> FxRate | None:
    return session.exec(
        select(FxRate)
        .where(FxRate.base == base, FxRate.quote == quote, FxRate.date <= today)
        .order_by(FxRate.date.desc())  # type: ignore[attr-defined]
    ).first()


def pair_status(session: Session, base: str, quote: str, today: dt.date) -> FxPairStatus:
    """Freshness of ONE pair, cache-only.

    ``stale`` is the house rule from docs/07: a row older than the
    carry-forward window is not carried forward any more, it is missing. A pair
    with no row at all is stale by definition — its legs have no CAD figure at
    all (docs/08 §Honesty: the cell is null, never a fabricated zero).
    """
    base = base.strip().upper()
    quote = quote.strip().upper()
    row = _newest_rate(session, base, quote, today)
    if row is None:
        return FxPairStatus(
            base=base, quote=quote, as_of=None, age_days=None, stale=True, source=None
        )
    age_days = (today - row.date).days
    return FxPairStatus(
        base=base,
        quote=quote,
        as_of=row.date,
        age_days=age_days,
        stale=age_days > fx.CARRY_FORWARD_MAX_DAYS,
        source=row.source,
    )


def book_status(session: Session, user_id: int, today: dt.date | None = None) -> FxStatusRead:
    """The whole rate book's status for one tenant (M150, docs/08).

    The top-level ``as_of``/``age_days`` describe the WORST-covered pair in
    use — the one that decides whether any number on screen is off a carried
    forward rate — and ``stale`` is true when ANY pair is stale. An empty
    ``pairs`` list means the tenant holds no foreign currency at all: there is
    no FX dependency to report on, and the chip renders nothing.
    """
    today = today or dt.datetime.now(dt.UTC).date()
    reporting = get_settings().reporting_currency
    bases = currencies_in_use(session, user_id, reporting)
    pairs = [pair_status(session, base, reporting, today) for base in bases]

    dated = [pair for pair in pairs if pair.as_of is not None]
    oldest = min(dated, key=lambda pair: pair.as_of) if dated else None
    missing = any(pair.as_of is None for pair in pairs)
    return FxStatusRead(
        reporting_currency=reporting.strip().upper(),
        as_of=None if missing or oldest is None else oldest.as_of,
        age_days=None if missing or oldest is None else oldest.age_days,
        stale=missing or any(pair.stale for pair in pairs),
        pairs=pairs,
    )

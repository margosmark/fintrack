"""fx — rate lookup with cache/fallback semantics + manual gap-fill (docs/07).

GET /fx: cache first; a true miss triggers ONE BoC Valet fetch whose rows are
cached forever; exhausted retries fall back to manual rows, else
{"code": "fx_unavailable"} — never a 5xx. Range queries backfill the whole
span in one bulk Valet call.

POST /fx: manual entry fills GAPS only (docs/07) —
- `date` after today (server UTC) -> 422 {"code": "fx_future_date"} so a
  future BoC publication date can never be pre-poisoned;
- an existing (base, quote, date) row of ANY source (BoC or manual)
  -> 409 {"code": "fx_date_not_gap"} — fetched rows are never overwritable
  through this endpoint, and manual gap-fills are create-once too.

GET /fx/status: cache-only freshness of the pairs that price THIS tenant's
money (M150, docs/07 §Rate-book status) — the read behind the UI's staleness
chip. It never fetches, so it can never 403 on privacy.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import get_current_user, get_db
from app.core.errors import api_error
from app.core.ratelimit import FX_WRITE_LIMIT, human_paced_limit
from app.models.fx_rates import FxRate, FxSource
from app.models.users import User
from app.schemas.fx import (
    FxBoardItem,
    FxBoardPoint,
    FxBoardRead,
    FxRangeRead,
    FxRateCreate,
    FxRateRead,
    FxStatusRead,
)
from app.services import fx as fx_service
from app.services import fx_status

router = APIRouter(prefix="/fx", tags=["fx"])


@router.get("", response_model=FxRateRead | FxRangeRead)
def get_fx(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
    base: Annotated[str, Query(min_length=3, max_length=3)],
    quote: Annotated[str, Query(min_length=3, max_length=3)],
    on_date: Annotated[date | None, Query(alias="date")] = None,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    fetch: Annotated[bool, Query()] = True,
) -> FxRateRead | FxRangeRead:
    """Single date (on-or-before resolution) or an explicit date range.

    `fetch=false` forces cache-only reads — client-side reports pass it so a
    report render NEVER triggers an external rate fetch (docs/12 principle,
    preserved after S2a moved aggregation into the browser).
    """
    if (start_date is None) != (end_date is None):
        raise api_error(
            422, "start_date and end_date must be provided together", "invalid_date_range"
        )
    if start_date is not None and end_date is not None:
        rows = fx_service.get_rates_between(
            db,
            base.strip().upper(),
            quote.strip().upper(),
            start_date,
            end_date,
            allow_fetch=fetch,
        )
        return FxRangeRead(
            base=base.strip().upper(),
            quote=quote.strip().upper(),
            start_date=start_date,
            end_date=end_date,
            items=[FxRateRead.model_validate(row) for row in rows],
        )
    rate = fx_service.get_rate_or_404(
        db, base.strip().upper(), quote.strip().upper(), on_date or datetime.now(UTC).date()
    )
    return FxRateRead.model_validate(rate)


@router.get("/board", response_model=FxBoardRead)
def get_fx_board(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
    days: Annotated[int, Query(ge=1, le=365)] = 90,
) -> FxBoardRead:
    """Latest CAD-per-1-unit rate + recent series for each curated currency.

    Degrades gracefully: a currency with no cached rate (and privacy mode
    blocking fetches) returns latest_rate=None, as_of=None, series=[].
    """
    today = datetime.now(UTC).date()
    start = today - timedelta(days=days)
    allow_fetch = False  # cache-only for board endpoint
    items: list[FxBoardItem] = []
    for base in fx_service.CURATED_FX_BASES:
        rows = fx_service.get_rates_between(db, base, "CAD", start, today, allow_fetch=allow_fetch)
        latest = rows[-1] if rows else None
        items.append(
            FxBoardItem(
                base=base,
                latest_rate=latest.rate if latest is not None else None,
                as_of=latest.date if latest is not None else None,
                series=[FxBoardPoint(date=r.date, rate=r.rate) for r in rows],
            )
        )
    return FxBoardRead(currencies=items)


@router.get("/status", response_model=FxStatusRead)
def get_fx_status(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> FxStatusRead:
    """Freshness of the rates that price this tenant's money (M150, docs/07).

    Cache-only by contract (docs/12): it reports what the book holds for every
    currency in use — the trade-cash leg's row currency, a pre-M132 row's
    account currency, and each held listing currency — and never fetches, so a
    page load cannot become a Valet call and privacy mode cannot 403 it.

    ``pairs: []`` means the tenant holds no foreign currency, so there is no
    FX dependency to report on (the UI chip then renders nothing).
    """
    return fx_status.book_status(db, user.id)  # type: ignore[arg-type]


@router.post("", response_model=FxRateRead, status_code=201)
@human_paced_limit(FX_WRITE_LIMIT)
def create_manual_rate(
    payload: FxRateCreate,
    request: Request,
    _: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> FxRate:
    """Manual gap-fill (docs/07): empty past/today dates only, create-once.

    `fx_rates` is shared across tenants with no user_id, so the guards are
    deliberately strict: future dates are rejected outright (422), and ANY
    existing row — fetched or manual — answers 409 (first writer wins).

    Rate limited because this is the ONLY write path any tenant has into a
    table every other tenant reads. The create-once rule above bounds ROWS
    PER (pair, date) but says nothing about how many distinct pairs a caller
    may invent, and registration is open — so without a ceiling a fresh
    account could fill the shared cache with unbounded keys. A person fills a
    gap or two.
    """
    base = payload.base.strip().upper()
    quote = payload.quote.strip().upper()
    if payload.date > datetime.now(UTC).date():
        raise api_error(
            422,
            f"{payload.date.isoformat()} is in the future; manual rates fill "
            "gaps on today or earlier dates only",
            "fx_future_date",
        )
    existing = db.exec(
        select(FxRate).where(
            FxRate.base == base, FxRate.quote == quote, FxRate.date == payload.date
        )
    ).first()
    if existing is not None:
        raise api_error(
            409,
            f"A {existing.source} rate already exists for {base}/{quote} on {payload.date}; "
            "manual entries fill gaps only",
            "fx_date_not_gap",
        )
    rate = FxRate(
        base=base, quote=quote, date=payload.date, rate=payload.rate, source=FxSource.manual
    )
    db.add(rate)
    db.commit()
    db.refresh(rate)
    return rate

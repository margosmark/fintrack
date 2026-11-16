"""prices — CRUD + manual-wins upsert (docs/02, docs/07, docs/08).

POST /prices is an upsert keyed (security_id, date): it sets price and
source='manual' even on an existing row — human data overwrites machine data;
the yfinance backfill never touches manual rows.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import (
    Pagination,
    get_current_user,
    get_db,
    get_pagination,
    owned_or_404,
    paginate,
)
from app.core.errors import api_error
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.users import User
from app.schemas.prices import (
    PriceCreate,
    PriceHistoryPage,
    PriceHistoryPoint,
    PricePage,
    PriceRead,
    PriceUpdate,
)
from app.services import price_series
from app.services.price_series import SeriesGranularity

router = APIRouter(prefix="/prices", tags=["prices"])


@router.get("", response_model=PricePage)
def list_prices(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
    security_id: Annotated[int | None, Query()] = None,
) -> PricePage:
    filters = [Price.user_id == current_user.id]
    if security_id is not None:
        filters.append(Price.security_id == security_id)
    rows, total = paginate(
        db,
        select(Price).where(*filters).order_by(Price.date.desc(), Price.id),
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return PricePage(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.get("/history", response_model=PriceHistoryPage)
def price_history(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
    security_id: Annotated[int, Query()],
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    granularity: SeriesGranularity = "auto",
) -> PriceHistoryPage:
    """Cached closes for one security as a chart series (M126, docs/08).

    Cache-only read — it never fetches, so it works under privacy mode and it
    has no side effect on the security's backfill (unlike
    `GET /securities/{id}/overview`, which refreshes on every call).

    - `security_id` **required**; another user's or a missing security →
      `404 not_found` (tenancy: `Price.user_id` + `owned_or_404`).
    - `from` optional (no floor = the whole cached history), `to` defaults to
      today; `from > to` → `422 invalid_date_range`.
    - `granularity=auto` (default) applies docs/12's documented formula;
      an explicit value is honoured and only coarsened past the point
      roofline. The response echoes both `granularity` and
      `requested_granularity`.
    - Points are valued at their bucket END (M118's convention): a week
      bucket carries the last close inside that week.
    """
    owned_or_404(Security, security_id, current_user, db)
    window_to = to if to is not None else date.today()
    if from_ is not None and from_ > window_to:
        raise api_error(
            422,
            f"from ({from_}) must be on or before to ({window_to})",
            "invalid_date_range",
        )
    response.headers["Cache-Control"] = "private, max-age=60"
    rows = price_series.load_closes(
        db,
        user_id=current_user.id,
        security_id=security_id,
        from_=from_,
        to=window_to,
    )
    resolved, requested, series = price_series.resolve_granularity(
        rows,
        requested=granularity,
        first=rows[0][0] if rows else from_,
        to=window_to,
    )
    # `from` echoes the window actually SERVED, not the ask: a caller who
    # asked for a floor that predates every cached close still learns where
    # the series really begins (docs/08 §Price history).
    served_from = from_ if from_ is not None else (rows[0][0] if rows else window_to)
    return PriceHistoryPage(
        security_id=security_id,
        from_=served_from,
        to=window_to,
        granularity=resolved,
        requested_granularity=requested,
        items=[
            PriceHistoryPoint(date=point_date, price=str(point_price))
            for point_date, point_price in series
        ],
        count=len(series),
    )


@router.post("", response_model=PriceRead, status_code=201)
def upsert_price(
    payload: PriceCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
) -> Price:
    owned_or_404(Security, payload.security_id, current_user, db)
    existing = db.exec(
        select(Price).where(Price.security_id == payload.security_id, Price.date == payload.date)
    ).first()
    if existing is not None:
        # Human overwrites machine (docs/08): same row, back to source=manual.
        existing.price = payload.price
        existing.source = PriceSource.manual
        # Option A (t_bd5ec95f): after a human edit the value is HUMAN data —
        # claiming a provider fetch time for it would be a lie, so the fetch
        # evidence is cleared. A later provider fetch never touches this row
        # (manual wins), so NULL is its honest, permanent state.
        existing.fetched_at = None
        db.add(existing)
        db.commit()
        db.refresh(existing)
        response.status_code = 200
        return existing
    price = Price(
        user_id=current_user.id,
        security_id=payload.security_id,
        date=payload.date,
        price=payload.price,
        source=PriceSource.manual,
        # A brand-new HUMAN row has no provider fetch (docs/08, t_bd5ec95f).
        fetched_at=None,
    )
    db.add(price)
    db.commit()
    db.refresh(price)
    return price


@router.get("/{price_id}", response_model=PriceRead)
def get_price(
    price_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Price:
    return owned_or_404(Price, price_id, current_user, db)


@router.patch("/{price_id}", response_model=PriceRead)
def update_price(
    price_id: int,
    payload: PriceUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Price:
    price = owned_or_404(Price, price_id, current_user, db)
    price.price = payload.price
    # A human edit of a cached close is human data from now on (docs/08).
    price.source = PriceSource.manual
    # ...and carries no provider fetch time from that moment (Option A,
    # t_bd5ec95f): the value is human data; a fetch stamp would be a lie.
    price.fetched_at = None
    db.add(price)
    db.commit()
    db.refresh(price)
    return price


@router.delete("/{price_id}", status_code=204)
def delete_price(
    price_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    price = owned_or_404(Price, price_id, current_user, db)
    db.delete(price)
    db.commit()

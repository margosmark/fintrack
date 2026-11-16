"""watchlist — securities tracked but not held (W11, user request).

Adding a security to the watchlist also queues the same 365-day historical
price backfill as security creation (w11-contract pin 2); failures degrade
silently per docs/07 and manual price rows are never touched.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Response
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import get_current_user, get_db
from app.api.securities import queue_backfill
from app.core.errors import api_error
from app.models.securities import Security
from app.models.users import User
from app.models.watchlist import WatchlistItem
from app.schemas.watchlist import WatchlistAdd, WatchlistItemRead, WatchlistPage
from app.services import market_data

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


def _watched_or_404(db: Session, user: User, security_id: int) -> WatchlistItem:
    item = db.exec(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user.id,
            WatchlistItem.security_id == security_id,
        )
    ).first()
    if item is None:
        raise api_error(404, "Security is not on your watchlist", "not_found")
    return item


def _to_read(db: Session, item: WatchlistItem) -> WatchlistItemRead:
    security = db.get(Security, item.security_id)
    today = date.today()
    price = market_data.newest_price(db, item.security_id, today)
    if price is None:
        return WatchlistItemRead(
            security_id=security.id,
            symbol=security.symbol,
            name=security.name,
            listing_currency=security.listing_currency,
            dead_symbol=security.dead_symbol,
            dead_symbol_reason=security.dead_symbol_reason,
        )
    age_days = (today - price.date).days
    return WatchlistItemRead(
        security_id=security.id,
        symbol=security.symbol,
        name=security.name,
        listing_currency=security.listing_currency,
        latest_price=price.price,
        price_date=price.date,
        price_source=price.source,
        price_age_days=age_days,
        # Option A (t_bd5ec95f): REAL provider-fetch evidence, straight off
        # the row — NULL when no fetch ever stamped it (the client then
        # omits the fetch-age segment; t_a16b7663).
        price_fetched_at=price.fetched_at,
        price_stale=age_days > market_data.PRICE_STALE_DAYS,
        dead_symbol=security.dead_symbol,
        dead_symbol_reason=security.dead_symbol_reason,
    )


@router.get("", response_model=WatchlistPage)
def list_watchlist(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> WatchlistPage:
    items = db.exec(
        select(WatchlistItem)
        .where(WatchlistItem.user_id == current_user.id)
        .order_by(WatchlistItem.id)
    ).all()
    reads = [_to_read(db, item) for item in items]
    return WatchlistPage(items=reads, total=len(reads))


@router.post("", response_model=WatchlistItemRead, status_code=201)
def add_to_watchlist(
    payload: WatchlistAdd,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> WatchlistItemRead:
    security = db.get(Security, payload.security_id)
    if security is None or security.user_id != current_user.id:
        raise api_error(404, "Security not found", "not_found")
    duplicate = db.exec(
        select(WatchlistItem).where(
            WatchlistItem.user_id == current_user.id,
            WatchlistItem.security_id == security.id,
        )
    ).first()
    if duplicate is not None:
        raise api_error(409, "Security is already on your watchlist", "already_watched")
    item = WatchlistItem(user_id=current_user.id, security_id=security.id)
    db.add(item)
    db.commit()
    db.refresh(item)
    # Same historical backfill as POST /securities (w11-contract pin 2).
    queue_backfill(background_tasks, security)
    return _to_read(db, item)


@router.delete("/{security_id}", status_code=204)
def remove_from_watchlist(
    security_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
) -> None:
    item = _watched_or_404(db, current_user, security_id)
    db.delete(item)
    db.commit()

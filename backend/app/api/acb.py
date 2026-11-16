"""acb — pool state reads + manual rebuild trigger (docs/08; docs/02 invariant).

`acb_pools` rows are derived state; the only write path is
`app.tax.acb.rebuild_user_pools`, which the trades write path invokes inside its
own transaction. This router additionally exposes POST /acb/rebuild for
repairs/backfills.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import (
    Pagination,
    get_current_user,
    get_db,
    get_pagination,
    paginate,
)
from app.models.acb_pools import AcbPool
from app.models.securities import Security
from app.models.users import User
from app.schemas.acb import AcbPoolPage, AcbPoolRead, RebuildRequest, RebuildResult
from app.tax.acb import rebuild_user_pools

router = APIRouter(prefix="/acb", tags=["acb"])


@router.get("", response_model=AcbPoolPage)
def list_pools(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
    account_id: Annotated[int | None, Query()] = None,
    security_id: Annotated[int | None, Query()] = None,
) -> AcbPoolPage:
    filters = [AcbPool.user_id == current_user.id]
    if account_id is not None:
        filters.append(AcbPool.account_id == account_id)
    if security_id is not None:
        filters.append(AcbPool.security_id == security_id)
    rows, total = paginate(
        db,
        select(AcbPool, Security.symbol)
        .join(Security, AcbPool.security_id == Security.id)
        .where(*filters)
        .order_by(Security.symbol, AcbPool.id),
        limit=pagination.limit,
        offset=pagination.offset,
    )
    items = [
        AcbPoolRead(
            id=pool.id,
            account_id=pool.account_id,
            security_id=pool.security_id,
            symbol=symbol,
            quantity=pool.quantity,
            total_acb_cad=pool.total_acb_cad,
            updated_at=pool.updated_at,
        )
        for pool, symbol in rows
    ]
    return AcbPoolPage(items=items, total=total, limit=pagination.limit, offset=pagination.offset)


@router.post("/rebuild", response_model=RebuildResult)
def rebuild(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    payload: RebuildRequest | None = None,
) -> RebuildResult:
    counts = rebuild_user_pools(db, current_user.id)
    db.commit()
    return RebuildResult(pools=counts["pools"], trades=counts["trades"])

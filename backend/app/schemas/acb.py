"""acb — request/response shapes (docs/08, docs/02)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class AcbPoolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    security_id: int
    symbol: str
    quantity: Decimal
    total_acb_cad: Decimal
    updated_at: dt.datetime | None = None


class AcbPoolPage(BaseModel):
    items: list[AcbPoolRead]
    total: int
    limit: int
    offset: int


class RebuildRequest(BaseModel):
    """Empty body — the rebuild always covers ALL of the caller's pools."""


class RebuildResult(BaseModel):
    pools: int
    trades: int

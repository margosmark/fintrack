"""securities — tradable instruments per user (docs/02)."""

import enum

from sqlalchemy import Column, Enum, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class SecurityKind(enum.StrEnum):
    stock = "stock"
    etf = "etf"
    mutual_fund = "mutual_fund"


class QuoteOutcome(enum.StrEnum):
    """Why a yfinance price fetch did or did not return closes (M141, docs/07).

    The fan-out must tell a STRUCTURALLY invalid symbol apart from a transient
    failure, because only the former is worth remembering: Yahoo answers
    ``404 Quote not found for symbol: APOTEX`` for a company that went private,
    and no amount of retrying will change that answer. A rate-limit or a dead
    network is the opposite — it says nothing about the symbol.

    - ``ok`` — at least one close came back.
    - ``not_found`` — Yahoo said the symbol does not exist (delisted, private,
      mistyped). The only outcome that counts toward ``dead_symbol``.
    - ``unavailable`` — rate-limit, timeout, or any other fetch failure. Says
      nothing about the symbol, so it never advances the miss counter.
    - ``unknown`` — no fetch ran in this context (a monkeypatched seam, the
      privacy guard, a cached read). Never advances the miss counter either.
    """

    ok = "ok"
    not_found = "not_found"
    unavailable = "unavailable"
    unknown = "unknown"


class Security(TimestampMixin, table=True):
    __tablename__ = "securities"
    __table_args__ = (
        UniqueConstraint("user_id", "symbol", "exchange", name="uq_securities_symbol"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    symbol: str
    name: str
    kind: SecurityKind = Field(
        sa_column=Column(Enum(SecurityKind, native_enum=False, length=32), nullable=False)
    )
    listing_currency: str
    exchange: str
    # --- M141 dead-symbol bookkeeping (docs/07 §Dead symbols) -----------------
    # Consecutive HARD "quote not found" outcomes. Every other outcome (a
    # rate-limit, a timeout, a cached read) leaves this untouched, so three
    # consecutive misses means three times Yahoo said the symbol does not
    # exist — not three flaky mornings.
    quote_miss_count: int = Field(default=0)
    # Set once quote_miss_count reaches DEAD_SYMBOL_MISSES. While it holds,
    # the automatic fan-out stops fetching this row entirely (docs/07).
    dead_symbol: bool = Field(default=False)
    # The last "quote not found" verdict, verbatim — the honest reason the UI
    # shows instead of a forever-"yet" promise.
    dead_symbol_reason: str | None = Field(default=None)

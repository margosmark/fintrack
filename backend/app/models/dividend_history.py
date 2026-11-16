"""Raw provider dividend events and per-tenant refresh state (docs/07)."""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Column, Enum, Index, Numeric, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class DividendHistoryState(enum.StrEnum):
    pending = "pending"
    refreshing = "refreshing"
    available = "available"
    empty = "empty"
    unavailable = "unavailable"


class DividendHistory(TimestampMixin, table=True):
    __tablename__ = "dividend_history"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_dividend_history_amount_positive"),
        CheckConstraint("source = 'yfinance'", name="ck_dividend_history_source"),
        UniqueConstraint(
            "user_id", "security_id", "source", "provider_order", name="uq_dividend_history_order"
        ),
        Index("ix_dividend_history_user_security_date", "user_id", "security_id", "ex_date"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    security_id: int = Field(foreign_key="securities.id")
    ex_date: date
    amount: Decimal = Field(sa_type=Numeric(20, 8))
    provider_order: int = Field(default=0)
    source: str = Field(default="yfinance", max_length=16)


class DividendHistoryRefresh(TimestampMixin, table=True):
    __tablename__ = "dividend_history_refresh"
    __table_args__ = (
        UniqueConstraint("user_id", "security_id", name="uq_dividend_history_refresh"),
        Index("ix_dividend_history_refresh_pending", "state", "last_attempt_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    security_id: int = Field(foreign_key="securities.id")
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    # M186 bounded retry (docs/07 §M184): consecutive provider failures since
    # the last success. Drives exponential backoff in `enqueue_due`; a
    # successful refresh resets it to 0 so recovery is immediate.
    consecutive_failures: int = Field(default=0)
    state: DividendHistoryState = Field(
        default=DividendHistoryState.unavailable,
        sa_column=Column(
            Enum(DividendHistoryState, native_enum=False, length=24),
            nullable=False,
            default=DividendHistoryState.unavailable,
        ),
    )
    error_code: str | None = Field(default=None, max_length=64)

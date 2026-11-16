"""trades — all position/cash actions; input conventions per docs/02."""

import enum
from datetime import date
from decimal import Decimal

from sqlalchemy import Column, Enum, Index, Numeric
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class TradeAction(enum.StrEnum):
    buy = "buy"
    sell = "sell"
    dividend = "dividend"
    distribution = "distribution"
    return_of_capital = "return_of_capital"
    drip = "drip"
    fee = "fee"
    contribution = "contribution"
    withdrawal = "withdrawal"
    transfer_in = "transfer_in"
    transfer_out = "transfer_out"
    corporate_action_adjustment = "corporate_action_adjustment"


CASH_INFLOW_ACTIONS = frozenset(
    {
        TradeAction.dividend,
        TradeAction.distribution,
        TradeAction.return_of_capital,
        TradeAction.contribution,
    }
)
CASH_OUTFLOW_ACTIONS = frozenset({TradeAction.fee, TradeAction.withdrawal})
COMMISSION_ACTIONS = frozenset({TradeAction.buy, TradeAction.sell, TradeAction.drip})
# Actions where quantity=0 and price_native carries the gross cash amount.
# NOTE: transfer_in / transfer_out are SHARE movements (acquisitions/dispositions
# handled by the ACB engine), not cash flows — intentionally excluded here.
CASH_ONLY_ACTIONS = CASH_INFLOW_ACTIONS | CASH_OUTFLOW_ACTIONS


class Trade(TimestampMixin, table=True):
    __tablename__ = "trades"
    __table_args__ = (
        Index(
            "ix_trades_user_account_security_date", "user_id", "account_id", "security_id", "date"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    account_id: int = Field(foreign_key="accounts.id")
    security_id: int = Field(foreign_key="securities.id")
    action: TradeAction = Field(
        sa_column=Column(Enum(TradeAction, native_enum=False, length=32), nullable=False)
    )
    date: date
    quantity: Decimal = Field(default=Decimal("0"), sa_type=Numeric(18, 6))
    # Gross cash amount (native currency) for cash-only actions; unit price otherwise.
    price_native: Decimal = Field(sa_type=Numeric(20, 8))
    commission: Decimal = Field(default=Decimal("0"), sa_type=Numeric(18, 2))
    #: The currency `price_native`/`commission` are STATED in — the row's own
    #: currency as the broker exported it (M132, docs/02 §trades). A USD buy
    #: inside a CAD account stores USD here, and the cash leg converts it at
    #: the row's currency rather than face-summing it into the account's.
    #: NULL = the row predates M132 and is read in the ACCOUNT's currency —
    #: exactly what every consumer did before the column existed, never a guess.
    currency: str | None = Field(default=None, max_length=3)
    # Display copy captured at entry — pool math NEVER reads it (docs/06 FX precedence).
    fx_rate_used: Decimal | None = Field(default=None, sa_type=Numeric(20, 8))
    # Staged-import dedup controls: `dedup_hash` is salted for duplicates;
    # `force_include` marks duplicates the user chose to keep at commit
    # time (docs/03). Trade dedup_hash has no UNIQUE constraint, but salting
    # keeps a single staging path and keeps forced dups distinct.
    dedup_hash: str | None = Field(default=None, index=True)
    is_duplicate: bool = Field(default=False)
    force_include: bool = Field(default=False)
    # import_batch_id: staged trades carry a batch reference; NULL for
    # manually entered trades (docs/03 import lifecycle).
    import_batch_id: int | None = Field(default=None, foreign_key="import_batches.id", index=True)

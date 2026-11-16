"""accounts — unified accounts across banking and brokerage (docs/02)."""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, Enum, Numeric
from sqlmodel import Field

from app.models.mixins import TimestampMixin
from app.models.sync_agreements import ProviderStatus


class AccountType(enum.StrEnum):
    chequing = "chequing"
    savings = "savings"
    credit_card = "credit_card"
    line_of_credit = "line_of_credit"
    brokerage_non_registered = "brokerage_non_registered"
    rrsp = "rrsp"
    tfsa = "tfsa"
    fhsa = "fhsa"
    resp = "resp"
    lira = "lira"
    rdsp = "rdsp"


# Account types whose custody rules allow securities positions (docs/05).
# Trades are rejected with 422 {"code": "account_not_investment"} on anything
# else (W11): chequing/savings/credit_card/line_of_credit hold cash only.
INVESTMENT_ACCOUNT_TYPES: frozenset[AccountType] = frozenset(
    {
        AccountType.brokerage_non_registered,
        AccountType.rrsp,
        AccountType.tfsa,
        AccountType.fhsa,
        AccountType.resp,
        AccountType.lira,
        AccountType.rdsp,
    }
)


class Account(TimestampMixin, table=True):
    __tablename__ = "accounts"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    institution_id: int | None = Field(default=None, foreign_key="institutions.id")
    name: str
    type: AccountType = Field(
        sa_column=Column(Enum(AccountType, native_enum=False, length=32), nullable=False)
    )
    currency: str
    # true only for brokerage_non_registered; registered plans are not taxable.
    taxable: bool = False
    llm_autopilot: bool = False
    starting_balance: Decimal | None = Field(default=None, sa_type=Numeric(18, 2))
    # E2EE Phase 2 (docs/33): opaque ciphertext for the account name.
    name_cipher: str | None = None
    # Bank-sync status-page groundwork (docs/39 item 4). NULL `provider_status`
    # means "not linked to a live sync agreement" — kept NULL through a revoke,
    # while `last_sync_at` survives as historical evidence of the last pull.
    last_sync_at: datetime | None = None
    provider_status: ProviderStatus | None = Field(
        default=None,
        sa_column=Column(Enum(ProviderStatus, native_enum=False, length=32), nullable=True),
    )

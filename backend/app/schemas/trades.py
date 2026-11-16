"""trades — request/response shapes (docs/02, docs/05, docs/08)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models.trades import TradeAction


class TradeCreate(BaseModel):
    # extra="forbid" (QA nit, M132): the create path RECORDS the account's
    # currency on purpose (docs/02 §trades — the form carries no currency
    # field), so a client that sends `currency` would otherwise be silently
    # ignored — the exact "accepts-and-discards" contract that misleads an
    # API consumer. Reject the unknown key instead: 422 validation_error.
    model_config = ConfigDict(extra="forbid")

    account_id: int
    security_id: int
    action: TradeAction
    date: dt.date
    quantity: Decimal = Decimal("0")
    price_native: Decimal
    commission: Decimal = Decimal("0")
    fx_rate_used: Decimal | None = None


class TradeUpdate(BaseModel):
    # Same rule as TradeCreate: PATCH ignores extra keys by default, and a
    # discarded field reads as "applied" to the caller. Nothing in-tree
    # sends extras (the one frontend caller matches TradeInput exactly).
    model_config = ConfigDict(extra="forbid")

    account_id: int | None = None
    security_id: int | None = None
    action: TradeAction | None = None
    # dt.date: a field named `date` would shadow the type during pydantic's
    # deferred annotation evaluation.
    date: dt.date | None = None
    quantity: Decimal | None = None
    price_native: Decimal | None = None
    commission: Decimal | None = None
    fx_rate_used: Decimal | None = None


class TradeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    security_id: int
    action: TradeAction
    date: dt.date
    quantity: Decimal
    price_native: Decimal
    commission: Decimal
    #: The currency `price_native`/`commission` are stated in (M132, docs/02).
    #: NULL on rows written before M132 — clients read it as the ACCOUNT's
    #: currency, exactly like the server, so both cash legs agree.
    currency: str | None = None
    fx_rate_used: Decimal | None = None


class TradePage(BaseModel):
    items: list[TradeRead]
    total: int
    limit: int
    offset: int


class DividendSuggestion(BaseModel):
    """A pending dividend from yfinance, scoped to a specific account holding."""

    security_id: int
    account_id: int
    symbol: str
    date: dt.date
    amount: Decimal  # per-share dividend in listing currency
    quantity: str  # shares held in this account
    gross_amount: str  # amount x quantity (pre-computed)
    currency: str


class DividendRecordInput(BaseModel):
    """Input for POST /trades/record-dividend — creates 1-3 records atomically."""

    model_config = ConfigDict(extra="forbid")

    account_id: int
    security_id: int
    date: dt.date
    gross_amount: Decimal  # total dividend in native currency
    action: TradeAction = TradeAction.dividend
    drip: bool = False
    drip_price: Decimal | None = None  # required if drip=True
    drip_commission: Decimal = Decimal("0")
    fractional: bool = True  # False = whole shares only, remainder stays as cash
    fx_rate_used: Decimal | None = None


class DividendRecordResult(BaseModel):
    trade_id: int
    drip_trade_id: int | None
    transaction_id: int


# --- Investment CSV import (docs/05) ------------------------------------------


class TradePreviewRow(BaseModel):
    """One parsed trade row from CSV import with dedup verdict."""

    id: int
    date: dt.date
    symbol: str
    security_name: str
    action: str
    quantity: Decimal
    price_native: Decimal
    commission: Decimal
    currency: str
    duplicate: bool


class UnmappedRow(BaseModel):
    """A CSV row that was skipped during import, with the reason."""

    line: int
    activity_type: str
    activity_sub_type: str | None = None
    symbol: str
    date: str | None = None
    reason: str
    #: Profile label that produced the row (docs/41); null on the legacy
    #: Wealthsimple parser so old responses stay byte-identical.
    institution: str | None = None
    #: Machine-readable refusal class. ``"stuck"`` means staging the file is
    #: refused outright (422) rather than the row being skipped — the dry run
    #: shows them, POST /trades/import refuses on the first one (docs/41).
    code: str | None = None


class TradeImportResponse(BaseModel):
    """Response from POST /trades/import — batch summary + parsed preview."""

    batch_id: int
    profile: str | None = None
    institution: str | None = None
    imported: int
    duplicates_skipped: int
    preview: list[TradePreviewRow]
    unmapped: list[UnmappedRow] = []
    notices: list[str] = []


# --- Brokerage profiles (docs/41, M82) ---------------------------------------


class TradeDryRunRow(BaseModel):
    """One parsed row of a zero-write dry run (POST /trades/import/preview)."""

    index: int
    date: dt.date
    symbol: str
    security_name: str
    action: str
    quantity: Decimal
    price_native: Decimal
    commission: Decimal
    currency: str
    #: ``new`` would stage as imported, ``duplicate`` with a salted hash.
    verdict: Literal["new", "duplicate"]
    #: ``existing`` when a securities row is already there, ``create`` when
    #: staging would insert one. The dry run never inserts it.
    security_action: Literal["existing", "create"]


class TradeDryRunSummary(BaseModel):
    total: int
    new: int
    duplicates: int
    unmapped: int


class TradeDryRunResponse(BaseModel):
    """POST /trades/import/preview — what POST /trades/import WOULD do."""

    profile: str | None = None
    rows: list[TradeDryRunRow]
    unmapped: list[UnmappedRow] = []
    notices: list[str] = []
    summary: TradeDryRunSummary

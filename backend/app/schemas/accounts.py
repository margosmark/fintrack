"""accounts — CRUD shapes (docs/08)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.accounts import AccountType
from app.models.sync_agreements import ProviderStatus


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    type: AccountType
    currency: Annotated[str, StringConstraints(to_upper=True, min_length=3, max_length=3)]
    institution_id: int | None = None
    starting_balance: Decimal | None = None
    name_cipher: str | None = None


class AccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    type: AccountType | None = None
    currency: (
        Annotated[str, StringConstraints(to_upper=True, min_length=3, max_length=3)] | None
    ) = None
    institution_id: int | None = None
    llm_autopilot: bool | None = None
    starting_balance: Decimal | None = None
    name_cipher: str | None = None


class AccountPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    institution_id: int | None
    name: str
    type: AccountType
    currency: str
    taxable: bool
    llm_autopilot: bool
    # M145 (docs/08 §cash_balance): KIND-dependent — for cash accounts this is
    # the committed-LEDGER sum (split parents excluded) + starting_balance; for
    # INVESTMENT accounts it is the docs/05 value-history cash chain
    # (`services/account_value.account_cash`), so the card and the account's
    # own chart cannot disagree (real account 459 records every dollar as a
    # CASH-security trade with ZERO transactions). `None` means no honest
    # figure for an investment account (a foreign leg past the M115 fx window)
    # — the same gap the chart draws, never a fabricated zero.
    cash_balance: str | None
    # Market value of securities held in the account, native currency, "0.00" if none.
    investment_value: str
    # cash_balance + investment_value, native currency, quantized 0.01; `None`
    # exactly when cash_balance is (M145).
    total_value: str | None
    starting_balance: str
    name_cipher: str | None = None
    # Bank-sync status-page groundwork (docs/39 item 4). `last_sync_at` is NULL
    # until a pull lands; `provider_status` NULL means "no live provider link".
    last_sync_at: datetime | None = None
    provider_status: ProviderStatus | None = None
    # M113 (docs/08): how many of THIS account's import batches are still
    # `pending_review` — a COUNT only, never batch contents (E2EE row-count
    # rule, docs/09). Computed on the single-account responses
    # (`GET /accounts/{id}`, `POST`, `PATCH`); the list endpoint and the
    # partner projection do not run the count and carry the 0 default.
    pending_import_count: int = 0


class AccountPage(BaseModel):
    items: list[AccountPublic]
    total: int
    limit: int
    offset: int


class LedgerEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: int
    date: date
    description: str
    amount: str
    running_balance: str


class LedgerPage(BaseModel):
    items: list[LedgerEntry]
    starting_balance: str
    current_balance: str
    total: int


# --- M118: per-account value history (docs/08 §Account value history) --------

ValueGranularity = Literal["day", "week", "month"]
# The closed set of marker labels the chart renders (docs/08 §Event markers).
ValueEventKind = Literal["contribution", "withdrawal", "transfer_in", "transfer_out", "deposit"]


class ValueHistoryBucket(BaseModel):
    """One period's account value, valued at the bucket END (docs/12 §Time bucketing).

    `valued=false` is the honest-gap contract: `value_cad` is null and the chart
    draws a visible break — never a zero, never an interpolation (docs/08
    §Honesty). `estimated=true` means the figure is real but an input was
    carried forward past docs/07's staleness rule, so the chart shows its
    "estimated" chip.

    M144 (docs/08 §Honesty — the two-reason empty state): `gap_reason` says
    WHICH kind of gap `valued=false` is, so the reader is never told "no
    valuation for this period" about a day the account simply held nothing on.
    """

    bucket_end: date
    cash_cad: Decimal | None
    positions_cad: Decimal | None
    value_cad: Decimal | None
    estimated: bool
    valued: bool
    #: `no_price` / `fx` = a DATA gap; `not_held` = a HOLDINGS gap (nothing was
    #: held on this day). Null whenever the bucket is valued.
    gap_reason: Literal["no_price", "fx", "not_held"] | None = None


class ValueHistoryEvent(BaseModel):
    """A significant cash-flow marker, DERIVED from committed rows on every
    request (never cached to a table) — docs/08 §Event markers."""

    id: str
    source: Literal["trade", "transaction"]
    trade_id: int | None
    transaction_id: int | None
    date: date
    bucket_end: date
    kind: ValueEventKind
    # +1 money/value in, -1 out; `amount_cad` carries the same sign.
    direction: Literal[-1, 1]
    amount_cad: Decimal
    amount_native: Decimal
    currency: str
    payee: str | None
    category_name: str | None
    symbol: str | None
    security_id: int | None
    quantity: Decimal | None
    action: str | None


class AccountValueHistory(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    account_id: int
    # `validation_alias` lets the service dict key be either shape; the JSON
    # key is always `from` (serialization_alias), like every other report.
    from_: date = Field(validation_alias="from", serialization_alias="from")
    to: date
    # `granularity` is what was RETURNED: the endpoint coarsens day → week →
    # month → year whenever a bucket would push the series past the ~400-point
    # detail roofline, and `requested_granularity` keeps the caller's ask.
    granularity: str
    requested_granularity: str
    # Reporting currency of every *_cad field (CAD unless overridden).
    currency: str
    native_currency: str
    min_event_value: Decimal
    # False when the account has no committed row and no starting balance —
    # the chart renders the three-part empty state instead of a flat $0 line.
    has_data: bool
    buckets: list[ValueHistoryBucket]
    events: list[ValueHistoryEvent]


class ValueHistoryStats(BaseModel):
    """The window's own move, server-computed from the series it ships with
    (M130, docs/08 §Portfolio value history).

    `None` when the window has fewer than two valued points — there is no move
    to state, only a level. `*_bucket` names the two dates the change spans so
    "over 6M" can say exactly which two points it subtracted.
    """

    from_bucket: date | None
    to_bucket: date | None
    from_value_cad: Decimal | None
    to_value_cad: Decimal | None
    change_cad: Decimal | None
    change_pct: float | None


class PortfolioValueHistoryEvent(ValueHistoryEvent):
    """One marker, with every account that produced it (M130).

    `trade:<id>` / `transaction:<id>` is the dedup key, so an event that is
    surfaced by more than one account's series renders ONCE and names them all.
    """

    account_ids: list[int]
    account_names: list[str]


class PortfolioValueHistory(BaseModel):
    """`GET /portfolio/value-history` — the summed investment-account series
    (M130, docs/08 §Portfolio value history).

    Same wire contract as `AccountValueHistory` with the scope widened: the id
    is the LIST of accounts in the sum, and `stats` carries the range's move so
    the header and the chart can never disagree about it.
    """

    model_config = ConfigDict(populate_by_name=True)

    account_ids: list[int]
    from_: date = Field(validation_alias="from", serialization_alias="from")
    to: date
    granularity: str
    requested_granularity: str
    currency: str
    native_currency: str
    min_event_value: Decimal
    has_data: bool
    buckets: list[ValueHistoryBucket]
    events: list[PortfolioValueHistoryEvent]
    stats: ValueHistoryStats

"""securities — request/response shapes (docs/05, docs/08)."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.prices import PriceSource
from app.models.securities import SecurityKind


class SecurityCreate(BaseModel):
    symbol: str
    name: str
    kind: SecurityKind = SecurityKind.stock
    listing_currency: str
    exchange: str = ""


class SecurityUpdate(BaseModel):
    symbol: str | None = None
    name: str | None = None
    kind: SecurityKind | None = None
    listing_currency: str | None = None
    exchange: str | None = None


class SecurityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    name: str
    kind: SecurityKind
    listing_currency: str
    exchange: str
    # M141 (docs/08 §Securities): the dead-symbol state, so every surface that
    # renders a security row can be honest about whether a price will ever
    # arrive without a second request.
    dead_symbol: bool = False
    quote_miss_count: int = 0
    dead_symbol_reason: str | None = None


class SecurityPage(BaseModel):
    items: list[SecurityRead]
    total: int
    limit: int
    offset: int


class SecurityLookupCandidate(BaseModel):
    """One listing a pick-one chooser offers (M142, extended M143, docs/08).

    A candidate is a **create-ready listing**, not a display row: symbol,
    name, kind, listing currency and exchange all come from the probe that
    found it, so the pick is applied directly and never re-resolved. M142
    sent the pick back through `GET /securities/lookup` — that is exactly
    what M143 breaks: re-resolving a bare-vs-`.TO` collision lands on the
    SAME collision, so the user would be handed the chooser they just
    answered, forever (docs/07 §Candidate resolution).
    """

    symbol: str
    name: str
    kind: SecurityKind = SecurityKind.stock
    listing_currency: str
    exchange: str
    exchange_display: str | None = None


class SecurityLookup(BaseModel):
    """yfinance autofill result (docs/05); lookup failures never reach here —
    the router degrades them to 404 {"code": "lookup_unavailable"} (docs/07).

    M142 (docs/07 §yfinance quotes): one endpoint, three answers, told apart
    by `match` — `direct`/`suffix` (the typed ticker resolved; `symbol` is
    the string the caller sent), `search` (a typed NAME resolved to ONE
    canonical listing; `symbol` is that ticker — `APOTEX` -> `APTX.TO`), and
    `ambiguous` (several listings matched: `result` fields are null and
    `candidates` is the pick-one chooser — never a silent guess).
    `exchange_display` is Yahoo's human venue name ("Toronto") when it sent
    one, else null and the UI falls back to `exchange`.
    """

    symbol: str | None = None
    name: str | None = None
    kind: SecurityKind | None = None
    listing_currency: str | None = None
    exchange: str | None = None
    exchange_display: str | None = None
    match: Literal["direct", "suffix", "search", "ambiguous"] = "direct"
    candidates: list[SecurityLookupCandidate] = Field(default_factory=list)


class QuoteCheck(BaseModel):
    """M141 add-time symbol validation (docs/08 §Quote check).

    Deliberately NOT a hard block: a user can legitimately watch a symbol that
    has no listing yet (a future IPO, a cash equivalent). The form shows a
    non-blocking warning when ``quote_found`` is false and lets the add
    through anyway.

    ``quote_found`` is a TRI-state by omission: ``None`` means we could not ask
    (network failure, rate-limit, privacy mode) — an answer we do not have is
    never reported as "no quote".
    """

    symbol: str
    quote_found: bool | None = None
    reason: str | None = None


class LatestPrice(BaseModel):
    date: date
    price: Decimal
    source: PriceSource
    age_days: int
    stale: bool
    # M129 daily delta — server-computed against the close strictly before
    # `date` (docs/08). None = no prior close cached (first-day listing): the
    # UI withholds the delta line instead of inventing a zero move.
    previous_close: str | None = None
    day_change_native: str | None = None
    day_change_pct: float | None = None


class SecurityStats(BaseModel):
    """365-day window over cached closes (docs/07 cache discipline)."""

    high_52w: str | None = None
    low_52w: str | None = None
    change_pct: float | None = None
    cached_days: int


class HistoryPoint(BaseModel):
    date: date
    price: str


class OverviewTotals(BaseModel):
    quantity: str
    book_cost_cad: str | None = None
    market_value_cad: str | None = None
    # M129 "own impact" block — sums of the SAME per-position server fields
    # the holdings table renders (docs/08), so the card's headline and the
    # table's rows can never disagree. `unrealized_gain_pct` is gain ÷ book
    # over the rows that carry BOTH numbers; None when none do.
    unrealized_gain_cad: str | None = None
    unrealized_gain_native: str | None = None
    unrealized_gain_pct: float | None = None
    day_change_cad: str | None = None


class RecentTrade(BaseModel):
    id: int
    account_id: int
    action: str
    date: date
    quantity: str
    price_native: str


class FundamentalsRead(BaseModel):
    """Cached research snapshot (M16); Decimals serialize as strings."""

    model_config = ConfigDict(from_attributes=True)

    trailing_eps: Decimal | None = None
    pe_ratio: Decimal | None = None
    market_cap: Decimal | None = None
    dividend_yield_pct: Decimal | None = None
    beta: Decimal | None = None
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    fifty_two_week_high: Decimal | None = None
    fifty_two_week_low: Decimal | None = None
    source: str
    fetched_at: date


class FundamentalsUpdate(BaseModel):
    """Manual override — any subset of the nullable columns; source must be manual."""

    trailing_eps: Decimal | None = None
    pe_ratio: Decimal | None = None
    market_cap: Decimal | None = None
    dividend_yield_pct: Decimal | None = None
    beta: Decimal | None = None
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    fifty_two_week_high: Decimal | None = None
    fifty_two_week_low: Decimal | None = None
    source: Literal["manual"]


class EtfHoldingRead(BaseModel):
    """One fund-composition row (M17); weight arrives as a string."""

    model_config = ConfigDict(from_attributes=True)

    holding_symbol: str | None = None
    holding_security_id: int | None = None
    holding_name: str
    asset_class: str
    region: str | None = None
    country: str | None = None
    sector: str | None = None
    weight_pct: Decimal
    asset_class_breakdown: dict[str, str] | None = None


class EtfHoldingInput(BaseModel):
    """PUT /securities/{id}/holdings row; weight must be in (0, 100]."""

    holding_symbol: str | None = None
    holding_name: str = Field(min_length=1, max_length=160)
    asset_class: Literal["stock", "bond", "cash", "commodity", "crypto", "other"] = "other"
    region: str | None = None
    country: str | None = None
    sector: str | None = None
    weight_pct: Decimal = Field(gt=0, le=100)


class EtfHoldingsBlock(BaseModel):
    """Composition payload embedded in the overview / returned by the
    holdings routes; source is the majority rows' provenance."""

    items: list[EtfHoldingRead] = []
    total_weight_pct: str
    source: str | None = None


class EtfHoldingsUpdate(BaseModel):
    """Manual bulk replace — yfinance-sourced rows are never touched."""

    items: list[EtfHoldingInput] = Field(max_length=250)


class SecurityOverview(BaseModel):
    """One payload for the per-security dashboard page (docs/05, M13).

    `positions` rows are `valuation.compute_positions` dicts filtered to this
    security — the same shape as GET /portfolio/positions items.
    """

    security: SecurityRead
    latest: LatestPrice | None = None
    stats: SecurityStats
    history: list[HistoryPoint] = []
    positions: list[dict] = []
    totals: OverviewTotals
    watchlisted: bool
    fundamentals: FundamentalsRead | None = None
    recent_trades: list[RecentTrade] = []
    holdings: EtfHoldingsBlock | None = None


class SplitEventRead(BaseModel):
    """One split Yahoo reported for the symbol (M149, docs/07 §Price frame)."""

    date: date
    #: `numerator / denominator` as a Decimal string — `8.0` for an 8:1 split,
    #: `0.1` for a 1:10 reverse split.
    ratio: str


class SplitRepairRead(BaseModel):
    """`POST /securities/{id}/split-repair` verdict (M149, docs/07)."""

    security_id: int
    symbol: str
    start: date
    end: date
    #: Closes the refetch produced for the window, in the LEDGER's frame.
    fetched: int
    #: Every split the refetch un-adjusted the window with; empty when the
    #: symbol has split nowhere.
    splits: list[SplitEventRead] = []
    updated: int
    inserted: int
    unchanged: int
    #: `source=manual` rows the repair refused to touch (manual wins).
    skipped_manual: int
    #: Machine-readable refusal token — `no_closes` when Yahoo answered
    #: nothing for the window, in which case the cache was left untouched.
    refused: str | None = None

"""reports — capital-gains (docs/06) + aggregation report shapes (docs/12, docs/08)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CapitalGainsItem(BaseModel):
    security: str
    security_id: int
    symbol: str
    proceeds_cad: Decimal
    acb_cad: Decimal
    gain_loss_cad: Decimal
    outlays_commissions_cad: Decimal


class CapitalGainsTotals(BaseModel):
    proceeds_cad: Decimal
    acb_cad: Decimal
    gain_loss_cad: Decimal
    outlays_commissions_cad: Decimal


class SuperficialLossDenialOut(BaseModel):
    security_id: int
    symbol: str
    sale_date: dt.date
    denied_loss_cad: Decimal
    outcome: str  # "deferral" | "registered"
    acquiring_account_id: int | None = None


class CapitalGainsReport(BaseModel):
    year: int
    items: list[CapitalGainsItem]
    totals: CapitalGainsTotals
    denials: list[SuperficialLossDenialOut]
    disclaimer: str


class SpendingItem(BaseModel):
    bucket: dt.date
    category_id: int | None
    category_name: str
    total_cad: Decimal


class TagSpendingItem(BaseModel):
    """One bucket of a `group_by=tag` series (M124, docs/12 §1).

    A separate model rather than a nullable pair on `SpendingItem`: the two
    dimensions are alternatives, so the JSON says which one it is by which
    fields are present, and `format=csv` header names stay pinned to the
    fields actually emitted (docs/12 §CSV).
    """

    bucket: dt.date
    tag_id: int | None
    tag_name: str
    total_cad: Decimal


class SpendingReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: dt.date = Field(serialization_alias="from")
    to: dt.date
    granularity: str
    group_by: Literal["category", "tag"] = "category"
    items: list[SpendingItem] | list[TagSpendingItem]
    total_cad: Decimal
    # Each transaction counted ONCE, whatever `items` did with it. Under
    # `group_by=tag` this is the honest denominator `total_cad` is not.
    spend_total_cad: Decimal


class CategorySlice(BaseModel):
    category_id: int | None
    name: str
    cad_total: Decimal


class TagSlice(BaseModel):
    """A donut slice under `group_by=tag` — same shape, different id field."""

    tag_id: int | None
    name: str
    cad_total: Decimal


class CategoryBreakdownReport(BaseModel):
    group_by: Literal["category", "tag"] = "category"
    items: list[CategorySlice] | list[TagSlice]
    total_cad: Decimal
    spend_total_cad: Decimal


class MatrixColumn(BaseModel):
    tag_id: int | None
    tag_name: str


class MatrixRow(BaseModel):
    category_id: int | None
    category_name: str


class MatrixCell(BaseModel):
    """One non-zero cell of the Category x Tag matrix, long form (docs/12 §1).

    Long form so `format=csv` is the same rows with a pinned header, and so a
    sparse matrix (most category x tag pairs are empty) does not ship a grid
    of zeros.
    """

    category_id: int | None
    category_name: str
    tag_id: int | None
    tag_name: str
    cad_total: Decimal


class CategoryTagMatrixReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: dt.date = Field(serialization_alias="from")
    to: dt.date
    columns: list[MatrixColumn]
    rows: list[MatrixRow]
    items: list[MatrixCell]
    total_cad: Decimal


class CashflowBucket(BaseModel):
    bucket: dt.date
    income_cad: Decimal
    expenses_cad: Decimal
    net_cad: Decimal


class CashflowReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: dt.date = Field(serialization_alias="from")
    to: dt.date
    granularity: str
    buckets: list[CashflowBucket]


class NetWorthBucket(BaseModel):
    bucket_end: dt.date
    cash_cad: Decimal
    positions_cad: Decimal
    net_cad: Decimal
    # M97 (docs/12 §4): the presentation split of the SAME signed sum, for
    # the net-worth tooltip — assets - liabilities == cash + positions. Each
    # cell rounds on its own, so the two rounded cells need not re-subtract
    # to net_cad exactly (same rule as cash + positions).
    assets_cad: Decimal
    liabilities_cad: Decimal


class NetWorthReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: dt.date = Field(serialization_alias="from")
    to: dt.date
    granularity: str
    buckets: list[NetWorthBucket]


class PerformanceItem(BaseModel):
    security_id: int
    symbol: str
    quantity: Decimal
    realized_cad: Decimal
    unrealized_cad: Decimal | None
    acb_pool_cad: Decimal | None
    market_value_cad: Decimal


class PerformanceTotals(BaseModel):
    realized_cad: Decimal
    unrealized_cad: Decimal
    market_value_cad: Decimal


class PerformanceReport(BaseModel):
    items: list[PerformanceItem]
    totals: PerformanceTotals


class DividendItem(BaseModel):
    security_id: int
    symbol: str
    name: str
    dividend_cad: Decimal
    distribution_cad: Decimal
    roc_cad: Decimal
    total_cad: Decimal
    payment_count: int
    last_payment_date: dt.date | None = None


class DividendTotals(BaseModel):
    dividend_cad: Decimal
    distribution_cad: Decimal
    roc_cad: Decimal
    total_cad: Decimal


class DividendMonth(BaseModel):
    month: str
    total_cad: Decimal


class DividendProvenanceSource(BaseModel):
    """One source behind a dividend view — WHICH feed, WHEN it last answered,
    and whether it is stale or missing (docs/07 §M184).

    `kind` is the honesty label: `market_data` sources age (they can go stale),
    `ledger` sources are recorded facts and answer ok/missing only.
    """

    key: str
    kind: Literal["market_data", "ledger"]
    label: str
    provider: str | None = None
    as_of: dt.date | None = None
    age_days: int | None = None
    status: Literal["ok", "stale", "missing"]
    detail: str


class DividendProvenance(BaseModel):
    """Freshness/provenance block carried by every dividend view.

    `status` is the WORST source status and `as_of` the OLDEST source
    timestamp — a view is only as fresh as its least fresh evidence.
    """

    status: Literal["ok", "stale", "missing"]
    as_of: dt.date | None = None
    sources: list[DividendProvenanceSource]


class DividendReport(BaseModel):
    year: int
    available_years: list[int]
    items: list[DividendItem]
    by_month: list[DividendMonth]
    totals: DividendTotals
    ttm_total_cad: Decimal
    unresolved_fx: int
    provenance: DividendProvenance


# --- Cash-flow Sankey (M77, docs/12 §Sankey cash-flow) -----------------------


class SankeyNode(BaseModel):
    id: str
    column: Literal["source", "total", "destination"]
    label: str
    amount_cad: Decimal
    group: str | None = None


class SankeyLink(BaseModel):
    source: str
    target: str
    amount_cad: Decimal


class SankeyTotals(BaseModel):
    income_cad: Decimal
    expenses_cad: Decimal
    invested_cad: Decimal
    saved_cad: Decimal
    drawn_cad: Decimal
    money_in_cad: Decimal


class CashflowSankeyReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: dt.date = Field(serialization_alias="from")
    to: dt.date
    columns: list[str]
    nodes: list[SankeyNode]
    links: list[SankeyLink]
    totals: SankeyTotals


# --- Total return — TWR / MWR (M77, docs/05 §Total return) ------------------


class TotalReturnReport(BaseModel):
    """Both returns are PERIOD returns over `[from, to]`, fixed 8 decimals.

    `twr_pct` / `mwr_pct` are plain strings (`"12.46915167"`) — never
    scientific notation — and are `null` when the metric is undefined for the
    window (no day opened with a portfolio value / zero Modified-Dietz base),
    never a numeric 0 standing in for "no data" (M65 honesty rule).
    """

    model_config = ConfigDict(populate_by_name=True)

    from_: dt.date = Field(serialization_alias="from")
    to: dt.date
    status: Literal["ok", "no_data"]
    start_value_cad: Decimal
    end_value_cad: Decimal
    net_flow_cad: Decimal
    income_cad: Decimal
    twr_pct: str | None
    mwr_pct: str | None
    valuation_days: int
    active_days: int
    flow_days: int
    account_count: int


# --- Cash-flow calendar (M52, docs/36) --------------------------------------


class CashflowCalendarEvent(BaseModel):
    """One scheduled cash-flow item on one day.

    `amount` is SIGNED (dividend positive, bill/subscription negative) and
    `currency` is the ISO code it is STATED in — the pair is all-or-nothing,
    so a figure is never rendered under a currency it was not recorded in
    (docs/36 §Currency). Both are `None` when the source carries no honest
    figure: a bill with a variable amount, or an accepted recurring group
    whose tagged rows do not agree on one currency (mixed / unknown).
    """

    kind: Literal["bill", "dividend", "subscription"]
    label: str
    amount: Decimal | None
    currency: str | None
    source_id: str
    status: Literal["past", "today", "upcoming"]


class CashflowCalendarDay(BaseModel):
    date: dt.date
    events: list[CashflowCalendarEvent]


class CashflowCalendarReport(BaseModel):
    """Zero-filled month: exactly one entry per calendar day (docs/36).

    `confirmed_subscription_count` (M170) is the size of the user's WHOLE
    confirmed set — active bills + accepted recurring groups, deduped — not a
    count of this month's events. The grid says which of them fall here; this
    number is what lets the client distinguish "no subscription payments this
    month" from "no confirmed subscriptions at all" and render the honest
    empty state instead of silence (docs/36 §M170).
    """

    month: str
    today: dt.date
    days: list[CashflowCalendarDay]
    confirmed_subscription_count: int

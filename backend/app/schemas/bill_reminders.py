"""bill_reminders — shapes (M40 + M55 + M116 match rules, docs/25)."""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.transactions import TransactionPage
from app.services.match_terms import MAX_TERM_LENGTH, MAX_TERMS, validate_terms

# docs/25 M55 decision 11: `name`, `frequency` and `next_due_date` stay required
# exactly when the draft is NOT seeded from a detected subscription, so the M40
# contract (and its 422 `validation_error` envelope) is unchanged.
_DRAFT_FIELDS = ("name", "frequency", "next_due_date")

BillMatchModeLiteral = Literal["contains", "starts_with", "exact"]
# M121 D2 (docs/25 §M121): ANY = widening (default), ALL = narrowing.
# `Literal` over the DB column's two values, so an unknown mode is a 422 at the
# seam instead of a row nothing can interpret.
BillMatchLogicLiteral = Literal["any", "all"]
BillFrequencyLiteral = Literal["monthly", "quarterly", "yearly", "one_time"]

# M116 D1: the term is bounded in BOTH directions — 1..120 chars, and it must
# still name something after the shared normalizer runs (a term of `"123"`
# normalizes to nothing and would match everything or nothing). M121 bounds the
# LIST the same way (1..10 terms): a rule surface that can grow without limit
# is a scan cost nobody chose (docs/25 §M121 D7).
_MATCH_TERM_MAX = MAX_TERM_LENGTH
_MATCH_TERMS_MAX = MAX_TERMS


def resolve_terms(
    match_terms: Sequence[str] | None,
    match_term: str | None,
    *,
    required: bool,
    field: str = "match_terms",
) -> list[str]:
    """The effective term list of a rule request — validated or 422.

    `match_terms` is the M121 field; `match_term` (singular) is the pre-M121
    wire field and still accepted, because every client written before this
    milestone must keep working unchanged (docs/25 §M121 D5):

    * `match_terms` alone        → those terms, in order;
    * `match_term` alone         → the ONE-term list `[match_term]`;
    * both                       → `match_term` must be `match_terms[0]`
      (a client that disagrees with itself is refused, never silently
      resolved one way — AGENTS §3);
    * neither (and not required) → no terms: `POST /bills` derives them from
      `name` in the handler, exactly as a blank `match_term` always did.

    Every term is then bounded by the shared `validate_terms` (trimmed,
    1..120 chars, must name a payee, 1..10 terms), so the three surfaces that
    read terms validate them the same way.
    """
    raw = [str(term) for term in (match_terms or [])]
    legacy = (match_term or "").strip()
    if raw and legacy and raw[0].strip() != legacy:
        raise ValueError(f"{field}[0] must equal match_term when both are sent")
    terms = [term.strip() for term in raw] if raw else ([legacy] if legacy else [])
    if not terms:
        if required:
            raise ValueError(f"{field} must name at least one term")
        return []
    return validate_terms(terms, field=field)


class BillReminderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Nullable at the schema layer only: a model validator re-requires them
    # unless `from_subscription_id` supplies the draft (docs/25 M55).
    name: str | None = Field(default=None, min_length=1, max_length=200)
    amount: Decimal | None = None
    category_id: int | None = None
    frequency: str | None = Field(default=None, pattern="^(monthly|quarterly|yearly|one_time)$")
    # Typed date: Pydantic rejects garbage with 422 (docs/25 M47 envelope).
    next_due_date: date | None = None
    remind_days_before: int = Field(default=3, ge=0, le=30)
    enabled: bool = True
    # M55: detector `payee` of the subscription seeding this draft (docs/25).
    from_subscription_id: str | None = Field(default=None, min_length=1, max_length=200)
    # --- M116 (docs/25 §M116 D1) -------------------------------------------
    # Absent OR BLANK = derive from `name` in the handler (after the M55 draft
    # has resolved what `name` actually is), so a bill always ships with a
    # rule: the create form's match-term field starts empty, and an untouched
    # field is the same statement as not sending one. A NON-blank term is
    # validated hard — letters required after `normalize_payee_key` runs.
    match_mode: BillMatchModeLiteral = "contains"
    match_term: str | None = Field(default=None, max_length=_MATCH_TERM_MAX)
    # M121 D1 (docs/25 §M121): the term LIST. Absent, empty, or only-blank
    # chips all mean the same thing as a blank `match_term` always did —
    # derive from `name` in the handler — so the create form can start with no
    # chips at all. What is SENT is validated hard.
    match_terms: list[str] | None = None
    match_logic: BillMatchLogicLiteral = "any"
    amount_tolerance: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _draft_fields_required_without_subscription(self) -> "BillReminderCreate":
        resolve_terms(self.match_terms, self.match_term, required=False)
        if self.from_subscription_id is None:
            missing = [field for field in _DRAFT_FIELDS if getattr(self, field) is None]
            if missing:
                raise ValueError(f"Field required: {', '.join(missing)}")
        return self


class BillReminderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    amount: Decimal | None = None
    category_id: int | None = None
    frequency: str | None = Field(default=None, pattern="^(monthly|quarterly|yearly|one_time)$")
    next_due_date: date | None = None
    remind_days_before: int | None = Field(default=None, ge=0, le=30)
    enabled: bool | None = None
    # M116: the amount band lives in the RULE (`PUT /bills/{id}/rule`), so
    # `PATCH /bills/{id}` cannot set it — it can only clear it by nulling the
    # amount the band is measured against (refused, see the handler).


class BillRuleUpdate(BaseModel):
    """`PUT /bills/{id}/rule` — a full replacement of the match rule.

    PUT, not PATCH: a rule is read as a whole ("contains NETFLIX, ±$5") and a
    partially-applied mode/term pair is never a state the user asked for.
    """

    model_config = ConfigDict(extra="forbid")

    match_mode: BillMatchModeLiteral = "contains"
    # Optional at the schema layer ONLY so a client may send `match_terms`
    # instead; `required=True` below makes "neither" a 422 (this endpoint has
    # no `name` to derive from — it REPLACES the whole rule).
    match_term: str | None = Field(default=None, max_length=_MATCH_TERM_MAX)
    match_terms: list[str] | None = None
    match_logic: BillMatchLogicLiteral = "any"
    amount_tolerance: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _terms_must_match_something(self) -> "BillRuleUpdate":
        resolve_terms(self.match_terms, self.match_term, required=True)
        return self


class BillRulePreviewRequest(BaseModel):
    """`POST /bills/find-past` — run an UNSAVED rule (the create form).

    Everything the saved-rule route (`/bills/{id}/find-past`) reads off the row
    is explicit here instead, because no row exists yet.
    """

    model_config = ConfigDict(extra="forbid")

    match_mode: BillMatchModeLiteral = "contains"
    match_term: str | None = Field(default=None, max_length=_MATCH_TERM_MAX)
    match_terms: list[str] | None = None
    match_logic: BillMatchLogicLiteral = "any"
    amount_tolerance: Decimal | None = Field(default=None, ge=0)
    category_id: int | None = None
    amount: Decimal | None = None
    frequency: BillFrequencyLiteral = "monthly"

    @model_validator(mode="after")
    def _terms_must_match_something(self) -> "BillRulePreviewRequest":
        resolve_terms(self.match_terms, self.match_term, required=True)
        return self


class BillFindPastRequest(BaseModel):
    """`POST /bills/{id}/find-past` — the saved rule, optionally overridden.

    Fields are absent by default, so an empty body means "run exactly what is
    stored". `model_fields_set` (the M55 decision-10 rule) tells an explicit
    `null` apart from "not sent", so a tolerance can be cleared.
    """

    model_config = ConfigDict(extra="forbid")

    match_mode: BillMatchModeLiteral | None = None
    # `min_length=1` is M116's own rule kept: an EXPLICITLY blank term on this
    # route is a mistake, not "use the saved rule" — only ABSENT means that.
    match_term: str | None = Field(default=None, min_length=1, max_length=_MATCH_TERM_MAX)
    match_terms: list[str] | None = None
    match_logic: BillMatchLogicLiteral | None = None
    amount_tolerance: Decimal | None = Field(default=None, ge=0)
    # M123 (docs/25 §Create from search): the category scope is part of the
    # rule, so an edit that moves it must be testable BEFORE it is saved.
    # Absent = the saved `bill.category_id`, same rule as every field here.
    category_id: int | None = None

    @model_validator(mode="after")
    def _terms_must_match_something(self) -> "BillFindPastRequest":
        # Not required: absent means "run the SAVED rule". Anything that IS
        # sent is validated exactly like the rule endpoint's own terms.
        resolve_terms(self.match_terms, self.match_term, required=False)
        return self


class BillReminderPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    amount: Decimal | None
    category_id: int | None
    frequency: str
    next_due_date: str
    remind_days_before: int
    enabled: bool
    # M116 (docs/25 §M116 D1)
    match_mode: str
    match_term: str
    # M121 (docs/25 §M121 D1/D2). `match_term` above is terms[0] and never
    # goes away (pre-M121 readers keep working). `match_terms` lives in a CHILD
    # table, so a bare ORM row cannot supply it through `from_attributes` —
    # the default keeps `model_validate(bill)` working and
    # `api/bill_reminders.py::_public` replaces it with the loaded list.
    match_terms: list[str] = Field(default_factory=list)
    match_logic: str
    amount_tolerance: Decimal | None


class BillStatPublic(BillReminderPublic):
    """A bill plus what its rule found over the whole committed ledger.

    `GET /bills` and the `bill` inside `GET /bills/{id}/detail` answer with
    this; create/update/rule carry the plain shape, so no write path ever pays
    for a scan it did not ask for.
    """

    payment_count: int
    rolling_average: Decimal | None
    # M122 T4: calendar-year-to-date spend from the SAME matched set (Jan 1 →
    # today, server clock) — the compact list row's YTD figure. `0.00` is the
    # honest "nothing this year yet", never a placeholder.
    ytd_total: Decimal


# --- M116 B2/B3 — the bill detail page -------------------------------------


class BillChartPoint(BaseModel):
    period: str  # bucket key = first date of the period (reports bucket shape)
    label: str
    total: Decimal
    count: int
    missed: bool
    # M122 T3: trailing ≤6-period mean ENDING at this point (missed periods
    # as $0.00). The last point's value IS `rolling.six.mean` — the chart line
    # and the card's stat are one number, server-computed.
    rolling_six: Decimal | None = None


class BillChart(BaseModel):
    frequency: str
    unit: Literal["month", "quarter", "year"]
    granularity: Literal["month", "quarter", "year"]
    first_period: str | None
    last_period: str | None
    points: list[BillChartPoint]


class BillRollingWindow(BaseModel):
    periods: int
    window_periods: int
    payments: int
    # M122 T2/T4: the window's SUM — "what the last N periods actually cost".
    total: Decimal | None
    mean: Decimal | None
    median: Decimal | None


class BillRolling(BaseModel):
    frequency: str
    six: BillRollingWindow
    twelve: BillRollingWindow


# --- M122 T4 — all-time + YTD spend stats ------------------------------------


class BillAllTimeStats(BaseModel):
    """The matched set over its whole life (docs/25 §M122 D4).

    Missed periods never enter `total` — that is spend that did not happen;
    the rolling windows keep their missed-$0.00 semantics separately.
    """

    total: Decimal
    payments: int
    average: Decimal | None  # per payment; None while nothing matched
    years: int  # distinct calendar years holding >= 1 payment
    yearly_average: Decimal | None  # total / years (quantized to cents)


class BillYtdStats(BaseModel):
    """Calendar year to date (Jan 1 -> server `today_et()`), not the
    bill's anniversary."""

    year: int
    total: Decimal
    payments: int


class BillSpendingStats(BaseModel):
    all_time: BillAllTimeStats
    ytd: BillYtdStats


class BillDetail(BaseModel):
    """`GET /bills/{id}/detail` — everything the drill-down renders."""

    bill: BillStatPublic
    matched_count: int
    transactions: TransactionPage
    rolling: BillRolling
    chart: BillChart
    # M122 T4: recomputed from the LIFETIME matched set on every read — the
    # `from`/`to` window above never narrows these (M91 anti-fabrication).
    stats: BillSpendingStats


# --- M116 B1 — "Find past payments" ----------------------------------------


class BillFindPastItem(BaseModel):
    id: int
    date: date
    amount_native: Decimal
    currency: str
    cad_amount: Decimal | None
    normalized_payee: str
    account_id: int
    account_name: str


class BillFindPastGroup(BaseModel):
    period: str
    label: str
    count: int
    total: Decimal
    items: list[BillFindPastItem]


class BillFindPastAccountCount(BaseModel):
    """M122 T2: how many of the rule's matches sit on one account."""

    account_id: int
    account_name: str
    count: int


class BillFindPastStats(BaseModel):
    """M122 T2 — the dry-run's summary, computed over the WHOLE matched set.

    Never over the previewed slice: `items` truncates at the 500-row cap, and
    a summary that silently described the slice would be the fabricated number
    the M91 rule exists to prevent.
    """

    first_date: date | None
    last_date: date | None
    # Sum of the last 12 CALENDAR period totals (missed periods as $0.00) —
    # the same population `rolling.twelve` divides on the detail page.
    last_12_total: Decimal | None
    account_counts: list[BillFindPastAccountCount]


class BillFindPast(BaseModel):
    match_mode: str
    match_term: str
    match_terms: list[str]
    match_logic: str
    frequency: str
    total_count: int
    truncated: bool
    groups: list[BillFindPastGroup]
    # M122 T2: the panel's headline numbers ("Draft preview — not saved").
    stats: BillFindPastStats


# --- M122 T1 — term suggestions from the caller's own ledger -----------------


class BillSuggestTerm(BaseModel):
    """One payee group the ledger actually contains (docs/25 §M122 D1).

    `normalized_payee` is a REAL string from the caller's own committed rows
    (the newest spelling of the group) — frequency is social proof, and
    nothing here can hallucinate a payee that does not exist in the data.
    """

    normalized_payee: str
    count: int
    total: Decimal  # CAD magnitude of the group (same as the chart's bars)
    last_date: date

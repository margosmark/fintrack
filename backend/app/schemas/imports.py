"""imports — batch summary + parsed preview shapes (docs/03, docs/08)."""

from datetime import date as dt_date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.import_batches import ImportBatchStatus


class NearDuplicateMatchPublic(BaseModel):
    """A committed/staged/batch twin of a row — surfaced, never auto-dropped (docs/03)."""

    source: str  # committed | staged | batch
    transaction_id: int | None = None
    date: dt_date
    amount_native: Decimal
    currency: str
    raw_description: str
    delta_days: int
    similarity: float


class PreviewRow(BaseModel):
    id: int
    date: dt_date
    amount_native: Decimal
    currency: str
    raw_description: str
    duplicate: bool
    cad_amount: Decimal | None = None
    # M71 near-duplicate verdict (docs/03): false is the default so older
    # clients keep rendering; a near-duplicate is staged as a normal row and
    # flagged here for the user's decision — it is never silently skipped.
    near_duplicate: bool = False
    near_duplicate_match: NearDuplicateMatchPublic | None = None


class FieldRole(BaseModel):
    """One column-role mapping the dry-run preview shows before any write."""

    field: str  # date | amount_native | debit | credit | raw_description | posting_date | currency
    source: str  # CSV header name / 1-based position, or the QIF/OFX token


class FormatMapping(BaseModel):
    format: str  # ofx | qif | csv (QFX files report "ofx" — same format)
    kind: str  # csv_profile | structured
    fields: list[FieldRole]
    mode: str | None = None  # signed | split (CSV only)
    date_format: str | None = None
    decimal_comma: bool | None = None
    credits_positive: bool | None = None
    signs: dict[str, int] | None = None
    headerless: bool | None = None


class DryRunRow(BaseModel):
    """One parsed row of a cancel-safe preview — no id, nothing persisted."""

    index: int
    date: dt_date
    amount_native: Decimal
    currency: str
    raw_description: str
    cad_amount: Decimal | None = None
    verdict: str  # new | duplicate | near_duplicate
    near_duplicate_match: NearDuplicateMatchPublic | None = None


class DryRunSummary(BaseModel):
    total: int
    new: int
    duplicates: int
    near_duplicates: int
    unresolved: int  # cad_amount unresolved (no cached fx rate for the date)


class DryRunResponse(BaseModel):
    """POST /imports/preview — full staged parse with ZERO writes (docs/03)."""

    format: str
    mapping: FormatMapping
    rows: list[DryRunRow]
    summary: DryRunSummary


class ImportBatchPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    institution: str
    file_name: str
    status: ImportBatchStatus
    imported: int
    duplicates_skipped: int
    unresolved: int


class UploadResponse(ImportBatchPublic):
    preview: list[PreviewRow]


class ImportBatchPage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[ImportBatchPublic]
    total: int
    limit: int
    offset: int


class SuggestProfileResponse(BaseModel):
    """Response from POST /imports/suggest-profile (LLM or heuristic)."""

    columns: dict[str, str]
    date_format: str = "%Y-%m-%d"
    decimal_comma: bool = False
    credits_positive: bool = False
    headerless: bool = False
    confidence: float = 0.0  # 0.0-1.0; a format preset answers 0.95
    reasoning: str = ""
    # Known export format that resolved this mapping (docs/58 §A), else None.
    format: str | None = None


class NetWorthAccountPlan(BaseModel):
    """One account the net-worth file would touch (docs/58 §B)."""

    name: str
    source_type: str | None = None  # the file's own type/subtype token
    mapped_type: str | None = None  # fin_tracker AccountType value
    action: str  # create | reuse | unmapped
    existing_id: int | None = None
    file_balance: str  # Decimal as a string (docs/01)
    current_balance: str | None = None  # reuse only: what the account holds now
    currency: str
    reason: str | None = None
    notes: list[str] = []


class NetWorthSkipItem(BaseModel):
    """Something the file carries that was NOT imported — never silent."""

    kind: str  # asset | progress_history | plan_data | account
    name: str
    reason: str


class NetWorthWarning(BaseModel):
    code: str
    detail: str


class NetWorthCounts(BaseModel):
    accounts: int = 0
    accounts_create: int = 0
    accounts_reuse: int = 0
    accounts_unmapped: int = 0
    balance_rows_collapsed: int = 0
    progress_snapshots_skipped: int = 0
    assets_skipped: int = 0


class NetWorthPlanResponse(BaseModel):
    """Full preview plan for POST /imports/networth/preview (writes nothing)."""

    source_format: str  # projectionlab_json | balance_csv
    file_name: str
    currency: str
    counts: NetWorthCounts
    accounts: list[NetWorthAccountPlan]
    skipped: list[NetWorthSkipItem]
    warnings: list[NetWorthWarning]
    can_commit: bool


class NetWorthCommitResult(NetWorthPlanResponse):
    """The plan plus what the single-transaction commit did."""

    created: int
    reused: int


class CommitBatchBody(BaseModel):
    """Optional allow-list of preview row ids to force-include at commit."""

    include_ids: list[int] = []

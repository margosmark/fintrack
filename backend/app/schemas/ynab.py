"""ynab — migration plan / result shapes for POST /imports/ynab/* (docs/38).

The plan is the whole preview-first contract: every account, category, tag and
target the commit would touch, plus the complete `unmappable` list (never
truncated — AGENTS §3: reject or flag, never skip).
"""

from datetime import date as dt_date
from decimal import Decimal

from pydantic import BaseModel

# Row lists are capped so a 50k-row budget still answers in one round trip;
# `unmappable` is NOT capped — it is the acceptance surface.
PREVIEW_ROW_CAP = 500


class YnabAccountPlan(BaseModel):
    ynab_id: str
    name: str
    ynab_type: str  # YNAB's own type token (checking/creditCard/mortgage/...)
    mapped_type: str | None = None  # fin_tracker AccountType value, None when unmapped
    action: str  # create | reuse | unmapped
    existing_id: int | None = None
    currency: str
    closed: bool = False
    on_budget: bool = True
    type_assumed: bool = False  # CSV register: no type in the file
    reason: str | None = None


class YnabGroupPlan(BaseModel):
    ynab_id: str
    name: str
    action: str  # create | reuse
    existing_id: int | None = None
    tag_name: str | None = None  # group name carried onto its rows as a tag
    tag_action: str | None = None  # create | reuse | skipped
    hidden: bool = False


class YnabCategoryPlan(BaseModel):
    ynab_id: str
    name: str
    group: str | None = None
    group_ynab_id: str | None = None
    action: str  # create | reuse
    existing_id: int | None = None


class YnabTargetPlan(BaseModel):
    category_ynab_id: str
    category_name: str
    goal_type: str
    kind: str  # budget | goal | unsupported
    amount: str | None = None  # Decimal as a string (docs/01 serialization)
    period: str | None = None
    target_date: str | None = None
    action: str  # create | reuse | skipped
    reason: str | None = None


class YnabIssuePlan(BaseModel):
    code: str
    reason: str
    index: int | None = None  # 1-based source row; None = file-level note
    detail: str | None = None


class YnabRowPlan(BaseModel):
    index: int
    date: dt_date
    amount: Decimal
    currency: str
    account: str | None
    payee: str
    description: str
    category: str | None
    group: str | None
    tag: str | None
    cleared: str | None
    transfer: bool = False
    # CAD view of `amount` through the fx cache (docs/07); None = no rate row.
    cad_amount: Decimal | None = None
    # None = the target account is created by this run, so the dedup hash is
    # only computable at commit (the hash includes the account id).
    duplicate: bool | None = None


class YnabTransactionPlan(BaseModel):
    total: int  # transaction rows read from the file
    importable: int
    duplicates: int = 0  # verdict known now (target account already exists)
    duplicates_pending: int = 0  # verdict deferred to commit
    transfer_pairs: int = 0
    rows: list[YnabRowPlan]
    rows_truncated: bool = False
    unmappable: list[YnabIssuePlan]  # complete, never truncated
    warnings: list[YnabIssuePlan]
    warnings_truncated: bool = False


class YnabCounts(BaseModel):
    accounts_create: int = 0
    accounts_reuse: int = 0
    accounts_unmapped: int = 0
    groups_create: int = 0
    groups_reuse: int = 0
    categories_create: int = 0
    categories_reuse: int = 0
    tags_create: int = 0
    tags_reuse: int = 0
    budgets_create: int = 0
    budgets_reuse: int = 0
    goals_create: int = 0
    goals_reuse: int = 0
    targets_unsupported: int = 0


class YnabMigrationPlan(BaseModel):
    source_format: str  # json | csv
    budget_name: str
    currency: str
    date_format: str | None = None  # CSV: the date order actually applied
    accounts: list[YnabAccountPlan]
    category_groups: list[YnabGroupPlan]
    categories: list[YnabCategoryPlan]
    targets: list[YnabTargetPlan]
    transactions: YnabTransactionPlan
    notes: list[YnabIssuePlan]
    counts: YnabCounts


class YnabBatchResult(BaseModel):
    batch_id: int
    account_id: int
    account_name: str
    imported: int
    duplicates_skipped: int
    unresolved: int


class YnabCommitResult(BaseModel):
    plan: YnabMigrationPlan
    batches: list[YnabBatchResult]
    net_new: int  # staged rows that become live at batch commit
    duplicates_skipped: int  # staged duplicates, excluded from live reads

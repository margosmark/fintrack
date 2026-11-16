"""YNAB → fin_tracker migration: preview-first plan builder + applier (docs/38).

Two entry points share one plan:

* ``build_plan`` — READ-ONLY. Resolves every YNAB entity against the tenant's
  existing rows (accounts / categories / tags / budgets / goals matched
  case-insensitively by name, so a re-import reuses rather than duplicates),
  and classifies every transaction row as importable or **unmappable with a
  reason**. It writes nothing: the preview endpoint must leave every table's
  row count untouched (docs/38 verification).
* ``apply_plan`` — creates what the plan marked ``create``, then stages the
  importable transactions into per-account ``import_batches``
  (status ``pending_review``) **through the shared
  ``services.import_staging.stage_rows`` pipeline** (docs/03): batch creation,
  the docs/03 dedup contract (the canonical hash spans pending AND committed
  rows, so a second run stages duplicates instead of net-new rows), the
  counters and the 2000-row cap all live there. This module only decorates
  each row with what YNAB knows (``category_id``, ``cleared``) and links the
  group tags and transfer pairs after the flush.

Deviation (noted in docs/38): M71's shared dry-run contract had not landed
when this card was built, so the preview IS this plan builder rather than a
reused M71 dry-run helper.
"""

from dataclasses import dataclass, field
from datetime import date as dt_date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.importers.base import RawTransaction, build_dedup_hash, normalized_payee
from app.importers.ynab import YnabBudget, YnabTxn
from app.models.accounts import Account, AccountType
from app.models.budgets import Budget
from app.models.categories import Category
from app.models.goals import Goal
from app.models.tags import Tag
from app.models.transaction_tags import TransactionTag
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.ynab import (
    PREVIEW_ROW_CAP,
    YnabAccountPlan,
    YnabCategoryPlan,
    YnabCounts,
    YnabGroupPlan,
    YnabIssuePlan,
    YnabMigrationPlan,
    YnabRowPlan,
    YnabTargetPlan,
    YnabTransactionPlan,
)
from app.services.fx import cached_cad_amount
from app.services.import_staging import RowDecorator, stage_rows

CENT = Decimal("0.01")
THOUSAND = Decimal("1000")
TAG_NAME_MAX = 100
MISSING_PAYEE_PLACEHOLDER = "(YNAB import)"
WARNING_CAP = 500

# YNAB account type → fin_tracker AccountType (docs/38 mapping table). Keys are
# normalized (lowercased, `_`/`-`/spaces stripped), so `creditCard`,
# `credit_card` and `CREDIT CARD` all land on the same row.
YNAB_ACCOUNT_TYPES: dict[str, str] = {
    "checking": "chequing",
    "cash": "chequing",
    "savings": "savings",
    "otherasset": "savings",
    "creditcard": "credit_card",
    "lineofcredit": "line_of_credit",
    "mortgage": "line_of_credit",
    "autoloan": "line_of_credit",
    "studentloan": "line_of_credit",
    "personalloan": "line_of_credit",
    "medicaldebt": "line_of_credit",
    "otherdebt": "line_of_credit",
    "otherliability": "line_of_credit",
}


def map_account_type(ynab_type: str) -> str | None:
    key = ynab_type.strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    return YNAB_ACCOUNT_TYPES.get(key)


@dataclass
class PlanOptions:
    """Knobs the endpoints pass in (a CSV register carries no definitions)."""

    default_currency: str = "CAD"
    fallback_account_id: int | None = None  # CSV rows with no Account column
    file_name: str = "ynab-export"
    row_cap: int = PREVIEW_ROW_CAP


@dataclass
class _RowBinding:
    """What apply needs for one importable row (DB ids resolved at write time)."""

    index: int
    ynab_id: str | None
    account_ref: str
    date: dt_date
    amount: Decimal
    currency: str
    description: str
    category_ref: str | None
    tag_name: str | None
    cleared: str | None
    transfer_ref: str | None  # counterpart YNAB transaction id
    transfer: bool


@dataclass
class ResolvedPlan:
    plan: YnabMigrationPlan
    file_name: str = "ynab-export"
    account_plans: dict[str, YnabAccountPlan] = field(default_factory=dict)
    account_ids: dict[str, int] = field(default_factory=dict)  # ref → db id (reuse)
    category_ids: dict[str, int] = field(default_factory=dict)
    group_ids: dict[str, int] = field(default_factory=dict)
    tag_ids: dict[str, int] = field(default_factory=dict)
    rows: list[_RowBinding] = field(default_factory=list)


def build_plan(db: Session, user: User, budget: YnabBudget, opts: PlanOptions) -> ResolvedPlan:
    """Read-only mapping of the export onto this tenant's data."""
    currency = (budget.currency or opts.default_currency).upper()
    notes = [
        YnabIssuePlan(code=n.code, reason=n.reason, index=n.index, detail=n.detail)
        for n in budget.notes
    ]

    existing_accounts = {
        a.name.strip().lower(): a
        for a in db.exec(select(Account).where(Account.user_id == user.id)).all()
    }
    existing_categories = {
        c.name.strip().lower(): c
        for c in db.exec(select(Category).where(Category.user_id == user.id)).all()
    }
    existing_tags = {
        t.name.strip().lower(): t for t in db.exec(select(Tag).where(Tag.user_id == user.id)).all()
    }
    existing_budgets = {
        b.category_id: b for b in db.exec(select(Budget).where(Budget.user_id == user.id)).all()
    }
    existing_goals = {
        g.name.strip().lower(): g
        for g in db.exec(select(Goal).where(Goal.user_id == user.id)).all()
    }

    counts = YnabCounts()
    account_plans: dict[str, YnabAccountPlan] = {}
    for acct in budget.accounts:
        plan = _account_plan(acct, existing_accounts, currency, counts)
        account_plans[acct.ynab_id] = plan
        if plan.action == "reuse" and plan.currency != currency:
            notes.append(
                YnabIssuePlan(
                    code="account_currency_kept",
                    reason=(
                        f"`{plan.name}` already exists in {plan.currency}; its rows keep that "
                        f"currency instead of the export's {currency}"
                    ),
                )
            )
        if acct.closed and plan.action != "unmapped":
            notes.append(
                YnabIssuePlan(
                    code="account_closed_in_ynab",
                    reason=f"`{acct.name}` is closed in YNAB — imported as an open account",
                )
            )

    if any(t.account_ref is None for t in budget.transactions) and opts.fallback_account_id:
        fallback = db.get(Account, opts.fallback_account_id)
        if fallback is not None and fallback.user_id == user.id:
            ref = f"existing:{fallback.id}"
            account_plans[ref] = YnabAccountPlan(
                ynab_id=ref,
                name=fallback.name,
                ynab_type="supplied",
                mapped_type=fallback.type.value,
                action="reuse",
                existing_id=fallback.id,
                currency=fallback.currency,
                reason="Target account supplied with the upload (rows carry no Account column)",
            )
            counts.accounts_reuse += 1

    # Groups → parent categories, plus one tag per group carried onto its rows.
    group_plans: list[YnabGroupPlan] = []
    for group in budget.groups:
        if group.deleted:
            continue
        existing = existing_categories.get(group.name.strip().lower())
        action = "reuse" if existing is not None else "create"
        if action == "create":
            counts.groups_create += 1
        else:
            counts.groups_reuse += 1
        tag_name: str | None = None
        tag_action: str | None = None
        if len(group.name) <= TAG_NAME_MAX:
            tag_name = group.name
            tag_action = "reuse" if group.name.strip().lower() in existing_tags else "create"
            if tag_action == "create":
                counts.tags_create += 1
            else:
                counts.tags_reuse += 1
        else:
            tag_action = "skipped"
            notes.append(
                YnabIssuePlan(
                    code="tag_name_too_long",
                    reason=(
                        f"Group `{group.name}` is over {TAG_NAME_MAX} characters, so its rows "
                        "are imported without that tag"
                    ),
                )
            )
        group_plans.append(
            YnabGroupPlan(
                ynab_id=group.ynab_id,
                name=group.name,
                action=action,
                existing_id=existing.id if existing is not None else None,
                tag_name=tag_name,
                tag_action=tag_action,
                hidden=group.hidden,
            )
        )
    group_by_id = {g.ynab_id: g for g in group_plans}

    # Categories → child categories under the group's category (docs/38).
    category_plans: list[YnabCategoryPlan] = []
    for cat in budget.categories:
        if cat.deleted:
            continue
        group = group_by_id.get(cat.group_id or "")
        existing = existing_categories.get(cat.name.strip().lower())
        action = "reuse" if existing is not None else "create"
        if action == "create":
            counts.categories_create += 1
        else:
            counts.categories_reuse += 1
        category_plans.append(
            YnabCategoryPlan(
                ynab_id=cat.ynab_id,
                name=cat.name,
                group=group.name if group else None,
                group_ynab_id=group.ynab_id if group else None,
                action=action,
                existing_id=existing.id if existing is not None else None,
            )
        )
    category_by_id = {c.ynab_id: c for c in category_plans}

    targets = _target_plans(budget, category_by_id, existing_budgets, existing_goals, notes, counts)

    # Transaction rows.
    unmappable = [
        YnabIssuePlan(code=i.code, reason=i.reason, index=i.index, detail=i.detail)
        for i in budget.unmappable
    ]
    warnings: list[YnabIssuePlan] = []
    row_plans: list[YnabRowPlan] = []
    bindings: list[_RowBinding] = []
    seen_hashes: set[str] = set()
    duplicates = pending_duplicates = 0
    quantized = False
    payee_by_id = {p.ynab_id: p.name for p in budget.payees}

    for txn in budget.transactions:
        ref = txn.account_ref
        if ref is None:
            if opts.fallback_account_id is None:
                unmappable.append(
                    YnabIssuePlan(
                        code="unknown_account",
                        index=txn.index,
                        reason="Row carries no account and no target account was supplied",
                        detail=txn.raw,
                    )
                )
                continue
            ref = f"existing:{opts.fallback_account_id}"
        account_plan = account_plans.get(ref)
        if account_plan is None:
            unmappable.append(
                YnabIssuePlan(
                    code="unknown_account",
                    index=txn.index,
                    reason="Row references an account that is not in this export",
                    detail=txn.raw,
                )
            )
            continue
        if account_plan.action == "unmapped":
            unmappable.append(
                YnabIssuePlan(
                    code="account_unmappable",
                    index=txn.index,
                    reason=(
                        f"Account `{account_plan.name}` cannot be imported: "
                        f"{account_plan.reason or 'unmapped'}"
                    ),
                    detail=txn.raw,
                )
            )
            continue

        description, payee, payee_missing = _describe(txn, payee_by_id)
        if payee_missing:
            warnings.append(
                YnabIssuePlan(
                    code="missing_payee",
                    index=txn.index,
                    reason="Row has no payee name and no memo — imported with a placeholder",
                    detail=txn.raw,
                )
            )
        elif budget.source_format == "json" and txn.payee_ref and txn.payee_ref not in payee_by_id:
            warnings.append(
                YnabIssuePlan(
                    code="unknown_payee",
                    index=txn.index,
                    reason="Row's payee id is not in the export's payee list — memo used instead",
                    detail=txn.raw,
                )
            )

        category = category_by_id.get(txn.category_ref or "")
        if txn.category_ref and category is None:
            warnings.append(
                YnabIssuePlan(
                    code="unknown_category",
                    index=txn.index,
                    reason=(
                        "Row's category is deleted or missing in the export — left uncategorized"
                    ),
                    detail=txn.raw,
                )
            )
        group = group_by_id.get(category.group_ynab_id or "") if category else None
        if txn.split_count:
            warnings.append(
                YnabIssuePlan(
                    code="split_detail_dropped",
                    index=txn.index,
                    reason=(
                        f"Row is a split with {txn.split_count} sub-row(s); the total imports "
                        "but the per-part categories do not (fin_tracker does not import YNAB "
                        "subtransactions — split rows import as the parent total; you can split "
                        "them by hand afterwards)"
                    ),
                    detail=txn.raw,
                )
            )
        if txn.cleared_issue:
            warnings.append(
                YnabIssuePlan(
                    code="cleared_not_recognized",
                    index=txn.index,
                    reason="Cleared value is not uncleared/cleared/reconciled — left unset",
                    detail=txn.raw,
                )
            )

        amount = txn.amount.quantize(CENT, rounding=ROUND_HALF_UP)
        if amount != txn.amount:
            quantized = True
        row_plan = YnabRowPlan(
            index=txn.index,
            date=txn.date,
            amount=amount,
            currency=account_plan.currency,
            account=account_plan.name,
            payee=payee,
            description=description,
            category=category.name if category else None,
            group=group.name if group else None,
            tag=group.tag_name if group else None,
            cleared=txn.cleared,
            transfer=bool(txn.transfer_account_ref or txn.transfer_txn_ref)
            or _looks_like_transfer(description),
        )
        row_plan.cad_amount = cached_cad_amount(db, account_plan.currency, txn.date, amount)
        if account_plan.existing_id is not None:
            canonical = build_dedup_hash(
                account_plan.existing_id,
                RawTransaction(
                    date=txn.date,
                    amount_native=amount,
                    raw_description=description,
                    currency=account_plan.currency,
                ),
            )
            exists = (
                db.exec(
                    select(Transaction.id)
                    .where(Transaction.account_id == account_plan.existing_id)
                    .where(Transaction.dedup_hash == canonical)
                    .limit(1)
                ).first()
                is not None
            )
            duplicate = exists or canonical in seen_hashes
            seen_hashes.add(canonical)
            duplicates += int(duplicate)
        else:
            duplicate = None
            pending_duplicates += 1
        row_plan.duplicate = duplicate
        row_plans.append(row_plan)
        bindings.append(
            _RowBinding(
                index=txn.index,
                ynab_id=txn.ynab_id,
                account_ref=ref,
                date=txn.date,
                amount=amount,
                currency=account_plan.currency,
                description=description,
                category_ref=category.ynab_id if category else None,
                tag_name=group.tag_name if group else None,
                cleared=txn.cleared,
                transfer_ref=txn.transfer_txn_ref,
                transfer=row_plan.transfer,
            )
        )

    # Transfer counters whose counterpart never made it into the importable set.
    importable_ids = {b.ynab_id for b in bindings if b.ynab_id}
    for binding in bindings:
        if binding.transfer_ref and binding.transfer_ref not in importable_ids:
            warnings.append(
                YnabIssuePlan(
                    code="transfer_counterpart_missing",
                    index=binding.index,
                    reason="Transfer counterpart is not importable — the pair cannot be linked",
                    detail=binding.description,
                )
            )

    if quantized:
        notes.append(
            YnabIssuePlan(
                code="amounts_quantized",
                reason=(
                    "Some YNAB amounts had sub-cent precision (milliunits); they were rounded "
                    "to cents — `transactions.amount_native` is Numeric(18,2) (docs/02)"
                ),
            )
        )

    transfer_pairs = _count_transfer_pairs(bindings)
    plan = YnabMigrationPlan(
        source_format=budget.source_format,
        budget_name=budget.name,
        currency=currency,
        date_format=budget.date_format,
        accounts=list(account_plans.values()),
        category_groups=group_plans,
        categories=category_plans,
        targets=targets,
        transactions=YnabTransactionPlan(
            total=budget.source_rows,
            importable=len(bindings),
            duplicates=duplicates,
            duplicates_pending=pending_duplicates,
            transfer_pairs=transfer_pairs,
            rows=row_plans[: opts.row_cap],
            rows_truncated=len(row_plans) > opts.row_cap,
            unmappable=unmappable,
            warnings=warnings[:WARNING_CAP],
            warnings_truncated=len(warnings) > WARNING_CAP,
        ),
        notes=notes,
        counts=counts,
    )
    return ResolvedPlan(
        plan=plan,
        file_name=opts.file_name,
        account_plans=account_plans,
        account_ids={
            ref: p.existing_id
            for ref, p in account_plans.items()
            if p.action == "reuse" and p.existing_id is not None
        },
        category_ids={
            c.ynab_id: c.existing_id
            for c in category_plans
            if c.action == "reuse" and c.existing_id is not None
        },
        group_ids={
            g.ynab_id: g.existing_id
            for g in group_plans
            if g.action == "reuse" and g.existing_id is not None
        },
        rows=bindings,
    )


def _account_plan(acct, existing_accounts, currency: str, counts: YnabCounts) -> YnabAccountPlan:
    """Classify one YNAB account: create / reuse (same name) / unmapped."""
    plan = YnabAccountPlan(
        ynab_id=acct.ynab_id,
        name=acct.name,
        ynab_type=acct.ynab_type,
        mapped_type=map_account_type(acct.ynab_type),
        action="create",
        currency=currency,
        closed=acct.closed,
        on_budget=acct.on_budget,
        type_assumed=acct.type_assumed,
    )
    if acct.deleted:
        plan.action = "unmapped"
        plan.reason = "Account is marked deleted in YNAB"
    elif not acct.name.strip():
        plan.action = "unmapped"
        plan.reason = "Account has no name"
    elif plan.mapped_type is None:
        plan.action = "unmapped"
        plan.reason = (
            f"YNAB account type `{acct.ynab_type}` has no fin_tracker equivalent (docs/38)"
        )
    else:
        existing = existing_accounts.get(acct.name.strip().lower())
        if existing is not None:
            plan.action = "reuse"
            plan.existing_id = existing.id
            plan.currency = existing.currency
            plan.reason = "An account with this name already exists — reused, not duplicated"

    if plan.action == "unmapped":
        counts.accounts_unmapped += 1
    elif plan.action == "reuse":
        counts.accounts_reuse += 1
    else:
        counts.accounts_create += 1
    return plan


def _describe(txn: YnabTxn, payee_by_id: dict[str, str]) -> tuple[str, str, bool]:
    """(description, payee, missing_payee) — the memo rides in the description (docs/38)."""
    payee = ""
    if txn.payee_ref:
        payee = (payee_by_id.get(txn.payee_ref) or "").strip()
    memo = (txn.memo or "").strip()
    if payee and memo:
        description = f"{payee} — {memo}"
    elif memo:
        description = memo
    elif payee:
        description = payee
    else:
        description = MISSING_PAYEE_PLACEHOLDER
    return description, normalized_payee(description), not (payee or memo)


def _looks_like_transfer(description: str) -> bool:
    lowered = description.lower()
    return lowered.startswith("transfer") and ":" in description


def _count_transfer_pairs(bindings: list[_RowBinding]) -> int:
    """Both sides of a YNAB transfer present AND importable → one linked pair."""
    by_ynab_id = {b.ynab_id: b for b in bindings if b.ynab_id}
    counted: set[str] = set()
    pairs = 0
    for binding in bindings:
        if not binding.ynab_id or binding.ynab_id in counted or not binding.transfer_ref:
            continue
        other = by_ynab_id.get(binding.transfer_ref)
        if other is None or other.transfer_ref != binding.ynab_id:
            continue
        counted.add(binding.ynab_id)
        counted.add(other.ynab_id or "")
        pairs += 1
    return pairs


def _target_plans(
    budget: YnabBudget,
    category_by_id: dict[str, YnabCategoryPlan],
    existing_budgets: dict[int, Budget],
    existing_goals: dict[str, Goal],
    notes: list[YnabIssuePlan],
    counts: YnabCounts,
) -> list[YnabTargetPlan]:
    """YNAB targets → fin_tracker Budget (monthly funding) or Goal (balance/date)."""
    plans: list[YnabTargetPlan] = []
    rounded = False
    for cat in budget.categories:
        if cat.deleted or not cat.goal_type or cat.goal_target is None:
            continue
        plan = category_by_id.get(cat.ynab_id)
        if plan is None:
            continue
        exact = Decimal(cat.goal_target) / THOUSAND
        amount = exact.quantize(CENT, rounding=ROUND_HALF_UP)
        if amount != exact:
            rounded = True
        if amount <= 0:
            continue
        goal_type = cat.goal_type
        base = dict(
            category_ynab_id=cat.ynab_id,
            category_name=cat.name,
            goal_type=goal_type,
        )
        if goal_type == "MF":
            reused = plan.existing_id is not None and plan.existing_id in existing_budgets
            counts.budgets_reuse += int(reused)
            counts.budgets_create += int(not reused)
            plans.append(
                YnabTargetPlan(
                    kind="budget",
                    amount=str(amount),
                    period="monthly",
                    target_date=None,  # a monthly funding target has no due date
                    action="reuse" if reused else "create",
                    reason="Monthly funding target — the existing budget is kept"
                    if reused
                    else None,
                    **base,
                )
            )
        elif goal_type in {"TB", "TBD"}:
            goal_name = f"{cat.name} target"
            reused = goal_name.lower() in existing_goals
            counts.goals_reuse += int(reused)
            counts.goals_create += int(not reused)
            plans.append(
                YnabTargetPlan(
                    kind="goal",
                    amount=str(amount),
                    target_date=cat.goal_target_month,
                    action="reuse" if reused else "create",
                    reason="A goal with this name already exists — kept" if reused else None,
                    **base,
                )
            )
        else:
            counts.targets_unsupported += 1
            plans.append(
                YnabTargetPlan(
                    kind="unsupported",
                    target_date=cat.goal_target_month,
                    action="skipped",
                    reason=(
                        f"YNAB goal type `{goal_type}` has no fin_tracker equivalent "
                        "(docs/38 gaps table)"
                    ),
                    **base,
                )
            )
    if rounded:
        notes.append(
            YnabIssuePlan(
                code="targets_rounded",
                reason="Some targets had sub-cent precision and were rounded to cents",
            )
        )
    return plans


# ------------------------------------------------------------------------ apply


def _ynab_row_decorator(resolved: ResolvedPlan, bindings: list[_RowBinding]) -> RowDecorator:
    """The per-row extras `stage_rows` cannot know: YNAB's category + clearing state.

    `bindings[i]` is the source binding for `rows[i]` — `stage_rows` walks the
    sequence in order, so the position is the key. Returns the hook rather than
    mutating afterwards, so the whole row (these two columns included) is built
    once, before the INSERT.
    """

    def decorate(txn: Transaction, _raw: RawTransaction, index: int) -> None:
        binding = bindings[index]
        txn.category_id = resolved.category_ids.get(binding.category_ref or "")
        txn.cleared = binding.cleared

    return decorate


def apply_plan(
    db: Session,
    user: User,
    budget: YnabBudget,
    resolved: ResolvedPlan,
) -> tuple[list[dict], int, int]:
    """Write the plan: ``(batch_results, net_new, duplicates_skipped)``.

    ONE transaction — either the whole migration lands or nothing does.
    """
    plan = resolved.plan

    # 1. Accounts.
    for ref, account_plan in resolved.account_plans.items():
        if account_plan.action != "create":
            continue
        account = Account(
            user_id=user.id,
            name=account_plan.name,
            type=AccountType(account_plan.mapped_type),
            currency=account_plan.currency,
        )
        # Taxability is server-derived (docs/14 C1) — same rule as POST /accounts.
        account.taxable = account.type == AccountType.brokerage_non_registered
        db.add(account)
        db.flush()
        resolved.account_ids[ref] = account.id

    # 2. Group → parent categories.
    for group in plan.category_groups:
        if group.action == "create":
            row = Category(user_id=user.id, name=group.name)
            db.add(row)
            db.flush()
            resolved.group_ids[group.ynab_id] = row.id
        elif group.existing_id is not None:
            resolved.group_ids[group.ynab_id] = group.existing_id

    # 3. Categories (children of the group's category when YNAB had a group).
    for category in plan.categories:
        if category.action == "reuse":
            if category.existing_id is not None:
                resolved.category_ids[category.ynab_id] = category.existing_id
            continue
        row = Category(
            user_id=user.id,
            name=category.name,
            parent_id=resolved.group_ids.get(category.group_ynab_id or ""),
        )
        db.add(row)
        db.flush()
        resolved.category_ids[category.ynab_id] = row.id

    # 4. Tags — one per group; UNIQUE(user_id, name) is the idempotency guard.
    for group in plan.category_groups:
        if not group.tag_name:
            continue
        existing = db.exec(
            select(Tag).where(Tag.user_id == user.id).where(Tag.name == group.tag_name)
        ).first()
        if existing is None and group.tag_action == "create":
            existing = Tag(user_id=user.id, name=group.tag_name)
            db.add(existing)
            db.flush()
        if existing is not None:
            resolved.tag_ids[group.tag_name] = existing.id

    # 5. Targets → budgets / goals.
    for target in plan.targets:
        if target.action != "create" or not target.amount:
            continue
        category_id = resolved.category_ids.get(target.category_ynab_id)
        if category_id is None:
            continue
        amount = Decimal(target.amount)
        if target.kind == "budget":
            db.add(
                Budget(
                    user_id=user.id,
                    category_id=category_id,
                    amount=amount,
                    period=target.period or "monthly",
                )
            )
        elif target.kind == "goal":
            db.add(
                Goal(
                    user_id=user.id,
                    name=f"{target.category_name} target",
                    target_amount=amount,
                    target_date=target.target_date,
                )
            )
    db.flush()

    # 6. Transactions → one staged batch per target account.
    rows_by_account: dict[str, list[_RowBinding]] = {}
    for binding in resolved.rows:
        rows_by_account.setdefault(binding.account_ref, []).append(binding)

    batch_results: list[dict] = []
    net_new = duplicates_skipped = 0
    txn_by_ynab_id: dict[str, Transaction] = {}
    for ref, bindings in rows_by_account.items():
        account_id = resolved.account_ids.get(ref)
        if account_id is None:
            continue
        account = db.get(Account, account_id)
        # ONE staging path (docs/03): `stage_rows` builds the batch, hashes the
        # rows, probes committed+pending rows, salts duplicates, resolves the
        # cached CAD amount, sets the counters and enforces `MAX_STAGED_ROWS`
        # (413 `too_many_rows` rolls the whole migration back). YNAB only adds
        # the two columns it knows (via `decorate`) and links tags below.
        staged = stage_rows(
            db,
            user=user,
            account=account,
            institution_name="",
            file_name=resolved.file_name,
            rows=[
                RawTransaction(
                    date=binding.date,
                    amount_native=binding.amount,
                    raw_description=binding.description,
                    currency=binding.currency,
                )
                for binding in bindings
            ],
            source_label="YNAB import",
            decorate=_ynab_row_decorator(resolved, bindings),
        )

        # Post-flush decoration — ids exist now: group tags on this account's
        # staged rows, and the ynab-id → staged-row map the transfer pairing
        # below reads. Both stay here; the pipeline stays source-neutral.
        for position, row in enumerate(staged.rows):
            binding = bindings[position]
            if binding.ynab_id:
                txn_by_ynab_id[binding.ynab_id] = row.txn
            if not binding.tag_name:
                continue
            tag_id = resolved.tag_ids.get(binding.tag_name)
            if tag_id is None:
                continue
            link = db.exec(
                select(TransactionTag)
                .where(TransactionTag.transaction_id == row.txn_id)
                .where(TransactionTag.tag_id == tag_id)
                .limit(1)
            ).first()
            if link is None:
                db.add(TransactionTag(transaction_id=row.txn_id, tag_id=tag_id))

        net_new += staged.imported
        duplicates_skipped += staged.duplicates_skipped
        batch_results.append(
            {
                "batch_id": staged.batch.id,
                "account_id": account_id,
                "account_name": account.name if account else "",
                "imported": staged.imported,
                "duplicates_skipped": staged.duplicates_skipped,
                "unresolved": staged.unresolved,
            }
        )

    # 7. Link both sides of every transfer whose counterpart landed in this run.
    for binding in resolved.rows:
        if not binding.transfer_ref or not binding.ynab_id:
            continue
        left = txn_by_ynab_id.get(binding.ynab_id)
        right = txn_by_ynab_id.get(binding.transfer_ref)
        if left is None or right is None or left is right:
            continue
        if left.transfer_pair_id or right.transfer_pair_id:
            continue
        left.transfer_pair_id = right.id
        right.transfer_pair_id = left.id

    db.commit()
    return batch_results, net_new, duplicates_skipped

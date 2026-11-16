"""rules — priority-ordered categorization rules CRUD (docs/02, docs/04, docs/08).

Regex patterns must compile (case-insensitive use) or the write is rejected
with 422 invalid_regex. Amount matchers compare against transaction.amount_native.

M137 P1-2 (docs/33 §Server-first computing): `POST /rules/preview` is
RE-MOUNTED. M48-S3 deleted it and the browser dry-ran the draft over a
full-ledger download; the verdict is now the server's again, from the same
`match_transactions` engine `POST /rules/{id}/apply` writes with — so a preview
and the apply it precedes can never disagree about what matched.
"""

import re
from datetime import date as dt_date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import (
    Pagination,
    get_current_user,
    get_db,
    get_pagination,
    owned_or_404,
    paginate,
)
from app.core.errors import api_error
from app.models.categories import Category
from app.models.import_batches import ImportBatch
from app.models.rule_tags import RuleTag
from app.models.rules import Rule, RuleMatchType
from app.models.tags import Tag
from app.models.users import User
from app.schemas.rules import (
    RuleApplyRequest,
    RuleApplyResponse,
    RuleCreate,
    RulePage,
    RulePreviewRequest,
    RulePreviewResponse,
    RulePublic,
    RuleUpdate,
)
from app.services.rules_engine import apply_single_rule, match_transactions
from app.services.txn_filters import countable_tx_conditions

router = APIRouter(prefix="/rules", tags=["rules"])

AMOUNT_MATCH_TYPES = {
    RuleMatchType.amount_equals,
    RuleMatchType.amount_gt,
    RuleMatchType.amount_lt,
    RuleMatchType.amount_between,
}


def _validate_pattern(match_type: object, pattern: str | None) -> None:
    if pattern is None:
        return
    # Amount matchers: validate the pattern is a valid decimal (or pipe-separated pair).
    mt = match_type.value if hasattr(match_type, "value") else match_type
    if mt in ("amount_equals", "amount_gt", "amount_lt"):
        try:
            Decimal(pattern)
        except InvalidOperation:
            raise api_error(
                422, "Amount pattern must be a valid number", "invalid_amount"
            ) from None
    elif mt == "amount_between":
        parts = pattern.split("|")
        if len(parts) != 2:
            raise api_error(422, "Between pattern must be 'min|max'", "invalid_amount") from None
        for part in parts:
            try:
                Decimal(part.strip())
            except InvalidOperation:
                raise api_error(
                    422, f"Amount '{part.strip()}' is not a valid number", "invalid_amount"
                ) from None
    elif mt == "regex":
        # ReDoS defense-in-depth (#4): bound pattern size before compile-check.
        if len(pattern) > 256:
            raise api_error(
                422, "Pattern exceeds maximum length of 256 characters", "pattern_too_long"
            )
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise api_error(422, f"Pattern is not a valid regex: {exc}", "invalid_regex") from None


def _validate_category(category_id: int | None, user: User, db: Session) -> None:
    if category_id is None:
        return
    category = db.get(Category, category_id)
    if category is None or category.user_id != user.id:
        raise api_error(404, "Category not found", "not_found")


def _validate_tags(tag_ids: list[int], user: User, db: Session) -> None:
    """Ensure all tag_ids exist and are owned by the user."""
    for tid in tag_ids:
        owned_or_404(Tag, tid, user, db)


def _sync_rule_tags(rule: Rule, tag_ids: list[int], db: Session) -> None:
    """Replace all rule_tags for the given rule."""
    existing = db.exec(select(RuleTag).where(RuleTag.rule_id == rule.id)).all()
    for row in existing:
        db.delete(row)
    for tid in tag_ids:
        db.add(RuleTag(rule_id=rule.id, tag_id=tid))


def _load_rule_tags(rule_id: int, db: Session) -> list[Tag]:
    """Load tags attached to a rule via the junction table."""
    rows = db.exec(
        select(Tag)
        .join(RuleTag, RuleTag.tag_id == Tag.id)
        .where(RuleTag.rule_id == rule_id)
        .order_by(Tag.name)
    ).all()
    return list(rows)


def _enrich_rule(rule: Rule, db: Session) -> RulePublic:
    """Build RulePublic with eagerly loaded tags."""
    pub = RulePublic.model_validate(rule)
    pub.tags = _load_rule_tags(rule.id, db)
    return pub


@router.get("", response_model=RulePage)
def list_rules(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
) -> RulePage:
    scope = Rule.user_id == current_user.id
    stmt = select(Rule).where(scope).order_by(Rule.priority.asc(), Rule.id.asc())
    rows, total = paginate(db, stmt, limit=pagination.limit, offset=pagination.offset)
    items = [_enrich_rule(r, db) for r in rows]
    return RulePage(items=items, total=total, limit=pagination.limit, offset=pagination.offset)


@router.post("", response_model=RulePublic, status_code=201)
def create_rule(
    payload: RuleCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RulePublic:
    _validate_pattern(payload.match_type.value, payload.pattern)
    _validate_category(payload.category_id, current_user, db)
    _validate_tags(payload.tag_ids, current_user, db)
    data = payload.model_dump(exclude={"tag_ids"})
    rule = Rule(**data, user_id=current_user.id)
    db.add(rule)
    db.flush()  # Get rule.id for junction rows.
    _sync_rule_tags(rule, payload.tag_ids, db)
    db.commit()
    db.refresh(rule)
    return _enrich_rule(rule, db)


@router.post("/preview", response_model=RulePreviewResponse)
def preview_rule(
    payload: RulePreviewRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RulePreviewResponse:
    """Dry-run a rule draft (docs/31): count + sample, never writes.

    M137 P1-2: re-mounted — the verdict is the SERVER's answer, computed by
    the same engine the apply path uses, over the committed scope
    (`_scoped_transactions`) the apply path writes to.
    """
    _validate_pattern(payload.match_type.value, payload.pattern)
    # Transient predicate carrier: required row fields get benign values;
    # the instance is never added to the session.
    draft = Rule(
        user_id=current_user.id,
        match_type=payload.match_type,
        pattern=payload.pattern,
        priority=0,
        category_id=0,
    )
    scoped = _scoped_transactions(
        db, current_user.id, payload.from_, payload.to, only_unassigned=False
    )
    matched = match_transactions(draft, scoped)
    matched.sort(key=lambda t: (t.date, t.id or 0), reverse=True)
    visible = [t for t in matched if not payload.only_unassigned or t.category_id is None]
    cat_names = _category_names(db, current_user.id)
    return RulePreviewResponse(
        matched_count=len(visible),
        assigned_skipped=len(matched) - len(visible) if payload.only_unassigned else 0,
        items=[
            {
                "id": t.id,
                "date": t.date,
                "payee": t.normalized_payee,
                "amount_native": str(t.amount_native),
                "category_id": t.category_id,
                "category_name": cat_names.get(t.category_id) if t.category_id else None,
            }
            for t in visible[: payload.limit]
        ],
    )


@router.post("/{rule_id}/apply", response_model=RuleApplyResponse)
def apply_rule(
    rule_id: int,
    payload: RuleApplyRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RuleApplyResponse:
    """Targeted backfill of one rule (docs/31)."""
    rule = owned_or_404(Rule, rule_id, current_user, db)
    if payload.mode == "range" and (
        payload.from_ is None or payload.to is None or payload.from_ > payload.to
    ):
        raise api_error(422, "Range mode requires from <= to dates", "invalid_date_range")
    if payload.mode == "forward":
        return RuleApplyResponse(
            rule_id=rule.id,
            mode="forward",
            matched_count=0,
            applied_count=0,
            note="Rule stays enabled; future imports are categorized by the pipeline.",
        )
    scoped = _scoped_transactions(
        db, current_user.id, payload.from_, payload.to, only_unassigned=False
    )
    matched = match_transactions(rule, scoped)
    # M120 (docs/02 §transaction_events, docs/M120_RECEIPTS_SPEC §2): one
    # receipt per row this backfill actually updates, appended into `db`
    # inside `apply_single_rule`'s loop, so the single `db.commit()` below
    # lands every change and its receipt together. The category NAME is
    # resolved once per apply call (inside the engine, which owns the loop and
    # therefore knows when it needs one) and captured at write time.
    # Rows skipped under `overwrite=false` never reach that loop, so they write
    # nothing; `mode=forward` returned before any of this.
    updated = apply_single_rule(
        db,
        rule,
        matched,
        overwrite=payload.overwrite,
        receipt_via="rule_backfill",
        actor=current_user.id,
    )
    db.commit()
    return RuleApplyResponse(
        rule_id=rule.id,
        mode=payload.mode,
        matched_count=len(matched),
        applied_count=len(updated),
    )


def _category_names(db: Session, user_id: int) -> dict:
    rows = db.exec(select(Category.id, Category.name).where(Category.user_id == user_id)).all()
    return dict(rows)


def _scoped_transactions(
    db: Session,
    user_id: int,
    from_: dt_date | None,
    to: dt_date | None,
    only_unassigned: bool,
) -> list:
    """Committed-scope transaction candidates, booking-date bounded (docs/02).

    docs/03 staging + docs/31 preview scope: only manual rows and rows from
    `committed` batches are backfillable. A `pending_review` import has not
    been reviewed yet and a `discarded` one is dead, so neither may be
    counted in a preview or written by apply. The caller outerjoins
    ImportBatch per the shared predicate's contract.
    """
    from app.models.transactions import Transaction

    stmt = (
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(*countable_tx_conditions(user_id))
    )
    if from_ is not None:
        stmt = stmt.where(Transaction.date >= from_)
    if to is not None:
        stmt = stmt.where(Transaction.date <= to)
    if only_unassigned:
        stmt = stmt.where(Transaction.category_id.is_(None))
    return list(db.exec(stmt.order_by(Transaction.date.desc())).all())


@router.get("/{rule_id}", response_model=RulePublic)
def get_rule(
    rule_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RulePublic:
    rule = owned_or_404(Rule, rule_id, current_user, db)
    return _enrich_rule(rule, db)


@router.patch("/{rule_id}", response_model=RulePublic)
def update_rule(
    rule_id: int,
    payload: RuleUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RulePublic:
    rule = owned_or_404(Rule, rule_id, current_user, db)
    data = payload.model_dump(exclude_unset=True, exclude={"tag_ids", "tag_action"})
    match_type = data.get("match_type", rule.match_type)
    _validate_pattern(
        match_type.value if hasattr(match_type, "value") else match_type,
        data.get("pattern"),
    )
    if "category_id" in data:
        _validate_category(data["category_id"], current_user, db)
    for field, value in data.items():
        setattr(rule, field, value)
    # Handle tag_action if provided.
    if "tag_action" in payload.model_fields_set and payload.tag_action is not None:
        rule.tag_action = payload.tag_action
    # Handle tag_ids if provided.
    if payload.tag_ids is not None:
        _validate_tags(payload.tag_ids, current_user, db)
        _sync_rule_tags(rule, payload.tag_ids, db)
    db.commit()
    db.refresh(rule)
    return _enrich_rule(rule, db)


@router.delete("/{rule_id}", status_code=204)
def delete_rule(
    rule_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    rule = owned_or_404(Rule, rule_id, current_user, db)
    # Clean up junction rows.
    existing = db.exec(select(RuleTag).where(RuleTag.rule_id == rule.id)).all()
    for row in existing:
        db.delete(row)
    db.delete(rule)
    db.commit()

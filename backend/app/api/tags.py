"""tags — user-created labels for transactions (docs/08, docs/02)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import delete as sa_delete
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
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
from app.models.rule_tags import RuleTag
from app.models.tags import Tag
from app.models.transaction_tags import TransactionTag
from app.models.users import User
from app.schemas.tags import (
    TagCreate,
    TagMergeRequest,
    TagMergeResult,
    TagPage,
    TagPublic,
    TagUpdate,
    TagWithUsage,
)

router = APIRouter(prefix="/tags", tags=["tags"])


def _usage_counts(db: Session, tag_ids: list[int]) -> tuple[dict[int, int], dict[int, int]]:
    """(transaction links, rule links) per tag — one grouped query per table
    (docs/34: one statement for the whole page, never one per row)."""
    if not tag_ids:
        return {}, {}
    tx = {
        int(tid): int(count)
        for tid, count in db.execute(
            select(TransactionTag.tag_id, func.count())
            .where(TransactionTag.tag_id.in_(tag_ids))
            .group_by(TransactionTag.tag_id)
        ).all()
    }
    rule = {
        int(tid): int(count)
        for tid, count in db.execute(
            select(RuleTag.tag_id, func.count())
            .where(RuleTag.tag_id.in_(tag_ids))
            .group_by(RuleTag.tag_id)
        ).all()
    }
    return tx, rule


@router.get("", response_model=TagPage)
def list_tags(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
) -> TagPage:
    scope = Tag.user_id == current_user.id
    stmt = select(Tag).where(scope).order_by(Tag.name)
    rows, total = paginate(db, stmt, limit=pagination.limit, offset=pagination.offset)
    ids = [row.id for row in rows if row.id is not None]
    tx_count, rule_count = _usage_counts(db, ids)
    items = [
        TagWithUsage(
            id=row.id,
            user_id=row.user_id,
            name=row.name,
            transaction_count=tx_count.get(row.id, 0) if row.id is not None else 0,
            rule_count=rule_count.get(row.id, 0) if row.id is not None else 0,
        )
        for row in rows
    ]
    return TagPage(items=items, total=total, limit=pagination.limit, offset=pagination.offset)


@router.post("", response_model=TagPublic, status_code=201)
def create_tag(
    payload: TagCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Tag:
    tag = Tag(user_id=current_user.id, name=payload.name.strip())
    db.add(tag)
    try:
        db.commit()
    except IntegrityError as exc:
        # Only the name-uniqueness violation is a 409; anything else
        # (e.g. a missing server default, see 0022) must not masquerade as
        # "already exists".
        db.rollback()
        if "uq_tags_user_name" not in str(exc):
            raise
        raise api_error(409, "Tag name already exists", "duplicate_tag") from None
    db.refresh(tag)
    return tag


@router.post("/merge", response_model=TagMergeResult)
def merge_tags(
    payload: TagMergeRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TagMergeResult:
    """Fold every `source_ids` tag into `target_id`, then delete the sources.

    The typo case ("Goceries" + "Groceries" both already exist) cannot be
    fixed by `PATCH` — the rename would 409 `duplicate_tag` — so the junction
    rows are re-pointed at the survivor instead. Links the target already has
    are dropped on the floor (`ON CONFLICT DO NOTHING`), so a transaction
    carrying both sides of the merge keeps exactly one row.
    """
    target = owned_or_404(Tag, payload.target_id, current_user, db)
    source_ids = list(dict.fromkeys(payload.source_ids))
    if target.id in source_ids:
        raise api_error(422, "A tag cannot be merged into itself", "validation_error")
    # Every id ownership-checked BEFORE the first write (same rule as
    # PUT /transactions/{id}/tags): a foreign source must not half-merge.
    sources = [owned_or_404(Tag, source_id, current_user, db) for source_id in source_ids]

    for source in sources:
        txn_ids = [
            int(row[0])
            for row in db.execute(
                select(TransactionTag.transaction_id).where(TransactionTag.tag_id == source.id)
            ).all()
        ]
        if txn_ids:
            db.execute(sa_delete(TransactionTag).where(TransactionTag.tag_id == source.id))
            db.execute(
                pg_insert(TransactionTag)
                .values([{"transaction_id": tid, "tag_id": target.id} for tid in txn_ids])
                .on_conflict_do_nothing(index_elements=["transaction_id", "tag_id"])
            )
        rule_ids = [
            int(row[0])
            for row in db.execute(select(RuleTag.rule_id).where(RuleTag.tag_id == source.id)).all()
        ]
        if rule_ids:
            db.execute(sa_delete(RuleTag).where(RuleTag.tag_id == source.id))
            db.execute(
                pg_insert(RuleTag)
                .values([{"rule_id": rid, "tag_id": target.id} for rid in rule_ids])
                .on_conflict_do_nothing(index_elements=["rule_id", "tag_id"])
            )
        db.delete(source)
    db.commit()
    db.refresh(target)
    return TagMergeResult(target=target, merged_tag_ids=source_ids)


@router.get("/{tag_id}", response_model=TagPublic)
def get_tag(
    tag_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Tag:
    return owned_or_404(Tag, tag_id, current_user, db)


@router.patch("/{tag_id}", response_model=TagPublic)
def update_tag(
    tag_id: int,
    payload: TagUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Tag:
    tag = owned_or_404(Tag, tag_id, current_user, db)
    tag.name = payload.name.strip()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, "Tag name already exists", "duplicate_tag") from None
    db.refresh(tag)
    return tag


@router.delete("/{tag_id}", status_code=204)
def delete_tag(
    tag_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    tag = owned_or_404(Tag, tag_id, current_user, db)
    # Block deletion if tag is referenced by transactions or rules.
    in_use_tx = db.exec(
        select(TransactionTag.id).where(TransactionTag.tag_id == tag.id).limit(1)
    ).first()
    in_use_rule = db.exec(select(RuleTag.id).where(RuleTag.tag_id == tag.id).limit(1)).first()
    if in_use_tx is not None or in_use_rule is not None:
        raise api_error(409, "Tag is in use and cannot be deleted", "tag_in_use")
    db.delete(tag)
    db.commit()

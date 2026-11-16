"""suggestions — suggest-only queue, pipeline trigger, accept/reject (docs/04).

POST /run executes pipeline layers 1-2 inline and dispatches layer-3 LLM
batches via BackgroundTasks (202 {queued: n_batches}). Accept applies the
category + learned-map upsert; reject records the negative signal that
excludes the payee from future few-shot pools.
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import Pagination, get_current_user, get_db, get_pagination, paginate
from app.api.transactions import _refuse_split_row_write
from app.core.errors import api_error
from app.models.categories import Category
from app.models.llm_suggestions import LlmSuggestion, SuggestionStatus
from app.models.transaction_events import TransactionEventKind
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.suggestions import (
    RunAccepted,
    SuggestionPage,
    SuggestionPublic,
    SuggestionRunRequest,
)
from app.services.categorization import process_llm_batches, run_pipeline_sync, upsert_learned
from app.services.tx_events import record_event

router = APIRouter(prefix="/suggestions", tags=["suggestions"])


def _owned_suggestion(suggestion_id: int, user: User, db: Session) -> LlmSuggestion:
    suggestion = db.get(LlmSuggestion, suggestion_id)
    if suggestion is None or suggestion.user_id != user.id:
        # 404 (not 409) for missing-or-foreign ids, consistent with every
        # other router (security review Low finding).
        raise api_error(404, "Suggestion not found", "not_found")
    return suggestion


@router.post("/run", response_model=RunAccepted, status_code=202)
def run_suggestions(
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    payload: SuggestionRunRequest | None = None,
) -> RunAccepted:
    batches = run_pipeline_sync(
        db,
        current_user,
        payload.transaction_ids if payload else None,
        from_=payload.from_ if payload else None,
        to=payload.to if payload else None,
    )
    if batches:
        background_tasks.add_task(process_llm_batches, current_user.id, batches)
    return RunAccepted(queued=len(batches))


@router.get("", response_model=SuggestionPage)
def list_suggestions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
    status: SuggestionStatus = SuggestionStatus.suggested,
) -> SuggestionPage:
    scope = LlmSuggestion.user_id == current_user.id
    if status is not None:
        scope &= LlmSuggestion.status == status
    stmt = (
        select(LlmSuggestion, Transaction.normalized_payee, Category.name)
        .join(Transaction, Transaction.id == LlmSuggestion.transaction_id)
        .join(Category, Category.id == LlmSuggestion.proposed_category_id)
        .where(scope)
        .order_by(LlmSuggestion.id)
    )
    rows, total = paginate(db, stmt, limit=pagination.limit, offset=pagination.offset)
    items = [
        SuggestionPublic(
            id=suggestion.id,
            transaction_id=suggestion.transaction_id,
            payee=payee,
            proposed_category_id=suggestion.proposed_category_id,
            category_name=category_name,
            confidence=suggestion.confidence,
            status=suggestion.status,
            model_used=suggestion.model_used,
        )
        for suggestion, payee, category_name in rows
    ]
    return SuggestionPage(
        items=items, total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.post("/{suggestion_id}/accept", response_model=SuggestionPublic)
def accept_suggestion(
    suggestion_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SuggestionPublic:
    suggestion = _owned_suggestion(suggestion_id, current_user, db)
    if suggestion.status != SuggestionStatus.suggested:
        raise api_error(409, "Suggestion is not pending", "suggestion_not_pending")
    transaction = db.exec(
        select(Transaction).where(
            Transaction.id == suggestion.transaction_id,
            Transaction.user_id == current_user.id,
        )
    ).first()
    category = db.exec(
        select(Category).where(
            Category.id == suggestion.proposed_category_id,
            Category.user_id == current_user.id,
        )
    ).first()
    if transaction is None or category is None:
        raise api_error(404, "Suggestion target not found", "not_found")
    # M107 (docs/44 §5): accepting a suggestion writes `category_id`, which is
    # a LEDGER field. A stale suggestion whose target has since been split must
    # not move the parent's inert category out from under D5 — the candidate
    # scan never offers one, this closes the row that was already there.
    _refuse_split_row_write(transaction, "patch", ["category_id"])
    transaction.category_id = category.id
    upsert_learned(db, current_user.id, transaction.normalized_payee, category.id)
    suggestion.status = SuggestionStatus.accepted
    # M120 (docs/02 §transaction_events): an Accept click moves `category_id`,
    # so it is a receipt like any other category move — written in the SAME
    # commit as the move, so "the row changed and the log says nothing" is not
    # a state the database can reach. `via`/`suggestion_id` name the origin;
    # the category NAME is captured here, at write time, not looked up later
    # against a category the user may have renamed (M106's own rule).
    record_event(
        db,
        tx_id=transaction.id,
        user_id=current_user.id,
        kind=TransactionEventKind.categorized,
        payload={
            "category_id": category.id,
            "category_name": category.name,
            "via": "suggestion_accept",
            "suggestion_id": suggestion.id,
        },
        actor=current_user.id,
    )
    db.commit()
    db.refresh(suggestion)
    return SuggestionPublic(
        id=suggestion.id,
        transaction_id=suggestion.transaction_id,
        payee=transaction.normalized_payee,
        proposed_category_id=suggestion.proposed_category_id,
        category_name=category.name,
        confidence=suggestion.confidence,
        status=suggestion.status,
        model_used=suggestion.model_used,
    )


@router.post("/{suggestion_id}/reject", response_model=SuggestionPublic)
def reject_suggestion(
    suggestion_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SuggestionPublic:
    suggestion = _owned_suggestion(suggestion_id, current_user, db)
    if suggestion.status != SuggestionStatus.suggested:
        raise api_error(409, "Suggestion is not pending", "suggestion_not_pending")
    # Defense-in-depth tenancy scope; tolerant fallbacks stay (never 404 here).
    transaction = db.exec(
        select(Transaction).where(
            Transaction.id == suggestion.transaction_id,
            Transaction.user_id == current_user.id,
        )
    ).first()
    category = db.exec(
        select(Category).where(
            Category.id == suggestion.proposed_category_id,
            Category.user_id == current_user.id,
        )
    ).first()
    suggestion.status = SuggestionStatus.rejected
    db.commit()
    db.refresh(suggestion)
    return SuggestionPublic(
        id=suggestion.id,
        transaction_id=suggestion.transaction_id,
        payee=transaction.normalized_payee if transaction else "",
        proposed_category_id=suggestion.proposed_category_id,
        category_name=category.name if category else "",
        confidence=suggestion.confidence,
        status=suggestion.status,
        model_used=suggestion.model_used,
    )

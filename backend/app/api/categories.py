"""categories — CRUD over the self-referencing hierarchy (docs/02, docs/08).

DELETE guards (409 category_in_use) against all referencing tables:
transactions, child categories, rules, learned_payees, llm_suggestions,
budgets, bill_reminders. `is_system` rows may be renamed but never deleted.
"""

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
from app.models.bill_reminders import BillReminder
from app.models.budgets import Budget
from app.models.categories import Category
from app.models.learned_payees import LearnedPayee
from app.models.llm_suggestions import LlmSuggestion
from app.models.rules import Rule
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.categories import (
    CategoryCreate,
    CategoryPage,
    CategoryPublic,
    CategoryUpdate,
)

router = APIRouter(prefix="/categories", tags=["categories"])


def _validate_parent(
    parent_id: int | None, user: User, db: Session, self_id: int | None = None
) -> None:
    if parent_id is None:
        return
    parent = owned_or_404(Category, parent_id, user, db)
    if self_id is not None:
        # Reject cycles: the new parent must not be the node itself or a descendant.
        seen = {self_id}
        cursor: Category | None = parent
        while cursor is not None:
            if cursor.id in seen:
                raise api_error(422, "Category parent would create a cycle", "category_cycle")
            seen.add(cursor.id)
            cursor = db.get(Category, cursor.parent_id) if cursor.parent_id else None


@router.get("")
def list_categories(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
) -> CategoryPage:
    scope = Category.user_id == current_user.id
    stmt = select(Category).where(scope).order_by(Category.id)
    rows, total = paginate(db, stmt, limit=pagination.limit, offset=pagination.offset)
    return CategoryPage(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.post("", response_model=CategoryPublic, status_code=201)
def create_category(
    payload: CategoryCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Category:
    _validate_parent(payload.parent_id, current_user, db)
    category = Category(name=payload.name, parent_id=payload.parent_id, user_id=current_user.id)
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


@router.get("/{category_id}", response_model=CategoryPublic)
def get_category(
    category_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Category:
    return owned_or_404(Category, category_id, current_user, db)


@router.patch("/{category_id}", response_model=CategoryPublic)
def update_category(
    category_id: int,
    payload: CategoryUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Category:
    category = owned_or_404(Category, category_id, current_user, db)
    data = payload.model_dump(exclude_unset=True)
    if "parent_id" in data:
        _validate_parent(data["parent_id"], current_user, db, self_id=category.id)
    for field, value in data.items():
        setattr(category, field, value)
    db.commit()
    db.refresh(category)
    return category


@router.delete("/{category_id}", status_code=204)
def delete_category(
    category_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    category = owned_or_404(Category, category_id, current_user, db)
    if category.is_system:
        raise api_error(409, "System categories cannot be deleted", "system_category")
    # docs/29 F3: every reference select is user_id-scoped; Budget and
    # BillReminder now block deletion like the other referencing tables.
    reference_checks = (
        (Transaction, Transaction.category_id),
        (Rule, Rule.category_id),
        (LearnedPayee, LearnedPayee.category_id),
        (LlmSuggestion, LlmSuggestion.proposed_category_id),
        (Budget, Budget.category_id),
        (BillReminder, BillReminder.category_id),
    )
    for _model, column in reference_checks:
        referenced = db.exec(
            select(column).where(
                column == category_id,
                _model.user_id == current_user.id,
            )
        ).first()
        if referenced is not None:
            raise api_error(409, "Category is in use; reassign references first", "category_in_use")
    has_children = db.exec(
        select(Category.id)
        .where(Category.parent_id == category_id, Category.user_id == current_user.id)
        .limit(1)
    ).first()
    if has_children is not None:
        raise api_error(409, "Category has child categories; move them first", "category_in_use")
    db.delete(category)
    db.commit()

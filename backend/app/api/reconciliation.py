"""reconciliation — compare imports against bank statements (docs/08)."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import get_current_user, get_db
from app.core.errors import api_error
from app.models.accounts import Account
from app.models.reconciliation import ReconciliationItem
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.reconciliation import ReconciliationItemPublic, ReconciliationUpload

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("/{account_id}/reconciliation")
def get_reconciliation(
    account_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    acct = db.get(Account, account_id)
    if acct is None or acct.user_id != current_user.id:
        raise api_error(404, "Account not found", "not_found")
    items = db.exec(
        select(ReconciliationItem)
        .where(
            ReconciliationItem.account_id == account_id,
            ReconciliationItem.user_id == current_user.id,
        )
        .order_by(ReconciliationItem.statement_date)
    ).all()
    matched = [i for i in items if i.status == "matched"]
    unmatched = [i for i in items if i.status == "unmatched"]
    matched_sum = sum(i.statement_amount for i in matched)
    unmatched_sum = sum(i.statement_amount for i in unmatched)
    return {
        "items": [ReconciliationItemPublic.model_validate(i) for i in items],
        "summary": {
            "total": len(items),
            "matched": len(matched),
            "unmatched": len(unmatched),
            "matched_sum": matched_sum,
            "unmatched_sum": unmatched_sum,
        },
    }


@router.post("/{account_id}/reconciliation/upload", status_code=201)
def upload_statement(
    account_id: int,
    payload: ReconciliationUpload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    acct = db.get(Account, account_id)
    if acct is None or acct.user_id != current_user.id:
        raise api_error(404, "Account not found", "not_found")
    # Clear existing unmatched items for this account (user-scoped)
    existing = db.exec(
        select(ReconciliationItem).where(
            ReconciliationItem.account_id == account_id,
            ReconciliationItem.user_id == current_user.id,
            ReconciliationItem.status == "unmatched",
        )
    ).all()
    for item in existing:
        db.delete(item)
    # Add new items (entries are typed StatementEntry rows — docs/29 F7).
    # The schema pattern only pins the digit shape; calendar validity is
    # checked here so a malformed row is a 422 `invalid_statement`, not 500.
    for entry in payload.items:
        try:
            datetime.strptime(entry.date, "%Y-%m-%d")
        except ValueError:
            raise api_error(
                422,
                f"Invalid statement entry date: {entry.date}",
                "invalid_statement",
            ) from None
        ri = ReconciliationItem(
            user_id=current_user.id,
            account_id=account_id,
            statement_date=entry.date,
            statement_amount=entry.amount,
            statement_description=entry.description or "",
        )
        db.add(ri)
    db.commit()
    return {"count": len(payload.items)}


@router.post("/{account_id}/reconciliation/match")
def match_item(
    account_id: int,
    item_id: int,
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ReconciliationItemPublic:
    ri = db.get(ReconciliationItem, item_id)
    if ri is None or ri.account_id != account_id or ri.user_id != current_user.id:
        raise api_error(404, "Item not found", "not_found")
    tx = db.get(Transaction, transaction_id)
    if tx is None or tx.user_id != current_user.id:
        raise api_error(404, "Transaction not found", "not_found")
    if tx.account_id != ri.account_id:
        raise api_error(
            409,
            "Transaction belongs to a different account",
            "transaction_account_mismatch",
        )
    # M107 (docs/44 §5): a statement line never matches PART of one. The
    # parent stays a valid target — a reconciliation entry matches the
    # statement row and writes `reconciliation_items`, never the transaction,
    # so splitting it changes nothing about that.
    if tx.parent_split_id is not None:
        raise api_error(
            409,
            "This row is part of a split. Match the statement line instead.",
            "split_leg_immutable",
        )
    ri.transaction_id = transaction_id
    ri.status = "matched"
    db.add(ri)
    db.commit()
    db.refresh(ri)
    return ri

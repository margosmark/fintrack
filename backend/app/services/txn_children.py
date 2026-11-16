"""Child-row unlink for a `transactions` delete (docs/29 F3).

`app/api/transactions.py::_purge_transaction` is the single-ROW delete path and
defines the child set: tag links, LLM suggestions and reconciliation matches.
None of those foreign keys cascade (docs/02), so any BULK delete of
`transactions` — the batch paths' duplicate removal, DISCARD and REVERT
(`app/api/imports.py`) and the terminal-batch sweep
(`app/services/batch_hygiene.py`) — has to unlink them in the same
transaction first, or the DELETE itself raises an IntegrityError and the
endpoint 500s.

Keeping the child set in one function is the whole point of docs/29 F3: a
table added here is fixed for the single-row path, the batch paths and the
repair script at once, instead of rotting in three places.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Session

from app.models.llm_suggestions import LlmSuggestion
from app.models.reconciliation import ReconciliationItem
from app.models.transaction_tags import TransactionTag


def unlink_transaction_children(
    db: Session, txn_ids: Sequence[int | None], *, user_id: int | None = None
) -> None:
    """Drop every child row hanging off `txn_ids`.

    `user_id` mirrors `_purge_transaction`'s `…_user_id == transaction.user_id`
    guard: the bulk callers pass rows already scoped to ONE owner's batch, so
    it is a tenant belt-and-braces check rather than the thing doing the
    scoping. `txn_ids` tolerates unsaved ids (`int | None` on an ORM object) by
    dropping them; an empty list is a no-op — no `IN ()` statement is emitted.
    """
    ids = [txn_id for txn_id in txn_ids if txn_id is not None]
    if not ids:
        return
    db.execute(sa_delete(TransactionTag).where(TransactionTag.transaction_id.in_(ids)))
    suggestion_filters = [LlmSuggestion.transaction_id.in_(ids)]
    reconciliation_filters = [ReconciliationItem.transaction_id.in_(ids)]
    if user_id is not None:
        suggestion_filters.append(LlmSuggestion.user_id == user_id)
        reconciliation_filters.append(ReconciliationItem.user_id == user_id)
    db.execute(sa_delete(LlmSuggestion).where(*suggestion_filters))
    db.execute(sa_delete(ReconciliationItem).where(*reconciliation_filters))

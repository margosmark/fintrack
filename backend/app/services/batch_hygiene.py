"""Terminal-batch row hygiene — the repair for pre-M112 orphans (docs/03).

`POST /imports/{batch_id}/discard|revert` delete the batch's transactions AND
trades as of M112. Batches that reached `discarded`/`reverted` BEFORE that fix
can still hold rows: prod batch 176 (`reverted`, 71 trades) and 177
(`discarded`, 70) are the known cases. Those rows are invisible to every
staging-filtered reader (they are out of scope by status) yet they are still
IN the `trades` table, so any surface that lists raw trades shows data the
user already took back.

`sweep_terminal_batch_trades` is that repair. It is deliberately scoped:

- ONLY `discarded` and `reverted` batches. A `committed` batch's rows are LIVE
  data and a `pending_review` batch is the user's undecided review — neither
  is an orphan, no matter how old it looks.
- Both row kinds, symmetric with what discard/revert are supposed to leave
  behind: `trades` and `transactions` (children unlinked via
  `services.txn_children` first).
- `acb_pools` is rebuilt for every user whose trades were removed (docs/02:
  derived state may only move through `app.tax.acb.rebuild_user_pools`).

Position-neutral by construction: `services/trade_filters` and
`services/txn_filters` already exclude `discarded`/`reverted` batches, so the
rows this removes were never counted anywhere — deleting them cannot change a
number the user can see. The tests in
`tests/test_batch_data_deletion.py` pin that byte-for-byte.

Runs DRY by default (`apply=False`) so an operator reads the report before
committing to anything; `scripts/sweep_terminal_batch_trades.py` is the CLI.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.trades import Trade
from app.models.transactions import Transaction
from app.services.txn_children import unlink_transaction_children
from app.tax.acb import rebuild_user_pools

# The two terminal statuses discard/revert produce. `committed` and
# `pending_review` are deliberately absent — see the module docstring.
TERMINAL_STATUSES = (ImportBatchStatus.discarded, ImportBatchStatus.reverted)


def orphan_terminal_batches(
    db: Session, *, user_id: int | None = None, batch_ids: list[int] | None = None
) -> list[ImportBatch]:
    """Terminal batches that still carry rows, oldest batch id first."""
    candidates = db.exec(
        select(ImportBatch)
        .where(ImportBatch.status.in_(TERMINAL_STATUSES))
        .order_by(ImportBatch.id)
    ).all()
    wanted = set(batch_ids) if batch_ids else None
    return [
        batch
        for batch in candidates
        if (user_id is None or batch.user_id == user_id)
        and (wanted is None or batch.id in wanted)
        and _carries_rows(db, batch.id)
    ]


def _count(db: Session, model: type[Any], batch_id: int) -> int:
    column = model.import_batch_id  # both row kinds key on the same column
    return len(db.exec(select(model.id).where(column == batch_id)).all())


def _carries_rows(db: Session, batch_id: int) -> bool:
    return bool(_count(db, Trade, batch_id) or _count(db, Transaction, batch_id))


def sweep_terminal_batch_trades(
    db: Session,
    *,
    user_id: int | None = None,
    batch_ids: list[int] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Report (default) or delete rows still linked to terminal batches.

    Returns a JSON-able report::

        {
          "applied": bool,             # False = dry run, nothing was deleted
          "batches": [{"batch_id", "status", "user_id",
                       "trades", "transactions"}],   # orphans only
          "trades_matched": int,        # rows the sweep would/did target
          "transactions_matched": int,
          "trades_deleted": int,        # 0 unless apply=True
          "transactions_deleted": int,
          "users_rebuilt": [user_id, ...]  # acb_pools rebuilds performed
        }

    The caller owns the transaction (same rule as `import_staging.stage_rows`):
    nothing is committed here, so a dry run and a failed run leave the database
    exactly as they found it.
    """
    report: dict[str, Any] = {
        "applied": bool(apply),
        "batches": [],
        "trades_matched": 0,
        "transactions_matched": 0,
        "trades_deleted": 0,
        "transactions_deleted": 0,
        "users_rebuilt": [],
    }
    rebuilt: set[int] = set()
    for batch in orphan_terminal_batches(db, user_id=user_id, batch_ids=batch_ids):
        # `db.execute` + row[0], the shape `imports._batch_transaction_ids`
        # already uses: an id list, not ORM objects (nothing needs the rows).
        trade_ids = [
            row[0]
            for row in db.execute(select(Trade.id).where(Trade.import_batch_id == batch.id)).all()
        ]
        txn_ids = [
            row[0]
            for row in db.execute(
                select(Transaction.id).where(Transaction.import_batch_id == batch.id)
            ).all()
        ]
        report["batches"].append(
            {
                "batch_id": batch.id,
                "status": batch.status.value,
                "user_id": batch.user_id,
                "trades": len(trade_ids),
                "transactions": len(txn_ids),
            }
        )
        report["trades_matched"] += len(trade_ids)
        report["transactions_matched"] += len(txn_ids)
        if not apply:
            continue
        unlink_transaction_children(db, txn_ids, user_id=batch.user_id)
        if txn_ids:
            db.execute(sa_delete(Transaction).where(Transaction.id.in_(txn_ids)))
            report["transactions_deleted"] += len(txn_ids)
        if trade_ids:
            db.execute(sa_delete(Trade).where(Trade.id.in_(trade_ids)))
            report["trades_deleted"] += len(trade_ids)
            if batch.user_id not in rebuilt:
                rebuilt.add(batch.user_id)
                report["users_rebuilt"].append(batch.user_id)

    # AFTER every delete: one rebuild per affected tenant, inside this
    # transaction (docs/02 — rebuild_user_pools never commits).
    for uid in sorted(rebuilt):
        rebuild_user_pools(db, uid)
    return report

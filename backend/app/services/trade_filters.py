"""Shared committed-only TRADE predicate (docs/03 staging model, docs/29 F1).

Transactions have had a committed-only scope predicate since M29
(`app/services/txn_filters.py::committed_tx_conditions`). The investment-CSV
import stages `trades` through the SAME `import_batches` lifecycle
(`trades.import_batch_id`, migration 0011) but no equivalent predicate existed,
so a batch sitting in `pending_review` — or one already `discarded` — moved
positions, ACB pools, dividends, tax alerts, benchmarks, the dividend calendar
and the security-detail page BEFORE the user ever committed it.

This module is the trades-side twin of `committed_tx_conditions`. Every
trade-derived LIVE read applies it. The import REVIEW surface
(`POST /trades/import` preview, `commit`, `discard`, and the batch row lookups)
deliberately does NOT — it is the only window into staged rows (docs/03).

Mirrors the transaction helper exactly: same shape, same outerjoin contract.
"""

from __future__ import annotations

from sqlalchemy import or_
from sqlmodel import select

from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.trades import Trade


def committed_trade_conditions(user_id: int) -> list[object]:
    """Where-clause conditions selecting only committed (or manual) trades.

    A trade is IN SCOPE when it was entered manually (`import_batch_id IS NULL`)
    or when its import batch has been committed. Rows in `pending_review`,
    `discarded`, and `reverted` batches are all OUT of scope.

    Usage (the caller owns the outerjoin, exactly as for transactions):
        stmt = (
            select(Trade, Account, Security)
            .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
            .join(Account, Trade.account_id == Account.id)
            .join(Security, Trade.security_id == Security.id)
            .where(*committed_trade_conditions(user.id))
        )
    """
    return [
        Trade.user_id == user_id,
        or_(
            ImportBatch.status == ImportBatchStatus.committed,
            Trade.import_batch_id.is_(None),
        ),
    ]


def committed_trade_rows(session, user_id: int) -> list[Trade]:
    """Convenience loader: every committed/manual trade of the user, ordered.

    Callers that only need plain trade rows; multi-entity queries (ACB replay,
    valuation) build their own statement with `committed_trade_conditions`.
    """
    stmt = (
        select(Trade)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(*committed_trade_conditions(user_id))
        .order_by(Trade.date, Trade.id)  # type: ignore[attr-defined]
    )
    return list(session.exec(stmt).all())

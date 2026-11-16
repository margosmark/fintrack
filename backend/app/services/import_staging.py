"""Shared staging pipeline — every ingestion source stages through ONE code path.

Docs: `docs/03` (ingestion) + `docs/39` (bank sync) + `docs/38` (YNAB). The
point of this module is ingestion parity by construction rather than by
convention: `POST /imports/upload` (OFX/QFX, generic CSV, and every future file
format), `POST /sync/agreements/{id}/pull` and `POST /imports/ynab/commit`
(all-or-nothing budget migration) all land here, so the dedup bridge, the
cache-only CAD rule, the `import_batches` lifecycle and the 2000-row cap are
identical for a file, a bank pull and a YNAB import.

A source that carries per-row data this pipeline cannot know (YNAB's category
and clearing state) passes a `decorate` hook; everything else — batch creation,
counters, the row cap — stays here.

Callers own the transaction: `stage_rows` only adds + flushes. Every caller
either commits (upload, pull, YNAB) or lets its outer unit of work roll back.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.errors import api_error
from app.importers.base import RawTransaction, build_dedup_hash, normalized_payee
from app.models.accounts import Account
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transactions import Transaction
from app.models.users import User
from app.services.fx import cached_cad_amount
from app.services.tx_events import TransactionEventKind, record_event

# Parsed-row cap per batch (docs/03 "Upload validation order" step 7). Shared so
# a bank pull can never stage an unbounded row set either.
MAX_STAGED_ROWS = 2000


@dataclass(frozen=True)
class StagedRow:
    """One staged `transactions` row plus the parsed row that produced it."""

    txn_id: int
    raw: RawTransaction
    currency: str
    cad_amount: Decimal | None
    duplicate: bool
    # The row itself, once flushed: callers that decorate after staging (YNAB
    # links `transaction_tags` and pairs transfers) need the id-bearing object.
    txn: Transaction


# Per-row decoration seam: source-specific fields the shared pipeline does not
# model (YNAB's `category_id` / `cleared`). Called once per row, in `rows`
# order, after the row is built and BEFORE it is added to the session, so the
# extras ride the same INSERT. It must not touch the dedup/counters contract:
# `dedup_hash`, `is_duplicate`, `import_batch_id` and the counters are owned by
# `stage_rows`.
RowDecorator = Callable[[Transaction, RawTransaction, int], None]


@dataclass(frozen=True)
class StagedBatch:
    """A freshly created `pending_review` batch and its staged rows."""

    batch: ImportBatch
    rows: list[StagedRow]

    @property
    def imported(self) -> int:
        return self.batch.imported

    @property
    def duplicates_skipped(self) -> int:
        return self.batch.duplicates_skipped

    @property
    def unresolved(self) -> int:
        return self.batch.unresolved


def stage_rows(
    db: Session,
    *,
    user: User,
    account: Account,
    institution_name: str,
    file_name: str,
    rows: Sequence[RawTransaction],
    source_label: str = "File",
    decorate: RowDecorator | None = None,
    system_actor: bool = False,
) -> StagedBatch:
    """Stage `rows` for `account` into a new `pending_review` batch.

    Dedup (docs/03): the canonical hash spans ALL of the account's rows —
    committed AND pending — so a transaction the user imported by CSV and the
    bank later reports again through sync lands as ONE live row plus a staged
    duplicate surfaced for review. The duplicate is salted so the
    `UNIQUE(account_id, dedup_hash)` constraint holds, and it is only ever
    force-included or deleted at commit time — never silently merged.

    `decorate(txn, raw, index)` (optional) is the seam for source-specific
    columns the pipeline does not model — `index` is the row's position in
    `rows`, so a caller can look its own per-row extras up without keying on
    parsed content.

    `system_actor=True` (bank sync, docs/39) stamps each row's `created`
    receipt with a NULL actor: no person was at the keyboard, and the partner
    projection (docs/43 §6) drops machine receipts from a partner's view.
    Every staged row also gets a `created` receipt, so a committed import
    lands with its history already started — "created from import Batch #412"
    is the first line of the drawer, not a gap.

    Raises `413 too_many_rows` BEFORE the batch exists, so a rejected source
    leaves no rows behind.
    """
    if len(rows) > MAX_STAGED_ROWS:
        raise api_error(
            413, f"{source_label} contains more than {MAX_STAGED_ROWS} rows", "too_many_rows"
        )

    batch = ImportBatch(
        user_id=user.id,
        account_id=account.id,
        institution=institution_name,
        file_name=file_name,
        status=ImportBatchStatus.pending_review,
    )
    db.add(batch)
    db.flush()  # assign batch.id before staging rows

    imported = duplicates_skipped = unresolved = 0
    # (obj, raw, currency, cad_amount, duplicate) staged so we can flush for ids
    # before building the returned rows.
    staged: list[tuple[Transaction, RawTransaction, str, Decimal | None, bool]] = []
    seen_canonical: set[str] = set()  # canonical hashes already kept this batch
    for index, txn in enumerate(rows):
        currency = txn.currency or account.currency
        cad_amount = cached_cad_amount(db, currency, txn.date, txn.amount_native)
        if cad_amount is None:
            unresolved += 1
        canonical = build_dedup_hash(account.id, txn)
        exists = (
            db.exec(
                select(Transaction.id)
                .where(Transaction.account_id == account.id)
                .where(Transaction.dedup_hash == canonical)
                .limit(1)
            ).first()
            is not None
        )
        duplicate = exists or canonical in seen_canonical
        if duplicate:
            # Salt the hash so the UNIQUE(account_id, dedup_hash) constraint is
            # not violated; the row is staged and can be force-included at commit.
            dedup_hash = f"{canonical}:{uuid.uuid4().hex}"
            is_dup = True
            duplicates_skipped += 1
        else:
            dedup_hash = canonical
            is_dup = False
            seen_canonical.add(canonical)
            imported += 1
        obj = Transaction(
            user_id=user.id,
            account_id=account.id,
            date=txn.date,
            posting_date=txn.posting_date,
            raw_description=txn.raw_description,
            normalized_payee=normalized_payee(txn.raw_description),
            amount_native=txn.amount_native,
            currency=currency,
            cad_amount=cad_amount,
            import_batch_id=batch.id,
            dedup_hash=dedup_hash,
            is_duplicate=is_dup,
            force_include=False,
            # M106 (docs/02): imports seed review = FALSE by writing NEITHER
            # column, which is what makes a review queue form by itself after
            # every import. Set explicitly so a future default cannot quietly
            # mark a stranger's statement pre-reviewed.
            reviewed_at=None,
            reviewed_by=None,
        )
        if decorate is not None:
            decorate(obj, txn, index)
        db.add(obj)
        staged.append((obj, txn, currency, cad_amount, duplicate))

    db.flush()  # assign ids to staged rows before returning them
    # M106 (docs/02): one `created` receipt per staged row, in the SAME
    # transaction as the rows themselves. Callers own the commit (see the
    # module docstring), so a rejected upload still leaves nothing behind.
    for obj, _raw, _currency, _cad_amount, _duplicate in staged:
        record_event(
            db,
            tx_id=obj.id,
            user_id=user.id,
            kind=TransactionEventKind.created,
            payload={
                "source": "sync" if system_actor else "import",
                "batch_id": batch.id,
                "file_name": file_name,
                "source_label": source_label,
            },
            actor=None if system_actor else user.id,
        )
    rows_out = [
        StagedRow(
            txn_id=obj.id,
            raw=raw,
            currency=currency,
            cad_amount=cad_amount,
            duplicate=duplicate,
            txn=obj,
        )
        for obj, raw, currency, cad_amount, duplicate in staged
    ]
    batch.imported = imported
    batch.duplicates_skipped = duplicates_skipped
    batch.unresolved = unresolved
    return StagedBatch(batch=batch, rows=rows_out)

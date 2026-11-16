"""Near-duplicate detection for the import preview + staging flow (docs/03).

The canonical SHA-256 dedup hash only catches a byte-identical re-upload. The
real world re-exports the same purchase with a padded payee or a posting date
one day off, and every OSS importer silently drops or silently doubles those.

M71's contract (AGENTS §3 arbitration principle): a **near-duplicate is
surfaced for the user's decision, never silently skipped and never silently
imported as if it were new.** This module only ever *reads* — it annotates
rows with a verdict, it never writes or deletes.

Verdict precedence (docs/03):

1. ``duplicate``       — canonical dedup hash already exists (committed or staged).
2. ``near_duplicate``  — this module: same account, identical amount + currency,
                         within ``NEAR_DUP_DATE_WINDOW_DAYS``, payee similarity
                         >= ``NEAR_DUP_MIN_SIMILARITY`` — but not an exact hash hit.
3. ``new``             — everything else.

Candidates are compared against committed AND staged rows (any batch), plus
the other rows of the file being previewed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as dt_date
from datetime import timedelta
from decimal import Decimal
from difflib import SequenceMatcher

from sqlmodel import Session, select

from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transactions import Transaction
from app.services.txn_filters import IDENTITY_EXCLUDE_CHILDREN

NEAR_DUP_DATE_WINDOW_DAYS = 3
NEAR_DUP_MIN_SIMILARITY = 0.75

# Source precedence when two candidates score the same: a committed twin is
# the strongest signal, then another pending batch, then a row of this file.
_SOURCE_RANK = {"committed": 0, "staged": 1, "batch": 2}

_NON_WORD = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class RowKey:
    """The minimum projection of a row the matcher needs."""

    index: int
    date: dt_date
    amount_native: Decimal
    currency: str
    payee: str
    id: int | None = None  # transactions.id when the row is already persisted


@dataclass(frozen=True)
class NearDuplicateMatch:
    source: str  # committed | staged | batch
    transaction_id: int | None
    date: dt_date
    amount_native: Decimal
    currency: str
    raw_description: str
    delta_days: int
    similarity: float


def normalize_payee(text: str) -> str:
    """Lowercase, punctuation-free, whitespace-collapsed payee key."""
    return " ".join(_NON_WORD.sub(" ", (text or "").lower()).split())


def payee_similarity(left: str, right: str) -> float:
    """Fuzzy payee score in ``[0, 1]``: max(character ratio, token Jaccard).

    The token term catches a payee that grew a store number or city suffix
    (``METRO`` vs ``METRO STORES #123 TORONTO``) where a pure character ratio
    collapses. Empty payees never match — two blank descriptions are not
    evidence of anything.
    """
    a = normalize_payee(left)
    b = normalize_payee(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    tokens_a, tokens_b = set(a.split()), set(b.split())
    union = tokens_a | tokens_b
    jaccard = len(tokens_a & tokens_b) / len(union) if union else 0.0
    return max(ratio, jaccard)


def _amount_key(amount: Decimal, currency: str) -> tuple[str, str]:
    return (currency.upper(), str(amount.quantize(Decimal("0.01"))))


def _score(row: RowKey, candidate: RowKey, source: str) -> tuple[bool, float, int]:
    delta = abs((row.date - candidate.date).days)
    if delta > NEAR_DUP_DATE_WINDOW_DAYS:
        return False, 0.0, delta
    if _amount_key(row.amount_native, row.currency) != _amount_key(
        candidate.amount_native, candidate.currency
    ):
        return False, 0.0, delta
    # An identical (date, amount, payee) triple is the canonical dedup hash's
    # input: it is already reported as `duplicate`, so never double-report it.
    if delta == 0 and normalize_payee(row.payee) == normalize_payee(candidate.payee):
        return False, 1.0, delta
    similarity = payee_similarity(row.payee, candidate.payee)
    return similarity >= NEAR_DUP_MIN_SIMILARITY, similarity, delta


def find_near_duplicates(
    session: Session,
    account_id: int,
    rows: list[RowKey],
    exclude_ids: set[int] | None = None,
) -> dict[int, NearDuplicateMatch]:
    """Annotate *rows* (keyed by ``RowKey.index``) with their best match.

    Reads committed **and** staged transactions for the account inside the
    date window; ``exclude_ids`` drops rows that must not be compared (the
    caller's own persisted rows are NOT excluded — a batch's rows compare
    against each other too, so two near-identical lines in one file surface).
    """
    if not rows:
        return {}
    lo = min(r.date for r in rows) - timedelta(days=NEAR_DUP_DATE_WINDOW_DAYS)
    hi = max(r.date for r in rows) + timedelta(days=NEAR_DUP_DATE_WINDOW_DAYS)

    # `import_batch_id` survives commit — the batch's STATUS decides whether a
    # row is still awaiting review or already live, so the join reads it.
    stmt = (
        select(Transaction, ImportBatch.status)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(Transaction.account_id == account_id)
        .where(Transaction.date >= lo)
        .where(Transaction.date <= hi)
        # M107 (docs/44 §7): IDENTITY — a CHILD's amount must never make a
        # re-upload look like a new row. The parent stays: it IS the statement.
        .where(IDENTITY_EXCLUDE_CHILDREN)
    )
    if exclude_ids:
        stmt = stmt.where(Transaction.id.notin_(exclude_ids))
    stored = session.exec(stmt).all()

    db_candidates: list[tuple[RowKey, str]] = [
        (
            RowKey(
                index=-1,
                date=t.date,
                amount_native=t.amount_native,
                currency=t.currency,
                payee=t.raw_description,
                id=t.id,
            ),
            "staged" if status == ImportBatchStatus.pending_review else "committed",
        )
        for t, status in stored
    ]
    batch_candidates = [(row, "batch") for row in rows]

    # Bucket both sides by (currency, amount) so the quadratic scan only ever
    # sees rows that are amount-identical — the cheapest possible prefilter.
    buckets: dict[tuple[str, str], list[tuple[RowKey, str]]] = {}
    for candidate, source in db_candidates + batch_candidates:
        buckets.setdefault(_amount_key(candidate.amount_native, candidate.currency), []).append(
            (candidate, source)
        )

    results: dict[int, NearDuplicateMatch] = {}
    for row in rows:
        bucket = buckets.get(_amount_key(row.amount_native, row.currency), [])
        best: tuple[tuple[float, int, int], NearDuplicateMatch] | None = None
        for candidate, source in bucket:
            if candidate is row or (candidate.id is not None and candidate.id == row.id):
                continue
            matched, similarity, delta = _score(row, candidate, source)
            if not matched:
                continue
            # Deterministic best-of: similarity desc, delta asc, then source rank.
            rank = (round(similarity, 6), -delta, -_SOURCE_RANK[source])
            match = NearDuplicateMatch(
                source=source,
                transaction_id=candidate.id,
                date=candidate.date,
                amount_native=candidate.amount_native,
                currency=candidate.currency,
                raw_description=candidate.payee,
                delta_days=delta,
                similarity=round(similarity, 4),
            )
            if best is None or rank > best[0]:
                best = (rank, match)
        if best is not None:
            results[row.index] = best[1]
    return results

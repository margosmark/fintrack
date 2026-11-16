"""Shared fuzzy-search scoring helpers (docs/08).

Extracted from ``app.api.transactions`` so the transactions router and the
global search router both reuse identical trigram scoring without one router
importing private helpers from the other (layering fix, scout M3).
"""

from sqlalchemy import func

from app.models.transactions import Transaction
from app.schemas.transactions import SimilarTransaction

# Trigram threshold (pg_trgm's default): matches below this score are noise.
SCORE_THRESHOLD = 0.3


def _trgm_score(text_column, query: str):
    """Trigram score for any text column: full-string similarity OR containment."""
    return func.greatest(
        func.similarity(text_column, query),
        func.word_similarity(query, text_column),
    )


def _payee_score(query: str):
    """Trigram score: full-string similarity OR query-as-words containment."""
    return _trgm_score(Transaction.normalized_payee, query)


def _similar_item(
    transaction: Transaction, category_name: str | None, raw_score: float
) -> SimilarTransaction:
    return SimilarTransaction(
        id=transaction.id,
        account_id=transaction.account_id,
        date=transaction.date,
        raw_description=transaction.raw_description,
        normalized_payee=transaction.normalized_payee,
        amount_native=transaction.amount_native,
        currency=transaction.currency,
        cad_amount=transaction.cad_amount,
        category_id=transaction.category_id,
        category_name=category_name,
        score=round(float(raw_score), 3),
    )

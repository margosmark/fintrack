"""search — global lookup: metadata groups + blind-index payee hits (docs/08).

docs/33 Phase 3 (S1) → M137 §Server-first computing: transaction hits come
from the blind-index `payee_token` (the HMAC the client computes with its own
index key — an indexed hash lookup) AND from the pg_trgm matcher, which
re-landed with M137. The token ranks first; the trigram half fills the window.
`hide_encrypted` is how a locked-vault client asks for only what it could
display, and `withheld` reports the difference.

The four metadata groups (categories/accounts/securities/tags) stay
trigram-ranked server-side: plaintext by design in v1 (docs/33), small
tables, and never user PII beyond names the server already groups on.

The transaction group honors the docs/03 staging model like every other
ledger consumer: only manual rows and rows from `committed` batches are
searchable, so an unreviewed import never leaks into results. Review
surfaces (docs/03 import pages) read the batch scope directly and still see
staged rows. Unknown text returns empty groups, never 404.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api._search_helpers import SCORE_THRESHOLD, _payee_score, _similar_item, _trgm_score
from app.api.deps import get_current_user, get_db
from app.models.accounts import Account
from app.models.categories import Category
from app.models.import_batches import ImportBatch
from app.models.securities import Security
from app.models.tags import Tag
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.search import AccountHit, CategoryHit, SearchResults, SecurityHit, TagHit
from app.schemas.transactions import SimilarTransaction
from app.services.txn_filters import identity_tx_conditions

router = APIRouter(prefix="/search", tags=["search"])


def _hit(txn: Transaction, name: str | None, score: float) -> SimilarTransaction:
    """`SimilarTransaction` + the flag a locked client withholds on."""
    item = _similar_item(txn, name, score)
    item.encrypted = txn.payee_cipher is not None
    return item


@router.get("", response_model=SearchResults)
def search_global(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str, Query(min_length=1, max_length=200)],
    payee_token: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=10)] = 5,
    # M137: a locked-vault client asks for what it could display; `withheld`
    # answers with the count instead of letting the group look empty.
    hide_encrypted: Annotated[bool, Query()] = False,
) -> SearchResults:
    # Transaction hits come from TWO sources, in this order (M137, docs/33
    # §Server-first computing): the blind-index token when the vault is
    # unlocked — an exact, indexed hash lookup the client computes as
    # HMAC(index_key, "payee-v1" | normalize(q)) — and then the pg_trgm
    # matcher the database already serves everywhere else. The token's hits
    # rank first (score 1.0); the trigram half fills whatever window is left.
    txn_rows: list[tuple[Transaction, str | None, float]] = []
    if payee_token:
        token_scope = [
            *identity_tx_conditions(current_user.id),
            Transaction.payee_token == payee_token,
        ]
        if hide_encrypted:
            token_scope.append(Transaction.payee_cipher.is_(None))
        token_stmt = (
            # Any category state here — uncategorized hits still matter for search.
            select(Transaction, Category.name)
            .join(Category, Category.id == Transaction.category_id, isouter=True)
            .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
            .where(*token_scope)
            .order_by(Transaction.date.desc(), Transaction.id.desc())
            .limit(limit)
        )
        txn_rows = [(txn, name, 1.0) for txn, name in db.exec(token_stmt).all()]

    # M137: the fuzzy half of ⌘K, scored by the database (docs/33 §Server-first
    # computing). Same 0.3 threshold as `GET /transactions?q=` and
    # `/transactions/similar` — one matcher, three surfaces — but the IDENTITY
    # population rule (docs/44 §2), the same one the token half runs with: a
    # split STATEMENT and its parts must not all surface as search hits.
    fuzzy = _payee_score(q)
    fuzzy_scope = [
        *identity_tx_conditions(current_user.id),
        fuzzy >= SCORE_THRESHOLD,
    ]
    if hide_encrypted:
        fuzzy_scope.append(Transaction.payee_cipher.is_(None))
    seen = {txn.id for txn, _name, _score in txn_rows}
    if seen:
        fuzzy_scope.append(~Transaction.id.in_(seen))
    room = limit - len(txn_rows)
    if room > 0:
        fuzzy_stmt = (
            select(Transaction, Category.name, fuzzy)
            .join(Category, Category.id == Transaction.category_id, isouter=True)
            .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
            .where(*fuzzy_scope)
            .order_by(fuzzy.desc(), Transaction.id.desc())
            .limit(room)
        )
        txn_rows.extend((txn, name, score) for txn, name, score in db.exec(fuzzy_stmt).all())
    withheld = 0
    if hide_encrypted:
        withheld = int(
            db.exec(
                select(func.count()).select_from(
                    select(Transaction)
                    .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
                    .where(
                        *identity_tx_conditions(current_user.id),
                        fuzzy >= SCORE_THRESHOLD,
                        Transaction.payee_cipher.is_not(None),
                        *([Transaction.payee_token != payee_token] if payee_token else []),
                    )
                    .subquery()
                )
            ).one()
        )

    category_score = _trgm_score(Category.name, q)
    category_rows = db.exec(
        select(Category)
        .where(Category.user_id == current_user.id, category_score >= SCORE_THRESHOLD)
        .order_by(category_score.desc(), Category.id.desc())
        .limit(3)
    ).all()

    account_score = _trgm_score(Account.name, q)
    account_rows = db.exec(
        select(Account)
        .where(Account.user_id == current_user.id, account_score >= SCORE_THRESHOLD)
        .order_by(account_score.desc(), Account.id.desc())
        .limit(3)
    ).all()

    security_score = func.greatest(_trgm_score(Security.symbol, q), _trgm_score(Security.name, q))
    security_rows = db.exec(
        select(Security)
        .where(Security.user_id == current_user.id, security_score >= SCORE_THRESHOLD)
        .order_by(security_score.desc(), Security.id.desc())
        .limit(3)
    ).all()

    tag_score = _trgm_score(Tag.name, q)
    tag_rows = db.exec(
        select(Tag)
        .where(Tag.user_id == current_user.id, tag_score >= SCORE_THRESHOLD)
        .order_by(tag_score.desc(), Tag.id.desc())
        .limit(3)
    ).all()

    return SearchResults(
        query=q,
        transactions=[_hit(txn, name, s) for txn, name, s in txn_rows],
        categories=[CategoryHit(id=c.id, name=c.name) for c in category_rows],
        accounts=[AccountHit(id=a.id, name=a.name, type=a.type.value) for a in account_rows],
        securities=[
            SecurityHit(
                id=s.id, symbol=s.symbol, name=s.name, kind=s.kind.value, exchange=s.exchange
            )
            for s in security_rows
        ],
        tags=[TagHit(id=t.id, name=t.name) for t in tag_rows],
        withheld=withheld,
    )

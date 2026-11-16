"""transactions — filtered, paginated list + inline corrections (docs/08, docs/09).

GET filters AND-compose: account_id, category_id (`null` = uncategorized),
date range `from`/`to`, payee text (normalized_payee OR raw_description,
case-insensitive contains), amount range on amount_native. PATCH accepts
category_id / normalized_payee corrections (docs/09 inline assignment).

M137 (docs/33 §Server-first computing): the fuzzy `q` parameter is BACK —
re-mounted on the server over the SAME pg_trgm predicate `_search_helpers`
already serves to `services/categorization`. `q` thresholds AND ranks
(`greatest(similarity, word_similarity) >= 0.3`, `score desc, id desc`), so the
browser renders a page the server computed instead of downloading the ledger to
re-derive it. `lib/searchMatch.ts` survives as the E2EE/offline fallback.

M106 (docs/02 §transaction_events): every mutation this router performs writes
exactly ONE append-only receipt per affected transaction, in the same
transaction as the change itself — see `services/tx_events.py`. The query
parameters and the scope they build live in `get_txn_filters` /
`_ledger_scope` so `GET /transactions` and `GET /transactions/unreviewed-count`
cannot drift apart: the queue chip's number and the list it opens are built
from one predicate.
"""

import base64
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, File, Query, Request, Response, UploadFile
from pydantic import ValidationError
from sqlalchemy import Select, case, func, or_
from sqlalchemy import delete as sa_delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased
from sqlmodel import select

from app.api._offloop import blocking
from app.api._search_helpers import SCORE_THRESHOLD, _payee_score, _similar_item
from app.api.deps import (
    Pagination,
    get_current_user,
    get_db,
    get_pagination,
    owned_or_404,
    paginate,
)
from app.core.errors import APIError, _normalize_validation_errors, api_error
from app.core.ratelimit import RECEIPT_UPLOAD_LIMIT, human_paced_limit
from app.importers.base import RawTransaction, build_dedup_hash, normalized_payee
from app.models.accounts import INVESTMENT_ACCOUNT_TYPES, Account
from app.models.categories import Category
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.recurring_suppressions import RecurringSuppression
from app.models.tags import Tag
from app.models.transaction_events import TransactionEvent, TransactionEventKind
from app.models.transaction_receipts import TransactionReceipt
from app.models.transaction_tags import TransactionTag
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.transactions import (
    RECEIPT_CONTENT_TYPES,
    RECEIPT_MAX_BYTES,
    AcceptTransferSuggestionPayload,
    AddTransactionTagPayload,
    BulkDeletePayload,
    BulkFilter,
    BulkPayeePayload,
    BulkRecategorizePayload,
    BulkResult,
    BulkReviewPayload,
    BulkSelection,
    BulkSkippedRow,
    BulkTagPayload,
    LinkTransferPayload,
    RecurringAcceptPayload,
    RecurringAcceptResult,
    RecurringCandidate,
    RecurringCandidatePage,
    RecurringSource,
    RecurringSuppressPayload,
    ReviewTogglePayload,
    SetTransactionTagsPayload,
    SimilarPage,
    SimilarTransaction,
    SplitCreatePayload,
    SplitParentPublic,
    TransactionCreate,
    TransactionEventPage,
    TransactionEventPublic,
    TransactionPage,
    TransactionPublic,
    TransactionReceiptPublic,
    TransactionSplit,
    TransactionUpdate,
    TransferSuggestion,
    TransferSuggestionPage,
    UnreviewedCount,
)
from app.services.categorization import learn_from_correction
from app.services.fx import cached_cad_amount
from app.services.recurring import (
    LOOKBACK_DAYS,
    LedgerRow,
    RecurringGroup,
    detect_recurring,
    make_subscriptions_group_key,
    merchant_answered,
    merchant_key,
    next_expected_date,
    rekey_suppression,
)
from app.services.tx_events import (
    RECEIPT_DERIVED_FIELDS,
    actor_label,
    load_tag_names,
    record_edit_event,
    record_event,
    record_transfer_event,
    render_summary,
)
from app.services.txn_children import unlink_transaction_children
from app.services.txn_filters import (
    IDENTITY_EXCLUDE_CHILDREN,
    LEDGER_EXCLUDE_SPLIT,
    identity_tx_conditions,
    today_et,
)

router = APIRouter(prefix="/transactions", tags=["transactions"])

_TX_CIPHER_FIELDS = (
    "payee_cipher",
    "raw_desc_cipher",
    "amount_cipher",
    "cad_amount_cipher",
    "payee_token",
)
_MAX_CIPHER_LEN = 8000


def _validate_cipher_values(data: dict) -> None:
    for field in _TX_CIPHER_FIELDS:
        value = data.get(field)
        if value is not None and len(value) > _MAX_CIPHER_LEN:
            raise api_error(422, f"{field} exceeds 8000 characters", "invalid_cipher")


def _require_tx_ciphers(payload: TransactionCreate, user: User) -> None:
    if user.encryption_required and not (payload.payee_cipher and payload.amount_cipher):
        raise api_error(
            422,
            "encryption_required: payee_cipher and amount_cipher are required",
            "encryption_required",
        )


def _parse_category_filter(raw: str | None) -> tuple[bool, int | None]:
    """(active, category_id); raw 'null' selects uncategorized rows."""
    if raw is None:
        return False, None
    if raw.lower() == "null":
        return True, None
    try:
        return True, int(raw)
    except ValueError:
        raise api_error(
            422, "category_id must be an integer or 'null'", "validation_error"
        ) from None


# --- Query contract (M106) ---------------------------------------------------
#
# The list route and the review-queue count route answer the SAME question
# over the SAME scope, so the query parameters are parsed ONCE, by one
# dependency, and folded into the WHERE clause by one builder. Two copies of
# this predicate would eventually disagree about what "current scope" means —
# and the queue chip's number is only trustworthy if it is the list's number.


@dataclass(frozen=True)
class TxnFilters:
    """Parsed `GET /transactions` query parameters (raw `category_id` string,
    because `'null'` (uncategorized) and an id are both legal values)."""

    account_id: int | None = None
    category: str | None = None
    from_: date | None = None
    to: date | None = None
    payee: str | None = None
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    direction: str | None = None
    tag_id: int | None = None
    tag_ids: tuple[int, ...] = ()
    payee_token: str | None = None
    # M106: None = no review filter, False = the "Needs review" queue.
    reviewed: bool | None = None
    # M137: fuzzy payee text. Non-None thresholds the scope (0.3) and, on the
    # list route, swaps the date sort for `score desc, id desc`.
    q: str | None = None


def _build_txn_filters(
    db: Session,
    current_user: User,
    *,
    account_id: int | None,
    category: str | None,
    from_: date | None,
    to: date | None,
    payee: str | None,
    amount_min: Decimal | None,
    amount_max: Decimal | None,
    direction: str | None,
    tag_id: int | None,
    tag_ids: tuple[int, ...] | list[int],
    payee_token: str | None,
    reviewed: bool | None,
    q: str | None = None,
) -> TxnFilters:
    """THE one construction of `TxnFilters` (M125): parse + ownership-check.

    `GET /transactions` (query params) and every `POST /transactions/bulk/*`
    run (JSON body) funnel through HERE, so "the rows the page shows" and "the
    rows a bulk run touches" are built from one predicate and can never drift
    apart (docs/08 §Transactions, docs/09 §Bulk selection toolbar). This
    function builds SQL and raises the ownership 404s — it never reads a
    request.
    """
    if account_id is not None:
        owned_or_404(Account, account_id, current_user, db)
    filter_active, filter_category_id = _parse_category_filter(category)
    if filter_active and filter_category_id is not None:
        owned_or_404(Category, filter_category_id, current_user, db)
    if tag_id is not None:
        owned_or_404(Tag, tag_id, current_user, db)
    for wanted_tag_id in dict.fromkeys(tag_ids):
        owned_or_404(Tag, wanted_tag_id, current_user, db)
    return TxnFilters(
        account_id=account_id,
        category=category,
        from_=from_,
        to=to,
        payee=payee,
        amount_min=amount_min,
        amount_max=amount_max,
        direction=direction,
        tag_id=tag_id,
        tag_ids=tuple(dict.fromkeys(tag_ids)),
        payee_token=payee_token,
        reviewed=reviewed,
        q=q,
    )


def get_txn_filters(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    account_id: Annotated[int | None, Query()] = None,
    # Declared for the removal guard below only: FastAPI drops undeclared
    # query params on the floor, and this one must fail loudly instead.
    category_id: Annotated[str | None, Query()] = None,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
    payee: Annotated[str | None, Query(min_length=1)] = None,
    amount_min: Annotated[Decimal | None, Query()] = None,
    amount_max: Annotated[Decimal | None, Query()] = None,
    direction: Annotated[Literal["paid", "received"] | None, Query()] = None,
    tag_id: Annotated[int | None, Query()] = None,
    # M104 (docs/08): repeated `tag_ids` AND-composes — a row matches only
    # when it carries EVERY id, which is what the multi-tag filter in
    # docs/09 §Tag UX promises ("Matches ALL selected"). `tag_id` (singular)
    # stays as the single-tag form and ANDs with the rest too.
    tag_ids: Annotated[list[int] | None, Query()] = None,
    payee_token: Annotated[str | None, Query()] = None,
    # M106 (docs/08): `reviewed=false` IS the "Needs review" queue.
    reviewed: Annotated[bool | None, Query()] = None,
    # M137 (docs/08): fuzzy payee text, scored server-side again.
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
) -> TxnFilters:
    """Parse + ownership-validate the list query.

    The 404s for a foreign account/category/tag belong here rather than in the
    handler body so both routes answer them identically and in the same order.
    """
    return _build_txn_filters(
        db,
        current_user,
        account_id=account_id,
        category=category_id,
        from_=from_,
        to=to,
        payee=payee,
        amount_min=amount_min,
        amount_max=amount_max,
        direction=direction,
        tag_id=tag_id,
        tag_ids=tag_ids or [],
        payee_token=payee_token,
        reviewed=reviewed,
        q=q,
    )


def _effective_amount(target):
    """`COALESCE(parent.amount_native, own.amount_native)` (docs/44 §6.2).

    Correlated on `transactions` so the subquery is re-evaluated per row
    instead of resolving once as an initplan — an UNcorrelated version would
    hand every row the same parent's amount, which is exactly the kind of
    silently-wrong money predicate this feature must not introduce (AGENTS
    §2). An ordinary row (`parent_split_id IS NULL`) gets its own amount back.
    """
    parent = aliased(Transaction)
    return func.coalesce(
        select(parent.amount_native)
        .where(parent.id == target.parent_split_id)
        .correlate(Transaction)
        .scalar_subquery(),
        target.amount_native,
    )


def _ledger_scope(user: User, filters: TxnFilters):
    """The WHERE every LEDGER read in this router shares (M107, docs/44 §2).

    Ownership + committed-only staging first (AGENTS §2), then the LEDGER
    population rule — split PARENTS out, children in — then the filters
    AND-compose in the order `TxnFilters` declares them. Ownership of the
    filter ids was checked in `get_txn_filters`, so this builds SQL only.

    `GET /transactions`, `GET /transactions/unreviewed-count` and everything
    downstream of them therefore answer with the same population AND the same
    rule, because they call this one function.
    """
    scope = (Transaction.user_id == user.id) & _live_scope(user.id) & LEDGER_EXCLUDE_SPLIT
    if filters.payee_token is not None:
        scope &= Transaction.payee_token == filters.payee_token
    if filters.account_id is not None:
        scope &= Transaction.account_id == filters.account_id
    filter_active, filter_category_id = _parse_category_filter(filters.category)
    if filter_active:
        if filter_category_id is None:
            scope &= Transaction.category_id.is_(None)
        else:
            scope &= Transaction.category_id == filter_category_id
    if filters.from_ is not None:
        scope &= Transaction.date >= filters.from_
    if filters.to is not None:
        scope &= Transaction.date <= filters.to
    if filters.payee is not None:
        # Escape LIKE wildcards so user input matches literally (#11).
        escaped = filters.payee.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        scope &= (Transaction.normalized_payee.ilike(pattern, escape="\\")) | (
            Transaction.raw_description.ilike(pattern, escape="\\")
        )
    # M107 (docs/44 §6.2): an amount filter finds the STATEMENT, not the part.
    # `COALESCE(parent.amount_native, own.amount_native)` over a correlated
    # self-subquery gives an ordinary row its own amount and a child its
    # parent's, so a `+187.42` statement split into `+120.00 / +67.42` still
    # returns BOTH parts for `amount_min=100` (the +67.42 leg would not match
    # on its own), and `amount_max=-100` returns all three parts of a
    # `-187.42` line. The comparison is on the parent's SIGNED amount — what
    # the user typed was the price of the trip to Costco. A content filter
    # (date, payee, category, tag) still matches each child on its OWN copied
    # identity — that is the filter splitting exists for.
    if filters.amount_min is not None or filters.amount_max is not None or filters.direction:
        amount = _effective_amount(Transaction)
        if filters.amount_min is not None:
            scope &= amount >= filters.amount_min
        if filters.amount_max is not None:
            scope &= amount <= filters.amount_max
        if filters.direction == "paid":
            scope &= amount < 0
        elif filters.direction == "received":
            scope &= amount > 0
    if filters.tag_id is not None:
        scope &= Transaction.id.in_(
            select(TransactionTag.transaction_id).where(TransactionTag.tag_id == filters.tag_id)
        )
    for wanted_tag_id in filters.tag_ids:
        # One subquery per id — an AND of "carries this tag" predicates, so
        # the composition rule stays the single one docs/08 pins for filters.
        scope &= Transaction.id.in_(
            select(TransactionTag.transaction_id).where(TransactionTag.tag_id == wanted_tag_id)
        )
    if filters.reviewed is not None:
        # M106: the queue chip. `reviewed_at IS NULL` IS "unreviewed".
        if filters.reviewed:
            scope &= Transaction.reviewed_at.is_not(None)
        else:
            scope &= Transaction.reviewed_at.is_(None)
    if filters.q is not None:
        # M137 (docs/33 §Server-first computing): the pg_trgm threshold is part
        # of the SCOPE, so the list, the queue chip and a bulk run all agree on
        # what "matched" means — the same predicate, never three.
        scope &= _payee_score(filters.q) >= SCORE_THRESHOLD
    return scope


def _relevance_order(filters: TxnFilters) -> tuple:
    """Ordering for a list read: `score desc, id desc` when `q` ranks it,
    otherwise the ledger's `date desc, id desc`.

    M48-S5 deleted the first branch and M137 (docs/33) puts it back — the
    ordering the removed endpoint used, from the same `_payee_score` helper
    `services/categorization` scores with.
    """
    if filters.q is None:
        return (Transaction.date.desc(), Transaction.id.desc())
    return (_payee_score(filters.q).desc(), Transaction.id.desc())


def _count_scope(db: Session, scope) -> int:
    """One COUNT over a scope built by `_ledger_scope` (no row materialised)."""
    return int(
        db.exec(select(func.count()).select_from(select(Transaction).where(scope).subquery())).one()
    )


def _live_scope(user_id: int, account_ids: list[int] | None = None):
    """Rows visible to ledger consumers: manual or committed imports only.

    `account_ids` (M90, docs/43 §6) additionally pins the batch lookup to an
    allowlist for the partner projection — belt-and-braces beside the
    `transactions.account_id IN` predicate: a batch belongs to exactly one
    account, so this never changes WHICH rows are visible (the ledger
    predicate already scopes them), it keeps every account-scoped statement
    in the projection carrying the allowlist. Default `None` keeps every
    existing caller's SQL byte-identical.
    """
    committed = select(ImportBatch.id).where(
        ImportBatch.user_id == user_id,
        ImportBatch.status == ImportBatchStatus.committed,
    )
    if account_ids is not None:
        committed = committed.where(ImportBatch.account_id.in_(account_ids))
    return or_(Transaction.import_batch_id.is_(None), Transaction.import_batch_id.in_(committed))


def _ensure_live(transaction: Transaction, user: User, db: Session) -> None:
    if transaction.import_batch_id is None:
        return
    batch = db.get(ImportBatch, transaction.import_batch_id)
    if batch is None or batch.user_id != user.id or batch.status != ImportBatchStatus.committed:
        if batch is not None and batch.status == ImportBatchStatus.pending_review:
            raise api_error(
                409,
                "Transaction belongs to a pending import batch",
                "transaction_in_pending_batch",
            )
        raise api_error(404, "Transaction not found", "not_found")


@router.post("", response_model=TransactionPublic, status_code=201)
def create_transaction(
    payload: TransactionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Transaction:
    account = owned_or_404(Account, payload.account_id, current_user, db)
    if payload.category_id is not None:
        owned_or_404(Category, payload.category_id, current_user, db)
    _validate_cipher_values(payload.model_dump())
    _require_tx_ciphers(payload, current_user)
    currency = payload.currency or account.currency
    payee = payload.normalized_payee or normalized_payee(payload.raw_description)
    raw = RawTransaction(
        date=payload.date,
        posting_date=payload.posting_date,
        raw_description=payload.raw_description,
        amount_native=payload.amount_native,
        currency=currency,
    )
    dedup_hash = build_dedup_hash(account.id, raw)
    duplicate = db.exec(
        select(Transaction.id).where(
            Transaction.account_id == account.id,
            Transaction.dedup_hash == dedup_hash,
        )
    ).first()
    if duplicate is not None:
        raise api_error(409, "Transaction already exists", "duplicate_transaction")
    transaction = Transaction(
        user_id=current_user.id,
        account_id=account.id,
        date=payload.date,
        posting_date=payload.posting_date,
        raw_description=payload.raw_description,
        normalized_payee=payee,
        amount_native=payload.amount_native,
        currency=currency,
        cad_amount=cached_cad_amount(db, currency, payload.date, payload.amount_native),
        category_id=payload.category_id,
        import_batch_id=None,
        dedup_hash=dedup_hash,
        payee_cipher=payload.payee_cipher,
        raw_desc_cipher=payload.raw_desc_cipher,
        amount_cipher=payload.amount_cipher,
        cad_amount_cipher=payload.cad_amount_cipher,
        payee_token=payload.payee_token,
    )
    db.add(transaction)
    try:
        # Flush first: `transaction.id` must exist before its receipt can
        # name it, and a duplicate still surfaces here as the same 409.
        db.flush()
        record_event(
            db,
            tx_id=transaction.id,
            user_id=current_user.id,
            kind=TransactionEventKind.created,
            payload={"source": "manual"},
            actor=current_user.id,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, "Transaction already exists", "duplicate_transaction") from None
    db.refresh(transaction)
    return transaction


def _tags_by_txn(db: Session, ids: list[int]) -> dict[int, list[Tag]]:
    """ONE statement for every transaction's tags on the page (docs/34).

    Grouping a globally `Tag.name`-ordered result keeps each row's tags in
    name order, exactly as a per-row query would have returned them — the
    per-row form was an N+1 that scaled with `limit`: 53 queries at limit=50,
    203 at limit=200.
    """
    tags_by_txn: dict[int, list[Tag]] = {}
    if not ids:
        return tags_by_txn
    for link, tag in db.exec(
        select(TransactionTag, Tag)
        .join(Tag, Tag.id == TransactionTag.tag_id)
        .where(TransactionTag.transaction_id.in_(ids))
        .order_by(Tag.name)
    ).all():
        tags_by_txn.setdefault(link.transaction_id, []).append(tag)
    return tags_by_txn


def _split_parent_rows(db: Session, parent_ids: list[int], *, owner_id: int) -> dict[int, Any]:
    """The page's split parents + `child_count` — ONE conditional statement
    (M107, docs/44 §6.4).

    Taken ONLY when the page actually contains children, so a ledger without
    splits pays nothing for the feature. `LEFT JOIN … GROUP BY` folds the
    count into the same fetch — no second query, and never one per group
    (the N+1 this exists to avoid).

    Ownership is explicit in the statement itself (`Transaction.user_id ==
    owner_id`) rather than inherited from the driving page: `transactions` is
    a tenant table and the M93 tripwire requires every statement touching one
    to say whose it is. Grouping by the primary key lets Postgres return the
    rest of the row legally (functional dependency), so no column is lost.
    """
    if not parent_ids:
        return {}
    child = aliased(Transaction)
    stmt = (
        select(Transaction, func.count(child.id))
        .outerjoin(child, child.parent_split_id == Transaction.id)
        .where(
            Transaction.user_id == owner_id,
            _live_scope(owner_id),
            Transaction.id.in_(parent_ids),
        )
        .group_by(Transaction.id)
    )
    out: dict[int, Any] = {}
    for parent, count in db.exec(stmt).all():
        item = SplitParentPublic.model_validate(parent)
        item.child_count = count
        out[parent.id] = item
    return out


def _transaction_page(
    db: Session,
    stmt: Select,
    *,
    owner_id: int,
    pagination: Pagination,
    pair_account_ids: list[int] | None = None,
) -> TransactionPage:
    """Ordering, pagination and serialization shared by the owner's list and
    the partner projection (docs/43 §6): ONE builder, so the staging rule,
    the date rule and the pagination shape cannot drift between the two
    surfaces. The caller assembles the WHERE; this owns everything after it.

    `owner_id` is whose ledger the page belongs to (`current_user.id` for
    the owner, the link owner for a partner). `pair_account_ids` narrows the
    transfer-pair name lookup to a partner's allowlist so a pair account the
    owner never shared contributes no name and no id (docs/43 §2).
    """
    rows, total = paginate(db, stmt, limit=pagination.limit, offset=pagination.offset)
    # M107 (docs/44 §6.4): the group headers for whatever children this page
    # returned — ONE conditional statement, fetched FIRST so the parents join
    # the same tag / receipt / pair lookups as the rows and none of them costs
    # an extra query. `[]` when the page has no children, so a ledger without
    # splits pays nothing for this feature.
    parent_ids = sorted({t.parent_split_id for t in rows if t.parent_split_id is not None})
    parents = _split_parent_rows(db, parent_ids, owner_id=owner_id)
    page_ids = [t.id for t in rows]
    # Parents ride along in the SAME statements as the rows: their tags and
    # receipt flag come back accurate for free, and the pair lookup below gets
    # their ids too. A header that answered `tags: []` / `has_receipt: false`
    # because the budget ran out would be a lie dressed as an optimization.
    lookup_ids = [*page_ids, *parent_ids]
    # Batch-resolve pair account names (avoid N+1).
    pair_ids = {
        t.transfer_pair_id for t in [*rows, *parents.values()] if t.transfer_pair_id is not None
    }
    pair_map: dict[int, str | None] = {}
    if pair_ids:
        pair_stmt = select(Transaction).where(
            Transaction.user_id == owner_id,
            _live_scope(owner_id, account_ids=pair_account_ids),
            Transaction.id.in_(pair_ids),
        )
        if pair_account_ids is not None:
            pair_stmt = pair_stmt.where(Transaction.account_id.in_(pair_account_ids))
        pair_txns = db.exec(pair_stmt).all()
        acct_ids = {pt.account_id for pt in pair_txns}
        acct_map: dict[int, str] = {}
        if acct_ids:
            accts = db.exec(select(Account).where(Account.id.in_(acct_ids))).all()
            acct_map = {a.id: a.name for a in accts}
        pair_map = {pt.id: acct_map.get(pt.account_id) for pt in pair_txns}
    # ONE tag statement for the whole page (docs/34). The per-row form was an
    # N+1 that scaled with `limit`: 53 queries at limit=50, 203 at limit=200.
    # Grouping a globally `Tag.name`-ordered result keeps every transaction's
    # tags in name order, exactly as the old per-row query returned them.
    tags_by_txn = _tags_by_txn(db, lookup_ids)

    # M108 P4: ONE statement for the page's receipt-image flags — the same
    # no-N+1 rule the tags above follow (docs/34). Only the ids move; the
    # image itself is fetched by `GET /transactions/{id}/receipt`, never here.
    # `user_id == owner_id` is redundant with the driving page (its rows are
    # already the owner's) but is NOT optional: `transaction_receipts` is a
    # tenant table, and the M93 tripwire
    # (`test_every_sharing_query_is_scoped_by_the_join` R3) requires every
    # statement touching one to carry an explicit ownership predicate — the
    # same rule the partner projection's `Transaction.user_id == owner_id`
    # satisfies above. Binding `owner_id` (not the caller) keeps it correct on
    # BOTH surfaces this builder serves.
    receipt_ids: set[int] = set()
    if lookup_ids:
        receipt_ids = set(
            db.exec(
                select(TransactionReceipt.transaction_id).where(
                    TransactionReceipt.user_id == owner_id,
                    TransactionReceipt.transaction_id.in_(lookup_ids),
                )
            ).all()
        )

    items = []
    for t in rows:
        item = TransactionPublic.model_validate(t)
        item.pair_account_name = pair_map.get(t.transfer_pair_id)
        item.tags = tags_by_txn.get(t.id, [])
        item.has_receipt = t.id in receipt_ids
        items.append(item)
    # `split_parents` is `date desc, id desc` — the same order the page is in,
    # so a client that walks both arrays together never sees them disagree.
    ordered_parents = sorted(parents.values(), key=lambda pt: (pt.date, pt.id), reverse=True)
    for header in ordered_parents:
        header.pair_account_name = pair_map.get(header.transfer_pair_id)
        header.tags = tags_by_txn.get(header.id, [])
        header.has_receipt = header.id in receipt_ids
    return TransactionPage(
        items=items,
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        split_parents=ordered_parents,
    )


@router.get("", response_model=TransactionPage)
def list_transactions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
    filters: Annotated[TxnFilters, Depends(get_txn_filters)],
    # M137: a locked-vault client asks for the rows it could actually display.
    # Cipher payees stay on the server either way (they are plaintext there);
    # this only decides whether they are SENT, and `withheld` says how many
    # were left out — the number `VaultLocked`'s `partial` copy needs.
    hide_encrypted: Annotated[bool, Query()] = False,
) -> TransactionPage:
    """The ledger page — date-ordered, or relevance-ranked when `q` is sent
    (docs/08, docs/33 §Server-first computing).

    Every query parameter is parsed and ownership-checked by `get_txn_filters`
    and folded into SQL by `_ledger_scope` — the SAME two functions
    `GET /transactions/unreviewed-count` runs, so the "Needs review" chip's
    number can never disagree with the list it opens.
    """
    scope = _ledger_scope(current_user, filters)
    if hide_encrypted:
        scope &= Transaction.payee_cipher.is_(None)
    stmt = select(Transaction).where(scope).order_by(*_relevance_order(filters))
    page = _transaction_page(db, stmt, owner_id=current_user.id, pagination=pagination)
    if hide_encrypted:
        # Matches the request WOULD have returned without the hide — the count
        # `partial` shows as "…of Y" instead of inventing one.
        page.withheld = _count_scope(db, _ledger_scope(current_user, filters)) - page.total
    return page


@router.get("/unreviewed-count", response_model=UnreviewedCount)
def unreviewed_count(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    filters: Annotated[TxnFilters, Depends(get_txn_filters)],
) -> UnreviewedCount:
    """M106 (docs/09 §Review queue): how many rows `reviewed=false` would
    return, over the SAME scope the list runs with — one indexed COUNT.

    Deliberately its own route rather than a field on the page: `GET
    /transactions` carries a 4-query budget (docs/34), and the header hint
    needs the number even while the queue filter is off.
    """
    # `replace(..., reviewed=None)` first: the answer is "how many would be
    # in the queue", which must not depend on whether the queue filter is
    # currently applied — asking while it is ON is exactly when the chip
    # shows its number, and `reviewed=true` must not cancel the count out.
    scope = _ledger_scope(current_user, replace(filters, reviewed=None)) & (
        Transaction.reviewed_at.is_(None)
    )
    count = db.exec(select(func.count()).select_from(Transaction).where(scope)).one()
    return UnreviewedCount(count=count)


# --- M137 (docs/04 §Similar examples, docs/33 §Server-first computing): the
# ranked payee examples route, RE-MOUNTED. It left with M48-S3 (the content
# scans); the M119 popover then ranked every committed row in the browser with
# the 285-line TypeScript port. The database already owns this matcher —
# pg_trgm at 0.3 behind `ix_trgm_transactions_payee`, the same helper
# `services/categorization` scores with — so this is a re-mount, not a port.


@router.get("/similar", response_model=SimilarPage)
def similar_transactions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str, Query(min_length=1, max_length=200)],
    # The row asking for its own family never appears in it (M119).
    exclude_id: Annotated[int | None, Query()] = None,
    # docs/04's original contract: only categorized rows are "examples".
    categorized_only: Annotated[bool, Query()] = False,
    # M119: rank a categorized sibling ahead of an EQUAL-SCORING uncategorized
    # one, so the example the user can APPLY is the top row. `score desc` still
    # decides everything the tiebreak does not.
    categorized_first: Annotated[bool, Query()] = True,
    # Locked-vault clients ask for what they could display; `withheld` is the
    # count `VaultLocked`'s `partial` copy prints rather than invents.
    hide_encrypted: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> SimilarPage:
    """Rank past transactions by payee similarity (docs/04, docs/33 S1).

    `greatest(similarity, word_similarity) >= 0.3` over `normalized_payee` —
    the mandatory plaintext column, so the answer does not depend on the vault.
    Ordering is `score desc, [categorized desc,] id desc`.
    """
    score = _payee_score(q)
    scope = _ledger_scope(current_user, TxnFilters())
    scope &= score >= SCORE_THRESHOLD
    if exclude_id is not None:
        scope &= Transaction.id != exclude_id
    if categorized_only:
        scope &= Transaction.category_id.is_not(None)
    all_matches = scope
    if hide_encrypted:
        scope = scope & Transaction.payee_cipher.is_(None)
    order: list = [score.desc()]
    if categorized_first:
        # 0 = categorized (renders first), 1 = uncategorized — an ASC key.
        order.append(case((Transaction.category_id.is_not(None), 0), else_=1))
    order.append(Transaction.id.desc())
    rows = db.exec(
        select(Transaction, Category.name, score)
        .join(Category, Category.id == Transaction.category_id, isouter=True)
        .where(scope)
        .order_by(*order)
        .limit(limit)
    ).all()
    items: list[SimilarTransaction] = []
    for txn, category_name, raw_score in rows:
        item = _similar_item(txn, category_name, raw_score)
        # What a locked client withholds is decided by THIS flag, not by the
        # lock: cipher rows are plaintext on the server and always matched.
        item.encrypted = txn.payee_cipher is not None
        items.append(item)
    return SimilarPage(
        query=q,
        items=items,
        total=_count_scope(db, scope),
        withheld=(
            _count_scope(db, all_matches & Transaction.payee_cipher.is_not(None))
            if hide_encrypted
            else 0
        ),
    )


# --- M125: bulk over a matched set (docs/08 §Bulk over a matched set, docs/09
# §Bulk selection toolbar §Select-all-matched) --------------------------------
#
# The five `/bulk/*` routes are declared HERE — before every
# `POST /transactions/{transaction_id}/…` route below — on purpose. Starlette
# matches in registration order and `bulk` is a string where those routes
# expect an int path parameter, so `/transactions/bulk/tags` registered after
# `/{transaction_id}/tags` would be swallowed by it and answer 422 on
# `int("bulk")`. One note, next to the first route, not five.

# Rows loaded into the session at a time. The matched set is resolved to IDS
# first (ints — cheap at any realistic size), then the ORM objects come in
# bounded chunks so a 100k-row ledger never materialises 100k live objects.
_BULK_CHUNK = 500


def _filters_from_bulk(db: Session, user: User, body: BulkFilter) -> TxnFilters:
    """`BulkFilter` → `TxnFilters` through the SAME builder the GET route uses.

    Field names are the query contract's names, so this is a rename, not a
    second parser: the only difference between the two callers is WHERE the
    values came from (query string vs JSON body).
    """
    return _build_txn_filters(
        db,
        user,
        account_id=body.account_id,
        # `None` → no filter at all; `"null"` → uncategorized; an int → that id.
        category=None if body.category_id is None else str(body.category_id),
        from_=body.from_,
        to=body.to,
        payee=body.payee,
        amount_min=body.amount_min,
        amount_max=body.amount_max,
        direction=body.direction,
        tag_id=body.tag_id,
        tag_ids=body.tag_ids,
        payee_token=body.payee_token,
        reviewed=body.reviewed,
        # M137: a bulk run over a ranked set is the SAME set the page ranked —
        # `_ledger_scope` thresholds `q` for both.
        q=body.q,
    )


def _bulk_ids(db: Session, user: User, filters: TxnFilters, payload: BulkSelection) -> list[int]:
    """The selection's ids, in the page's own order (docs/08: date desc, id desc).

    `_ledger_scope` IS the list route's predicate — ownership, committed-only
    staging, the LEDGER population rule and every filter — so the set a bulk
    run touches is the set `GET /transactions` would have paged through, minus
    the ids the user unchecked. There is no second filter implementation.

    `filters` is built ONCE, by the route, BEFORE that route resolves its own
    target (a category id, a tag): the filter's guards then answer in `GET`'s
    order — ownership 404s — instead of whichever happens to run first.
    """
    scope = _ledger_scope(user, filters)
    excluded = list(dict.fromkeys(payload.exclude_ids))
    if excluded:
        scope = scope & ~Transaction.id.in_(excluded)
    return list(
        db.exec(select(Transaction.id).where(scope).order_by(*_relevance_order(filters))).all()
    )


def _run_bulk(
    db: Session,
    user: User,
    payload: BulkSelection,
    apply,
    *,
    filters: TxnFilters,
    expected_count: int | None = None,
) -> BulkResult:
    """Resolve the selection, apply `apply` to every row, ONE commit.

    Partial success is still the reported outcome (docs/09): a row the server
    refuses becomes a `skipped` entry carrying that row's own `{detail, code}`
    and the run continues — never a batch that silently lost or silently
    skipped rows. Every per-row guard runs BEFORE that row's first mutation,
    so a caught refusal leaves the session clean and the rest of the batch
    still lands.

    `expected_count` (delete only) is checked BEFORE the first row is touched:
    a set that no longer matches the number the user confirmed writes nothing.
    """
    ids = _bulk_ids(db, user, filters, payload)
    matched = len(ids)
    if expected_count is not None and matched != expected_count:
        raise api_error(
            409,
            f"This filter now matches {matched} transactions, not {expected_count}. "
            "Nothing was changed — the ledger moved after that count was read; "
            "check the new number and confirm again.",
            "bulk_count_mismatch",
        )
    skipped: list[BulkSkippedRow] = []
    updated = 0
    for start in range(0, matched, _BULK_CHUNK):
        chunk = ids[start : start + _BULK_CHUNK]
        rows = db.exec(
            select(Transaction)
            .where(Transaction.id.in_(chunk))
            .order_by(Transaction.date.desc(), Transaction.id.desc())
        ).all()
        for tx in rows:
            try:
                apply(db, tx)
            except APIError as exc:
                skipped.append(
                    BulkSkippedRow(
                        id=tx.id,
                        label=tx.normalized_payee,
                        code=exc.code,
                        detail=exc.detail,
                    )
                )
                continue
            updated += 1
    db.commit()
    return BulkResult(matched=matched, updated=updated, skipped=skipped)


def _bulk_recategorize(user: User, category: Category | None):
    """One `categorized` receipt per row whose category actually moved — the
    same receipt `PATCH /transactions/{id}` writes, so the History drawer
    cannot tell a bulk run from N clicks (M120, docs/02 §transaction_events)."""
    target = category.id if category is not None else None
    name = category.name if category is not None else None

    def apply(db: Session, tx: Transaction) -> None:
        _refuse_split_row_write(tx, "patch", ["category_id"])
        if tx.category_id == target:
            return  # already there: no write, no receipt, exactly like PATCH
        tx.category_id = target
        # Kilter adoption (docs/04 §Learning loop): a bulk recategorize is the
        # SAME correction as N clicks, so it learns the same way — one learned
        # rule per row's own payee, and the identical receipt marker.
        learned_rule = learn_from_correction(db, tx.user_id, tx)
        payload: dict[str, Any] = {
            "fields": ["category_id"],
            "changes": {"category_id": target},
            "category_id": target,
            "category_name": name,
        }
        if learned_rule is not None:
            payload["learned"] = learned_rule
        record_edit_event(
            db,
            transaction_id=tx.id,
            user_id=tx.user_id,
            actor=user.id,
            changed=payload,
        )

    return apply


def _bulk_payee(user: User, payload: BulkPayeePayload):
    """Rename the payee on every matched row.

    Only `normalized_payee` + the E2EE payee fields move. `amount_cipher` and
    `raw_desc_cipher` are deliberately untouched: a payee rename changes no
    money and no description, so re-encrypting them here would rewrite rows the
    user never edited. The `encryption_required` rule is PATCH's own, applied
    per row — a row that cannot satisfy it becomes a named conflict instead of
    failing the batch.
    """

    def apply(db: Session, tx: Transaction) -> None:
        _refuse_split_row_write(tx, "patch", ["normalized_payee"])
        if user.encryption_required:
            effective_payee = payload.payee_cipher or tx.payee_cipher
            if not (effective_payee and tx.amount_cipher):
                raise api_error(
                    422,
                    "encryption_required: payee_cipher and amount_cipher are required",
                    "encryption_required",
                )
        data: dict[str, Any] = {"normalized_payee": payload.normalized_payee}
        if payload.payee_cipher is not None:
            data["payee_cipher"] = payload.payee_cipher
        if payload.payee_token is not None:
            data["payee_token"] = payload.payee_token
        changes = {k: v for k, v in data.items() if getattr(tx, k, None) != v}
        if not changes:
            return
        for key, value in changes.items():
            setattr(tx, key, value)
        # Kilter adoption (docs/04): correcting the MERCHANT teaches the rule
        # for the row's existing category under its new payee — the same write
        # a single-row PATCH makes, so a bulk rename cannot be told apart.
        # NB: named `edit_payload` — `payload` is the enclosing BulkPayeePayload,
        # and assigning to it inside `apply` would make every earlier read of it
        # an UnboundLocalError.
        edit_payload: dict[str, Any] = {"fields": list(changes), "changes": changes}
        if "normalized_payee" in changes:
            learned_rule = learn_from_correction(db, tx.user_id, tx)
            if learned_rule is not None:
                edit_payload["learned"] = learned_rule
        record_edit_event(
            db,
            transaction_id=tx.id,
            user_id=tx.user_id,
            actor=user.id,
            changed=edit_payload,
        )

    return apply


def _bulk_review(user: User, reviewed: bool):
    """Idempotent on purpose — asking for the state a row already has writes
    no receipt, so a review queue's log stays one line per real flip (M106).
    Deliberately unguarded on split rows: review state is a statement-line
    property (docs/44 §5), the same rule the single-row route follows."""

    def apply(db: Session, tx: Transaction) -> None:
        if reviewed and tx.reviewed_at is None:
            tx.reviewed_at = datetime.now(UTC)
            tx.reviewed_by = user.id
            record_event(
                db,
                tx_id=tx.id,
                user_id=tx.user_id,
                kind=TransactionEventKind.reviewed,
                payload={"reviewed": True},
                actor=user.id,
            )
        elif not reviewed and tx.reviewed_at is not None:
            tx.reviewed_at = None
            tx.reviewed_by = None
            record_event(
                db,
                tx_id=tx.id,
                user_id=tx.user_id,
                kind=TransactionEventKind.reviewed,
                payload={"reviewed": False},
                actor=user.id,
            )

    return apply


def _bulk_add_tag(user: User, tag: Tag):
    """Idempotent, race-safe add — the same ON CONFLICT DO NOTHING insert the
    single-row `POST /transactions/{id}/tags` does, and the same rule for the
    receipt: only a genuinely NEW link writes one."""

    def apply(db: Session, tx: Transaction) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        _refuse_split_row_write(tx, "tag")
        already = db.exec(
            select(TransactionTag.id).where(
                TransactionTag.transaction_id == tx.id,
                TransactionTag.tag_id == tag.id,
            )
        ).first()
        db.execute(
            pg_insert(TransactionTag)
            .values({"transaction_id": tx.id, "tag_id": tag.id})
            .on_conflict_do_nothing(index_elements=["transaction_id", "tag_id"])
        )
        if already is None:
            record_event(
                db,
                tx_id=tx.id,
                user_id=tx.user_id,
                kind=TransactionEventKind.tagged,
                payload={
                    "added": [tag.name],
                    "removed": [],
                    "added_ids": [tag.id],
                    "removed_ids": [],
                },
                actor=user.id,
            )

    return apply


def _bulk_delete(user: User):
    """The ONE internal delete path, per row: dependents unlinked, one `deleted`
    receipt, row gone — byte-for-byte the blast radius of a single
    `DELETE /transactions/{id}` (docs/19), just not 345 requests wide."""

    def apply(db: Session, tx: Transaction) -> None:
        _refuse_split_row_write(tx, "delete")
        _purge_transaction(db, tx, actor=user.id, write_receipt=True)

    return apply


@router.post("/bulk/recategorize", response_model=BulkResult)
def bulk_recategorize(
    payload: BulkRecategorizePayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BulkResult:
    """Recategorize every row the filter matches, minus the unchecked ids."""
    filters = _filters_from_bulk(db, current_user, payload.filter)
    category = None
    if payload.category_id is not None:
        category = owned_or_404(Category, payload.category_id, current_user, db)
    return _run_bulk(
        db,
        current_user,
        payload,
        _bulk_recategorize(current_user, category),
        filters=filters,
    )


@router.post("/bulk/payee", response_model=BulkResult)
def bulk_payee(
    payload: BulkPayeePayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BulkResult:
    """Set the payee on every row the filter matches, minus the unchecked ids."""
    filters = _filters_from_bulk(db, current_user, payload.filter)
    _validate_cipher_values(
        {"payee_cipher": payload.payee_cipher, "payee_token": payload.payee_token}
    )
    return _run_bulk(db, current_user, payload, _bulk_payee(current_user, payload), filters=filters)


@router.post("/bulk/review", response_model=BulkResult)
def bulk_review(
    payload: BulkReviewPayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BulkResult:
    """Mark every matched row reviewed / unreviewed, minus the unchecked ids."""
    filters = _filters_from_bulk(db, current_user, payload.filter)
    return _run_bulk(
        db,
        current_user,
        payload,
        _bulk_review(current_user, payload.reviewed),
        filters=filters,
    )


@router.post("/bulk/tags", response_model=BulkResult)
def bulk_add_tag(
    payload: BulkTagPayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BulkResult:
    """Add ONE tag to every row the filter matches, minus the unchecked ids."""
    filters = _filters_from_bulk(db, current_user, payload.filter)
    tag = owned_or_404(Tag, payload.tag_id, current_user, db)
    return _run_bulk(db, current_user, payload, _bulk_add_tag(current_user, tag), filters=filters)


@router.post("/bulk/delete", response_model=BulkResult)
def bulk_delete(
    payload: BulkDeletePayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BulkResult:
    """Delete every matched row — refused outright if the count moved.

    `expected_count` is checked before the first row is touched, so the set
    that gets destroyed is exactly the set the confirmation named
    (docs/09 §Destructive actions, `409 bulk_count_mismatch`).
    """
    filters = _filters_from_bulk(db, current_user, payload.filter)
    return _run_bulk(
        db,
        current_user,
        payload,
        _bulk_delete(current_user),
        filters=filters,
        expected_count=payload.expected_count,
    )


# --- Recurring-transactions detection (M96, docs/09 §Recurring prompt) --------
#
# Three routes, all local-only: the scan reads rows the caller already owns and
# computes in-process (`services/recurring.py` — no network import, no LLM, no
# new dependency), accept writes the three tag columns, decline writes one
# suppression row. Detection itself is never persisted.


def _recurring_inputs(
    db: Session, user_id: int
) -> tuple[list[LedgerRow], set[str], set[str], date]:
    """(untagged rows in the lookback, already-tagged merchants, suppressed keys).

    Committed-only staging comes from the shared predicate, so a pending import
    can never be proposed for tagging. A merchant that already carries a tagged
    row is excluded WHOLE: the user answered that group once (docs/09 — a
    decision is final, the prompt does not come back for the same merchant).
    """
    window_start = today_et() - timedelta(days=LOOKBACK_DAYS)
    rows = db.exec(
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(
            # M107 (docs/44 §2, §11): IDENTITY — a recurring badge is a
            # statement-line property, so the candidates are the parents and
            # never the parts. Children carry `is_recurring=false` for the
            # same reason; this is belt and braces, not redundancy.
            *identity_tx_conditions(user_id),
            Transaction.date >= window_start,
        )
        .order_by(Transaction.date, Transaction.id)
    ).all()

    untagged: list[LedgerRow] = []
    tagged_merchants: set[str] = set()
    for transaction in rows:
        payee = transaction.normalized_payee or transaction.raw_description or ""
        if transaction.is_recurring:
            key = merchant_key(payee)
            if key:
                tagged_merchants.add(key)
            continue
        untagged.append(
            LedgerRow(
                id=transaction.id,
                date=transaction.date,
                payee=payee,
                amount=transaction.amount_native,
            )
        )

    suppressed = set(
        db.exec(
            select(RecurringSuppression.group_key).where(RecurringSuppression.user_id == user_id)
        ).all()
    )
    # M165: a stored key is the key as it looked WHEN THE USER ANSWERED, and
    # `normalize_payee_key` has learned since (whitespace is not identity now).
    # Re-read each one through today's normalizer so a previously declined
    # group stays declined — and keep the raw value too, so the answer can only
    # ever be WIDER than it was before, never narrower: no group the user
    # already declined may come back because a key format moved (docs/09
    # §Recurring prompt: "a decision cannot be re-asked").
    suppressed |= {rekey_suppression(key) for key in suppressed}
    return untagged, tagged_merchants, suppressed, window_start


def _dismiss_key(source: RecurringSource, group: RecurringGroup) -> str:
    """The suppression key `source` answers with (M138, docs/09).

    `recurring_prompt` is M96's `make_group_key` — merchant + cadence, the key
    `accept`/`decline` have always used. `subscriptions` is the dashboard card's
    per-payee key. They can never collide (a cadence is never the literal
    `subscriptions`), which is the whole mechanism behind the boundary test:
    dismissing a suggestion on the dashboard leaves the prompt's candidate on
    the screen, and declining at the prompt leaves the dashboard row.
    """
    if source == "subscriptions":
        return make_subscriptions_group_key(group.payee)
    return group.group_key


@router.get("/recurring/candidates", response_model=RecurringCandidatePage)
def list_recurring_candidates(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    source: Annotated[RecurringSource, Query()] = "recurring_prompt",
) -> RecurringCandidatePage:
    """Candidate groups for a dismissible suggestion card.

    Suppressed groups are subtracted before answering, so a declined group
    never comes back — that is the whole point of the suppression row. Which
    suppressions count is decided by `source` (M138): the M96 prompt card
    reads the default `recurring_prompt`, the dashboard's "Bills you probably
    have" card reads `subscriptions`, and each only ever sees the answers its
    OWN surface collected.

    `dismiss_key` echoes the key this source filters on, so the caller never
    re-derives the merchant normalization client-side.
    """
    untagged, tagged_merchants, suppressed, window_start = _recurring_inputs(db, current_user.id)
    found = detect_recurring(untagged)

    def key(group: RecurringGroup) -> str:
        return _dismiss_key(source, group)

    visible = [group for group in found if key(group) not in suppressed]
    # M121 D3 (docs/25 §M121): "already answered" is the detector's OWN
    # helper, which runs through the SHARED `match_terms` — the same contract
    # a bill's term list calls, so the recurring family and a bill can never
    # drift on what "matches this merchant" means. Byte-identical answer to
    # the `in` test it replaced (ANY of equal keys), one contract instead of
    # two.
    tagged_terms = tuple(tagged_merchants)
    visible = [group for group in visible if not merchant_answered(group, tagged_terms)]
    items = [
        RecurringCandidate(
            group_key=group.group_key,
            dismiss_key=key(group),
            payee=group.payee,
            cadence=group.cadence,
            occurrence_count=group.occurrence_count,
            transaction_ids=list(group.transaction_ids),
            monthly_total=group.monthly_total,
            first_date=group.first_date,
            last_date=group.last_date,
            next_expected_date=group.next_expected_date,
        )
        for group in visible
    ]
    combined = sum((group.monthly_total for group in visible), Decimal("0.00"))
    return RecurringCandidatePage(
        groups=items,
        combined_monthly_total=combined,
        suppressed_count=sum(1 for group in found if key(group) in suppressed),
        window_start=window_start,
    )


@router.post("/recurring/accept", response_model=RecurringAcceptResult)
def accept_recurring_group(
    payload: RecurringAcceptPayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RecurringAcceptResult:
    """Tag the chosen rows: `is_recurring` + cadence + the group's next date.

    Per-group, never all-or-nothing: the client sends exactly the ids it
    showed. Every id is ownership-checked and live-checked BEFORE any write, so
    a mixed batch can never half-apply through this path.
    """
    transactions: list[Transaction] = []
    seen: set[int] = set()
    for transaction_id in payload.transaction_ids:
        if transaction_id in seen:
            continue
        seen.add(transaction_id)
        transaction = owned_or_404(Transaction, transaction_id, current_user, db)
        _ensure_live(transaction, current_user, db)
        transactions.append(transaction)

    next_date = next_expected_date(max(t.date for t in transactions), payload.cadence)
    for transaction in transactions:
        transaction.is_recurring = True
        transaction.recurring_cadence = payload.cadence
        transaction.recurring_next_date = next_date
        db.add(transaction)
        # M120 (docs/02 §transaction_events): a person just tagged this group,
        # so the log says so — one `edited` receipt per row, in the SAME
        # commit, through `record_edit_event` (the PATCH writer of record).
        # No new helper and NO `via` key: this surface introduces no new
        # wording, so `render_summary`'s plain `edited` fallback renders it
        # byte-identically to a multi-field PATCH.
        record_edit_event(
            db,
            transaction_id=transaction.id,
            user_id=current_user.id,
            actor=current_user.id,
            changed={
                "fields": [
                    "is_recurring",
                    "recurring_cadence",
                    "recurring_next_date",
                ],
                "changes": {
                    "is_recurring": True,
                    "recurring_cadence": payload.cadence,
                    "recurring_next_date": next_date,
                },
            },
        )
    db.commit()
    for transaction in transactions:
        db.refresh(transaction)
    return RecurringAcceptResult(
        updated=len(transactions),
        group_key=payload.group_key,
        cadence=payload.cadence,
        next_expected_date=next_date,
    )


@router.post("/recurring/suppress", status_code=204)
def suppress_recurring_group(
    payload: RecurringSuppressPayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Decline a group for good: one row, idempotent, no unsuppress route.

    Deliberately write-once (docs/09): the answer to "is this recurring?" is
    final, so there is no second prompt and no settings screen to undo it —
    the user tags the rows by hand if they change their mind.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    db.execute(
        pg_insert(RecurringSuppression)
        .values(user_id=current_user.id, group_key=payload.group_key)
        .on_conflict_do_nothing(index_elements=["user_id", "group_key"])
    )
    db.commit()
    return Response(status_code=204)


@router.get("/transfer-suggestions", response_model=TransferSuggestionPage)
def list_transfer_suggestions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    account_id: Annotated[int | None, Query()] = None,
) -> TransferSuggestionPage:
    """Find unpaired transactions with opposite amounts within ±10 days."""
    if account_id is not None:
        owned_or_404(Account, account_id, current_user, db)
    scope = (Transaction.user_id == current_user.id) & _live_scope(current_user.id)
    # M107 (docs/44 §2): IDENTITY — a transfer suggestion pairs STATEMENT
    # lines, so children never appear and split parents do.
    scope &= IDENTITY_EXCLUDE_CHILDREN
    scope &= Transaction.transfer_pair_id.is_(None)

    unpaired = db.exec(
        select(Transaction).where(scope).order_by(Transaction.date, Transaction.id)
    ).all()

    # Index by (abs_amount, date) for efficient matching.
    by_amount: dict[str, list[Transaction]] = {}
    for txn in unpaired:
        key = str(txn.amount_native)
        by_amount.setdefault(key, []).append(txn)

    seen_pairs: set[tuple[int, int]] = set()
    suggestions: list[TransferSuggestion] = []

    for txn in unpaired:
        if account_id is not None and txn.account_id != account_id:
            continue
        target_amount = -txn.amount_native
        key = str(target_amount)
        candidates = by_amount.get(key, [])
        for cand in candidates:
            if cand.id == txn.id:
                continue
            if cand.account_id == txn.account_id:
                continue
            if abs((cand.date - txn.date).days) > 10:
                continue
            pair = (min(txn.id, cand.id), max(txn.id, cand.id))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            source, candidate = (txn, cand) if txn.id < cand.id else (cand, txn)
            source_acct = db.get(Account, source.account_id)
            cand_acct = db.get(Account, candidate.account_id)
            suggestions.append(
                TransferSuggestion(
                    source_id=source.id,
                    source_account_id=source.account_id,
                    source_account_name=source_acct.name if source_acct else None,
                    source_amount=str(source.amount_native),
                    source_date=str(source.date),
                    source_payee=source.normalized_payee or source.raw_description,
                    candidate_id=candidate.id,
                    candidate_account_id=candidate.account_id,
                    candidate_account_name=cand_acct.name if cand_acct else None,
                    candidate_amount=str(candidate.amount_native),
                    candidate_date=str(candidate.date),
                    candidate_payee=candidate.normalized_payee or candidate.raw_description,
                )
            )

    return TransferSuggestionPage(items=suggestions)


@router.post("/accept-transfer-suggestion", response_model=TransactionPublic)
def accept_transfer_suggestion(
    payload: AcceptTransferSuggestionPayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Transaction:
    """Accept a transfer suggestion: pair two transactions and assign transfers category."""
    source = owned_or_404(Transaction, payload.source_id, current_user, db)
    candidate = owned_or_404(Transaction, payload.candidate_id, current_user, db)
    _ensure_live(source, current_user, db)
    _ensure_live(candidate, current_user, db)
    _refuse_split_row_write(source, "transfer")
    _refuse_split_row_write(candidate, "transfer")

    if source.transfer_pair_id is not None or candidate.transfer_pair_id is not None:
        raise api_error(409, "One or both transactions are already paired", "already_paired")

    transfers_category = db.exec(
        select(Category).where(
            Category.user_id == current_user.id,
            Category.name == "transfers",
            Category.is_system == True,  # noqa: E712
        )
    ).first()
    if not transfers_category:
        raise api_error(404, "Transfers category not found", "transfers_category_not_found")

    source.category_id = transfers_category.id
    candidate.category_id = transfers_category.id
    source.transfer_pair_id = candidate.id
    candidate.transfer_pair_id = source.id
    # M106 (docs/02 §transaction_events): this route moves TWO rows, so it
    # writes two receipts — one per affected transaction, in the same DB
    # transaction as the pairing itself.
    record_transfer_event(
        db,
        transaction_id=source.id,
        user_id=current_user.id,
        actor=current_user.id,
        pair_id=candidate.id,
        category_id=transfers_category.id,
        category_name="transfers",
    )
    record_transfer_event(
        db,
        transaction_id=candidate.id,
        user_id=current_user.id,
        actor=current_user.id,
        pair_id=source.id,
        category_id=transfers_category.id,
        category_name="transfers",
    )
    db.commit()
    db.refresh(source)
    return source


# --- M107: the mutation matrix (docs/44 §5, D8/D9) ----------------------------
#
# While a split exists the parent admits EXACTLY ONE edit: merge. Everything
# else that would move money, identity or categorisation is refused here —
# and a child is frozen on the identity/money fields for the same reason,
# because "change an amount by editing a leg" is the one thing a split must
# not allow (replace the WHOLE split instead, `PUT /split`).

# Fields whose change would move the statement while its children exist.
_SPLIT_LEDGER_FIELDS = frozenset(
    {
        "account_id",
        "date",
        "posting_date",
        "raw_description",
        "normalized_payee",
        "amount_native",
        "currency",
        "category_id",
        "note",
    }
)
# Fields a CHILD may never change: the identity it inherited and the money it
# was allocated. Everything else (category, note, ciphers, review, tags) is a
# normal row edit — this is exactly why D1 made children ordinary rows.
_CHILD_FROZEN_FIELDS = frozenset(
    {
        "account_id",
        "date",
        "posting_date",
        "raw_description",
        "normalized_payee",
        "amount_native",
        "currency",
    }
)


def _refuse_split_row_write(
    transaction: Transaction, operation: str, fields: tuple[str, ...] | list[str] = ()
) -> None:
    """docs/44 §5 — one function, every route that writes a ledger row.

    `operation` is one of `patch` / `delete` / `tag` / `transfer` /
    `reconcile`. `fields` is only consulted for `patch`, where the answer
    depends on WHICH fields moved: a cipher-only PATCH on a split parent MUST
    keep working (the E2EE backfill, docs/33 A17) while the same PATCH with
    `amount_native` in it must not.

    Reconciliation is deliberately allowed on a parent (a statement line
    matches a statement row; the link writes `reconciliation_items`, never the
    transaction) and refused on a child (a statement line never matches part
    of one).
    """
    if transaction.is_split:
        if operation == "reconcile":
            return
        if operation == "patch" and not [f for f in fields if f in _SPLIT_LEDGER_FIELDS]:
            return  # cipher/index fields only — the docs/33 A17 backfill
        raise api_error(
            409,
            "This transaction is split. Merge its parts first.",
            "transaction_is_split",
        )
    if transaction.parent_split_id is None:
        return
    if operation == "tag":
        return
    # Deliberately NOT guarded anywhere: `POST /transactions/{id}/review`,
    # recurring accept/suppress and reconciliation ON the parent. Review state
    # and the recurring badge are identity-row properties that live on the
    # statement line (docs/44 §5), so splitting neither moves nor freezes
    # them — a reader looking for a guard here will not find one on purpose.
    if operation == "patch" and not [f for f in fields if f in _CHILD_FROZEN_FIELDS]:
        return  # category / note / ciphers — a child's own columns
    raise api_error(
        409,
        "This row is part of a split. Change an amount by re-splitting, or merge the parts back.",
        "split_leg_immutable",
    )


@router.patch("/{transaction_id}", response_model=TransactionPublic)
def update_transaction(
    transaction_id: int,
    payload: TransactionUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Transaction:
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    _ensure_live(transaction, current_user, db)
    data = payload.model_dump(exclude_unset=True)
    # M107 (docs/44 §5): FIRST, before any other validation — a PATCH aimed at
    # a row the split feature has frozen answers with the split's code, not
    # with whatever the body happens to trip over later.
    _refuse_split_row_write(transaction, "patch", list(data))
    _validate_cipher_values(data)
    if current_user.encryption_required:
        effective_payee = data.get("payee_cipher", transaction.payee_cipher)
        effective_amount = data.get("amount_cipher", transaction.amount_cipher)
        if not (effective_payee and effective_amount):
            raise api_error(
                422,
                "encryption_required: payee_cipher and amount_cipher are required",
                "encryption_required",
            )
    if "account_id" in data and data["account_id"] is None:
        raise api_error(422, "account_id cannot be null", "validation_error")
    if "raw_description" in data and data["raw_description"] is None:
        raise api_error(422, "raw_description cannot be null", "validation_error")

    account = db.get(Account, transaction.account_id)
    if account is None or account.user_id != current_user.id:
        raise api_error(404, "Account not found", "not_found")
    if "account_id" in data:
        account = owned_or_404(Account, data["account_id"], current_user, db)
    category_name: str | None = None
    if "category_id" in data and data["category_id"] is not None:
        category = owned_or_404(Category, data["category_id"], current_user, db)
        # Named now: the receipt's summary ("you set category Groceries") is
        # written from THIS value, not looked up later against a category the
        # user may have renamed or deleted (M106, docs/02).
        category_name = category.name
    new_currency = transaction.currency
    if "currency" in data:
        new_currency = data["currency"] or account.currency
    new_account_id = account.id
    new_date = data.get("date", transaction.date)
    new_raw = data.get("raw_description", transaction.raw_description)
    new_amount = data.get("amount_native", transaction.amount_native)
    new_currency = data.get("currency") or account.currency
    identity_changed = any(
        field in data
        for field in ("account_id", "date", "amount_native", "raw_description", "currency")
    )
    if "normalized_payee" in data and data["normalized_payee"] is None:
        data["normalized_payee"] = normalized_payee(new_raw)
    if "currency" in data:
        data["currency"] = new_currency
    if identity_changed:
        raw = RawTransaction(
            date=new_date,
            posting_date=data.get("posting_date", transaction.posting_date),
            raw_description=new_raw,
            amount_native=new_amount,
            currency=new_currency,
        )
        new_dedup_hash = build_dedup_hash(new_account_id, raw)
        duplicate = db.exec(
            select(Transaction.id).where(
                Transaction.account_id == new_account_id,
                Transaction.dedup_hash == new_dedup_hash,
                Transaction.id != transaction.id,
            )
        ).first()
        if duplicate is not None:
            raise api_error(409, "Transaction already exists", "duplicate_transaction")
        data["dedup_hash"] = new_dedup_hash
        data["cad_amount"] = cached_cad_amount(db, new_currency, new_date, new_amount)
    if "account_id" in data:
        data["account_id"] = new_account_id
    # M106 (docs/02): the receipt is diffed against the PRE-patch row, before
    # the new values land, so a PATCH that re-sends what was already there
    # changes nothing and receipts nothing. `dedup_hash` / `cad_amount` are
    # recomputed from the identity fields — never intent — so they are neither
    # diffed nor rendered as "you edited …".
    receipt_changes = {
        key: value
        for key, value in data.items()
        if key not in RECEIPT_DERIVED_FIELDS and getattr(transaction, key, None) != value
    }
    for field, value in data.items():
        setattr(transaction, field, value)
    # Kilter adoption (docs/04 §Learning loop): a correction that moved the
    # MERCHANT or the CATEGORY teaches the learned rule. Gated on those two
    # fields so an unrelated edit (date, note, amount) can never bump
    # `hit_count` — the counter means "this mapping was confirmed", not "this
    # row was touched". The marker rides the same receipt, so the drawer can
    # say WHERE the rule came from.
    learned_rule = None
    if "category_id" in receipt_changes or "normalized_payee" in receipt_changes:
        learned_rule = learn_from_correction(db, current_user.id, transaction)
    if receipt_changes:
        receipt: dict = {"fields": list(receipt_changes), "changes": receipt_changes}
        if "category_id" in receipt_changes:
            receipt["category_id"] = receipt_changes["category_id"]
            receipt["category_name"] = category_name
        if learned_rule is not None:
            receipt["learned"] = learned_rule
        record_edit_event(
            db,
            transaction_id=transaction.id,
            user_id=current_user.id,
            actor=current_user.id,
            changed=receipt,
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, "Transaction already exists", "duplicate_transaction") from None
    db.refresh(transaction)
    return transaction


def _purge_transaction(
    db: Session,
    transaction: Transaction,
    *,
    actor: int | None,
    write_receipt: bool,
) -> None:
    """Remove one ledger row and everything hanging off it (docs/29 F3).

    The ONE internal delete path, shared by `DELETE /transactions/{id}` and by
    merge-back / replace — which is the whole reason docs/44 §5 pins merge to
    "the same internal helper as a normal transaction delete": a child carries
    tag links, LLM suggestions and reconciliation matches exactly like any
    other row, so a bespoke child-delete would rot the first time one of those
    gained a column.

    `write_receipt=False` suppresses the M106 `deleted` event. Merge and
    replace delete children WITHOUT receipts (docs/44 D15: children write no
    `created`/`deleted` events — the parent's `split`/`merge` receipt IS the
    record of their lifecycle), while an ordinary row delete keeps writing
    exactly the one it always did. `transaction_events.tx_id` has no FK, so
    whatever receipts a child earned while it existed outlive it — the same
    rule every normal delete follows.
    """
    # docs/29 F3: dependents are cleaned in the same transaction before the
    # delete — reconciliation matches, tag junction rows, LLM suggestions.
    # One child set for EVERY delete of a transaction: the batch paths
    # (app/api/imports.py) and the terminal-batch sweep call the same helper,
    # so a table added here is fixed everywhere at once.
    unlink_transaction_children(db, [transaction.id], user_id=transaction.user_id)
    if write_receipt:
        # M106 (docs/02): the receipt outlives the row — `tx_id` has no FK for
        # exactly this line. Written in the SAME transaction as the delete, so
        # "the row vanished and left no trace" is not a state the database can
        # reach.
        record_event(
            db,
            tx_id=transaction.id,
            user_id=transaction.user_id,
            kind=TransactionEventKind.deleted,
            payload={
                "payee": transaction.normalized_payee,
                "date": transaction.date,
                "amount_native": transaction.amount_native,
                "currency": transaction.currency,
                "import_batch_id": transaction.import_batch_id,
            },
            actor=actor,
        )
    db.delete(transaction)


@router.delete("/{transaction_id}", status_code=204)
def delete_transaction(
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    _ensure_live(transaction, current_user, db)
    _refuse_split_row_write(transaction, "delete")
    _purge_transaction(db, transaction, actor=current_user.id, write_receipt=True)
    db.commit()
    return Response(status_code=204)


@router.post("/{transaction_id}/detect-transfer", response_model=TransactionPublic)
def detect_transfer_pair(
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Transaction:
    """Auto-detect transfer pair by matching opposite amounts within ±3 days."""
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    _ensure_live(transaction, current_user, db)
    _refuse_split_row_write(transaction, "transfer")

    transfers_category = db.exec(
        select(Category).where(
            Category.user_id == current_user.id,
            Category.name == "transfers",
            Category.is_system == True,  # noqa: E712
        )
    ).first()
    if not transfers_category:
        raise api_error(404, "Transfers category not found", "transfers_category_not_found")

    target_amount = -transaction.amount_native
    date_start = transaction.date - timedelta(days=3)
    date_end = transaction.date + timedelta(days=3)

    match = db.exec(
        select(Transaction).where(
            Transaction.user_id == current_user.id,
            _live_scope(current_user.id),
            # M107: IDENTITY — never pair against a part of a split.
            IDENTITY_EXCLUDE_CHILDREN,
            Transaction.id != transaction.id,
            Transaction.account_id != transaction.account_id,
            Transaction.category_id == transfers_category.id,
            Transaction.amount_native == target_amount,
            Transaction.date >= date_start,
            Transaction.date <= date_end,
            Transaction.transfer_pair_id.is_(None),
        )
    ).first()
    if not match:
        raise api_error(404, "No matching transfer found", "no_matching_transfer")

    transaction.transfer_pair_id = match.id
    match.transfer_pair_id = transaction.id
    # M106: two rows moved, two receipts (docs/02 §transaction_events).
    record_transfer_event(
        db,
        transaction_id=transaction.id,
        user_id=current_user.id,
        actor=current_user.id,
        pair_id=match.id,
    )
    record_transfer_event(
        db,
        transaction_id=match.id,
        user_id=current_user.id,
        actor=current_user.id,
        pair_id=transaction.id,
    )
    db.add(transaction)
    db.add(match)
    db.commit()
    db.refresh(transaction)
    return transaction


@router.post("/{transaction_id}/link-transfer", response_model=TransactionPublic)
def link_transfer(
    transaction_id: int,
    payload: LinkTransferPayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Transaction:
    """Manually link two transactions as a transfer pair."""
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    pair_txn = owned_or_404(Transaction, payload.pair_id, current_user, db)
    _ensure_live(transaction, current_user, db)
    _ensure_live(pair_txn, current_user, db)
    # Both halves: pairing a parent OR a child is refused (docs/44 §5) —
    # pairs are row-level statement facts, never part-level.
    _refuse_split_row_write(transaction, "transfer")
    _refuse_split_row_write(pair_txn, "transfer")

    # M106: every row this route moves gets exactly ONE receipt, so the new
    # pair values are collected in a dict first — a row unlinked and then
    # re-paired by the same call (re-linking an existing partner) collapses to
    # its final state instead of writing two receipts.
    affected: dict[int, int | None] = {}

    # Unlink existing pairs on both sides.
    if transaction.transfer_pair_id is not None:
        old = db.get(Transaction, transaction.transfer_pair_id)
        if old is not None:
            old.transfer_pair_id = None
            affected[old.id] = None
            db.add(old)
    if pair_txn.transfer_pair_id is not None:
        old = db.get(Transaction, pair_txn.transfer_pair_id)
        if old is not None:
            old.transfer_pair_id = None
            affected[old.id] = None
            db.add(old)

    transaction.transfer_pair_id = pair_txn.id
    pair_txn.transfer_pair_id = transaction.id
    affected[transaction.id] = pair_txn.id
    affected[pair_txn.id] = transaction.id
    for row_id, counterpart in affected.items():
        record_transfer_event(
            db,
            transaction_id=row_id,
            user_id=current_user.id,
            actor=current_user.id,
            pair_id=counterpart,
        )
    db.add(transaction)
    db.add(pair_txn)
    db.commit()
    db.refresh(transaction)
    return transaction


@router.post("/{transaction_id}/unlink-transfer", response_model=TransactionPublic)
def unlink_transfer(
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Transaction:
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    _ensure_live(transaction, current_user, db)
    _refuse_split_row_write(transaction, "transfer")
    if transaction.transfer_pair_id is not None:
        pair_txn = db.get(Transaction, transaction.transfer_pair_id)
        if pair_txn is not None:
            pair_txn.transfer_pair_id = None
            # M106: both halves of the pair moved, so both are receipted.
            record_transfer_event(
                db,
                transaction_id=pair_txn.id,
                user_id=current_user.id,
                actor=current_user.id,
                pair_id=None,
            )
            db.add(pair_txn)
        transaction.transfer_pair_id = None
        record_transfer_event(
            db,
            transaction_id=transaction.id,
            user_id=current_user.id,
            actor=current_user.id,
            pair_id=None,
        )
        db.add(transaction)
        db.commit()
        db.refresh(transaction)
    return transaction


# --- M107: split transactions (docs/44) ----------------------------------------
#
# Four routes under `/transactions/{id}/split`: POST create, PUT replace,
# GET drilldown, DELETE merge-back. Two population rules, one money invariant
# and one event per mutation make this feature; everything else is refusal.
#
# The validation ORDER is part of the contract (docs/44 §4.2): steps 1-5 run
# in the handler before the body is parsed, so a request that trips several
# rules answers with the code §4.2 puts first, and a malformed body aimed at a
# child still says `split_nested` rather than `validation_error`. That is why
# the body arrives as a raw dict and is parsed HERE instead of being declared
# as a typed parameter (FastAPI would validate it before the handler ran).

_SPLIT_MIN_LEGS = 2
_SPLIT_MAX_LEGS = 20


def _split_gate(transaction: Transaction, user: User, db: Session, op: str) -> None:
    """docs/44 §4.2 steps 3-5. Steps 1 (`owned_or_404`) and 2 (`_ensure_live`)
    are the caller's — they are identical on all four routes."""
    if transaction.parent_split_id is not None:
        raise api_error(
            422,
            "This row is a part of a split; split the statement line instead",
            "split_nested",
        )
    account = db.get(Account, transaction.account_id)
    if account is None or account.user_id != user.id:
        raise api_error(404, "Account not found", "not_found")
    # docs/44 §13: splits are a personal-surface feature. Refused BEFORE any
    # write, so an investment account never sees a child row, a `trades` row
    # or an `acb_pools` row from this path — the ACB engine reads `trades`
    # only and `trades` has no FK to `transactions` at all.
    if account.type in INVESTMENT_ACCOUNT_TYPES:
        raise api_error(
            422,
            f"Splitting is not available for {account.type} accounts",
            "split_account_not_allowed",
        )
    if op == "POST":
        if transaction.is_split:
            raise api_error(409, "This transaction is already split", "already_split")
    elif not transaction.is_split:
        raise api_error(409, "This transaction is not split", "not_split")


def _parse_split_body(raw: dict[str, Any]) -> SplitCreatePayload:
    """Step 6 — the house `validation_error` envelope, shape only."""
    try:
        return SplitCreatePayload.model_validate(raw)
    except ValidationError as exc:
        # Same `detail` shape the FastAPI handler produces for a typed body
        # (`_normalize_validation_errors`), so a client cannot tell which of
        # the two routes the rejection came from.
        raise api_error(
            422, _normalize_validation_errors(exc.errors()), "validation_error"
        ) from None


def _validate_split_legs(
    parent: Transaction, payload: SplitCreatePayload, user: User, db: Session
) -> None:
    """docs/44 §4.2 steps 7-12, in that order.

    Every refusal happens BEFORE anything is inserted, so a rejected split
    writes nothing at all (test `test_split_sum_mismatch_is_refused_and_writes_nothing`).
    """
    if user.encryption_required:  # step 7
        if any(not leg.amount_cipher for leg in payload.children):
            raise api_error(
                422,
                "encryption_required: amount_cipher is required for every split part",
                "encryption_required",
            )
    count = len(payload.children)  # step 8
    if count < _SPLIT_MIN_LEGS:
        raise api_error(
            422,
            f"A split needs at least {_SPLIT_MIN_LEGS} parts; you sent {count}",
            "split_too_few_legs",
        )
    if count > _SPLIT_MAX_LEGS:
        raise api_error(
            422,
            f"A split allows at most {_SPLIT_MAX_LEGS} parts; you sent {count}",
            "split_too_many_legs",
        )
    if parent.amount_native == 0:  # step 9
        raise api_error(422, "A zero-amount transaction cannot be split", "split_zero_parent")
    wants_positive = parent.amount_native > 0
    for index, leg in enumerate(payload.children, start=1):  # step 10
        if leg.amount_native == 0:
            raise api_error(422, f"Part {index} has amount 0.00", "split_leg_zero")
        if (leg.amount_native > 0) != wants_positive:
            raise api_error(
                422,
                f"Part {index} must have the same sign as the transaction ({parent.amount_native})",
                "split_leg_sign",
            )
    for leg in payload.children:  # step 11
        if leg.category_id is not None:
            owned_or_404(Category, leg.category_id, user, db)
        for tag_id in leg.tag_ids:
            owned_or_404(Tag, tag_id, user, db)
    expected = (  # step 12
        parent.split_checksum if parent.split_checksum is not None else parent.amount_native
    )
    total = sum((leg.amount_native for leg in payload.children), Decimal("0"))
    if total != expected:
        raise api_error(
            422,
            f"Children must sum to {expected}; they sum to {total} (residual {expected - total}).",
            "split_sum_mismatch",
        )


def _allocate_cad(parent: Transaction, amounts: list[Decimal]) -> list[Decimal | None]:
    """docs/44 §3.3 — allocate the parent's CACHED cad_amount, never re-derive
    from FX.

    Re-deriving would round each child independently (±1c drift) and could use
    a different rate than the parent was priced at. Largest-remainder: every
    child takes its proportional share rounded HALF_UP to the cent, and the
    residual lands on the child with the LARGEST |amount| (ties → lowest
    index) so the split moves no CAD total anywhere — test
    `test_cad_allocation_never_moves_a_cad_total`.
    """
    if parent.cad_amount is None or parent.amount_native == 0:
        return [None] * len(amounts)  # unresolved stays unresolved
    cent = Decimal("0.01")
    shares = [
        (parent.cad_amount * amount / parent.amount_native).quantize(cent, rounding=ROUND_HALF_UP)
        for amount in amounts
    ]
    residual = parent.cad_amount - sum(shares, Decimal("0"))
    biggest = max(range(len(amounts)), key=lambda i: (abs(amounts[i]), -i))
    shares[biggest] += residual
    return shares


def _build_split_children(
    parent: Transaction, payload: SplitCreatePayload, cad_values: list[Decimal | None]
) -> list[Transaction]:
    """docs/44 §1 — identity inherited verbatim, categorisation and money owned
    by the leg, and a SALT on the dedup hash so no matcher can ever reach a
    child (docs/44 §7).

    `reviewed_at`/`reviewed_by` are copied from the parent on purpose: the
    statement line was reviewed, and its parts are the same money under
    different buckets. Leaving them NULL would silently re-queue a row the
    user already signed off on, and re-queueing is not a thing a split does.
    """
    children: list[Transaction] = []
    for leg, cad_amount in zip(payload.children, cad_values, strict=True):
        raw = RawTransaction(
            date=parent.date,
            posting_date=parent.posting_date,
            raw_description=parent.raw_description,
            amount_native=leg.amount_native,
            currency=parent.currency,
        )
        canonical = build_dedup_hash(parent.account_id, raw)
        children.append(
            Transaction(
                user_id=parent.user_id,
                account_id=parent.account_id,
                date=parent.date,
                posting_date=parent.posting_date,
                raw_description=parent.raw_description,
                normalized_payee=parent.normalized_payee,
                amount_native=leg.amount_native,
                currency=parent.currency,
                cad_amount=cad_amount,
                category_id=leg.category_id,
                note=leg.note,
                import_batch_id=parent.import_batch_id,
                cleared=parent.cleared,
                force_include=False,
                is_duplicate=False,
                # The salted shape staged duplicates already use
                # (`import_staging.py`), so the canonical hash of the PARENT
                # can never equal a child's: a re-imported statement line
                # matches the parent, never a part.
                dedup_hash=f"{canonical}:{uuid.uuid4().hex}",
                payee_cipher=parent.payee_cipher,
                raw_desc_cipher=parent.raw_desc_cipher,
                amount_cipher=leg.amount_cipher,
                cad_amount_cipher=leg.cad_amount_cipher,
                payee_token=parent.payee_token,
                is_recurring=False,
                recurring_cadence=None,
                recurring_next_date=None,
                transfer_pair_id=None,
                split_checksum=None,
                is_split=False,
                parent_split_id=parent.id,
                reviewed_at=parent.reviewed_at,
                reviewed_by=parent.reviewed_by,
            )
        )
    return children


def _child_rows(db: Session, parent: Transaction) -> list[Transaction]:
    """The group, `id ASC` = entry order (docs/44 §4.1)."""
    rows = db.exec(
        select(Transaction)
        .where(
            Transaction.user_id == parent.user_id,
            Transaction.parent_split_id == parent.id,
        )
        .order_by(Transaction.id)
    ).all()
    return list(rows)


def _split_response(
    db: Session, parent: Transaction, children: list[Transaction]
) -> TransactionSplit:
    """The shared answer of all four routes (docs/44 §4.1).

    `sum_ok` is recomputed here, on every read — never read back from a
    stored column, because a checksum nobody recomputes is a checksum nobody
    checks.
    """
    child_ids = [child.id for child in children if child.id is not None]
    tags_by_txn = _tags_by_txn(db, child_ids)
    # One statement for the group's receipt-image flags — the same no-N+1
    # rule the list route follows, minus its budget: this drilldown is
    # one group, and a child that had an image attached must not answer
    # `has_receipt: false`.
    receipt_ids: set[int] = set()
    if child_ids:
        receipt_ids = set(
            db.exec(
                select(TransactionReceipt.transaction_id).where(
                    TransactionReceipt.user_id == parent.user_id,
                    TransactionReceipt.transaction_id.in_([parent.id, *child_ids]),
                )
            ).all()
        )
    child_items = []
    for child in children:
        item = TransactionPublic.model_validate(child)
        item.tags = tags_by_txn.get(child.id, [])
        item.has_receipt = child.id in receipt_ids
        child_items.append(item)
    header = SplitParentPublic.model_validate(parent)
    header.child_count = len(children)
    header.tags = _load_transaction_tags(db, parent.id)
    header.has_receipt = parent.id in receipt_ids
    checksum = parent.split_checksum if parent.split_checksum is not None else parent.amount_native
    total = sum((child.amount_native for child in children), Decimal("0"))
    return TransactionSplit(
        parent=header,
        children=child_items,
        split_checksum=checksum,
        sum_ok=total == checksum,
    )


def _commit_split(
    db: Session,
    *,
    parent: Transaction,
    children: list[Transaction],
    payload: SplitCreatePayload,
    op: Literal["create", "replace"],
    before_ids: list[int],
    actor: int,
) -> TransactionSplit:
    """Step 13 — one DB transaction, then ONE `split` receipt (docs/44 §11).

    Children write no `created` receipt: their lifecycle IS the parent's
    event (D15), so one mutation path yields one row in the log no matter how
    many rows moved.
    """
    for child in children:
        db.add(child)
    db.flush()  # child ids exist before the receipt names them
    for child, leg in zip(children, payload.children, strict=True):
        for tag_id in dict.fromkeys(leg.tag_ids):
            db.add(TransactionTag(transaction_id=child.id, tag_id=tag_id))
    parent.is_split = True
    parent.split_checksum = parent.amount_native
    if op == "create":
        event_payload = {
            "op": "create",
            "parent_id": parent.id,
            "child_ids": [child.id for child in children],
            "amounts": [child.amount_native for child in children],
        }
    else:
        event_payload = {
            "op": "replace",
            "parent_id": parent.id,
            "before": before_ids,
            "after": [child.id for child in children],
        }
    record_event(
        db,
        tx_id=parent.id,
        user_id=parent.user_id,
        kind=TransactionEventKind.split,
        payload=event_payload,
        actor=actor,
    )
    try:
        db.commit()
    except IntegrityError:
        # Unreachable in practice — child hashes are salted — but the same
        # path as `POST /transactions`, so a race answers the same way.
        db.rollback()
        raise api_error(409, "Transaction already exists", "duplicate_transaction") from None
    db.refresh(parent)
    return _split_response(db, parent, _child_rows(db, parent))


@router.post("/{transaction_id}/split", response_model=TransactionSplit, status_code=201)
def create_split(
    transaction_id: int,
    raw: Annotated[dict[str, Any], Body()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionSplit:
    """Split one statement line into 2-20 parts (docs/44 §4.1)."""
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)  # 1
    _ensure_live(transaction, current_user, db)  # 2
    _split_gate(transaction, current_user, db, "POST")  # 3-5
    payload = _parse_split_body(raw)  # 6
    _validate_split_legs(transaction, payload, current_user, db)  # 7-12
    amounts = [leg.amount_native for leg in payload.children]
    children = _build_split_children(transaction, payload, _allocate_cad(transaction, amounts))
    return _commit_split(
        db,
        parent=transaction,
        children=children,
        payload=payload,
        op="create",
        before_ids=[],
        actor=current_user.id,
    )


@router.put("/{transaction_id}/split", response_model=TransactionSplit)
def replace_split(
    transaction_id: int,
    raw: Annotated[dict[str, Any], Body()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionSplit:
    """Replace ALL of a split's children — the Re-split path (docs/44 §4.1).

    Old children go through `_purge_transaction` (the same internal helper as
    a normal delete, docs/44 §5) so their tag links, suggestions and
    reconciliation matches are cleaned exactly as a normal delete cleans them,
    and with `write_receipt=False` because D15 gives children no `deleted`
    receipt — the ONE `split` event below carries `before`/`after` instead.
    """
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)  # 1
    _ensure_live(transaction, current_user, db)  # 2
    _split_gate(transaction, current_user, db, "PUT")  # 3-5
    payload = _parse_split_body(raw)  # 6
    _validate_split_legs(transaction, payload, current_user, db)  # 7-12
    existing = _child_rows(db, transaction)
    before_ids = [child.id for child in existing if child.id is not None]
    for child in existing:
        _purge_transaction(db, child, actor=current_user.id, write_receipt=False)
    amounts = [leg.amount_native for leg in payload.children]
    children = _build_split_children(transaction, payload, _allocate_cad(transaction, amounts))
    return _commit_split(
        db,
        parent=transaction,
        children=children,
        payload=payload,
        op="replace",
        before_ids=before_ids,
        actor=current_user.id,
    )


@router.get("/{transaction_id}/split", response_model=TransactionSplit)
def get_split(
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionSplit:
    """The drilldown the register opens — always fresh, always recomputes
    `sum_ok`, so integrity is assertable without writing (docs/44 §3.1)."""
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)  # 1
    _ensure_live(transaction, current_user, db)  # 2
    _split_gate(transaction, current_user, db, "GET")  # 3-5
    return _split_response(db, transaction, _child_rows(db, transaction))


@router.delete("/{transaction_id}/split", response_model=TransactionPublic)
def merge_split(
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Transaction:
    """Merge the parts back into the statement line (docs/44 §5).

    Children are removed through `_purge_transaction`, the same internal
    helper a normal delete uses; the parent keeps everything it had — its
    category, tags, note, amount, payee and ciphers were never touched, which
    is what makes merge an exact inverse rather than a reconstruction.
    One `merge` receipt carries the deleted child ids, so the history of what
    was merged survives the deletion.
    """
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)  # 1
    _ensure_live(transaction, current_user, db)  # 2
    _split_gate(transaction, current_user, db, "DELETE")  # 3-5
    children = _child_rows(db, transaction)
    child_ids = [child.id for child in children if child.id is not None]
    for child in children:
        _purge_transaction(db, child, actor=current_user.id, write_receipt=False)
    transaction.is_split = False
    transaction.split_checksum = None
    record_event(
        db,
        tx_id=transaction.id,
        user_id=transaction.user_id,
        kind=TransactionEventKind.merge,
        payload={"op": "merge", "parent_id": transaction.id, "child_ids": child_ids},
        actor=current_user.id,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, "Transaction already exists", "duplicate_transaction") from None
    db.refresh(transaction)
    return transaction


# --- Transaction tags ----------------------------------------------------------


def _load_transaction_tags(db: Session, transaction_id: int) -> list[Tag]:
    """Load tags attached to a transaction via the junction table."""
    rows = db.exec(
        select(Tag)
        .join(TransactionTag, TransactionTag.tag_id == Tag.id)
        .where(TransactionTag.transaction_id == transaction_id)
        .order_by(Tag.name)
    ).all()
    return list(rows)


@router.get("/{transaction_id}/tags")
def get_transaction_tags(
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    tags = _load_transaction_tags(db, transaction.id)
    return {"items": tags}


@router.put("/{transaction_id}/tags")
def set_transaction_tags(
    transaction_id: int,
    payload: SetTransactionTagsPayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Replace all tags on a transaction.

    Diff-based and duplicate-safe: payload ids are deduped, only genuinely
    added/removed rows are written, and inserts use ON CONFLICT DO NOTHING
    so concurrent setters cannot 500 on the unique constraint.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    _refuse_split_row_write(transaction, "tag")
    for tid in payload.tag_ids:
        owned_or_404(Tag, tid, current_user, db)
    wanted = list(dict.fromkeys(payload.tag_ids))
    existing = db.exec(
        select(TransactionTag).where(TransactionTag.transaction_id == transaction.id)
    ).all()
    have = {row.tag_id for row in existing}
    stale = [row.tag_id for row in existing if row.tag_id not in wanted]
    if stale:
        # Bulk delete: no per-object row-count check, so a concurrent
        # setter deleting the same rows logs no warning.
        db.execute(
            sa_delete(TransactionTag).where(
                TransactionTag.transaction_id == transaction.id,
                TransactionTag.tag_id.in_(stale),
            )
        )
    missing = [tid for tid in wanted if tid not in have]
    if missing:
        db.execute(
            pg_insert(TransactionTag)
            .values([{"transaction_id": transaction.id, "tag_id": tid} for tid in missing])
            .on_conflict_do_nothing(index_elements=["transaction_id", "tag_id"])
        )
    if stale or missing:
        # M106 (docs/02): ONE receipt per PUT, only when the junction actually
        # moved — re-PUTting the set a transaction already has is a no-op in
        # the log too, exactly as it is in the table.
        record_event(
            db,
            tx_id=transaction.id,
            user_id=transaction.user_id,
            kind=TransactionEventKind.tagged,
            payload={
                "added": load_tag_names(db, missing),
                "removed": load_tag_names(db, stale),
                "added_ids": sorted(missing),
                "removed_ids": sorted(stale),
            },
            actor=current_user.id,
        )
    db.commit()
    tags = _load_transaction_tags(db, transaction.id)
    return {"items": tags}


@router.post("/{transaction_id}/tags")
def add_transaction_tag(
    transaction_id: int,
    payload: AddTransactionTagPayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Add one tag to a transaction (idempotent, race-safe)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    _refuse_split_row_write(transaction, "tag")
    tag = owned_or_404(Tag, payload.tag_id, current_user, db)
    # M106 (docs/02): receipt ONLY when the link is genuinely new — the insert
    # below is ON CONFLICT DO NOTHING, so re-attaching a tag the row already
    # carries must not manufacture a second "you added tag …" line.
    already = db.exec(
        select(TransactionTag.id).where(
            TransactionTag.transaction_id == transaction.id,
            TransactionTag.tag_id == tag.id,
        )
    ).first()
    db.execute(
        pg_insert(TransactionTag)
        .values({"transaction_id": transaction.id, "tag_id": tag.id})
        .on_conflict_do_nothing(index_elements=["transaction_id", "tag_id"])
    )
    if already is None:
        record_event(
            db,
            tx_id=transaction.id,
            user_id=transaction.user_id,
            kind=TransactionEventKind.tagged,
            payload={
                "added": [tag.name],
                "removed": [],
                "added_ids": [tag.id],
                "removed_ids": [],
            },
            actor=current_user.id,
        )
    db.commit()
    tags = _load_transaction_tags(db, transaction.id)
    return {"items": tags}


@router.delete("/{transaction_id}/tags/{tag_id}")
def remove_transaction_tag(
    transaction_id: int,
    tag_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Remove one tag from a transaction."""
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    _refuse_split_row_write(transaction, "tag")
    tag = owned_or_404(Tag, tag_id, current_user, db)
    row = db.exec(
        select(TransactionTag).where(
            TransactionTag.transaction_id == transaction_id,
            TransactionTag.tag_id == tag_id,
        )
    ).first()
    if row is not None:
        db.delete(row)
        # M106 (docs/02): one receipt per link that actually came off — a
        # DELETE for a tag the row never carried writes nothing (404-free
        # no-op stays a no-op in the log too).
        record_event(
            db,
            tx_id=transaction_id,
            user_id=transaction.user_id,
            kind=TransactionEventKind.tagged,
            payload={
                "added": [],
                "removed": [tag.name],
                "added_ids": [],
                "removed_ids": [tag.id],
            },
            actor=current_user.id,
        )
        db.commit()


# --- M106: review state + the per-transaction activity log (docs/02, docs/09) ---


def _event_public(
    event: TransactionEvent, viewer_id: int, names: dict[int, str | None]
) -> TransactionEventPublic:
    """One receipt, rendered for whoever is reading it.

    The label is computed per VIEWER: the same row reads "you set category
    Groceries" to the person who did it and "Mark set category Groceries" to
    everyone else, without storing the sentence twice.
    """
    label = actor_label(event.actor, viewer_id, names)
    return TransactionEventPublic(
        id=event.id,
        tx_id=event.tx_id,
        event_kind=event.event_kind,
        actor=event.actor,
        actor_label=label,
        at=event.created_at,
        payload_hash=event.payload_hash,
        summary=render_summary(event.event_kind, event.payload, label),
        payload=event.payload,
    )


@router.post("/{transaction_id}/review", response_model=TransactionPublic)
def set_transaction_review(
    transaction_id: int,
    payload: ReviewTogglePayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Transaction:
    """Mark one row reviewed / unreviewed.

    The row toggle and the bulk toolbar BOTH drive this single-row route
    (docs/09 §Bulk selection toolbar: N rows are N sequential requests, no
    bulk schema is invented here), so a bulk run leaves N receipts — one per
    row, exactly like N individual clicks would.

    Idempotent on purpose: asking for the state a row already has changes
    nothing, so it writes no receipt. A review queue whose log fills with
    no-ops is a log nobody can read.
    """
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    _ensure_live(transaction, current_user, db)
    if payload.reviewed:
        if transaction.reviewed_at is None:
            transaction.reviewed_at = datetime.now(UTC)
            transaction.reviewed_by = current_user.id
            record_event(
                db,
                tx_id=transaction.id,
                user_id=transaction.user_id,
                kind=TransactionEventKind.reviewed,
                payload={"reviewed": True},
                actor=current_user.id,
            )
    elif transaction.reviewed_at is not None:
        transaction.reviewed_at = None
        transaction.reviewed_by = None
        record_event(
            db,
            tx_id=transaction.id,
            user_id=transaction.user_id,
            kind=TransactionEventKind.reviewed,
            payload={"reviewed": False},
            actor=current_user.id,
        )
    db.commit()
    db.refresh(transaction)
    return transaction


@router.get("/{transaction_id}/events", response_model=TransactionEventPage)
def list_transaction_events(
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionEventPage:
    """The History drawer: every receipt for one transaction, newest first.

    Owner surface. A partner reaches the same log through
    `GET /partner/transactions/{id}/events`, which additionally keeps only
    receipts whose actor is the owner or the partner themselves — a
    cloud-service receipt is nobody's business but the owner's (docs/43 §6).

    Unpaginated on purpose: one transaction's history is bounded by how many
    times a person touched it, and a drawer that pages through its own past
    is a drawer nobody opens twice.
    """
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    events = db.exec(
        select(TransactionEvent)
        .where(
            TransactionEvent.tx_id == transaction.id,
            TransactionEvent.user_id == current_user.id,
        )
        .order_by(TransactionEvent.created_at.desc(), TransactionEvent.id.desc())
    ).all()
    actor_ids = {event.actor for event in events if event.actor is not None}
    if current_user.id is not None:
        actor_ids.add(current_user.id)
    names = {
        row.id: row.name
        for row in db.exec(select(User).where(User.id.in_(sorted(actor_ids)))).all()
    }
    items = [_event_public(event, current_user.id, names) for event in events]
    return TransactionEventPage(items=items, total=len(items))


# --- M108 P4: receipt images (docs/09 §Receipt images, docs/08) ---------------
#
# ONE image per transaction, stored locally and served back only to its owner
# over the authenticated API. Deliberately NOT built:
#   * OCR — see docs/09 for the viability check and its numbers;
#   * email forwarding — an always-open inbound address is an abuse surface
#     (the Monarch scan's privacy posture, carried verbatim);
#   * any cloud call — `app/services/fx.py` and `market_data.py` remain the
#     only egress seams (AGENTS §2).
# The demo tenant is covered by the CENTRAL write guard in
# `app.api.deps::get_current_user` (docs/20), so there is no per-handler
# `_assert_not_demo` to remember here.

# Magic bytes for every type we accept — the `Content-Type` header is the
# client's claim, this is the body's. Mismatch is a 422, never a stored file
# we would later serve back from our own origin under an image type.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _sniff_image_type(raw: bytes) -> str | None:
    """Content type claimed by the bytes, or None if they are not an image."""
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    for magic, content_type in _SIGNATURES:
        if raw.startswith(magic):
            return content_type
    return None


def _reject_oversize_receipt(request: Request) -> None:
    """Refuse a declared body over the cap BEFORE reading it (docs/08 413)."""
    declared = request.headers.get("content-length")
    if declared is None:
        return  # chunked: the real check is on the bytes we actually read
    try:
        size = int(declared)
    except ValueError:
        return
    if size > RECEIPT_MAX_BYTES:
        raise api_error(413, "Receipt image exceeds the 2 MB limit", "receipt_too_large")


def _load_receipt_row(db: Session, transaction_id: int, user: User) -> TransactionReceipt | None:
    """This transaction's image, scoped to the caller.

    The `user_id` predicate is redundant with the `owned_or_404` every caller
    runs first — `transaction_receipts.user_id` equals `transactions.user_id`
    by construction — but ownership of a tenant table is expressed in the
    statement itself here, the same way the list route's flag query does it.
    One rule, both places, so the M93 tripwire never has to special-case a
    table it cannot see the reasoning for.
    """
    return db.exec(
        select(TransactionReceipt).where(
            TransactionReceipt.user_id == user.id,
            TransactionReceipt.transaction_id == transaction_id,
        )
    ).first()


def _receipt_public(transaction_id: int, row: TransactionReceipt) -> TransactionReceiptPublic:
    return TransactionReceiptPublic(
        transaction_id=transaction_id,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
    )


@router.put("/{transaction_id}/receipt", response_model=TransactionReceiptPublic)
@human_paced_limit(RECEIPT_UPLOAD_LIMIT)
async def put_transaction_receipt(
    transaction_id: int,
    request: Request,
    file: Annotated[
        UploadFile,
        File(description="Receipt image — webp/jpeg/png/gif, at most 2 MB"),
    ],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionReceiptPublic:
    """Attach (or REPLACE) the transaction's one receipt image.

    One-to-one is the v1 contract, so this is a PUT and not a POST: a second
    upload replaces the first instead of accumulating a gallery (docs/09).
    404 `not_found` when the transaction is not the caller's; 413
    `receipt_too_large` over 2 MB; 422 `receipt_unsupported_type` for a body
    whose magic bytes are not one of the four accepted images; 422
    `receipt_empty` for a zero-byte upload.
    """
    transaction = await blocking(owned_or_404)(Transaction, transaction_id, current_user, db)
    _reject_oversize_receipt(request)
    raw = await file.read(RECEIPT_MAX_BYTES + 1)
    if len(raw) > RECEIPT_MAX_BYTES:
        raise api_error(413, "Receipt image exceeds the 2 MB limit", "receipt_too_large")
    if not raw:
        raise api_error(422, "Receipt image is empty", "receipt_empty")

    # M149: the sniff, the base64 round-trip and the write are blocking work;
    # a 2 MB upload on the loop would stall every other request (docs/01
    # § "Sessions and the event loop").
    def core() -> TransactionReceiptPublic:
        claimed = (file.content_type or "").lower().split(";")[0].strip()
        sniffed = _sniff_image_type(raw)
        if sniffed is None or sniffed not in RECEIPT_CONTENT_TYPES or claimed != sniffed:
            raise api_error(
                422,
                f"Unsupported receipt image type: expected one of "
                f"{', '.join(sorted(RECEIPT_CONTENT_TYPES))}",
                "receipt_unsupported_type",
            )

        row = _load_receipt_row(db, transaction.id, current_user)
        if row is None:
            row = TransactionReceipt(
                user_id=current_user.id,
                transaction_id=transaction.id,
                content_type=sniffed,
                size_bytes=len(raw),
                data_base64=base64.b64encode(raw).decode("ascii"),
            )
        else:
            row.content_type = sniffed
            row.size_bytes = len(raw)
            row.data_base64 = base64.b64encode(raw).decode("ascii")
        db.add(row)
        db.commit()
        db.refresh(row)
        return _receipt_public(transaction.id, row)

    return await blocking(core)()


@router.get("/{transaction_id}/receipt")
def get_transaction_receipt(
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """The image itself, with the content type it was validated against.

    `private` caching: the bytes are per-user financial data, so a shared
    proxy must not keep them — but the row's own browser cache may, which is
    what makes reopening the same receipt instant.
    """
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    row = _load_receipt_row(db, transaction.id, current_user)
    if row is None:
        raise api_error(404, "No receipt image is attached", "receipt_not_found")
    return Response(
        content=base64.b64decode(row.data_base64),
        media_type=row.content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.delete("/{transaction_id}/receipt", status_code=204)
@human_paced_limit(RECEIPT_UPLOAD_LIMIT)
def delete_transaction_receipt(
    transaction_id: int,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Remove the receipt image. **Idempotent 204** whether or not one exists.

    Deliberately not a 404-on-absent: the row's chip and the editor's field
    both disappear on success, and a double click (or a second tab) must not
    turn that into an error the user has to read. The IMAGE is gone either
    way, which is the only thing this route promises.
    """
    transaction = owned_or_404(Transaction, transaction_id, current_user, db)
    row = _load_receipt_row(db, transaction.id, current_user)
    if row is not None:
        db.delete(row)
        db.commit()
    return Response(status_code=204)

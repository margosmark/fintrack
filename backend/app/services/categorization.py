"""Three-layer categorization pipeline (docs/04).

Layer 1 rules apply and receipt; layer 2 learned-map hits apply, receipt and
bump hit_count; layer 3 remainder is batched (<=25) into LLM calls executed as
a BackgroundTask with 60 s timeout, one retry, then the batch is abandoned and
logged. LLM failures never affect layers 1-2.

M120 (docs/02 §transaction_events, docs/M120_RECEIPTS_SPEC §1/§3): every
category write below leaves a `categorized` receipt whose payload carries
`via = "rule" | "learned" | "llm"`, with `actor=None` — the pipeline runs with
nobody at the keyboard, and `actor IN (owner, partner)` excludes NULL, so
those receipts land in the owner's drawer and nowhere else (docs/43 §6).

The background task opens its own DB session via `_new_session` (dependency
teardown precedes background execution in FastAPI); tests monkeypatch this to
share their transactional session.
"""

import datetime as dt
import logging
from collections.abc import Sequence

from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, select

from app.api._search_helpers import SCORE_THRESHOLD, _payee_score
from app.api.deps import get_engine
from app.core.config import get_settings
from app.llm.base import build_provider
from app.models.accounts import Account
from app.models.categories import Category
from app.models.learned_payees import LearnedPayee
from app.models.llm_suggestions import LlmSuggestion, SuggestionStatus
from app.models.transaction_events import TransactionEventKind
from app.models.transactions import Transaction
from app.models.users import User
from app.services.rules_engine import apply_rules
from app.services.session_release import release_before_provider_wait
from app.services.tx_events import load_category_names, record_event
from app.services.txn_filters import LEDGER_EXCLUDE_SPLIT

logger = logging.getLogger(__name__)

BATCH_SIZE = 25
MAX_RUN = 500
AUTO_APPLY_CONFIDENCE = 0.9
FEW_SHOT_LIMIT = 10

CATEGORIZATION_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "transaction_id": {"type": "integer"},
                    "category_id": {"type": "integer"},
                    "confidence": {"type": "number"},
                },
                "required": ["transaction_id", "category_id", "confidence"],
            },
        }
    },
    "required": ["suggestions"],
}


def _new_session() -> Session:
    """Session factory for background work (seam for tests)."""
    return Session(get_engine(), expire_on_commit=False)


def upsert_learned(
    session: SASession, user_id: int, normalized_payee: str, category_id: int
) -> None:
    """Learning-loop write: upsert (user, payee) -> category; repeats bump hit_count."""
    row = session.exec(
        select(LearnedPayee).where(
            LearnedPayee.user_id == user_id,
            LearnedPayee.normalized_payee == normalized_payee,
        )
    ).first()
    if row is None:
        session.add(
            LearnedPayee(
                user_id=user_id, normalized_payee=normalized_payee, category_id=category_id
            )
        )
    else:
        row.category_id = category_id
        row.hit_count += 1


def learn_from_correction(
    session: SASession, user_id: int, transaction: Transaction
) -> dict | None:
    """A HUMAN's correction teaches the learned rule (docs/04 §Learning loop).

    Kilter's model: transactions arrive categorized and the rules learn from the
    corrections. Any edit that leaves a row with BOTH a normalized payee and a
    category writes/updates that `(payee -> category)` rule — the layer-2 rule
    the pipeline applies to the next uncategorized row with the same payee — and
    returns the receipt marker the History drawer renders as the
    "learned from you" badge.

    Returns `None` (writes nothing) when:
      * the row has no category — clearing a category teaches no mapping, and
        `learned_payees.category_id` is NOT NULL, so an existing rule for that
        payee is deliberately LEFT ALONE (one cleared row must not silently
        wipe the learning behind every future row of that payee);
      * the row has no `normalized_payee` — an E2EE row whose payee lives only
        in `payee_cipher` has nothing to key a rule on.

    Promoting a payee to a `rules` ROW is still user-initiated from the rules
    editor and never automatic (docs/04): this writes the learned map only.
    """
    if transaction.category_id is None or not transaction.normalized_payee:
        return None
    upsert_learned(session, user_id, transaction.normalized_payee, transaction.category_id)
    return {
        "normalized_payee": transaction.normalized_payee,
        "category_id": transaction.category_id,
    }


def load_learned_map(session: SASession, user_id: int, payees: Sequence[str]) -> dict[str, int]:
    if not payees:
        return {}
    rows = session.exec(
        select(LearnedPayee).where(
            LearnedPayee.user_id == user_id,
            LearnedPayee.normalized_payee.in_(list(payees)),
        )
    ).all()
    return {row.normalized_payee: row.category_id for row in rows}


def _uncategorized(
    session: SASession,
    user_id: int,
    transaction_ids: Sequence[int] | None,
    from_: dt.date | None = None,
    to: dt.date | None = None,
):
    statement = select(Transaction).where(
        Transaction.user_id == user_id,
        Transaction.category_id.is_(None),
        # M107 (docs/44 §2): LEDGER — a split parent's category is inert while
        # the split exists, so it is never a candidate; a CHILD may receive one.
        LEDGER_EXCLUDE_SPLIT,
    )
    if transaction_ids:
        statement = statement.where(Transaction.id.in_(list(transaction_ids)))
    else:
        statement = statement.order_by(Transaction.id.asc()).limit(MAX_RUN)
    if from_ is not None:
        statement = statement.where(Transaction.date >= from_)
    if to is not None:
        statement = statement.where(Transaction.date <= to)
    return list(session.exec(statement).all())


def _pipeline_receipt(
    session: SASession,
    transaction: Transaction,
    category_id: int,
    names: dict[int, str],
    via: str,
) -> None:
    """One `categorized` receipt for a pipeline apply (M120 §1), actor NULL.

    The name is captured at write time so a later rename never rewords a
    receipt the drawer already showed someone.
    """
    if transaction.id is None:
        return
    record_event(
        session,
        tx_id=transaction.id,
        user_id=transaction.user_id,
        kind=TransactionEventKind.categorized,
        payload={
            "category_id": category_id,
            "category_name": names.get(category_id),
            "via": via,
        },
        actor=None,
    )


def run_pipeline_sync(
    session: SASession,
    user: User,
    transaction_ids: Sequence[int] | None = None,
    from_: dt.date | None = None,
    to: dt.date | None = None,
) -> list[list[int]]:
    """Layers 1-2 run inline; returns the remaining transaction ids batched for layer 3."""
    transactions = _uncategorized(session, user.id, transaction_ids, from_=from_, to=to)

    # Layer 1: rules engine — applies AND receipts (M120; docs/04 layer 1 no
    # longer says "applies silently"). `actor=None` marks the write as the
    # pipeline's own.
    apply_rules(session, user.id, transactions, receipt_via="rule", actor=None)

    # Layer 2: learned map — apply + receipt + hit_count bump.
    remaining = [t for t in transactions if t.category_id is None]
    learned = load_learned_map(session, user.id, {t.normalized_payee for t in remaining})
    if learned:
        names = load_category_names(session, learned.values())
        for transaction in remaining:
            category_id = learned.get(transaction.normalized_payee)
            if category_id is not None:
                transaction.category_id = category_id
                upsert_learned(session, user.id, transaction.normalized_payee, category_id)
                _pipeline_receipt(session, transaction, category_id, names, "learned")

    session.commit()

    still_uncategorized = sorted(t.id for t in transactions if t.category_id is None)
    return [
        still_uncategorized[i : i + BATCH_SIZE]
        for i in range(0, len(still_uncategorized), BATCH_SIZE)
    ]


def _rejected_payees(session: SASession, user_id: int) -> set[str]:
    rows = session.exec(
        select(Transaction.normalized_payee)
        .join(LlmSuggestion, LlmSuggestion.transaction_id == Transaction.id)
        .where(LlmSuggestion.user_id == user_id, LlmSuggestion.status == SuggestionStatus.rejected)
    ).all()
    return set(rows)


def _few_shot_examples(
    session: SASession, user_id: int, batch: Sequence[Transaction]
) -> list[Transaction]:
    """Up to 10 categorized examples matching batch payees via fuzzy search.

    For each unique payee in the batch, find the best past categorized match
    using pg_trgm scoring. Fallback to recent categorized transactions when
    fuzzy matches are insufficient.
    """
    rejected = _rejected_payees(session, user_id)
    base_filter = [
        Transaction.user_id == user_id,
        Transaction.category_id.is_not(None),
        # M107 (docs/44 §2): LEDGER — learn from rows whose categories are
        # actually counted, never from a parent's inert pre-split category.
        LEDGER_EXCLUDE_SPLIT,
    ]
    if rejected:
        base_filter.append(Transaction.normalized_payee.not_in(rejected))

    # Collect unique payees from the batch (cap at 5 to limit queries).
    batch_payees = list({t.normalized_payee for t in batch if t.normalized_payee})[:5]

    seen_ids: set[int] = set()
    candidates: list[tuple[float, Transaction]] = []

    for payee in batch_payees:
        score = _payee_score(payee)
        row = (
            session.exec(
                select(Transaction, score.label("score"))
                .where(*base_filter)
                .order_by(score.desc(), Transaction.id.desc())
                .limit(1)
            )
        ).first()
        if row is not None:
            tx, raw_score = row
            if tx.id not in seen_ids and float(raw_score) >= SCORE_THRESHOLD:
                seen_ids.add(tx.id)
                candidates.append((float(raw_score), tx))

    # Sort by score desc, then id desc for deterministic tiebreak.
    candidates.sort(key=lambda x: (-x[0], -x[1].id))
    result = [tx for _, tx in candidates[:FEW_SHOT_LIMIT]]

    # Backfill with recent categorized transactions if fewer than FEW_SHOT_LIMIT.
    if len(result) < FEW_SHOT_LIMIT:
        backfill = (
            session.exec(
                select(Transaction)
                .where(*base_filter)
                .order_by(Transaction.id.desc())
                .limit(FEW_SHOT_LIMIT - len(result))
            )
        ).all()
        for tx in backfill:
            if tx.id not in seen_ids:
                seen_ids.add(tx.id)
                result.append(tx)

    return result


def build_messages(
    categories: Sequence[Category],
    few_shots: Sequence[Transaction],
    category_names: dict[int, str],
    batch: Sequence[Transaction],
) -> list[dict]:
    category_lines = "\n".join(f"{c.id}: {c.name}" for c in categories)
    example_lines = "\n".join(
        f"{t.normalized_payee} => {category_names[t.category_id]}" for t in few_shots
    )
    transaction_lines = "\n".join(f"{t.id}: {t.normalized_payee}" for t in batch)
    system = (
        "You categorize bank transactions. Choose a category id from this list "
        f"for each transaction.\nCategories:\n{category_lines}\n"
        + (f"\nExamples:\n{example_lines}\n" if example_lines else "\n")
        + 'Respond with JSON: {"suggestions": [{"transaction_id": int, '
        '"category_id": int, "confidence": number}]}'
    )
    user = f"Transactions:\n{transaction_lines}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _valid_suggestions(
    payload: dict, batch_ids: set[int], allowed_category_ids: set[int]
) -> list[tuple[int, int, float]]:
    """Schema-validate one response; violating or unknown-id entries are discarded."""
    suggestions = payload.get("suggestions") if isinstance(payload, dict) else None
    if not isinstance(suggestions, list):
        return []
    valid: list[tuple[int, int, float]] = []
    seen: set[int] = set()
    for entry in suggestions:
        if not isinstance(entry, dict):
            continue
        transaction_id = entry.get("transaction_id")
        category_id = entry.get("category_id")
        confidence = entry.get("confidence")
        if not isinstance(transaction_id, int) or not isinstance(category_id, int):
            continue
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        if not 0.0 <= confidence <= 1.0:
            continue
        if transaction_id not in batch_ids or category_id not in allowed_category_ids:
            continue
        if transaction_id in seen:
            continue
        seen.add(transaction_id)
        valid.append((transaction_id, category_id, float(confidence)))
    return valid


def process_llm_batches(user_id: int, batches: list[list[int]]) -> None:
    """Layer-3 background worker: one provider call per batch of <=25 rows."""
    with _new_session() as session:
        user = session.get(User, user_id)
        if user is None:
            return
        resolved = build_provider(user, get_settings())
        if resolved.provider is None:
            logger.warning(
                "No LLM provider resolved for user %s; batches left uncategorized", user_id
            )
            return

        categories = list(session.exec(select(Category).where(Category.user_id == user_id)).all())
        # Exclude "uncategorized" — meaningless as an LLM suggestion.
        categories = [c for c in categories if c.name != "uncategorized"]
        category_names = {c.id: c.name for c in categories}
        allowed_category_ids = set(category_names)

        for batch_ids in batches:
            try:
                _process_one_batch(
                    session,
                    user,
                    resolved.provider,
                    resolved.model,
                    batch_ids,
                    categories,
                    category_names,
                    allowed_category_ids,
                )
                session.commit()
            except Exception:
                session.rollback()
                logger.warning("LLM batch abandoned after retry: %s", batch_ids, exc_info=True)


def _process_one_batch(
    session: SASession,
    user: User,
    provider,
    model: str,
    batch_ids: list[int],
    categories: Sequence[Category],
    category_names: dict[int, str],
    allowed_category_ids: set[int],
) -> None:
    transactions = list(
        session.exec(
            select(Transaction).where(
                Transaction.id.in_(batch_ids),
                Transaction.user_id == user.id,
                Transaction.category_id.is_(None),
            )
        )
    )
    if not transactions:
        return
    accounts = {
        a.id: a
        for a in session.exec(
            select(Account).where(
                Account.id.in_({t.account_id for t in transactions}),
                Account.user_id == user.id,
            )
        ).all()
    }

    few_shots = _few_shot_examples(session, user.id, transactions)
    messages = build_messages(categories, few_shots, category_names, transactions)

    # M175 (docs/07 §Provider-wait ledger): this is the LONGEST provider wait in
    # the app — the LLM egress deadline is 60 s and the loop below retries once,
    # so a single batch could park the worker's connection for two minutes. The
    # reads (`transactions`, `accounts`, `_few_shot_examples`) all happened
    # above; the batch's writes all happen below, and `process_llm_batches`
    # commits per batch anyway.
    release_before_provider_wait(session)
    payload = None
    last_error: Exception | None = None
    for attempt in range(2):  # initial attempt + ONE retry
        try:
            payload = provider.chat(messages, CATEGORIZATION_SCHEMA)
            break
        except Exception as exc:  # timeout / transport / undecodable content
            last_error = exc
            logger.info("LLM chat attempt %d failed for batch %s", attempt + 1, batch_ids)
    if payload is None:
        raise RuntimeError(f"LLM call failed twice for batch {batch_ids}") from last_error

    # Re-run hygiene: replace stale pending suggestions for these transactions.
    for stale in session.exec(
        select(LlmSuggestion).where(
            LlmSuggestion.user_id == user.id,
            LlmSuggestion.status == SuggestionStatus.suggested,
            LlmSuggestion.transaction_id.in_(batch_ids),
        )
    ).all():
        session.delete(stale)

    for transaction_id, category_id, confidence in _valid_suggestions(
        payload, {t.id for t in transactions}, allowed_category_ids
    ):
        transaction = next(t for t in transactions if t.id == transaction_id)
        account = accounts.get(transaction.account_id)
        auto_apply = (
            account is not None and account.llm_autopilot and confidence >= AUTO_APPLY_CONFIDENCE
        )
        status = SuggestionStatus.accepted if auto_apply else SuggestionStatus.suggested
        session.add(
            LlmSuggestion(
                user_id=user.id,
                transaction_id=transaction_id,
                proposed_category_id=category_id,
                confidence=confidence,
                status=status,
                model_used=model,
            )
        )
        if auto_apply:
            transaction.category_id = category_id
            upsert_learned(session, user.id, transaction.normalized_payee, category_id)
            # M120: an auto-applied assistant pick is a categorization like any
            # other — it receipts, `via=llm`, actor NULL (docs/04 layer 3).
            _pipeline_receipt(session, transaction, category_id, category_names, "llm")

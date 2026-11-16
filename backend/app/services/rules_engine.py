"""Rules engine (docs/04 layer 1): priority ASC, ties by id ASC, first match
wins unless a matching rule has stop_on_match=False (processing continues).

Matchers: exact_payee = normalized_payee equality; contains = case-insensitive
substring; regex = case-insensitive search via the `regex` module with a hard
per-call timeout (security review #4 ReDoS). Amount matchers compare against
transaction.amount_native. Disabled rules are skipped.

Tag application: when a rule matches, its tags are applied to the transaction
according to tag_action ("set" replaces, "add" unions).
"""

import logging
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from functools import lru_cache

import regex
from sqlmodel import Session, select

from app.models.rule_tags import RuleTag
from app.models.rules import Rule, RuleMatchType
from app.models.transaction_events import TransactionEventKind
from app.models.transaction_tags import TransactionTag
from app.models.transactions import Transaction
from app.services.match_terms import as_is, match_terms
from app.services.tx_events import load_category_names, load_tag_names, record_event

logger = logging.getLogger(__name__)

REGEX_TIMEOUT_SECONDS = 0.1


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> regex.Pattern[str]:
    return regex.compile(pattern, regex.IGNORECASE)


def _payee_matches(rule: Rule, normalized_payee: str) -> bool:
    """Check payee-based matchers (exact_payee, contains, regex).

    M121 D3 (docs/25 §M121): the two non-regex matchers run through the ONE
    shared `services/match_terms.py::match_terms`, the same call a bill's term
    list makes, so the rules engine and a bill can never drift on what
    "matches" means. A rule still STORES one `pattern` (docs/04), so it is
    passed as a one-element term list under `mode="any"` — which is the
    pre-M121 world exactly: one term answers identically under both modes.

    The arguments are chosen to reproduce the old comparisons byte for byte:
    `exact_payee` = identity normalizer + `exact` (case-SENSITIVE equality,
    as docs/04 pins it), `contains` = `str.lower` on both sides (the
    case-insensitive substring it always was).
    """
    if rule.match_type is RuleMatchType.exact_payee:
        return match_terms((rule.pattern,), "any", as_is, normalized_payee or "", "exact")
    if rule.match_type is RuleMatchType.contains:
        return match_terms((rule.pattern,), "any", str.lower, normalized_payee or "", "contains")
    # regex — deliberately NOT routed through `match_terms`: a regex is a
    # pattern language, not a term list, and `invalid_regex` is its own
    # documented failure shape (docs/04, docs/31).
    try:
        return (
            _compiled(rule.pattern).search(normalized_payee, timeout=REGEX_TIMEOUT_SECONDS)
            is not None
        )
    except TimeoutError:
        logger.warning("Rule regex %r timed out; treating as non-match", rule.pattern)
        return False


def _amount_matches(rule: Rule, amount: Decimal) -> bool:
    """Check amount-based matchers."""
    try:
        if rule.match_type is RuleMatchType.amount_equals:
            return amount == Decimal(rule.pattern)
        if rule.match_type is RuleMatchType.amount_gt:
            return amount > Decimal(rule.pattern)
        if rule.match_type is RuleMatchType.amount_lt:
            return amount < Decimal(rule.pattern)
        if rule.match_type is RuleMatchType.amount_between:
            parts = rule.pattern.split("|")
            lo = Decimal(parts[0].strip())
            hi = Decimal(parts[1].strip())
            return lo <= amount <= hi
    except InvalidOperation:
        logger.warning("Rule amount pattern %r is not a valid decimal", rule.pattern)
        return False
    return False


AMOUNT_MATCH_TYPES = {
    RuleMatchType.amount_equals,
    RuleMatchType.amount_gt,
    RuleMatchType.amount_lt,
    RuleMatchType.amount_between,
}


def rule_matches(rule: Rule, normalized_payee: str, amount: Decimal) -> bool:
    """Dispatch to payee or amount matcher based on rule.match_type."""
    if rule.match_type in AMOUNT_MATCH_TYPES:
        return _amount_matches(rule, amount)
    return _payee_matches(rule, normalized_payee)


def match_transactions(rule: Rule, transactions: Sequence[Transaction]) -> list[Transaction]:
    """Pure filter: the subset a rule's predicate matches (docs/31).

    Shared by preview (no writes) and targeted apply so the two can never
    disagree on what "matches" means.
    """
    return [
        t for t in transactions if rule_matches(rule, t.normalized_payee or "", t.amount_native)
    ]


def apply_single_rule(
    session: Session,
    rule: Rule,
    transactions: Sequence[Transaction],
    overwrite: bool = False,
    *,
    receipt_via: str | None = None,
    actor: int | None = None,
) -> dict[int, int]:
    """Apply ONE rule explicitly (docs/31 targeted apply).

    Unlike `apply_rules` there is no first-match-wins across rules: every
    transaction matching this rule's predicate is updated, subject to
    `overwrite` (False = only `category_id IS NULL` rows). Category and
    tags apply exactly like the pipeline. Returns {transaction_id:
    category_id} for updated rows; caller commits.

    M120 (docs/02 §transaction_events): pass `receipt_via` and every row this
    call actually updates gets its OWN `categorized` receipt (plus a `tagged`
    one when the rule's tags moved the junction) appended into the SAME
    session — the caller's single `db.commit()` therefore lands the change and
    its receipt together, and a row skipped under `overwrite=False` writes
    nothing at all. `receipt_via=None` (the default) keeps the engine
    receiptless for its own unit tests.
    """
    tag_ids = _load_rule_tag_ids(rule.id, session) if rule.id is not None else []
    names = load_category_names(session, [rule.category_id]) if receipt_via else {}
    updated: dict[int, int] = {}
    for transaction in match_transactions(rule, transactions):
        if not overwrite and transaction.category_id is not None:
            continue
        transaction.category_id = rule.category_id
        if transaction.id is not None:
            updated[transaction.id] = rule.category_id
        if receipt_via and transaction.id is not None:
            record_event(
                session,
                tx_id=transaction.id,
                user_id=transaction.user_id,
                kind=TransactionEventKind.categorized,
                payload={
                    "category_id": rule.category_id,
                    "category_name": names.get(rule.category_id),
                    "via": receipt_via,
                },
                actor=actor,
            )
        if tag_ids:
            added, removed = _apply_tags(session, transaction, tag_ids, rule.tag_action)
            if receipt_via and transaction.id is not None and (added or removed):
                _record_tagged(session, transaction, added, removed, actor)
    return updated


def _record_tagged(
    session: Session,
    transaction: Transaction,
    added: Sequence[int],
    removed: Sequence[int],
    actor: int | None,
) -> None:
    """One `tagged` receipt, in the EXACT shape `PUT /tags` writes (M120 §1).

    `added`/`removed` are NAMES because the drawer line reads words;
    `added_ids`/`removed_ids` ride along because the tag routes write them.
    Both sets are mirrored, never slimmed — a reader comparing a rule's rider
    with a hand-made PUT must not be able to tell them apart by shape.
    """
    if transaction.id is None:
        return
    payload = {
        "added": load_tag_names(session, added),
        "removed": load_tag_names(session, removed),
        "added_ids": sorted(added),
        "removed_ids": sorted(removed),
    }
    record_event(
        session,
        tx_id=transaction.id,
        user_id=transaction.user_id,
        kind=TransactionEventKind.tagged,
        payload=payload,
        actor=actor,
    )


def _load_rule_tag_ids(rule_id: int, session: Session) -> list[int]:
    """Return tag_ids attached to a rule."""
    rows = session.exec(select(RuleTag.tag_id).where(RuleTag.rule_id == rule_id)).all()
    return list(rows)


def _apply_tags(
    session: Session,
    transaction: Transaction,
    tag_ids: list[int],
    tag_action: str,
) -> tuple[list[int], list[int]]:
    """Apply tags to a transaction according to tag_action.

    Race-safe: inserts use ON CONFLICT DO NOTHING and "set" removals are a
    bulk delete, so concurrent appliers cannot 500 on the unique
    constraint (live incident: double-clicked rule Apply).

    Returns `(added_ids, removed_ids)` — the junction diff this call actually
    produced — so M120's caller can write a `tagged` receipt ONLY when the
    junction moved, the same rule `PUT /tags` follows. Rule tags are ADD/SET
    (there is no removal semantics in the engine), so `removed` is non-empty
    only under `tag_action="set"` replacing previously-carried tags.
    """
    from sqlalchemy import delete as sa_delete
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if not tag_ids:
        return [], []
    wanted = list(dict.fromkeys(tag_ids))
    have = set(
        session.exec(
            select(TransactionTag.tag_id).where(TransactionTag.transaction_id == transaction.id)
        ).all()
    )
    removed: list[int] = []
    if tag_action == "set":
        removed = sorted(have - set(wanted))
        if removed:
            session.execute(
                sa_delete(TransactionTag).where(
                    TransactionTag.transaction_id == transaction.id,
                    TransactionTag.tag_id.not_in(wanted),
                )
            )
    added = [tid for tid in wanted if tid not in have]
    if added:
        session.execute(
            pg_insert(TransactionTag)
            .values([{"transaction_id": transaction.id, "tag_id": tid} for tid in added])
            .on_conflict_do_nothing(index_elements=["transaction_id", "tag_id"])
        )
    return added, removed


def load_rules(session: Session, user_id: int) -> list[Rule]:
    """All of the user's rules ordered by evaluation precedence."""
    statement = (
        select(Rule).where(Rule.user_id == user_id).order_by(Rule.priority.asc(), Rule.id.asc())
    )
    return list(session.exec(statement).all())


def apply_rules(
    session: Session,
    user_id: int,
    transactions: Sequence[Transaction],
    *,
    receipt_via: str | None = None,
    actor: int | None = None,
) -> dict[int, int]:
    """Apply first matching enabled rule per transaction, mutating rows in place.

    A rule with stop_on_match=False lets processing continue to later rules:
    every matching rule applies in priority order (later categories overwrite,
    tags accumulate per each rule's action). Returns {transaction_id:
    category_id} for the matched subset. Tags are applied according to each
    rule's tag_action setting.

    M120 (docs/02 §transaction_events): with `receipt_via` set — layer 1 of
    the pipeline passes `"rule"`, `actor=None` — every row a rule touches
    writes its own `categorized` receipt plus a `tagged` rider when the
    junction moved. The write is UNCONDITIONAL on the previous `category_id`
    (the symmetric-write reading pinned in docs/M120_RECEIPTS_SPEC §6): a
    later continue-rule overwriting an earlier one is a mutation the log
    narrates, exactly like `PUT /tags` re-applying a set it already has.
    """
    matches: dict[int, int] = {}
    if not transactions:
        return matches
    stopped: set[int] = set()
    for rule in load_rules(session, user_id):
        if not rule.enabled:
            continue
        tag_ids = _load_rule_tag_ids(rule.id, session) if rule.tag_action else []
        names = load_category_names(session, [rule.category_id]) if receipt_via else {}
        for transaction in transactions:
            if transaction.id in stopped:
                continue
            if rule_matches(rule, transaction.normalized_payee, transaction.amount_native):
                transaction.category_id = rule.category_id
                matches[transaction.id] = rule.category_id
                if receipt_via and transaction.id is not None:
                    record_event(
                        session,
                        tx_id=transaction.id,
                        user_id=transaction.user_id,
                        kind=TransactionEventKind.categorized,
                        payload={
                            "category_id": rule.category_id,
                            "category_name": names.get(rule.category_id),
                            "via": receipt_via,
                        },
                        actor=actor,
                    )
                if tag_ids:
                    added, removed = _apply_tags(session, transaction, tag_ids, rule.tag_action)
                    if receipt_via and transaction.id is not None and (added or removed):
                        _record_tagged(session, transaction, added, removed, actor)
                if rule.stop_on_match:
                    stopped.add(transaction.id)
    return matches

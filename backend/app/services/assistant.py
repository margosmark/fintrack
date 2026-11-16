"""The plain-English assistant: context, prompt, validation, mutations (docs/57).

Three rules this module exists to enforce:

1. **The model proposes, the server decides.** Every value the model sends is
   re-validated against the LIVE row here (`_validate_change`), and anything
   unparseable raises `422 assistant_proposal_invalid` — no proposal row, no
   audit row, no domain write (docs/57 §5).
2. **Apply never clobbers.** `before` is the state captured at ask time;
   apply re-reads the row and refuses `409 assistant_proposal_stale` when it
   moved, so the card's *Now* column is a fact, not a hope (docs/57 §2).
3. **A mutation that cannot be logged never happens.** apply/undo stage the
   domain write, the `assistant_audit` row (and, for a ledger row, the same
   `transaction_events` receipt `PATCH /transactions/{id}` writes) before one
   `db.commit()`.

Egress: the ONE send is `provider.chat(...)` → `app/llm/base.py::
post_chat_completions` behind `assert_send_allowed` — no other network call
lives here, and `ask` releases the request session before the wait (docs/07
§Provider-wait ledger entry: `release`).
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import wraps
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.config import get_settings
from app.core.errors import APIError, api_error
from app.llm.base import build_provider
from app.models.accounts import Account
from app.models.assistant import AssistantAudit, AssistantProposal, AuditAction, ProposalStatus
from app.models.categories import Category
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.assistant import ProposalCreateRequest
from app.schemas.forecast import MAX_SCENARIOS_PER_USER, ScenarioOut, validate_events
from app.services.account_value import accounts_cash
from app.services.session_release import release_before_provider_wait
from app.services.tx_events import jsonable, record_edit_event
from app.services.txn_filters import committed_tx_conditions, today_et
from app.services.valuation import compute_positions

# --------------------------------------------------------------------- caps
# A prompt is not a data export: every section of the context is bounded
# (docs/57 §3). Counts above the cap simply stop at the cap, in id order.
ACCOUNT_CAP = 100
HOLDING_CAP = 100
TRANSACTION_CAP = 50
CATEGORY_CAP = 100
ANSWER_MAX = 300  # proposal_summary column width
NAME_MAX = 120  # accounts.name, as the accounts schema validates it
SCENARIO_NAME_MAX = 80  # ScenarioIn.name
NOTE_MAX = 500  # transactions.note
MONEY_LIMIT = Decimal("10000000000000000")  # Numeric(18, 2) holds < 10^16
CENT = Decimal("0.01")

# ------------------------------------------------------------ the registry
# Closed set (docs/57 §2): kind -> the `proposal_target` prefix and the ONLY
# field names accepted for it. A field outside these sets is a refusal.
KIND_TARGET_PREFIX: dict[str, str] = {
    "account.update": "account",
    "transaction.update": "transaction",
    "forecast_scenario.update": "forecast_scenario",
}
ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "account.update": frozenset({"name", "starting_balance", "llm_autopilot"}),
    "transaction.update": frozenset({"category_id", "note"}),
    "forecast_scenario.update": frozenset({"name", "events"}),
}

ASSISTANT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "proposal_kind": {
            "type": "string",
            "enum": ["none", *KIND_TARGET_PREFIX],
        },
        "proposal_target": {"type": "string"},
        "proposal_summary": {"type": "string"},
        "proposal_fields": {"type": "object"},
    },
    "required": [
        "answer",
        "proposal_kind",
        "proposal_target",
        "proposal_summary",
        "proposal_fields",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are the fintrack in-app assistant. Answer the user's question \
about THEIR OWN financial data, using ONLY the context JSON provided — never \
invent ids, balances, payees or dates that are not in it. Quote money with its \
currency and keep the answer short and concrete.

If — and ONLY if — the user asks you to CHANGE something, fill the proposal \
fields too; otherwise set proposal_kind to "none" and leave the other proposal \
fields as empty strings.

Supported changes (nothing else exists):
- "account.update" on target "account:<id>" — fields: name (text), \
starting_balance (decimal string or null), llm_autopilot (true/false).
- "transaction.update" on target "transaction:<id>" — fields: category_id \
(integer from the context's categories, or null to clear), note (text or null).
- "forecast_scenario.update" on target "forecast_scenario:<id>" — fields: name \
(text), events (the FULL replacement event list, each an object with a "type").

proposal_target's prefix must match proposal_kind. proposal_summary is one \
sentence describing the change. If you cannot map the request onto one of \
these three kinds, answer it in prose and set proposal_kind to "none"."""


@dataclass(frozen=True)
class ValidatedChange:
    """Model output the server fully re-validated against the live row."""

    kind: str
    target: str
    target_label: str
    summary: str
    before: dict[str, Any]
    after: dict[str, Any]


def _refuse(detail: str) -> APIError:
    """The one refusal constructor: honest failure, nothing written (§5)."""
    return api_error(422, detail, "assistant_proposal_invalid")


# ----------------------------------------------------------- value plumbing


def _money(value: Any, field: str) -> str | None:
    """Validate a model-supplied money value → canonical 2dp string."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _refuse(f"{field}: expected a decimal string or null")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise _refuse(f"{field}: {value!r} is not a decimal number") from None
    if not amount.is_finite():
        raise _refuse(f"{field}: {value!r} is not a finite number")
    amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if abs(amount) >= MONEY_LIMIT:
        raise _refuse(f"{field}: {value!r} is out of range")
    return str(amount)


def _text(value: Any, field: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise _refuse(f"{field}: expected text")
    stripped = value.strip()
    if not stripped:
        raise _refuse(f"{field}: must not be blank")
    if len(stripped) > limit:
        raise _refuse(f"{field}: must be at most {limit} characters")
    return stripped


def _optional_text(value: Any, field: str, *, limit: int) -> str | None:
    """`null`/empty clears the field; anything else is a bounded string."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _refuse(f"{field}: expected text or null")
    if value == "":
        return None
    if len(value) > limit:
        raise _refuse(f"{field}: must be at most {limit} characters")
    return value


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise _refuse(f"{field}: expected true or false")
    return value


def _int_or_none(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _refuse(f"{field}: expected an id or null")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise _refuse(f"{field}: expected an id or null")


# ------------------------------------------------------------- the context


def build_context(db: Session, user: User) -> dict[str, Any]:
    """Everything the model may know about this tenant — cache-only reads.

    Every section is bounded (docs/57 §3) and every `Decimal` is serialized
    through `jsonable` (string, never a float).
    """
    today = today_et()
    accounts = db.exec(
        select(Account).where(Account.user_id == user.id).order_by(Account.id).limit(ACCOUNT_CAP)
    ).all()
    cash = accounts_cash(db, user.id, list(accounts), today)

    positions = compute_positions(db, user.id)
    holdings = [
        {
            "account_id": row["account_id"],
            "account_name": row["account_name"],
            "symbol": row["symbol"],
            "name": row.get("security_name") or row["symbol"],
            "quantity": jsonable(row["quantity"]),
            "price": jsonable(row.get("price")),
            "market_value_cad": jsonable(row.get("market_value_cad")),
        }
        for row in positions[:HOLDING_CAP]
    ]

    categories = db.exec(
        select(Category)
        .where(Category.user_id == user.id)
        .order_by(Category.name, Category.id)
        .limit(CATEGORY_CAP)
    ).all()
    category_names = {c.id: c.name for c in categories}
    # docs/57 §3 / `committed_tx_conditions` docstring: the WHERE half only
    # works with the ImportBatch join — without it the `status == committed`
    # disjunct runs as a cartesian product, leaking staged rows into the
    # model's context, duplicating every row per batch row, and returning
    # NOTHING for a manual-only tenant while the batch table is empty (QA F1).
    transactions = db.exec(
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(*committed_tx_conditions(user.id))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(TRANSACTION_CAP)
    ).all()

    scenarios = [
        ScenarioOut.model_validate(entry)
        for entry in (user.forecast_scenarios or [])[:MAX_SCENARIOS_PER_USER]
    ]

    return {
        "today": today.isoformat(),
        "reporting_currency": get_settings().reporting_currency,
        "accounts": [
            {
                "id": account.id,
                "name": account.name,
                "type": account.type.value,
                "currency": account.currency,
                "starting_balance": jsonable(account.starting_balance),
                "llm_autopilot": account.llm_autopilot,
                "cash_balance_cad": jsonable(cash.get(account.id)),
            }
            for account in accounts
        ],
        "holdings": holdings,
        "recent_transactions": [
            {
                "id": tx.id,
                "date": tx.date.isoformat(),
                "payee": tx.normalized_payee,
                "amount_native": jsonable(tx.amount_native),
                "currency": tx.currency,
                "category_id": tx.category_id,
                "category": category_names.get(tx.category_id),
                "note": tx.note,
            }
            for tx in transactions
        ],
        "categories": [{"id": c.id, "name": c.name} for c in categories],
        "plan": {
            "scenarios": [
                {"id": s.id, "name": s.name, "events": jsonable(s.events)} for s in scenarios
            ],
            "max_scenarios": MAX_SCENARIOS_PER_USER,
        },
    }


# ------------------------------------------------------ target + validation


def _parse_target(kind: str, target: str) -> str:
    prefix, sep, ident = target.partition(":")
    if not sep or not ident:
        raise _refuse(f"proposal_target must look like '{KIND_TARGET_PREFIX[kind]}:<id>'")
    if prefix != KIND_TARGET_PREFIX[kind]:
        raise _refuse(f"proposal_target prefix {prefix!r} does not match proposal_kind {kind!r}")
    return ident


def _guard_transaction(db: Session, user: User, tx: Transaction) -> None:
    """The `PATCH /transactions/{id}` guards, in their own envelope codes.

    Asked (ask time) these are folded into `422 assistant_proposal_invalid`;
    applied (apply/undo) they keep their documented codes (docs/57 §4).
    """
    if tx.is_split:
        raise api_error(
            409, "This transaction is split. Merge its parts first.", "transaction_is_split"
        )
    if tx.import_batch_id is not None:
        batch = db.get(ImportBatch, tx.import_batch_id)
        if batch is None or batch.user_id != user.id or batch.status != ImportBatchStatus.committed:
            if batch is not None and batch.status == ImportBatchStatus.pending_review:
                raise api_error(
                    409,
                    "Transaction belongs to a pending import batch",
                    "transaction_in_pending_batch",
                )
            raise api_error(404, "Transaction not found", "not_found")
    if user.encryption_required and not (tx.payee_cipher and tx.amount_cipher):
        raise api_error(
            422,
            "encryption_required: payee_cipher and amount_cipher are required",
            "encryption_required",
        )


def _resolve_target(
    db: Session,
    user: User,
    kind: str,
    target: str,
    *,
    code: str,
    status: int,
    label_prefix: str,
    for_update: bool = False,
) -> Any:
    """Load the target row behind `target`, tenant-checked.

    Ask time calls it with `code="assistant_proposal_invalid"` (the model
    hallucinated an id → refusal); apply/undo call it with `code="not_found"`.
    The `label_prefix` is what the detail message names ("account", ...).

    `for_update=True` (apply/undo, through `_live_fields`) locks the target
    row for the rest of the transaction, so a hand edit cannot land between
    the stale check and the write: either it committed first and the check
    refuses `409`, or it waits and applies on top of this transaction's
    values — never the other way round (docs/57 §4).
    """
    ident = _parse_target(kind, target)
    if kind == "account.update":
        if not ident.isdigit():
            raise _refuse(f"{label_prefix} id {ident!r} is not an id")
        statement = select(Account).where(Account.id == int(ident), Account.user_id == user.id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        row = db.exec(statement).one_or_none()
    elif kind == "transaction.update":
        if not ident.isdigit():
            raise _refuse(f"{label_prefix} id {ident!r} is not an id")
        statement = select(Transaction).where(
            Transaction.id == int(ident), Transaction.user_id == user.id
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        row = db.exec(statement).one_or_none()
    else:
        if for_update:
            # The scenario lives in the user's JSON column: the row that
            # carries it is the one to lock.
            db.exec(
                select(User)
                .where(User.id == user.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        row = next(
            (entry for entry in (user.forecast_scenarios or []) if entry.get("id") == ident),
            None,
        )
    if row is None:
        raise api_error(
            status,
            f"The {label_prefix} this proposal changes was not found",
            code,
        )
    if kind == "transaction.update":
        try:
            _guard_transaction(db, user, row)
        except APIError as exc:
            if code == "assistant_proposal_invalid":
                # Ask time: any domain guard violation is a refusal, not a 4xx
                # against the user — nothing was written (docs/57 §5).
                raise _refuse(f"{exc.detail} ({exc.code})") from None
            raise
    return row


def _target_label(kind: str, row: Any) -> str:
    if kind == "account.update":
        return str(row.name)[:200]
    if kind == "transaction.update":
        raw = (
            f"{row.normalized_payee} · {row.date.isoformat()} · {row.amount_native} {row.currency}"
        )
        return raw[:200]
    return str(row.get("name", ""))[:200]


def _canonical(db: Session, user: User, kind: str, row: Any, fields: set[str]) -> dict[str, Any]:
    """The LIVE values of `fields`, in exactly the shape `before`/`after` use."""
    live: dict[str, Any] = {}
    if kind == "account.update":
        for field in fields:
            if field == "name":
                live[field] = str(row.name)
            elif field == "starting_balance":
                live[field] = None if row.starting_balance is None else str(row.starting_balance)
            else:
                live[field] = bool(row.llm_autopilot)
        return live
    if kind == "transaction.update":
        for field in fields:
            if field == "category_id":
                live[field] = row.category_id
            else:
                live[field] = row.note
        return live
    try:
        stored = ScenarioOut.model_validate(row).model_dump(mode="json")
    except ValidationError:
        # A stored scenario that no longer validates: only reachable if some
        # other writer broke the docs/45 contract. Treat as moved/invalid.
        raise api_error(
            409, "The stored plan scenario can no longer be validated", "assistant_proposal_stale"
        ) from None
    for field in fields:
        if field == "name":
            live[field] = stored["name"]
        else:
            live[field] = stored["events"]
    return live


def _validate_field(db: Session, user: User, kind: str, row: Any, field: str, raw: Any) -> Any:
    """Model-supplied value → canonical value, or a refusal."""
    if kind == "account.update":
        if field == "name":
            # The assistant has no vault key: renaming an account whose name
            # lives in ciphertext (or under `encryption_required`) would strand
            # the cipher — `PATCH /accounts` answers the same guard (docs/57 §2).
            if user.encryption_required or getattr(row, "name_cipher", None):
                raise _refuse(
                    "encryption_required: the assistant cannot rename an encrypted account"
                )
            return _text(raw, "name", limit=NAME_MAX)
        if field == "starting_balance":
            return _money(raw, "starting_balance")
        if field == "llm_autopilot":
            return _bool(raw, "llm_autopilot")
    elif kind == "transaction.update":
        if field == "category_id":
            category_id = _int_or_none(raw, "category_id")
            if category_id is None:
                return None
            category = db.exec(
                select(Category).where(Category.id == category_id, Category.user_id == user.id)
            ).one_or_none()
            if category is None:
                raise _refuse(f"category_id {category_id} is not one of your categories")
            return category.id
        if field == "note":
            return _optional_text(raw, "note", limit=NOTE_MAX)
    else:
        if field == "name":
            return _text(raw, "name", limit=SCENARIO_NAME_MAX)
        if field == "events":
            if not isinstance(raw, list):
                raise _refuse("events: expected a list of forecast events")
            # The model mints no ids: an event without one gets a fresh key
            # (a client-side convenience, not user data), everything else is
            # validated by the docs/45 union — its envelope codes become the
            # refusal's detail (docs/57 §5).
            prepared = [
                {**event, "id": secrets.token_hex(12)}
                if isinstance(event, dict) and not event.get("id")
                else event
                for event in raw
            ]
            try:
                validated = validate_events(prepared)
            except APIError as exc:
                raise _refuse(f"events: {exc.detail} ({exc.code})") from None
            return [event.model_dump(mode="json") for event in validated]
    raise _refuse(f"unknown proposal field {field!r} for {kind}")


def _validate_change(db: Session, user: User, raw: Any) -> ValidatedChange | None:
    """Model reply → `ValidatedChange`, or `None` for an answer-only reply.

    Raises `422 assistant_proposal_invalid` for everything unparseable; never
    writes (docs/57 §5).
    """
    if not isinstance(raw, dict):
        raise _refuse("the model returned an unexpected payload")
    kind = raw.get("proposal_kind")
    if kind == "none":
        return None
    if kind not in ALLOWED_FIELDS:
        raise _refuse(f"proposal_kind {kind!r} is not a supported change")
    target = raw.get("proposal_target")
    if not isinstance(target, str) or not target:
        raise _refuse("proposal_target is missing")
    summary = raw.get("proposal_summary")
    if not isinstance(summary, str) or not summary.strip():
        raise _refuse("proposal_summary is missing")
    summary = summary.strip()[:ANSWER_MAX]
    fields = raw.get("proposal_fields")
    if not isinstance(fields, dict) or not fields:
        raise _refuse("proposal_fields must name at least one field to change")
    unknown = sorted(set(fields) - ALLOWED_FIELDS[kind])
    if unknown:
        raise _refuse(f"unsupported field(s) for {kind}: {', '.join(unknown)}")

    row = _resolve_target(
        db, user, kind, target, code="assistant_proposal_invalid", status=422, label_prefix=kind
    )
    try:
        before = _canonical(db, user, kind, row, set(fields))
    except APIError as exc:
        # Ask time: a stored-state problem is a refusal, not a 4xx at the user
        # (nothing was written) — apply keeps the `stale` code.
        raise _refuse(f"{exc.detail} ({exc.code})") from None
    after = {
        field: _validate_field(db, user, kind, row, field, raw_value)
        for field, raw_value in fields.items()
    }
    if after == before:
        raise _refuse("the proposed change matches the current values — there is nothing to do")
    return ValidatedChange(
        kind=kind,
        target=target,
        target_label=_target_label(kind, row),
        summary=summary,
        before=before,
        after=after,
    )


# --------------------------------------------------------------------- ask


def ask(db: Session, user: User, question: str) -> tuple[str, AssistantProposal | None]:
    """One turn: answer the question, persist a proposal when there is one.

    The request session is released before the (up to 60 s) `chat()` wait —
    docs/07 §Provider-wait ledger classifies this function as `release`.
    """
    context = build_context(db, user)
    resolved = build_provider(user, get_settings())
    if resolved.provider is None:
        raise api_error(503, "No assistant model is configured", "assistant_unavailable")
    user_id = user.id
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Context (JSON):\n{json.dumps(context, sort_keys=True, default=str)}\n\n"
                f"Question: {question}"
            ),
        },
    ]
    release_before_provider_wait(db)
    raw = resolved.provider.chat(messages, ASSISTANT_JSON_SCHEMA)
    if not isinstance(raw, dict) or not isinstance(raw.get("answer"), str) or not raw["answer"]:
        raise api_error(502, "LLM upstream request failed", "llm_upstream_error")
    answer = raw["answer"]
    change = _validate_change(db, user, raw)
    if change is None:
        return answer, None
    proposal = AssistantProposal(
        user_id=user_id,
        question=question,
        answer=answer,
        kind=change.kind,
        target=change.target,
        target_label=change.target_label,
        summary=change.summary,
        before=change.before,
        after=change.after,
        status=ProposalStatus.pending,
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return answer, proposal


# ------------------------------------------------------------------ propose


def propose(db: Session, user: User, payload: ProposalCreateRequest) -> AssistantProposal:
    """A connector's change → pending card, with **no LLM call** (docs/59 §3.1).

    The external AI already did the reasoning, so this is ask-time validation
    minus the model: `_validate_change` re-reads the LIVE row, then the
    proposal row is persisted. Nothing about the target row is written — the
    only row this call creates is the proposal itself, and mutation happens
    only when the user applies the card from their own web session
    (docs/57 §4), which no MCP tool can do (docs/59 §3.3).
    """
    raw: dict[str, Any] = {
        "proposal_kind": payload.proposal_kind,
        "proposal_target": payload.proposal_target,
        "proposal_summary": payload.proposal_summary,
        "proposal_fields": payload.proposal_fields,
    }
    change = _validate_change(db, user, raw)
    if change is None:
        # `_validate_change` answers None only for kind "none" — a connector
        # only calls this tool when it has a concrete change (docs/59 §3.1).
        raise _refuse(f"proposal_kind {payload.proposal_kind!r} is not a supported change")
    proposal = AssistantProposal(
        user_id=user.id,
        question=payload.question,
        answer=payload.answer,
        kind=change.kind,
        target=change.target,
        target_label=change.target_label,
        summary=change.summary,
        before=change.before,
        after=change.after,
        status=ProposalStatus.pending,
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


# ------------------------------------------------------ apply / undo / state


def load_proposal(
    db: Session, user: User, proposal_id: int, *, for_update: bool = False
) -> AssistantProposal:
    """Load a proposal, tenant-checked.

    `for_update=True` locks the row (`SELECT ... FOR UPDATE`) for the rest of
    the transaction. Every transition (apply/dismiss/undo) takes it, so the
    status check that follows is no longer check-then-act: two racing
    decisions on one card serialize on the lock, the loser re-reads the
    winner's committed status and answers `409` — one transition, one
    `assistant_audit` row (docs/57 §4).
    """
    statement = select(AssistantProposal).where(AssistantProposal.id == proposal_id)
    if for_update:
        # populate_existing: the loser must re-read what the winner COMMITTED,
        # not whatever this session's identity map already holds.
        statement = statement.with_for_update().execution_options(populate_existing=True)
    proposal = db.exec(statement).one_or_none()
    if proposal is None or proposal.user_id != user.id:
        raise api_error(404, "Proposal not found", "assistant_proposal_not_found")
    return proposal


def _live_fields(
    db: Session,
    user: User,
    proposal: AssistantProposal,
    *,
    drift_code: str,
    expected: dict[str, Any],
) -> dict[str, Any]:
    """Live canonical values for the proposal's fields, verified == `expected`.

    `expected` is the state the caller is ENTITLED to find: `before` at apply
    (nothing may have moved since the ask) and `after` at undo (the applied
    state must still stand — a hand edit in between is the conflict, not a
    free clobber). A vanished target answers `404 not_found`; a moved one
    answers `drift_code` (`assistant_proposal_stale` at apply,
    `assistant_undo_conflict` at undo).
    """
    row = _resolve_target(
        db,
        user,
        proposal.kind,
        proposal.target,
        code="not_found",
        status=404,
        label_prefix="target",
        # Locked here, before the comparison, and held through the write in
        # `_write_target` (same transaction) — the verdict below is read under
        # the lock, so it cannot go stale between check and commit.
        for_update=True,
    )
    fields = set(proposal.before)
    live = _canonical(db, user, proposal.kind, row, fields)
    if live != expected:
        raise api_error(
            409,
            "This proposal no longer matches the current data — ask again",
            drift_code,
        )
    return live


def _write_transaction_receipt(
    db: Session,
    user: User,
    tx: Transaction,
    from_values: dict[str, Any],
    to_values: dict[str, Any],
) -> None:
    """The exact receipt `PATCH /transactions/{id}` writes (docs/57 §4).

    The History drawer therefore reads an applied card exactly like a click:
    one `categorized` receipt when the category moved (carrying `via:
    "assistant"`, payload-only), one `edited` receipt for anything else.
    """
    changed_keys = [key for key in to_values if from_values.get(key) != to_values.get(key)]
    if not changed_keys:
        return
    receipt: dict[str, Any] = {
        "fields": changed_keys,
        "changes": {k: to_values[k] for k in changed_keys},
    }
    if "category_id" in changed_keys:
        category = (
            db.get(Category, to_values["category_id"])
            if to_values["category_id"] is not None
            else None
        )
        receipt["category_id"] = to_values["category_id"]
        receipt["category_name"] = category.name if category is not None else None
        receipt["via"] = "assistant"
    record_edit_event(
        db,
        transaction_id=tx.id,
        user_id=user.id,
        actor=user.id,
        changed=receipt,
    )


def _write_target(
    db: Session,
    user: User,
    proposal: AssistantProposal,
    values: dict[str, Any],
    *,
    from_values: dict[str, Any],
) -> None:
    """Stage the domain write for `values` (apply → `after`, undo → `before`).

    `from_values` is the state this write moves AWAY from — `before` at
    apply, `after` at undo — so the transaction receipt names the right
    delta in both directions (docs/57 §4: one receipt per mutation).
    """
    row = _resolve_target(
        db,
        user,
        proposal.kind,
        proposal.target,
        code="not_found",
        status=404,
        label_prefix="target",
    )
    if proposal.kind == "account.update":
        if "name" in values and (user.encryption_required or getattr(row, "name_cipher", None)):
            # Ask-time refusal is the common case; re-check here so a tenant
            # that turned on encryption between ask and apply still cannot
            # strand the ciphertext (docs/57 §4 `422 encryption_required`).
            raise api_error(
                422,
                "encryption_required: the assistant cannot rename an encrypted account",
                "encryption_required",
            )
        for field, value in values.items():
            if field == "starting_balance":
                row.starting_balance = None if value is None else Decimal(str(value))
            else:
                setattr(row, field, value)
        db.add(row)
        return
    if proposal.kind == "transaction.update":
        for field, value in values.items():
            setattr(row, field, value)
        db.add(row)
        _write_transaction_receipt(db, user, row, from_values, values)
        return
    entries: list[dict[str, Any]] = []
    for entry in user.forecast_scenarios or []:
        if entry.get("id") == proposal.target.partition(":")[2]:
            replaced = dict(entry)
            replaced.update(values)
            entries.append(ScenarioOut.model_validate(replaced).model_dump(mode="json"))
        else:
            entries.append(entry)
    user.forecast_scenarios = entries
    db.add(user)


# The proposal-row lock wait is bounded PER TRANSACTION (security F1,
# t_108c24f3): production sets no database-wide `lock_timeout`, so an
# unbounded `SELECT ... FOR UPDATE` here could sit behind a stuck holder
# forever — and before this fix it did so ON the event loop, freezing the
# whole single-worker process (health included) with it. The bound turns the
# wait into the documented `503 assistant_busy`, and `SET LOCAL` scopes it to
# this transaction so a pooled connection can never inherit it.
LOCK_WAIT_LIMIT = "5s"


def _bound_lock_wait(db: Session) -> None:
    """`SET LOCAL lock_timeout = LOCK_WAIT_LIMIT` — the row-lock wait dies here.

    Called at the start of a transition's transaction, BEFORE the `FOR
    UPDATE`, so the bound covers that wait and every later lock the same
    transaction takes. `SET LOCAL`, never a session `SET`: the value lives
    exactly as long as this transaction.
    """
    db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_WAIT_LIMIT}'"))


def _bounded_lock_wait(
    fn: Callable[[Session, User, int], AssistantProposal],
) -> Callable[[Session, User, int], AssistantProposal]:
    """Bound the lock wait and map its expiry (or any database failure) to ONE
    envelope: `503 assistant_busy`, never a raw driver error.

    A `LockNotAvailable` escaping unmapped answers 500 with no retry hint —
    the base candidate did exactly that under concurrent transitions (the
    security probe's `LockNotAvailable: canceling statement due to lock
    timeout` surfaced as an unhandled exception). `503` says what happened
    (another decision on this card is in flight) and the client retries.
    """

    @wraps(fn)
    def wrapper(db: Session, user: User, proposal_id: int) -> AssistantProposal:
        _bound_lock_wait(db)
        try:
            return fn(db, user, proposal_id)
        except OperationalError as exc:
            raise api_error(
                503,
                "Another decision on this card is still in flight — try again in a moment",
                "assistant_busy",
            ) from exc

    return wrapper


@_bounded_lock_wait
def apply_proposal(db: Session, user: User, proposal_id: int) -> AssistantProposal:
    """`pending → applied`: verify, write, log — one transaction (docs/57 §4).

    The proposal row is locked first (`load_proposal(for_update=True)`), so a
    concurrent apply/dismiss/undo cannot pass the same status check: the
    second request blocks, then reads the winner's status and answers `409`.
    """
    proposal = load_proposal(db, user, proposal_id, for_update=True)
    if proposal.status != ProposalStatus.pending:
        raise api_error(
            409, "This proposal is no longer awaiting a decision", "assistant_proposal_not_pending"
        )
    _live_fields(
        db, user, proposal, drift_code="assistant_proposal_stale", expected=proposal.before
    )
    _write_target(db, user, proposal, proposal.after, from_values=proposal.before)
    db.add(
        AssistantAudit(
            user_id=user.id,
            proposal_id=proposal.id,
            action=AuditAction.apply,
            kind=proposal.kind,
            target=proposal.target,
            before=proposal.before,
            after=proposal.after,
        )
    )
    proposal.status = ProposalStatus.applied
    proposal.applied_at = datetime.now(UTC)
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


@_bounded_lock_wait
def dismiss_proposal(db: Session, user: User, proposal_id: int) -> AssistantProposal:
    """`pending → dismissed`: no domain write, so no audit row (docs/57 §4).

    Takes the same proposal-row lock as apply, so a racing apply and dismiss
    cannot both read `pending` — one wins, the other answers `409`.
    """
    proposal = load_proposal(db, user, proposal_id, for_update=True)
    if proposal.status != ProposalStatus.pending:
        raise api_error(
            409, "This proposal is no longer awaiting a decision", "assistant_proposal_not_pending"
        )
    proposal.status = ProposalStatus.dismissed
    proposal.dismissed_at = datetime.now(UTC)
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


@_bounded_lock_wait
def undo_proposal(db: Session, user: User, proposal_id: int) -> AssistantProposal:
    """`applied → undone`: restore the applied state exactly (round trip).

    Same lock discipline as apply: the proposal row is locked before the
    status check, so two racing undos (or an undo racing another transition)
    cannot both pass it.
    """
    proposal = load_proposal(db, user, proposal_id, for_update=True)
    if proposal.status != ProposalStatus.applied:
        raise api_error(
            409,
            "Only an applied change can be undone",
            "assistant_proposal_not_undoable",
        )
    _live_fields(db, user, proposal, drift_code="assistant_undo_conflict", expected=proposal.after)
    _write_target(db, user, proposal, proposal.before, from_values=proposal.after)
    db.add(
        AssistantAudit(
            user_id=user.id,
            proposal_id=proposal.id,
            action=AuditAction.undo,
            kind=proposal.kind,
            target=proposal.target,
            before=proposal.after,
            after=proposal.before,
        )
    )
    proposal.status = ProposalStatus.undone
    proposal.undone_at = datetime.now(UTC)
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


# ------------------------------------------------------------------ history


def history_item(db: Session, proposal: AssistantProposal) -> dict[str, Any]:
    """The proposal + its audit ids — the response of apply/undo/history."""
    audits = db.exec(
        select(AssistantAudit)
        .where(AssistantAudit.proposal_id == proposal.id)
        .order_by(AssistantAudit.id)
    ).all()
    apply_id = next((a.id for a in audits if a.action == AuditAction.apply), None)
    undo_id = next((a.id for a in audits if a.action == AuditAction.undo), None)
    return {
        "id": proposal.id,
        "question": proposal.question,
        "answer": proposal.answer,
        "kind": proposal.kind,
        "target": proposal.target,
        "target_label": proposal.target_label,
        "summary": proposal.summary,
        "before": proposal.before,
        "after": proposal.after,
        "status": proposal.status.value,
        "created_at": proposal.created_at,
        "applied_at": proposal.applied_at,
        "undone_at": proposal.undone_at,
        "apply_audit_id": apply_id,
        "undo_audit_id": undo_id,
    }


def list_history(db: Session, user: User, limit: int) -> list[dict[str, Any]]:
    """Applied + undone proposals, newest first (the History list)."""
    rows = db.exec(
        select(AssistantProposal)
        .where(
            AssistantProposal.user_id == user.id,
            AssistantProposal.status.in_([ProposalStatus.applied, ProposalStatus.undone]),
        )
        .order_by(AssistantProposal.created_at.desc(), AssistantProposal.id.desc())
        .limit(limit)
    ).all()
    return [history_item(db, row) for row in rows]

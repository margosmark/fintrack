"""transaction_events — the append-only, per-transaction receipt log (M106, docs/02).

"Edits never silently vanish": every mutation a person makes to a ledger row
leaves one row here, so the History drawer answers "what happened to this
transaction?" with a query instead of a vibe. Same shape family as `mcp_audit`
(M80) / `sharing_audit` (M90) — three logs, three scopes, never mixed:
this one is per-transaction and USER-FACING, the other two stay theirs.

Deliberate column decisions:

* `tx_id` has **no** foreign key — the `mcp_audit.mcp_token_id` /
  `sharing_audit.invite_id` rule (docs/40 §3, docs/43 §3): deleting a
  transaction must NOT delete the receipts it produced, or the `deleted`
  receipt — the whole point of the log — would be the one row that vanishes.
  A dangling `tx_id` is expected and renders as "this transaction is gone".
* `user_id` IS a foreign key and joins `_USER_OWNED_MODELS`, so a deleted
  account and the demo reset both leave no rows behind (AGENTS §2).
* `actor` is `NULL` for a cloud-service / system action (a bank-sync pull
  stages rows with no person at the keyboard) and otherwise the acting user's
  id. NO FK: deleting that account must never delete a receipt — the same
  reason `sharing_audit.actor_user_id` has none. The partner projection
  (docs/43 §6) filters on it: a partner sees owner/partner actions only.
* `payload` is the human detail the drawer renders ("you set category
  Groceries"), `payload_hash` is its unsalted sha256 over the canonical JSON
  (byte-identical formula to `mcp_audit.args_hash`) — hash and detail are
  written together, so the receipt can be re-verified rather than trusted.
  M120 (docs/02 §transaction_events) adds ONE optional provenance key,
  `via` (`rule | learned | llm | rule_backfill | suggestion_accept`), on
  `categorized` receipts written by the pipeline's three layers and by the
  suggestion-accept / rule-backfill routes (recurring-accept is a plain
  `edited` and carries none). It is payload-only: **zero** new `event_kind`
  values, no column, no migration — `render_summary` reads it at render time
  to word the line ("you set category Groceries (rule)"), and a plain PATCH
  payload carries no `via`, so its sentence is unchanged.

`created_at` (TimestampMixin — mandatory on every table, `tests/test_models.py`)
IS the event's `at`; an append-only log never updates, so `updated_at` only
rides along to satisfy the house invariant.
"""

import enum

from sqlalchemy import JSON, Column, Enum, Index, desc
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class TransactionEventKind(enum.StrEnum):
    """Closed set of ledger mutations a receipt can describe (docs/02).

    `split` was reserved for M107 before it existed, precisely so its arrival
    would be a no-op migration; `merge` (M107, docs/44 §11) widens the set the
    same way — `native_enum=False`, so widening is a constraint rebuild, never
    a table rewrite (AGENTS §2). Both are written by the four split routes and
    by nothing else.
    """

    created = "created"
    edited = "edited"
    categorized = "categorized"
    tagged = "tagged"
    reviewed = "reviewed"
    split = "split"
    merge = "merge"
    deleted = "deleted"


class TransactionEvent(TimestampMixin, table=True):
    __tablename__ = "transaction_events"
    __table_args__ = (
        # The drawer's query: one transaction's receipts, newest first.
        Index("ix_transaction_events_tx_created", "tx_id", desc("created_at")),
    )

    id: int | None = Field(default=None, primary_key=True)
    # The row this receipt is about. NO FK — see the module docstring.
    tx_id: int = Field(index=True)
    # The OWNER (the data subject), so export + account deletion cover it.
    user_id: int = Field(foreign_key="users.id", index=True)
    event_kind: TransactionEventKind = Field(
        sa_column=Column(
            Enum(TransactionEventKind, native_enum=False, length=16),
            nullable=False,
        )
    )
    # Human-facing detail, hashed together below (docs/02).
    payload: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    # sha256 hex of `payload` in canonical form — the `mcp_audit.args_hash`
    # formula (sorted keys, tight separators, default=str).
    payload_hash: str = Field(max_length=64)
    # Acting user, or NULL for a cloud-service/system action (no FK).
    actor: int | None = None

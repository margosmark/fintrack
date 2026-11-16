"""assistant_proposals / assistant_audit — the Kilter-style assistant (docs/57).

The model proposes, the server validates, the person applies: a `POST
/assistant/ask` that implies a data change leaves ONE `pending` row here, and
nothing else in the database moves until `POST /assistant/proposals/{id}/apply`
succeeds — which writes one `assistant_audit` row (before/after of every field
it touched) in the same transaction as the mutation itself. Undo does the same
in reverse, so the audit trail is append-only and complete: a mutation that
could not be logged never happens (docs/57 §4).

`before` / `after` are JSON maps of field → value with Decimals serialized as
strings (AGENTS §2) — snapshots for comparison and for the card's *Now vs
With change* rendering, never re-parsed into ORM values by a client.

Both tables join `_USER_OWNED_MODELS` (the audit BEFORE the proposal, so the
account cascade order holds) and are RLS-secured by their creating migration
(`secure_registered_table`), like every other tenant table (docs/55).
"""

import enum
from datetime import datetime

from sqlalchemy import JSON, Column, Enum, Index, Text, desc
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class ProposalStatus(enum.StrEnum):
    """Card lifecycle. `pending` is the only state a proposal starts in."""

    pending = "pending"
    applied = "applied"
    dismissed = "dismissed"
    undone = "undone"


class AuditAction(enum.StrEnum):
    """One row per MUTATION — `apply` and `undo`.

    `dismiss` is deliberately absent: dismissing moves no domain field, so it
    writes no audit row (docs/57 §4).
    """

    apply = "apply"
    undo = "undo"


class AssistantProposal(TimestampMixin, table=True):
    __tablename__ = "assistant_proposals"
    __table_args__ = (Index("ix_assistant_proposals_user_created", "user_id", desc("created_at")),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    # The ask and its answer, verbatim — History shows both (docs/57 §3).
    question: str = Field(max_length=2000)
    answer: str = Field(sa_column=Column(Text, nullable=False))
    # Registry kind (docs/57 §2): account.update | transaction.update |
    # forecast_scenario.update. Plain varchar: the closed set lives in
    # `services/assistant.py::KINDS`, not in a DB constraint.
    kind: str = Field(max_length=32)
    # "<prefix>:<id>" — e.g. `account:12`, `transaction:8841`,
    # `forecast_scenario:ab12cd34ef`. The prefix must match `kind` (refusal).
    target: str = Field(max_length=96)
    # Human label for the card: account name / payee line / scenario name.
    target_label: str = Field(max_length=200)
    # The model's one-line description of the change, rendered above the delta.
    summary: str = Field(max_length=300)
    # field -> value (Decimals as strings). `before` is verified live at apply
    # time — a mismatch is `409 assistant_proposal_stale` (docs/57 §2).
    before: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    after: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    status: ProposalStatus = Field(
        default=ProposalStatus.pending,
        sa_column=Column(Enum(ProposalStatus, native_enum=False, length=32), nullable=False),
    )
    applied_at: datetime | None = None
    undone_at: datetime | None = None
    dismissed_at: datetime | None = None


class AssistantAudit(TimestampMixin, table=True):
    __tablename__ = "assistant_audit"
    __table_args__ = (Index("ix_assistant_audit_user_created", "user_id", desc("created_at")),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    proposal_id: int = Field(foreign_key="assistant_proposals.id", index=True)
    action: AuditAction = Field(
        sa_column=Column(Enum(AuditAction, native_enum=False, length=32), nullable=False)
    )
    kind: str = Field(max_length=32)
    target: str = Field(max_length=96)
    # For `apply`: the pre-mutation state. For `undo`: the applied state, with
    # `after` carrying the restored values — every row reads as from → to.
    before: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    after: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

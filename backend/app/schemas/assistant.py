"""Assistant request/response shapes (docs/57 §5). `extra="forbid"` everywhere:
an unknown request field is a 422, never a silently ignored one."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ProposalKind = Literal["account.update", "transaction.update", "forecast_scenario.update"]
ProposalStatusLiteral = Literal["pending", "applied", "dismissed", "undone"]


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


class ProposalPublic(BaseModel):
    """One proposal in every shape it is returned (ask/open list/history).

    `before`/`after` are the *changed fields only* — the delta a card renders.
    `apply_audit_id`/`undo_audit_id` are null until that audit row exists
    (`history_item` fills them from `assistant_audit`).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    question: str
    answer: str
    kind: ProposalKind
    target: str
    target_label: str
    summary: str
    before: dict[str, Any]
    after: dict[str, Any]
    status: ProposalStatusLiteral
    created_at: datetime
    applied_at: datetime | None = None
    undone_at: datetime | None = None
    apply_audit_id: int | None = None
    undo_audit_id: int | None = None


class ProposalCreateRequest(BaseModel):
    """A BYO-AI connector's proposed change (docs/59 §3.1).

    `proposal_kind` is a plain `str`: the registry in
    `services/assistant.py`, not Pydantic, owns the vocabulary, so an unknown
    kind is the same refusal family as an LLM that hallucinated one
    (`422 assistant_proposal_invalid`). Only the SHAPE is validated here —
    an unknown field or a missing/overlong `question`/`answer` is a
    `422 validation_error`.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_kind: str
    proposal_target: str
    proposal_summary: str
    proposal_fields: dict[str, Any]
    question: str = Field(min_length=1, max_length=2000)
    # F2 (security t_108c24f3): `answer` used to be `min_length=1` with no
    # upper bound — a multi-megabyte payload was accepted and stored, and
    # proposal rows are never deleted (docs/57 §6 keeps them as history).
    # Same bound as `question`.
    answer: str = Field(min_length=1, max_length=2000)


class AskResponse(BaseModel):
    answer: str
    proposal: ProposalPublic | None = None


class ProposalList(BaseModel):
    items: list[ProposalPublic]

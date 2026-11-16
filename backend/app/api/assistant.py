"""The plain-English assistant routes (docs/57).

`POST /assistant/ask` is the ONLY LLM send here, and it reaches the wire
through `services/assistant.py::ask` → `llm/base.py::post_chat_completions`
behind `assert_send_allowed` — the egress policy docs/11 step 5 enforces for
every other LLM call. apply/dismiss/undo are ordinary authenticated writes:
the demo tenant is refused by `get_current_user`'s method-scoped guard
(`403 demo_account_restricted`), because a shared demo account must not
accumulate per-visitor assistant state (docs/57 §7).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api._offloop import blocking
from app.api.deps import get_current_user, get_db
from app.core.ratelimit import (
    ASSISTANT_ASK_LIMIT,
    ASSISTANT_PROPOSE_LIMIT,
    ASSISTANT_TRANSITION_LIMIT,
    _client_key,
    _no_forwarded_hop,
    _tenant_key,
    human_paced_limit,
    shared_limit,
)
from app.models.assistant import AssistantProposal, ProposalStatus
from app.models.users import User
from app.schemas.assistant import (
    AskRequest,
    AskResponse,
    ProposalCreateRequest,
    ProposalList,
    ProposalPublic,
    ProposalStatusLiteral,
)
from app.services import assistant as service

router = APIRouter(prefix="/assistant", tags=["assistant"])


def _proposal_of(db: Session, proposal: AssistantProposal) -> ProposalPublic:
    """One response shape: the proposal plus its audit ids (docs/57 §6)."""
    return ProposalPublic.model_validate(service.history_item(db, proposal))


def _transition_and_shape(
    transition: Callable[[Session, User, int], AssistantProposal],
    db: Session,
    user: User,
    proposal_id: int,
) -> ProposalPublic:
    """Run one transition AND shape its response, entirely off the event loop.

    Security F1 (t_108c24f3): apply/dismiss/undo used to run their
    synchronous `SELECT ... FOR UPDATE` on the event loop, so a row-lock wait
    there froze every request in the single-worker process — health included
    (docs/01 § "Sessions and the event loop"). The routes hop this whole
    body through `blocking()` (the M149 form): lock wait, writes and the
    audit read all happen in a worker thread, the loop stays free, and the
    request/session close cycles behind it always make progress.
    """
    return _proposal_of(db, transition(db, user, proposal_id))


def _ask_and_shape(db: Session, user: User, question: str) -> AskResponse:
    """Run one whole ask turn — context read, provider send, proposal write
    AND response shaping — entirely off the event loop.

    Security F4 (t_95eb8caf): the route was `async def` and called
    `service.ask(...)` directly, so the synchronous `httpx.post(timeout=60)`
    in `llm/base.py` ran ON the single-worker loop. A slow or hostile
    `llm_base_url` (any registered user may PATCH one) then froze the whole
    API — `/health` included — for up to 60 s per ask. Same hop as the
    transitions above (`blocking()`, M149): the loop only awaits a worker
    thread, and the request session is used inside the hop sequentially,
    never concurrently. The session has already been released before the
    provider wait by `release_before_provider_wait` (docs/07 ledger).
    """
    answer, proposal = service.ask(db, user, question)
    return AskResponse(answer=answer, proposal=_proposal_of(db, proposal) if proposal else None)


@router.post("/ask", response_model=AskResponse)
@human_paced_limit(ASSISTANT_ASK_LIMIT)
async def ask(
    request: Request,
    payload: AskRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AskResponse:
    """One turn: answer + at most one proposed change (docs/57 §5).

    A model reply the server cannot validate answers `422
    assistant_proposal_invalid` — an honest refusal with nothing written, and
    the question is NOT re-sent.

    The whole body runs in a worker thread (docs/01 § "Sessions and the
    event loop", security F4): an ask may hold the loop for the provider's
    full 60 s timeout otherwise.
    """
    return await blocking(_ask_and_shape)(db, current_user, payload.question)


@router.post("/proposals", response_model=ProposalPublic, status_code=201)
# F3-b (security t_95eb8caf): TWO ceilings, deliberately. The per-client-IP
# one is skipped when the request carries no forwarded hop — that hop is what
# says who the client is, and a direct call's socket address is the MCP
# server's container, not any tenant's — and the per-AUTHENTICATED-tenant
# bucket below carries those calls instead. It is also what a client minting
# a fresh X-Forwarded-For per request cannot reset.
@human_paced_limit(
    ASSISTANT_PROPOSE_LIMIT,
    key_func=_client_key,
    exempt_when=_no_forwarded_hop,
)
@human_paced_limit(ASSISTANT_PROPOSE_LIMIT, key_func=_tenant_key)
async def create_proposal(
    request: Request,
    payload: ProposalCreateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProposalPublic:
    """A BYO-AI connector's proposed change (docs/59 §3.1).

    Runs the docs/57 validation registry with NO LLM call and persists an
    inert pending card — the only row written here is the proposal itself.
    `422 assistant_proposal_invalid` for anything the registry refuses, and
    nothing about the target row changes until the user applies the card.

    Human-paced (security F3, t_108c24f3): docs/59 §8 used to claim this
    route "inherits /assistant/ask's middleware posture" — slowapi decorates
    per route and app/main.py has no rate-limit middleware, so it inherited
    nothing. It carries its own ceiling now.
    """
    return _proposal_of(db, service.propose(db, current_user, payload))


@router.get("/proposals", response_model=ProposalList)
async def list_proposals(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    status: Annotated[ProposalStatusLiteral | None, Query()] = None,
) -> ProposalList:
    """The open cards (`?status=pending`, the default view) or one status."""
    wanted = status or ProposalStatus.pending.value
    rows = db.exec(
        select(AssistantProposal)
        .where(
            AssistantProposal.user_id == current_user.id,
            AssistantProposal.status == wanted,
        )
        .order_by(AssistantProposal.created_at.desc(), AssistantProposal.id.desc())
        .limit(50)
    ).all()
    return ProposalList(items=[_proposal_of(db, row) for row in rows])


@router.get("/proposals/{proposal_id}", response_model=ProposalPublic)
async def get_proposal(
    proposal_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProposalPublic:
    return _proposal_of(db, service.load_proposal(db, current_user, proposal_id))


@router.post("/proposals/{proposal_id}/apply", response_model=ProposalPublic)
@shared_limit(ASSISTANT_TRANSITION_LIMIT, "assistant_transition")
async def apply_proposal(
    request: Request,
    proposal_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProposalPublic:
    """Verify the live row still matches the card, write, log (docs/57 §4)."""
    return await blocking(_transition_and_shape)(
        service.apply_proposal, db, current_user, proposal_id
    )


@router.post("/proposals/{proposal_id}/dismiss", response_model=ProposalPublic)
@shared_limit(ASSISTANT_TRANSITION_LIMIT, "assistant_transition")
async def dismiss_proposal(
    request: Request,
    proposal_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProposalPublic:
    """Close the card without writing anything (docs/57 §4)."""
    return await blocking(_transition_and_shape)(
        service.dismiss_proposal, db, current_user, proposal_id
    )


@router.post("/proposals/{proposal_id}/undo", response_model=ProposalPublic)
@shared_limit(ASSISTANT_TRANSITION_LIMIT, "assistant_transition")
async def undo_proposal(
    request: Request,
    proposal_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProposalPublic:
    """Restore the applied state exactly — the round trip (docs/57 §4)."""
    return await blocking(_transition_and_shape)(
        service.undo_proposal, db, current_user, proposal_id
    )


@router.get("/history", response_model=ProposalList)
async def history(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ProposalList:
    """Applied + undone proposals, newest first (docs/57 §6)."""
    return ProposalList(items=service.list_history(db, current_user, limit))

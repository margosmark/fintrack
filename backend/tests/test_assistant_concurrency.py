"""docs/57 §4 — the proposal row lock: two racing decisions, one winner.

Security finding S-2 (gate t_7a682d09): apply/dismiss/undo used to read
`proposal.status` and act on it with no lock, so two concurrent requests
(double-click, retry-after-timeout) could both read `pending` and both pass
the status check. The domain write is idempotent, but a SECOND
`assistant_audit` row landed for one mutation — breaking the append-only
"one audit row per mutation" invariant — and a racing `dismiss` could be
overridden by a late `apply`.

These pins race the real transitions on TWO connections. The house pattern
(tests/test_auth_security_hardening.py, tests/test_m155_price_session.py):
rows another connection can see must be COMMITTED for real, because the
`session` fixture's outer transaction is invisible to a second connection —
so each test builds its own tenant through `Session(get_engine())` and
deletes it again in `finally` (conftest's tenant-footprint guards fail the
run that leaks one).

Pins:

1. a second apply blocks on the locked proposal row, then answers
   `409 assistant_proposal_not_pending` — exactly ONE `assistant_audit`
   apply row for the one mutation;
2. apply vs dismiss resolves to ONE winner in either order, the loser 409s,
   and the stored status matches the winner (no audit row either way);
3. a second undo blocks the same way and answers
   `409 assistant_proposal_not_undoable` — exactly one `undo` audit row.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.api.deps import get_engine
from app.core.errors import APIError
from app.models.accounts import Account, AccountType
from app.models.assistant import AssistantAudit, AssistantProposal, AuditAction, ProposalStatus
from app.models.users import User
from app.services import assistant as assistant_service

# The suite database carries `lock_timeout = '500ms'` (conftest's BUG-3
# guard). A racer is SUPPOSED to wait on the row lock, so it raises the
# timeout on its own connection only — never the database-wide default.
_RACE_LOCK_TIMEOUT = "30s"


def _committed_proposal(*, status: ProposalStatus = ProposalStatus.pending) -> tuple[int, int, int]:
    """A really-committed tenant: user + account + one rename proposal.

    Returns `(user_id, account_id, proposal_id)`. With `status=applied` the
    proposal is applied through the real service first, so undo has a card
    to race for.
    """
    with Session(get_engine(), expire_on_commit=False) as setup:
        user = User(email=f"assistant-lock-{uuid4().hex}@example.com", hashed_password="x")
        setup.add(user)
        setup.commit()
        setup.refresh(user)
        assert user.id is not None
        account = Account(
            user_id=user.id,
            name="Chequing",
            type=AccountType.chequing,
            currency="CAD",
            starting_balance=Decimal("100.00"),
            taxable=False,
        )
        setup.add(account)
        setup.commit()
        setup.refresh(account)
        assert account.id is not None
        proposal = AssistantProposal(
            user_id=user.id,
            question="Rename the account",
            answer="I can rename Chequing.",
            kind="account.update",
            target=f"account:{account.id}",
            target_label="Chequing",
            summary="Apply the change",
            before={"name": "Chequing"},
            after={"name": "Renamed"},
            status=ProposalStatus.pending,
        )
        setup.add(proposal)
        setup.commit()
        setup.refresh(proposal)
        assert proposal.id is not None
        if status is ProposalStatus.applied:
            applied = assistant_service.apply_proposal(setup, user, proposal.id)
            assert applied.status is ProposalStatus.applied
        return user.id, account.id, proposal.id


def _drop_tenant(user_id: int) -> None:
    """Delete every row this module committed, in FK order (docs/57 tenant)."""
    with Session(get_engine()) as teardown:
        for table in ("assistant_audit", "assistant_proposals", "accounts", "categories"):
            teardown.execute(
                text(f"DELETE FROM {table} WHERE user_id = :uid"),  # noqa: S608 -- fixed tuple
                {"uid": user_id},
            )
        teardown.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})
        teardown.commit()


def _racer(
    user_id: int, proposal_id: int, transition: Callable[[Session, User, int], Any]
) -> tuple[threading.Thread, dict[str, Any]]:
    """Run `transition(racer_session, user, proposal_id)` on its own connection.

    Returns the thread and a dict filled with either `{"result": ...}` (the
    service's return value, or its `(status, code)` envelope when it refused)
    or `{"error": ...}` — never both.
    """
    outcome: dict[str, Any] = {}

    def run() -> None:
        with Session(get_engine(), expire_on_commit=False) as racer:
            racer.execute(text(f"SET lock_timeout = '{_RACE_LOCK_TIMEOUT}'"))
            user = racer.get(User, user_id)
            assert user is not None
            try:
                outcome["result"] = transition(racer, user, proposal_id)
            except APIError as exc:
                outcome["result"] = (exc.status_code, exc.code)
            except Exception as exc:  # the assertion below must see it
                outcome["error"] = f"{type(exc).__name__}: {exc}"

    return threading.Thread(target=run, name=f"racer-{user_id}"), outcome


def _audits(proposal_id: int) -> list[AssistantAudit]:
    with Session(get_engine()) as reader:
        return list(
            reader.exec(
                select(AssistantAudit)
                .where(AssistantAudit.proposal_id == proposal_id)
                .order_by(AssistantAudit.id)
            ).all()
        )


def _status(proposal_id: int) -> ProposalStatus | None:
    with Session(get_engine()) as reader:
        proposal = reader.get(AssistantProposal, proposal_id)
        return None if proposal is None else proposal.status


def test_concurrent_double_apply_writes_one_audit_row() -> None:
    """AC (1): the second apply blocks, then 409s — one audit row."""
    user_id, _account_id, proposal_id = _committed_proposal()
    thread, outcome = _racer(user_id, proposal_id, assistant_service.apply_proposal)
    try:
        with Session(get_engine(), expire_on_commit=False) as holder:
            holder.execute(text(f"SET lock_timeout = '{_RACE_LOCK_TIMEOUT}'"))
            holder_user = holder.get(User, user_id)
            assert holder_user is not None
            # apply's own first step, held open — the row lock under test
            held = assistant_service.load_proposal(
                holder, holder_user, proposal_id, for_update=True
            )
            assert held.status is ProposalStatus.pending

            thread.start()
            thread.join(timeout=1.0)
            assert thread.is_alive(), (
                "the second apply must block on the locked proposal row, "
                f"but it finished already: {outcome}"
            )

            # the holder wins by applying in its own transaction
            applied = assistant_service.apply_proposal(holder, holder_user, proposal_id)
            assert applied.status is ProposalStatus.applied
            thread.join(timeout=60)
            assert not thread.is_alive()

        assert outcome == {"result": (409, "assistant_proposal_not_pending")}, outcome
        assert _status(proposal_id) is ProposalStatus.applied
        rows = _audits(proposal_id)
        assert [(row.action, row.before, row.after) for row in rows] == [
            (AuditAction.apply, {"name": "Chequing"}, {"name": "Renamed"})
        ], f"exactly one apply audit row expected, got {len(rows)}"
    finally:
        thread.join(timeout=60)
        _drop_tenant(user_id)


@pytest.mark.parametrize("winner", ["apply", "dismiss"])
def test_concurrent_apply_and_dismiss_have_exactly_one_winner(winner: str) -> None:
    """AC (2): apply vs dismiss — one winner, the loser 409s, state matches."""
    user_id, _account_id, proposal_id = _committed_proposal()
    loser = (
        assistant_service.dismiss_proposal
        if winner == "apply"
        else assistant_service.apply_proposal
    )
    thread, outcome = _racer(user_id, proposal_id, loser)
    try:
        with Session(get_engine(), expire_on_commit=False) as holder:
            holder.execute(text(f"SET lock_timeout = '{_RACE_LOCK_TIMEOUT}'"))
            holder_user = holder.get(User, user_id)
            assert holder_user is not None
            held = assistant_service.load_proposal(
                holder, holder_user, proposal_id, for_update=True
            )
            assert held.status is ProposalStatus.pending

            thread.start()
            thread.join(timeout=1.0)
            assert thread.is_alive(), (
                f"the racing {loser.__name__} must block on the locked proposal row, "
                f"but it finished already: {outcome}"
            )

            if winner == "apply":
                decided = assistant_service.apply_proposal(holder, holder_user, proposal_id)
                expected_status, expected_audits = ProposalStatus.applied, 1
            else:
                decided = assistant_service.dismiss_proposal(holder, holder_user, proposal_id)
                expected_status, expected_audits = ProposalStatus.dismissed, 0
            assert decided.status is expected_status
            thread.join(timeout=60)
            assert not thread.is_alive()

        assert outcome == {"result": (409, "assistant_proposal_not_pending")}, outcome
        assert _status(proposal_id) is expected_status
        assert len(_audits(proposal_id)) == expected_audits
    finally:
        thread.join(timeout=60)
        _drop_tenant(user_id)


def test_concurrent_double_undo_writes_one_undo_audit_row() -> None:
    """The undo half of S-2: one `undo` audit row, the second undo 409s."""
    user_id, _account_id, proposal_id = _committed_proposal(status=ProposalStatus.applied)
    thread, outcome = _racer(user_id, proposal_id, assistant_service.undo_proposal)
    try:
        with Session(get_engine(), expire_on_commit=False) as holder:
            holder.execute(text(f"SET lock_timeout = '{_RACE_LOCK_TIMEOUT}'"))
            holder_user = holder.get(User, user_id)
            assert holder_user is not None
            held = assistant_service.load_proposal(
                holder, holder_user, proposal_id, for_update=True
            )
            assert held.status is ProposalStatus.applied

            thread.start()
            thread.join(timeout=1.0)
            assert thread.is_alive(), (
                "the second undo must block on the locked proposal row, "
                f"but it finished already: {outcome}"
            )

            undone = assistant_service.undo_proposal(holder, holder_user, proposal_id)
            assert undone.status is ProposalStatus.undone
            thread.join(timeout=60)
            assert not thread.is_alive()

        assert outcome == {"result": (409, "assistant_proposal_not_undoable")}, outcome
        assert _status(proposal_id) is ProposalStatus.undone
        rows = _audits(proposal_id)
        assert [row.action for row in rows] == [AuditAction.apply, AuditAction.undo], (
            f"exactly one apply + one undo audit row expected, got {[row.action for row in rows]}"
        )
        assert rows[1].before == {"name": "Renamed"} and rows[1].after == {"name": "Chequing"}
    finally:
        thread.join(timeout=60)
        _drop_tenant(user_id)


# ------------------------------------------- F1: the wait itself is bounded
#
# Security t_108c24f3 (comment 907) F1 came in two halves: the lock wait ran
# on the event loop (pinned at HTTP level by
# `tests/test_assistant_transition_offloop.py`), and nothing bounded it. The
# suite database carries a database-wide `lock_timeout = '500ms'` (conftest's
# BUG-3 guard), PRODUCTION has none — without the transition's own bound, a
# stuck holder anywhere in the system leaves every racing transition waiting
# indefinitely on a row lock.


def test_the_bound_is_applied_inside_the_transition_transaction() -> None:
    """`_bound_lock_wait` really changes the effective timeout, in-transaction.

    The deterministic half: read the GUC back instead of inferring it from a
    stopwatch. After the transaction ends the bound must be GONE — `SET
    LOCAL` never reaches the pooled connection the next checkout inherits.
    `after` is deliberately compared against what THIS call set, not against
    `before`: other tests in this file set a session-level 30 s on their own
    connections and pooled connections carry whatever the last borrower left,
    so `before`/`after` need not even be the same connection.
    """
    with Session(get_engine()) as session:
        before = session.execute(text("SELECT current_setting('lock_timeout')::interval")).scalar()
        assistant_service._bound_lock_wait(session)
        during = session.execute(text("SELECT current_setting('lock_timeout')::interval")).scalar()
        session.commit()
        after = session.execute(text("SELECT current_setting('lock_timeout')::interval")).scalar()
    assert during is not None and timedelta(milliseconds=100) <= during <= timedelta(seconds=30), (
        f"the transition's lock bound is not in effect (got {during!r}; "
        f"session default was {before!r})"
    )
    assert during != after, (
        f"the lock bound leaked past its transaction: in-transaction {during!r} -> "
        f"after commit {after!r} (it must be `SET LOCAL`, so a pooled connection "
        f"cannot inherit it; session-level before the call was {before!r})"
    )


def test_a_lock_wait_beyond_the_bound_answers_the_busy_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A holder that never releases ends at `503 assistant_busy`, bounded.

    The racer must come back on its own — if it does not, the wait is
    unbounded (production's real risk: no database-wide timeout there). The
    envelope must be the documented one, not a raw driver error: an unmapped
    `OperationalError` would surface as a 500 with no retry hint.
    """
    monkeypatch.setattr(assistant_service, "LOCK_WAIT_LIMIT", "300ms")
    user_id, _account_id, proposal_id = _committed_proposal()
    thread, outcome = _racer(user_id, proposal_id, assistant_service.apply_proposal)
    try:
        with Session(get_engine(), expire_on_commit=False) as holder:
            holder.execute(text(f"SET lock_timeout = '{_RACE_LOCK_TIMEOUT}'"))
            holder_user = holder.get(User, user_id)
            assert holder_user is not None
            # apply's own first step, held open and never released in time:
            held = assistant_service.load_proposal(
                holder, holder_user, proposal_id, for_update=True
            )
            assert held.status is ProposalStatus.pending

            started = time.monotonic()
            thread.start()
            thread.join(timeout=15)
            elapsed = time.monotonic() - started
            assert not thread.is_alive(), (
                f"the racing transition never came back — its row-lock wait is unbounded: {outcome}"
            )
            assert elapsed < 3.0, (
                f"the racer waited {elapsed:.2f}s; the bound is {assistant_service.LOCK_WAIT_LIMIT}"
            )
            assert outcome == {"result": (503, "assistant_busy")}, outcome

        # The holder never transitioned anything: the loser gave up instead of
        # writing, so the card is untouched and no audit row exists.
        assert _status(proposal_id) is ProposalStatus.pending
        assert _audits(proposal_id) == []
    finally:
        thread.join(timeout=60)
        _drop_tenant(user_id)

"""docs/59 — BYO-AI connector: propose → pending → confirm → audit → undo.

The card's AC in the spec's verification order (docs/59 §7), REST half —
the MCP seam half lives in `test_mcp_agent_surface.py`:

1. the proposal path requires confirmation before mutation: `propose()`
   writes ONLY the proposal card (target byte-identical, zero audit rows);
   the mutation happens on `apply` from the user's own web session, which
   no MCP tool can call (docs/59 §3.3);
2. audit: one `assistant_audit` row per mutation (apply AND undo), none for
   the propose itself;
3. every refusal writes nothing: `422 validation_error`,
   `422 assistant_proposal_invalid`, `401 invalid_token`,
   `403 demo_account_restricted`;
4. tenancy: a foreign proposal id is `404 assistant_proposal_not_found`,
   never a write.

No LLM exists anywhere on this path — `propose()` never builds a provider —
so this file needs no model stub; `test_propose_never_builds_a_provider`
pins that with a transport sentinel.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.assistant as assistant_service
from app.core.security import create_access_token
from app.models.assistant import AssistantAudit, AuditAction
from app.models.transaction_events import TransactionEvent, TransactionEventKind
from tests.test_assistant import (
    _account,
    _category,
    _counts,
    _headers,
    _rows,
    _snapshot,
    _txn,
    _user,
)

API = "/api/v1"
PROPOSALS = f"{API}/assistant/proposals"
HISTORY = f"{API}/assistant/history"

PUBLIC_FIELDS = {
    "id",
    "question",
    "answer",
    "kind",
    "target",
    "target_label",
    "summary",
    "before",
    "after",
    "status",
    "created_at",
    "applied_at",
    "undone_at",
    "apply_audit_id",
    "undo_audit_id",
}


def _live(session: Session, obj: Any) -> dict[str, Any]:
    """Byte-compare against the DB's CURRENT row.

    `session.expire_all()` (done inside `_counts`/`_rows`) leaves instances
    expired, and a snapshot of an expired instance is `{}` — always reload
    from the DB first so the comparison is real state, not an empty dict.
    """
    session.refresh(obj)
    return _snapshot(obj)


def _body(
    kind: str = "transaction.update",
    target: str | None = None,
    fields: dict[str, Any] | None = None,
    question: str = "Categorize my Loblaws run",
    answer: str = "Transaction #123 at Loblaws is groceries — no category set yet.",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "proposal_kind": kind,
        "proposal_target": target or "",
        "proposal_summary": "Reassign the Loblaws run to groceries",
        "proposal_fields": fields if fields is not None else {"category_id": 7},
        "question": question,
        "answer": answer,
    }
    return payload


# ------------------------------------------------- propose writes only a card


def test_connector_propose_creates_pending_card_and_touches_nothing(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    account = _account(session, user)
    txn = _txn(session, user, account)
    groceries = _category(session, user, "Groceries")
    target = f"transaction:{txn.id}"

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("propose() must not reach an LLM/egress path")

    monkeypatch.setattr(assistant_service, "build_provider", _boom)

    before_row = _live(session, txn)
    resp = client.post(
        PROPOSALS,
        headers=_headers(user),
        json=_body(target=target, fields={"category_id": groceries.id}),
    )

    assert resp.status_code == 201, resp.text
    card = resp.json()
    assert set(card) == PUBLIC_FIELDS
    assert card["status"] == "pending"
    assert card["kind"] == "transaction.update"
    assert card["target"] == target
    assert card["question"] == "Categorize my Loblaws run"
    assert card["answer"].startswith("Transaction #123")
    assert card["before"] == {"category_id": None}
    assert card["after"] == {"category_id": groceries.id}
    assert card["apply_audit_id"] is None and card["undo_audit_id"] is None

    # Exactly one row written: the proposal. Target byte-identical, no audit.
    assert _counts(session, user) == (1, 0)
    session.expire_all()
    assert _live(session, txn) == before_row
    assert _rows(session, TransactionEvent, user) == []

    # It lands in the open cards the user confirms from (default = pending).
    listing = client.get(PROPOSALS, headers=_headers(user))
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [card["id"]]


# ------------------------------------------- confirm → audit → undo round trip


def test_propose_apply_undo_round_trip_is_audited_both_ways(
    client: TestClient, session: Session
) -> None:
    user = _user(session)
    account = _account(session, user)
    txn = _txn(session, user, account)
    groceries = _category(session, user, "Groceries")
    headers = _headers(user)
    original = _live(session, txn)

    card = client.post(
        PROPOSALS,
        headers=headers,
        json=_body(target=f"transaction:{txn.id}", fields={"category_id": groceries.id}),
    ).json()
    assert card["status"] == "pending"

    # Confirmation is the user's own session — apply mutates AND audits.
    applied = client.post(f"{PROPOSALS}/{card['id']}/apply", headers=headers)
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    assert applied.json()["apply_audit_id"] is not None
    session.expire_all()
    assert txn.category_id == groceries.id

    audits = _rows(session, AssistantAudit, user)
    assert len(audits) == 1
    assert audits[0].action == AuditAction.apply
    assert audits[0].proposal_id == card["id"]
    assert audits[0].before == {"category_id": None}
    assert audits[0].after == {"category_id": groceries.id}
    # The ledger receipt is the same one PATCH /transactions/{id} writes.
    events = _rows(session, TransactionEvent, user)
    assert len(events) == 1
    assert events[0].event_kind == TransactionEventKind.categorized
    assert events[0].payload.get("via") == "assistant"

    # Undo restores the applied state exactly and is audited the same way.
    undone = client.post(f"{PROPOSALS}/{card['id']}/undo", headers=headers)
    assert undone.status_code == 200, undone.text
    assert undone.json()["status"] == "undone"
    assert undone.json()["undo_audit_id"] is not None
    session.expire_all()
    assert _live(session, txn) == original

    audits = _rows(session, AssistantAudit, user)
    assert len(audits) == 2
    assert audits[1].action == AuditAction.undo
    assert audits[1].before == {"category_id": groceries.id}
    assert audits[1].after == {"category_id": None}
    assert len(_rows(session, TransactionEvent, user)) == 2

    # The round trip shows up in History with both audit ids.
    history = client.get(HISTORY, headers=headers)
    assert history.status_code == 200
    entry = history.json()["items"][0]
    assert entry["id"] == card["id"]
    assert entry["apply_audit_id"] is not None and entry["undo_audit_id"] is not None


def test_dismissing_a_connector_card_writes_nothing(client: TestClient, session: Session) -> None:
    user = _user(session)
    account = _account(session, user)
    txn = _txn(session, user, account)
    headers = _headers(user)
    before_row = _live(session, txn)

    card = client.post(
        PROPOSALS,
        headers=headers,
        json=_body(target=f"transaction:{txn.id}", fields={"note": "Costco run"}),
    ).json()
    dismissed = client.post(f"{PROPOSALS}/{card['id']}/dismiss", headers=headers)
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "dismissed"

    assert _counts(session, user) == (1, 0)  # one card, zero audit rows
    session.expire_all()
    assert _live(session, txn) == before_row


# ------------------------------------------------------------ refusal envelopes


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("missing question", lambda b: b.pop("question")),
        ("empty answer", lambda b: b.update(answer="")),
        ("unknown request field", lambda b: b.update(consent=True)),
        ("overlong question", lambda b: b.update(question="x" * 2001)),
        # F2 (security t_108c24f3): `answer` used to be unbounded — a
        # 5,000,000-char payload was accepted and stored, and proposals are
        # never deleted. It is capped at question's bound now (docs/59 §3.1).
        ("overlong answer", lambda b: b.update(answer="x" * 2001)),
    ],
)
def test_shape_errors_are_validation_error_and_write_nothing(
    client: TestClient,
    session: Session,
    label: str,
    mutate: Any,
) -> None:
    user = _user(session)
    account = _account(session, user)
    txn = _txn(session, user, account)
    before_row = _live(session, txn)
    payload = _body(target=f"transaction:{txn.id}")
    mutate(payload)

    resp = client.post(PROPOSALS, headers=_headers(user), json=payload)

    assert resp.status_code == 422, f"{label}: {resp.text}"
    assert resp.json()["code"] == "validation_error"
    assert _counts(session, user) == (0, 0)
    session.expire_all()
    assert _live(session, txn) == before_row


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("unknown kind", {"proposal_kind": "thing.delete"}),
        ('kind "none"', {"proposal_kind": "none"}),
        ("target prefix mismatch", {"proposal_target": "txn:1"}),
        ("hallucinated target", {"proposal_target": "transaction:99999999"}),
        (
            "field outside the registry",
            {"proposal_fields": {"color": "red"}},
        ),
        ("empty proposal_fields", {"proposal_fields": {}}),
        ("blank summary", {"proposal_summary": "   "}),
        # A no-op change where after == before — nothing to confirm.
        ("no-op change", {"proposal_fields": {"category_id": None}}),
    ],
)
def test_registry_refusals_are_assistant_proposal_invalid_and_write_nothing(
    client: TestClient,
    session: Session,
    label: str,
    overrides: dict[str, Any],
) -> None:
    user = _user(session)
    account = _account(session, user)
    txn = _txn(session, user, account)
    before_row = _live(session, txn)
    payload = _body(target=f"transaction:{txn.id}", fields={"note": "Costco run"})
    payload.update(overrides)

    resp = client.post(PROPOSALS, headers=_headers(user), json=payload)

    assert resp.status_code == 422, f"{label}: {resp.text}"
    assert resp.json()["code"] == "assistant_proposal_invalid"
    assert _counts(session, user) == (0, 0)
    session.expire_all()
    assert _live(session, txn) == before_row


def test_answer_at_the_documented_bound_is_accepted_and_stored_verbatim(
    client: TestClient, session: Session
) -> None:
    """F2 boundary, the IN half: 2000 chars is accepted and stored as-is.

    The OUT half (2001) is the parametrized `overlong answer` case above, so
    both edges of `1..2000` are pinned and the cap cannot drift to a smaller
    value silently either.
    """
    user = _user(session)
    account = _account(session, user)
    txn = _txn(session, user, account)
    answer = "a" * 2000

    resp = client.post(
        PROPOSALS,
        headers=_headers(user),
        json=_body(target=f"transaction:{txn.id}", fields={"note": "Costco run"}, answer=answer),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["answer"] == answer
    assert _counts(session, user) == (1, 0)


def test_propose_requires_a_valid_token(client: TestClient, session: Session) -> None:
    user = _user(session)
    account = _account(session, user)
    txn = _txn(session, user, account)
    payload = _body(target=f"transaction:{txn.id}")

    for headers in ({}, {"Authorization": "Bearer not-a-jwt"}):
        resp = client.post(PROPOSALS, headers=headers, json=payload)
        assert resp.status_code == 401, resp.text
        assert resp.json()["code"] == "invalid_token"
    assert _counts(session, user) == (0, 0)


def test_another_tenant_cannot_apply_a_connector_proposal(
    client: TestClient, session: Session
) -> None:
    owner = _user(session)
    stranger = _user(session)
    account = _account(session, owner)
    txn = _txn(session, owner, account)
    owner_card = client.post(
        PROPOSALS,
        headers=_headers(owner),
        json=_body(target=f"transaction:{txn.id}", fields={"note": "mine"}),
    ).json()

    resp = client.post(f"{PROPOSALS}/{owner_card['id']}/apply", headers=_headers(stranger))
    assert resp.status_code == 404
    assert resp.json()["code"] == "assistant_proposal_not_found"

    # The card is untouched: still pending, still zero audits, no mutation.
    session.expire_all()
    assert txn.note is None
    assert _counts(session, owner) == (1, 0)
    assert _counts(session, stranger) == (0, 0)


def test_demo_tenant_cannot_propose(client: TestClient, session: Session) -> None:
    """Demo is query-only end to end: the method-scoped guard refuses the
    connector's write before the service runs (docs/59 §1: demo cannot mint
    read_propose, and the REST path refuses it anyway)."""
    from app.services.demo_seed import DEMO_EMAIL

    user = _user(session, email=DEMO_EMAIL)
    account = _account(session, user)
    txn = _txn(session, user, account)

    resp = client.post(
        PROPOSALS,
        headers={"Authorization": f"Bearer {create_access_token(user.id)}"},
        json=_body(target=f"transaction:{txn.id}"),
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "demo_account_restricted"
    assert _counts(session, user) == (0, 0)

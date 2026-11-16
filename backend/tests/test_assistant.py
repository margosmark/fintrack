"""docs/57 — the plain-English assistant: proposal lifecycle, audit, round trip.

The pins, in the spec's own Verification order:

1. ask happy path writes ONE `pending` proposal and zero audit rows;
2. answer-only replies write nothing;
3. every unparseable model reply is a `422 assistant_proposal_invalid`
   refusal with NOTHING written (the "no silent write" pin);
4. apply → history → undo restores the target byte-for-byte (per kind);
5. one audit row per mutation, none for dismiss;
6. `assistant_proposal_stale` / `assistant_undo_conflict` refuse drifted
   targets without writing;
7. tenancy: a foreign proposal id is 404 and a foreign row never enters
   another tenant's context;
8. gateway policy: privacy mode answers `403 privacy_mode_blocks_external_api`
   BEFORE any socket — pinned by a transport sentinel that fails the test if
   `app.llm.base` even reaches its `httpx.post`;
9. the kind-specific guards (split, staging, E2EE) refuse at ask time;
10. the registry pins (`test_provider_wait_ledger`, `test_rls_coverage`,
    `test_models`, `test_export_registry`) run green in the same suite;
11. the context's committed-only staging rule (docs/57 §3, QA F1): staged
    rows never enter `recent_transactions`, committed-batch and manual rows
    do — exactly once each — and a manual-only tenant's context is never
    silently empty, whatever batches other tenants own.

The `session` fixture is savepoint-joined to the suite transaction
(conftest `override_get_db`), so `release_before_provider_wait`'s commit
inside `ask` releases a SAVEPOINT, not the outer transaction — rows stay
visible to this test and roll back with the suite.
"""

from __future__ import annotations

import warnings
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

import app.llm.base as llm_base
from app.core.security import create_access_token
from app.models.accounts import Account, AccountType
from app.models.assistant import AssistantAudit, AssistantProposal, AuditAction, ProposalStatus
from app.models.categories import Category
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transaction_events import TransactionEvent
from app.models.transactions import Transaction
from app.models.users import User
from app.services import assistant as assistant_service
from app.services.demo_seed import DEMO_EMAIL

API = "/api/v1"
ASK = f"{API}/assistant/ask"
PROPOSALS = f"{API}/assistant/proposals"
HISTORY = f"{API}/assistant/history"

D = Decimal
_seq = 0


# --------------------------------------------------------------------- seeds


def _user(session: Session, *, email: str | None = None) -> User:
    global _seq
    _seq += 1
    user = User(
        email=email or f"assistant-{_seq}-{id(session)}@example.com",
        hashed_password="x",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _account(
    session: Session, user: User, *, name: str = "Chequing", starting: str = "100.00"
) -> Account:
    account = Account(
        user_id=user.id,
        name=name,
        type=AccountType.chequing,
        currency="CAD",
        starting_balance=D(starting),
        taxable=False,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _category(session: Session, user: User, name: str) -> Category:
    category = Category(user_id=user.id, name=name)
    session.add(category)
    session.commit()
    session.refresh(category)
    return category


def _txn(session: Session, user: User, account: Account, *, payee: str = "Loblaws") -> Transaction:
    global _seq
    _seq += 1
    txn = Transaction(
        user_id=user.id,
        account_id=account.id,
        date=date(2026, 11, 8),
        raw_description=f"raw {_seq}",
        normalized_payee=payee,
        amount_native=D("-84.12"),
        currency="CAD",
        cad_amount=D("-84.12"),
        dedup_hash=f"assistant-{user.id}-{_seq}",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _scenario(session: Session, user: User, *, name: str = "Retirement") -> dict[str, Any]:
    entry = {"id": f"scen{user.id}abcd", "name": name, "events": []}
    user.forecast_scenarios = [entry]
    session.add(user)
    session.commit()
    return entry


# -------------------------------------------------------------------- stubs


class _FakeProvider:
    """Answers with a canned model reply and records what the prompt held."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.prompts: list[list[dict]] = []

    def chat(self, messages: list[dict], json_schema: dict) -> dict:
        self.prompts.append(messages)
        assert json_schema["type"] == "object"
        return dict(self.payload)


def _stub(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> _FakeProvider:
    provider = _FakeProvider(payload)
    monkeypatch.setattr(
        assistant_service,
        "build_provider",
        lambda user, settings=None: SimpleNamespace(provider=provider, model="stub-model"),
    )
    return provider


def _proposal(
    kind: str,
    target: str,
    fields: dict[str, Any],
    *,
    answer: str = "Here is what I found.",
    summary: str = "Apply the change",
) -> dict[str, Any]:
    return {
        "answer": answer,
        "proposal_kind": kind,
        "proposal_target": target,
        "proposal_summary": summary,
        "proposal_fields": fields,
    }


# ----------------------------------------------------------------- db helpers


def _rows(session: Session, model: type, user: User) -> list[Any]:
    session.expire_all()
    return list(
        session.execute(
            select(model).where(model.user_id == user.id).order_by(model.id)  # type: ignore[attr-defined]
        )
        .scalars()
        .all()
    )


def _counts(session: Session, user: User) -> tuple[int, int]:
    return (
        len(_rows(session, AssistantProposal, user)),
        len(_rows(session, AssistantAudit, user)),
    )


def _snapshot(obj: Any) -> dict[str, Any]:
    """Every mapped column except `updated_at`, which the docs/02 mixin
    bumps on ANY update — the round trip pins the data, not the clock."""
    return {
        key: value
        for key, value in obj.__dict__.items()
        if not key.startswith("_sa") and key != "updated_at"
    }


# ------------------------------------------------------------- 1-3: the ask


def test_ask_persists_one_pending_proposal_and_no_audit(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    account = _account(session, user)
    stub = _stub(
        monkeypatch,
        _proposal(
            "account.update",
            f"account:{account.id}",
            {"name": "Everyday Chequing"},
            answer="Your chequing holds 100.00 CAD.",
            summary="Rename the account",
        ),
    )

    resp = client.post(ASK, headers=_headers(user), json={"question": "rename my chequing"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "Your chequing holds 100.00 CAD."
    proposal = body["proposal"]
    assert proposal is not None
    assert proposal["status"] == "pending"
    assert proposal["kind"] == "account.update"
    assert proposal["target"] == f"account:{account.id}"
    assert proposal["before"] == {"name": "Chequing"}
    assert proposal["after"] == {"name": "Everyday Chequing"}
    assert proposal["apply_audit_id"] is None and proposal["undo_audit_id"] is None
    # exactly one proposal row, zero audit rows, and nothing moved on the target
    assert _counts(session, user) == (1, 0)
    session.expire_all()
    assert account.name == "Chequing"
    # the prompt carried the bounded context (docs/57 §3), single-turn
    assert len(stub.prompts) == 1
    assert "rename my chequing" in stub.prompts[0][1]["content"]


def test_ask_answer_only_writes_nothing(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    _account(session, user)
    _stub(
        monkeypatch,
        {
            "answer": "You have one account.",
            "proposal_kind": "none",
            "proposal_target": "",
            "proposal_summary": "",
            "proposal_fields": {},
        },
    )

    resp = client.post(ASK, headers=_headers(user), json={"question": "how many accounts?"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["proposal"] is None
    assert _counts(session, user) == (0, 0)


@pytest.mark.parametrize(
    ("payload", "detail_fragment"),
    [
        # unknown kind (answer present — the reply must clear the answer gate
        # before kind validation, or it is a 502 for the wrong reason)
        (
            {
                "answer": "ok",
                "proposal_kind": "banana",
                "proposal_target": "x",
                "proposal_summary": "s",
                "proposal_fields": {"name": "n"},
            },
            "banana",
        ),
        # unknown field for the kind
        (_proposal("account.update", "account:1", {"bogus": 1}), "bogus"),
        # unparsable decimal
        (
            _proposal("account.update", "account:1", {"starting_balance": "not-a-number"}),
            "starting_balance",
        ),
        # target prefix that does not match the kind
        (_proposal("account.update", "transaction:1", {"name": "n"}), "prefix"),
        # no-op proposal (after == before)
        (None, "nothing to do"),  # filled per-test below
        # unowned category id
        (_proposal("transaction.update", "transaction:1", {"category_id": 999999}), "category_id"),
    ],
)
def test_ask_refusal_writes_nothing(
    client: TestClient,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any] | None,
    detail_fragment: str,
) -> None:
    """The honest-failure pin: 422 envelope, no proposal row, no audit row."""
    user = _user(session)
    account = _account(session, user)
    txn = _txn(session, user, account)
    _category(session, user, "Groceries")

    if payload is None:
        payload = _proposal(
            "account.update", f"account:{account.id}", {"name": "Chequing"}
        )  # exactly today's value -> no-op refusal
    # repoint the placeholder targets at the real rows (the prefix-mismatch
    # case keeps its WRONG target — that mismatch is the thing under test)
    if payload["proposal_kind"] == "account.update" and payload["proposal_target"] == "account:1":
        payload = {**payload, "proposal_target": f"account:{account.id}"}
    if (
        payload["proposal_kind"] == "transaction.update"
        and payload["proposal_target"] == "transaction:1"
    ):
        payload = {**payload, "proposal_target": f"transaction:{txn.id}"}
    _stub(monkeypatch, payload)

    resp = client.post(ASK, headers=_headers(user), json={"question": "change it"})

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "assistant_proposal_invalid", body
    assert detail_fragment in body["detail"], body
    assert _counts(session, user) == (0, 0)
    session.expire_all()
    assert account.name == "Chequing" and txn.category_id is None


def test_ask_rejects_malformed_requests(client: TestClient, session: Session) -> None:
    user = _user(session)
    headers = _headers(user)
    assert client.post(ASK, headers=headers, json={}).status_code == 422
    assert client.post(ASK, headers=headers, json={"question": ""}).status_code == 422
    # extra="forbid": an unknown request field is a 422, never ignored
    resp = client.post(ASK, headers=headers, json={"question": "hi", "consent": True})
    assert resp.status_code == 422, resp.text
    assert _counts(session, user) == (0, 0)


def test_ask_without_a_model_is_503_and_writes_nothing(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    monkeypatch.setattr(
        assistant_service,
        "build_provider",
        lambda user, settings=None: SimpleNamespace(provider=None, model=""),
    )
    resp = client.post(ASK, headers=_headers(user), json={"question": "hello"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "assistant_unavailable"
    assert _counts(session, user) == (0, 0)


# --------------------------------------------------- 4-6: apply / undo pins


def test_account_apply_history_undo_round_trip(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    account = _account(session, user, name="Chequing", starting="100.00")
    _stub(
        monkeypatch,
        _proposal("account.update", f"account:{account.id}", {"name": "Everyday Chequing"}),
    )
    headers = _headers(user)
    proposal_id = client.post(ASK, headers=headers, json={"question": "rename"}).json()["proposal"][
        "id"
    ]
    before = _snapshot(account)

    applied = client.post(f"{PROPOSALS}/{proposal_id}/apply", headers=headers)
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["status"] == "applied"
    assert body["apply_audit_id"] is not None and body["undo_audit_id"] is None
    session.expire_all()
    assert account.name == "Everyday Chequing"

    # exactly one audit row, carrying the complete before/after
    audits = _rows(session, AssistantAudit, user)
    assert len(audits) == 1
    assert audits[0].action == AuditAction.apply
    assert audits[0].before == {"name": "Chequing"}
    assert audits[0].after == {"name": "Everyday Chequing"}
    assert audits[0].proposal_id == proposal_id

    # history lists it with the audit id (docs/57 §6)
    history = client.get(HISTORY, headers=headers)
    assert history.status_code == 200, history.text
    items = history.json()["items"]
    assert [item["id"] for item in items] == [proposal_id]
    assert items[0]["apply_audit_id"] == audits[0].id
    assert items[0]["status"] == "applied"

    undone = client.post(f"{PROPOSALS}/{proposal_id}/undo", headers=headers)
    assert undone.status_code == 200, undone.text
    assert undone.json()["status"] == "undone"
    assert undone.json()["undo_audit_id"] is not None
    session.expire_all()
    # THE round trip: every column back to the pre-apply snapshot
    assert _snapshot(account) == before
    audits = _rows(session, AssistantAudit, user)
    assert len(audits) == 2
    assert audits[1].action == AuditAction.undo
    assert audits[1].before == {"name": "Everyday Chequing"}
    assert audits[1].after == {"name": "Chequing"}

    # undo is exactly once: a second undo is 409, nothing changes
    again = client.post(f"{PROPOSALS}/{proposal_id}/undo", headers=headers)
    assert again.status_code == 409 and again.json()["code"] == "assistant_proposal_not_undoable"
    assert _counts(session, user)[1] == 2


def test_transaction_round_trip_writes_the_patch_receipt(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    account = _account(session, user)
    txn = _txn(session, user, account, payee="Loblaws")
    groceries = _category(session, user, "Groceries")
    _stub(
        monkeypatch,
        _proposal(
            "transaction.update",
            f"transaction:{txn.id}",
            {"category_id": groceries.id},
            summary="Categorize this as Groceries",
        ),
    )
    headers = _headers(user)
    proposal_id = client.post(ASK, headers=headers, json={"question": "categorize"}).json()[
        "proposal"
    ]["id"]
    before = _snapshot(txn)

    assert client.post(f"{PROPOSALS}/{proposal_id}/apply", headers=headers).status_code == 200
    session.expire_all()
    assert txn.category_id == groceries.id

    # the same receipt `PATCH /transactions/{id}` writes, via the same writer
    events = list(
        session.execute(
            select(TransactionEvent)
            .where(TransactionEvent.tx_id == txn.id)
            .order_by(TransactionEvent.id)
        )
        .scalars()
        .all()
    )
    assert [event.event_kind.value for event in events] == ["categorized"]
    assert events[0].payload["via"] == "assistant"
    assert events[0].payload["category_id"] == groceries.id
    assert events[0].payload["category_name"] == "Groceries"
    assert events[0].payload_hash  # hashed like every receipt (docs/02)

    assert client.post(f"{PROPOSALS}/{proposal_id}/undo", headers=headers).status_code == 200
    session.expire_all()
    assert _snapshot(txn) == before  # byte-for-byte restore, category cleared again
    events = list(
        session.execute(
            select(TransactionEvent)
            .where(TransactionEvent.tx_id == txn.id)
            .order_by(TransactionEvent.id)
        )
        .scalars()
        .all()
    )
    assert [event.event_kind.value for event in events] == ["categorized", "categorized"]
    assert events[1].payload["category_id"] is None
    assert _counts(session, user)[1] == 2  # one audit per mutation


def test_forecast_round_trip_restores_the_event_list(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    _scenario(session, user, name="Retirement")
    # the model mints no event ids — the server does (docs/57 §2)
    _stub(
        monkeypatch,
        _proposal(
            "forecast_scenario.update",
            f"forecast_scenario:scen{user.id}abcd",
            {
                "events": [
                    {"type": "one_off_cash_event", "date": "2027-01-01", "amount_cad": "5000"}
                ]
            },
            summary="Add a one-off 5000 CAD cash event",
        ),
    )
    headers = _headers(user)
    proposal_id = client.post(ASK, headers=headers, json={"question": "add 5k"}).json()["proposal"][
        "id"
    ]

    session.expire_all()
    before = dict(user.forecast_scenarios[0])

    assert client.post(f"{PROPOSALS}/{proposal_id}/apply", headers=headers).status_code == 200
    session.expire_all()
    applied_entry = user.forecast_scenarios[0]
    assert len(applied_entry["events"]) == 1
    minted = applied_entry["events"][0]
    assert minted["type"] == "one_off_cash_event"
    assert len(minted["id"]) >= 8  # server-minted, valid against ID_PATTERN
    assert str(minted["amount_cad"]).startswith("5000")

    assert client.post(f"{PROPOSALS}/{proposal_id}/undo", headers=headers).status_code == 200
    session.expire_all()
    assert user.forecast_scenarios[0] == before  # exact restore
    assert _counts(session, user)[1] == 2


def test_dismiss_is_a_no_write_transition(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    account = _account(session, user)
    _stub(monkeypatch, _proposal("account.update", f"account:{account.id}", {"name": "Nope"}))
    headers = _headers(user)
    proposal_id = client.post(ASK, headers=headers, json={"question": "rename"}).json()["proposal"][
        "id"
    ]

    resp = client.post(f"{PROPOSALS}/{proposal_id}/dismiss", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "dismissed"
    # no audit row (nothing was mutated) and the target did not move
    assert _counts(session, user) == (1, 0)
    session.expire_all()
    assert account.name == "Chequing"

    # a decided card answers 409 from both remaining transitions
    for action in ("dismiss", "apply"):
        again = client.post(f"{PROPOSALS}/{proposal_id}/{action}", headers=headers)
        assert again.status_code == 409, again.text
        assert again.json()["code"] == "assistant_proposal_not_pending"
    # dismissed cards never appear in History (only applied/undone do)
    assert client.get(HISTORY, headers=headers).json()["items"] == []


def test_stale_apply_and_undo_conflict_write_nothing(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    account = _account(session, user, name="Chequing")
    _stub(monkeypatch, _proposal("account.update", f"account:{account.id}", {"name": "Renamed"}))
    headers = _headers(user)
    proposal_id = client.post(ASK, headers=headers, json={"question": "rename"}).json()["proposal"][
        "id"
    ]

    # the user renames the account by hand BETWEEN ask and apply
    account.name = "Hand-renamed"
    session.add(account)
    session.commit()

    stale = client.post(f"{PROPOSALS}/{proposal_id}/apply", headers=headers)
    assert stale.status_code == 409 and stale.json()["code"] == "assistant_proposal_stale"
    assert _counts(session, user)[1] == 0  # no audit, no write
    session.expire_all()
    assert account.name == "Hand-renamed"  # the hand edit survived
    proposals = _rows(session, AssistantProposal, user)
    assert proposals[0].status == ProposalStatus.pending

    # apply first, then edit, then undo -> the conflict pin
    account.name = "Chequing"  # back to the card's `before` so apply succeeds
    session.add(account)
    session.commit()
    assert client.post(f"{PROPOSALS}/{proposal_id}/apply", headers=headers).status_code == 200
    account.name = "Edited-after-apply"
    session.add(account)
    session.commit()

    conflict = client.post(f"{PROPOSALS}/{proposal_id}/undo", headers=headers)
    assert conflict.status_code == 409 and conflict.json()["code"] == "assistant_undo_conflict"
    session.expire_all()
    assert account.name == "Edited-after-apply"
    assert _counts(session, user) == (1, 1)  # only the apply audit exists


# ---------------------------------------------------------------- 7: tenancy


def test_tenancy_scopes_proposals_history_and_context(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = _user(session)
    bob = _user(session)
    alice_account = _account(session, alice, name="Alice-Private-Account")
    _account(session, bob, name="Bob-Account")
    _stub(
        monkeypatch,
        _proposal("account.update", f"account:{alice_account.id}", {"name": "Renamed"}),
    )
    alice_headers = _headers(alice)
    bob_headers = _headers(bob)
    proposal_id = client.post(ASK, headers=alice_headers, json={"question": "rename"}).json()[
        "proposal"
    ]["id"]

    # Bob cannot read, apply, dismiss or undo Alice's proposal
    for method, path in (
        ("get", f"{PROPOSALS}/{proposal_id}"),
        ("post", f"{PROPOSALS}/{proposal_id}/apply"),
        ("post", f"{PROPOSALS}/{proposal_id}/dismiss"),
        ("post", f"{PROPOSALS}/{proposal_id}/undo"),
    ):
        resp = getattr(client, method)(path, headers=bob_headers)
        assert resp.status_code == 404, (method, path, resp.text)
        assert resp.json()["code"] == "assistant_proposal_not_found"

    # Bob's history and open list never show it; Alice's do — Alice applies
    # first (History lists applied/undone cards, not pending ones)
    assert client.get(HISTORY, headers=bob_headers).json()["items"] == []
    assert client.get(PROPOSALS, headers=bob_headers).json()["items"] == []
    assert client.post(f"{PROPOSALS}/{proposal_id}/apply", headers=alice_headers).status_code == 200
    assert [i["id"] for i in client.get(HISTORY, headers=alice_headers).json()["items"]] == [
        proposal_id
    ]
    assert client.get(HISTORY, headers=bob_headers).json()["items"] == []

    # and Bob's ask never saw Alice's account name (context is tenant-scoped)
    bob_stub = _stub(
        monkeypatch,
        {
            "answer": "one account",
            "proposal_kind": "none",
            "proposal_target": "",
            "proposal_summary": "",
            "proposal_fields": {},
        },
    )
    resp = client.post(ASK, headers=bob_headers, json={"question": "my accounts?"})
    assert resp.status_code == 200
    prompt_text = str(bob_stub.prompts)
    assert "Alice-Private-Account" not in prompt_text
    assert "Bob-Account" in prompt_text


# -------------------------------------------------- 8: gateway policy (H1/H3)


def test_privacy_mode_refuses_before_any_socket(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-loopback destination under privacy mode answers 403 BEFORE the
    transport: `app.llm.base.httpx` is replaced by a sentinel whose `post`
    raises, so reaching the socket fails the test instead of sending."""
    user = _user(session)
    _account(session, user)
    user.privacy_mode = True
    user.llm_provider = "openai_compatible"
    user.llm_base_url = "http://192.0.2.110:4000"  # non-loopback host
    user.llm_model = "test-model"
    session.add(user)
    session.commit()

    reached: list[str] = []

    def _boom(*args: Any, **kwargs: Any) -> None:
        reached.append("httpx.post")
        raise AssertionError("the assistant opened a socket under privacy mode")

    sentinel = SimpleNamespace(post=_boom, HTTPError=Exception)
    monkeypatch.setattr(llm_base, "httpx", sentinel)

    resp = client.post(ASK, headers=_headers(user), json={"question": "hello"})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "privacy_mode_blocks_external_api"
    assert reached == []  # never got to the transport
    assert _counts(session, user) == (0, 0)


# ----------------------------------------------------------------- 9: guards


def test_kind_guards_refuse_at_ask_time(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    account = _account(session, user)
    split = _txn(session, user, account, payee="Split parent")
    split.is_split = True
    split.split_checksum = split.amount_native  # ck_transactions_split_flag/checksum
    session.add(split)
    session.commit()
    groceries = _category(session, user, "Groceries")
    headers = _headers(user)

    # a split parent cannot take a category (docs/44 §5) — folded into refusal
    _stub(
        monkeypatch,
        _proposal("transaction.update", f"transaction:{split.id}", {"category_id": groceries.id}),
    )
    resp = client.post(ASK, headers=headers, json={"question": "categorize"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "assistant_proposal_invalid"
    assert "split" in resp.json()["detail"].lower()
    assert _counts(session, user) == (0, 0)

    # an E2EE tenant's account name lives in ciphertext: the assistant has no
    # vault key, so a rename is refused (docs/57 §2 `encryption_required`)
    user.encryption_required = True
    session.add(user)
    session.commit()
    _stub(monkeypatch, _proposal("account.update", f"account:{account.id}", {"name": "New name"}))
    resp = client.post(ASK, headers=headers, json={"question": "rename"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "assistant_proposal_invalid"
    assert "encryption_required" in resp.json()["detail"]
    assert _counts(session, user) == (0, 0)


def test_staging_guard_refuses_a_pending_batch_transaction(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.import_batches import ImportBatch, ImportBatchStatus

    user = _user(session)
    account = _account(session, user)
    batch = ImportBatch(
        user_id=user.id,
        account_id=account.id,
        institution="Test Bank",
        file_name="pending.csv",
        status=ImportBatchStatus.pending_review,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    txn = _txn(session, user, account, payee="Staged")
    txn.import_batch_id = batch.id
    session.add(txn)
    session.commit()
    groceries = _category(session, user, "Groceries")

    _stub(
        monkeypatch,
        _proposal("transaction.update", f"transaction:{txn.id}", {"category_id": groceries.id}),
    )
    resp = client.post(ASK, headers=_headers(user), json={"question": "categorize"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "assistant_proposal_invalid"
    assert "pending import batch" in resp.json()["detail"]
    assert _counts(session, user) == (0, 0)


def test_demo_tenant_is_read_only_on_the_assistant(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session, email=DEMO_EMAIL)
    _account(session, user)
    _stub(monkeypatch, _proposal("account.update", "account:1", {"name": "x"}))
    headers = _headers(user)

    # every POST is refused by get_current_user's method-scoped demo guard
    for path, json_body in (
        (ASK, {"question": "hello"}),
        (f"{PROPOSALS}/1/apply", None),
        (f"{PROPOSALS}/1/dismiss", None),
        (f"{PROPOSALS}/1/undo", None),
    ):
        resp = client.post(path, headers=headers, json=json_body)
        assert resp.status_code == 403, (path, resp.text)
        assert resp.json()["code"] == "demo_account_restricted"

    # the reads pass through as reads (docs/57 §7)
    assert client.get(HISTORY, headers=headers).status_code == 200
    assert client.get(PROPOSALS, headers=headers).status_code == 200
    assert _counts(session, user) == (0, 0)


# ------------------------------------------------------- open list & history


def test_open_list_defaults_to_pending_and_history_is_newest_first(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    account = _account(session, user)
    headers = _headers(user)

    ids = []
    for name in ("First", "Second"):
        _stub(monkeypatch, _proposal("account.update", f"account:{account.id}", {"name": name}))
        ids.append(
            client.post(ASK, headers=headers, json={"question": f"rename to {name}"}).json()[
                "proposal"
            ]["id"]
        )
    # dismiss the older one, apply the newer one
    client.post(f"{PROPOSALS}/{ids[0]}/dismiss", headers=headers)
    client.post(f"{PROPOSALS}/{ids[1]}/apply", headers=headers)

    open_cards = client.get(PROPOSALS, headers=headers)
    assert open_cards.status_code == 200
    assert open_cards.json()["items"] == []  # default status=pending, both decided
    dismissed = client.get(PROPOSALS, headers=headers, params={"status": "dismissed"})
    assert [i["id"] for i in dismissed.json()["items"]] == [ids[0]]
    bad = client.get(PROPOSALS, headers=headers, params={"status": "banana"})
    assert bad.status_code == 422  # Literal-validated, never silently defaulted

    history = client.get(HISTORY, headers=headers, params={"limit": 10})
    assert [i["id"] for i in history.json()["items"]] == [ids[1]]  # newest first
    assert history.json()["items"][0]["apply_audit_id"] is not None

    # unknown proposal id is 404 with the assistant's own code
    missing = client.get(f"{PROPOSALS}/999999", headers=headers)
    assert missing.status_code == 404 and missing.json()["code"] == "assistant_proposal_not_found"


def test_apply_never_clobbers_a_field_the_card_did_not_touch(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`before`/`after` carry the CHANGED fields only; applying writes exactly
    those and leaves the rest of the row alone (docs/57 §2)."""
    user = _user(session)
    account = _account(session, user, name="Chequing", starting="100.00")
    account.llm_autopilot = True
    session.add(account)
    session.commit()
    _stub(
        monkeypatch,
        _proposal("account.update", f"account:{account.id}", {"starting_balance": "250.5"}),
    )
    headers = _headers(user)
    proposal_id = client.post(ASK, headers=headers, json={"question": "set balance"}).json()[
        "proposal"
    ]["id"]

    assert client.post(f"{PROPOSALS}/{proposal_id}/apply", headers=headers).status_code == 200
    session.expire_all()
    assert account.starting_balance == D("250.50")  # quantized to 0.01, as string-decoded
    assert account.name == "Chequing"  # untouched
    assert account.llm_autopilot is True  # untouched


# --------------------------- 11: the context's committed-only rule (QA F1)
#
# `committed_tx_conditions` is only a WHERE half: its docstring requires the
# caller to outerjoin ImportBatch, or the `status == committed` disjunct runs
# as a table-wide cartesian product — staged rows leak into the model's
# context, every row duplicates once per batch row, and a manual-only tenant
# reads back NOTHING while the batch table is empty (docs/57 §3: "staging
# rows never reach the model").


def _batch(
    session: Session, user: User, account: Account, *, status: ImportBatchStatus, name: str
) -> ImportBatch:
    batch = ImportBatch(
        user_id=user.id,
        account_id=account.id,
        institution="Test Bank",
        file_name=name,
        status=status,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def _staged(session: Session, txn: Transaction, batch: ImportBatch) -> Transaction:
    txn.import_batch_id = batch.id
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _context_txn_ids(session: Session, user: User) -> list[int]:
    """`recent_transactions` ids, failing loudly if build_context got there
    through a cartesian product (the exact defect this section regresses)."""
    session.expire_all()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        context = assistant_service.build_context(session, user)
    cartesian = [str(w.message) for w in caught if "cartesian" in str(w.message).lower()]
    assert not cartesian, f"staging predicate ran without the ImportBatch join: {cartesian}"
    return [row["id"] for row in context["recent_transactions"]]


def test_context_excludes_staged_rows_and_keeps_committed_and_manual(
    session: Session,
) -> None:
    """Staged rows never reach the model; committed-batch and manual rows do,
    and another tenant's batches decide nothing about ours (docs/57 §3)."""
    owner = _user(session)
    account = _account(session, owner)
    foreign = _user(session)
    foreign_account = _account(session, foreign, name="Foreign-Account")

    manual = _txn(session, owner, account, payee="Manual")
    committed_batch = _batch(
        session, owner, account, status=ImportBatchStatus.committed, name="own-committed.csv"
    )
    committed = _staged(session, _txn(session, owner, account, payee="Committed"), committed_batch)
    pending_batch = _batch(
        session, owner, account, status=ImportBatchStatus.pending_review, name="own-pending.csv"
    )
    staged = _staged(session, _txn(session, owner, account, payee="Staged"), pending_batch)

    # another tenant owns batches in BOTH states — the leak this creates on an
    # unjoined statement (any committed batch row anywhere satisfies the
    # disjunct) is exactly QA F1's cross-tenant failure
    for status, name in (
        (ImportBatchStatus.committed, "foreign-committed.csv"),
        (ImportBatchStatus.pending_review, "foreign-pending.csv"),
    ):
        foreign_batch = _batch(session, foreign, foreign_account, status=status, name=name)
        _staged(
            session, _txn(session, foreign, foreign_account, payee=f"Foreign {name}"), foreign_batch
        )
    foreign_manual = _txn(session, foreign, foreign_account, payee="Foreign Manual")

    ids = _context_txn_ids(session, owner)
    assert sorted(ids) == sorted([manual.id, committed.id])
    assert staged.id not in ids
    assert foreign_manual.id not in ids


def test_context_rows_appear_exactly_once_across_many_batches(
    session: Session,
) -> None:
    """No duplication: four batch rows in the table (one ours, three another
    tenant's, three different statuses) must multiply nothing — an unjoined
    statement fans every manual row out once per batch row."""
    owner = _user(session)
    account = _account(session, owner)
    foreign = _user(session)
    foreign_account = _account(session, foreign, name="Foreign-B-Account")

    manual = _txn(session, owner, account, payee="Manual")
    committed_batch = _batch(
        session, owner, account, status=ImportBatchStatus.committed, name="committed.csv"
    )
    first = _staged(session, _txn(session, owner, account, payee="Committed 1"), committed_batch)
    second = _staged(session, _txn(session, owner, account, payee="Committed 2"), committed_batch)

    for i, status in enumerate(
        (ImportBatchStatus.pending_review, ImportBatchStatus.committed, ImportBatchStatus.discarded)
    ):
        foreign_batch = _batch(session, foreign, foreign_account, status=status, name=f"f{i}.csv")
        _staged(
            session, _txn(session, foreign, foreign_account, payee=f"Foreign {i}"), foreign_batch
        )

    ids = _context_txn_ids(session, owner)
    assert sorted(ids) == sorted([manual.id, first.id, second.id])
    assert len(ids) == len(set(ids))


def test_context_manual_rows_survive_an_empty_import_batch_table(
    session: Session,
) -> None:
    """The manual-only tenant: zero import batches of its own, and none
    committed by this test anywhere — its transactions must still reach the
    context (QA F1 scenario A read back 0 of 3)."""
    owner = _user(session)
    account = _account(session, owner)
    txns = [_txn(session, owner, account, payee=f"Manual {i}") for i in range(3)]

    ids = _context_txn_ids(session, owner)
    assert sorted(ids) == sorted(t.id for t in txns)


def test_context_staging_statement_carries_the_import_batch_outerjoin(
    session: Session,
) -> None:
    """Structural pin: every statement build_context issues against
    `transactions` that references `import_batches` must join it — the WHERE
    half alone is a cartesian product, whatever the data happens to contain."""
    owner = _user(session)
    account = _account(session, owner)
    _txn(session, owner, account, payee="Manual")

    captured: list[str] = []

    def _record(orm_execute_state: Any) -> None:
        captured.append(str(orm_execute_state.statement))

    event.listen(session, "do_orm_execute", _record)
    try:
        assistant_service.build_context(session, owner)
    finally:
        event.remove(session, "do_orm_execute", _record)

    staging = [s for s in captured if "FROM transactions" in s and "import_batches" in s]
    assert staging, f"no batch-scoped transaction statement captured: {captured}"
    unjoined = [s for s in staging if "JOIN import_batches" not in s]
    assert not unjoined, f"statement references import_batches without joining it:\n{unjoined[0]}"

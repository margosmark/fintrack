"""Pipeline contract: layer precedence, learning loop, batching, autopilot, degradation."""

from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.llm.base import ResolvedLlm
from app.models.learned_payees import LearnedPayee
from app.models.llm_suggestions import LlmSuggestion, SuggestionStatus
from app.models.rules import Rule, RuleMatchType
from app.services import categorization
from tests.test_cat_common import (
    auth,
    autopilot_account,
    background_session_fixture,
    category_by_name,
    create_account,
    fake_provider_fixture,
    make_transaction,
    register_and_login,
)


@pytest.fixture(name="fake_provider")
def _fake_provider(monkeypatch):
    return fake_provider_fixture(monkeypatch)


@pytest.fixture(name="background_session")
def _background_session(monkeypatch, session: Session):
    return background_session_fixture(monkeypatch, session)


RUN = "/api/v1/suggestions/run"


def _suggester(transaction_map: dict[int, int], confidence: float = 0.8):
    """Responder proposing the mapped category for every listed transaction."""

    def respond(messages: list[dict], schema: dict) -> dict:
        user_text = messages[1]["content"]
        ids = [int(line.split(":", 1)[0]) for line in user_text.splitlines()[1:] if ":" in line]
        return {
            "suggestions": [
                {
                    "transaction_id": tid,
                    "category_id": transaction_map[tid],
                    "confidence": confidence,
                }
                for tid in ids
            ]
        }

    return respond


def _setup(client: TestClient) -> tuple[dict, int, int]:
    body = register_and_login(client)
    headers = auth(body["access_token"])
    account_id = create_account(client, headers)
    return headers, body["user"]["id"], account_id


def test_rule_beats_learned_map_when_both_match(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    headers, user_id, account_id = _setup(client)
    rule_cat = category_by_name(session, "groceries", user_id)
    learned_cat = category_by_name(session, "dining", user_id)
    session.add(
        Rule(
            user_id=user_id,
            match_type=RuleMatchType.exact_payee,
            pattern="Metro",
            priority=1,
            category_id=rule_cat.id,
        )
    )
    session.add(LearnedPayee(user_id=user_id, normalized_payee="Metro", category_id=learned_cat.id))
    session.commit()
    txn = make_transaction(session, user_id, account_id, "Metro")

    body = client.post(RUN, json={}, headers=headers)
    assert body.status_code == 202
    assert body.json() == {"queued": 0}  # fully resolved by layers 1-2

    session.refresh(txn)
    assert txn.category_id == rule_cat.id  # rule wins over learned map
    learned = session.exec(
        select(LearnedPayee).where(LearnedPayee.normalized_payee == "Metro")
    ).one()
    assert learned.hit_count == 1  # learned map never consulted -> no bump
    assert session.exec(select(LlmSuggestion)).all() == []  # silent apply, no queue rows
    assert fake_provider.calls == []


def test_accept_then_rerun_categorizes_in_layer2_without_provider_call(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    headers, user_id, account_id = _setup(client)
    cat = category_by_name(session, "transport", user_id)
    txn_a = make_transaction(session, user_id, account_id, "Uber Trip")
    txn_b = make_transaction(session, user_id, account_id, "Uber Trip", day=date(2026, 8, 2))
    fake_provider.responder = _suggester({txn_a.id: cat.id})

    first = client.post(RUN, json={"transaction_ids": [txn_a.id]}, headers=headers)
    assert first.json() == {"queued": 1}
    assert len(fake_provider.calls) == 1

    pending = session.exec(select(LlmSuggestion)).one()
    accepted = client.post(f"/api/v1/suggestions/{pending.id}/accept", headers=headers)
    assert accepted.status_code == 200
    session.refresh(txn_a)
    assert txn_a.category_id == cat.id

    second = client.post(RUN, json={"transaction_ids": [txn_b.id]}, headers=headers)
    assert second.json() == {"queued": 0}  # learned-map hit, nothing for the LLM
    assert len(fake_provider.calls) == 1  # NO further provider call
    session.refresh(txn_b)
    assert txn_b.category_id == cat.id  # layer 2 did it

    learned = session.exec(
        select(LearnedPayee).where(LearnedPayee.normalized_payee == "Uber Trip")
    ).one()
    assert learned.hit_count == 2  # accept created it; layer-2 hit bumped again


def test_reject_excludes_payee_from_few_shot_pool(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    headers, user_id, account_id = _setup(client)
    make_transaction(
        session,
        user_id,
        account_id,
        "Confirmed Store",
        category_id=category_by_name(session, "groceries", user_id).id,
    )
    rejected_txn = make_transaction(
        session,
        user_id,
        account_id,
        "Rejected Cafe",
        day=date(2026, 8, 3),
        category_id=category_by_name(session, "dining", user_id).id,
    )
    session.add(
        LlmSuggestion(
            user_id=user_id,
            transaction_id=rejected_txn.id,
            proposed_category_id=category_by_name(session, "housing", user_id).id,
            confidence=0.7,
            status=SuggestionStatus.rejected,
            model_used="fake-model",
        )
    )
    target = make_transaction(session, user_id, account_id, "New Vendor", day=date(2026, 8, 4))
    client.post(RUN, json={"transaction_ids": [target.id]}, headers=headers)

    system_prompt = fake_provider.calls[0][0][0]["content"]
    user_prompt = fake_provider.calls[0][0][1]["content"]
    assert "Confirmed Store => groceries" in system_prompt  # confirmed example present...
    assert "Rejected Cafe" not in system_prompt  # ...rejected payee excluded from few-shots
    assert "Confirmed Store" not in user_prompt  # categorized rows are never re-sent
    assert "New Vendor" in user_prompt


def test_autopilot_high_confidence_auto_applies(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    headers, user_id, _ = _setup(client)
    account = autopilot_account(session, user_id, enabled=True)
    cat = category_by_name(session, "utilities", user_id)
    txn = make_transaction(session, user_id, account.id, "Hydro One")
    fake_provider.responder = _suggester({txn.id: cat.id}, confidence=0.95)

    body = client.post(RUN, json={"transaction_ids": [txn.id]}, headers=headers)
    assert body.json() == {"queued": 1}

    session.refresh(txn)
    assert txn.category_id == cat.id  # auto-applied
    suggestion = session.exec(select(LlmSuggestion)).one()
    assert suggestion.status == SuggestionStatus.accepted  # identical to manual accept
    learned = session.exec(select(LearnedPayee)).one()
    assert learned.normalized_payee == "Hydro One" and learned.category_id == cat.id


def test_autopilot_off_leaves_suggested_even_at_high_confidence(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    headers, user_id, _ = _setup(client)  # llm_autopilot defaults false
    account_id = create_account(client, headers, name="Plain")
    cat = category_by_name(session, "utilities", user_id)
    txn = make_transaction(session, user_id, account_id, "Enbridge")
    fake_provider.responder = _suggester({txn.id: cat.id}, confidence=0.97)

    client.post(RUN, json={"transaction_ids": [txn.id]}, headers=headers)

    session.refresh(txn)
    assert txn.category_id is None  # gate honored: never auto-applied
    suggestion = session.exec(select(LlmSuggestion)).one()
    assert suggestion.status == SuggestionStatus.suggested
    assert session.exec(select(LearnedPayee)).first() is None


def test_low_confidence_not_auto_applied_on_autopilot_account(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    headers, user_id, _ = _setup(client)
    account = autopilot_account(session, user_id, enabled=True)
    cat = category_by_name(session, "health", user_id)
    txn = make_transaction(session, user_id, account.id, "Shoppers Drug Mart")
    fake_provider.responder = _suggester({txn.id: cat.id}, confidence=0.89)

    client.post(RUN, json={"transaction_ids": [txn.id]}, headers=headers)

    session.refresh(txn)
    assert txn.category_id is None  # below the 0.9 gate even with autopilot on
    suggestion = session.exec(select(LlmSuggestion)).one()
    assert suggestion.status == SuggestionStatus.suggested


def test_more_than_25_uncategorized_spawns_multiple_batches(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    headers, user_id, account_id = _setup(client)
    for i in range(30):
        make_transaction(
            session,
            user_id,
            account_id,
            f"Vendor {i}",
            amount=f"-{i + 1}.00",
            day=date(2026, 8, i % 28 + 1),
        )
    fake_provider.responder = lambda m, s: {"suggestions": []}

    body = client.post(RUN, json={}, headers=headers)
    assert body.status_code == 202
    assert body.json() == {"queued": 2}

    assert len(fake_provider.calls) == 2
    batch_sizes = []
    for messages, schema in fake_provider.calls:
        assert schema == categorization.CATEGORIZATION_SCHEMA
        txn_lines = messages[1]["content"].splitlines()[1:]
        batch_sizes.append(len(txn_lines))
    assert batch_sizes == [25, 5]


def test_invalid_llm_payload_discarded_rows_stay_uncategorized(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    headers, user_id, account_id = _setup(client)
    txn = make_transaction(session, user_id, account_id, "Weird Payee")
    bad_payload = {
        "suggestions": [
            {"transaction_id": txn.id, "category_id": 987654, "confidence": 0.9},  # unknown id
            {"transaction_id": txn.id},  # missing fields
            {"transaction_id": "oops", "category_id": 1, "confidence": 0.5},  # wrong types
            {
                "transaction_id": txn.id,
                "category_id": category_by_name(session, "health", user_id).id,
                "confidence": 1.5,  # out-of-range confidence
            },
            "not-even-a-dict",
        ]
    }
    fake_provider.responder = lambda m, s: bad_payload

    client.post(RUN, json={"transaction_ids": [txn.id]}, headers=headers)

    session.refresh(txn)
    assert txn.category_id is None
    assert session.exec(select(LlmSuggestion)).all() == []


def test_valid_rows_kept_from_mixed_payload(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    headers, user_id, account_id = _setup(client)
    keep = make_transaction(session, user_id, account_id, "Good Row")
    drop = make_transaction(session, user_id, account_id, "Bad Row", day=date(2026, 8, 5))
    health_id = category_by_name(session, "health", user_id).id
    fake_provider.responder = lambda m, s: {
        "suggestions": [
            {"transaction_id": keep.id, "category_id": health_id, "confidence": 0.6},
            {"transaction_id": drop.id, "category_id": 55555555, "confidence": 0.6},
        ]
    }

    client.post(RUN, json={"transaction_ids": [keep.id, drop.id]}, headers=headers)
    session.refresh(keep)
    session.refresh(drop)
    assert keep.category_id is None  # suggest-only: not auto-applied without autopilot
    assert drop.category_id is None  # unknown-id result discarded individually
    suggestions = session.exec(select(LlmSuggestion)).all()
    assert len(suggestions) == 1
    assert suggestions[0].transaction_id == keep.id
    assert suggestions[0].proposed_category_id == health_id
    assert suggestions[0].status == SuggestionStatus.suggested


def test_timeout_retry_then_abandonment_preserves_layers_1_2(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    headers, user_id, account_id = _setup(client)
    ruled_cat = category_by_name(session, "income", user_id)
    session.add(
        Rule(
            user_id=user_id,
            match_type=RuleMatchType.contains,
            pattern="payroll",
            priority=1,
            category_id=ruled_cat.id,
        )
    )
    session.commit()
    payroll = make_transaction(session, user_id, account_id, "ACME Payroll")
    llm_only = make_transaction(
        session, user_id, account_id, "Mystery Merchant", day=date(2026, 8, 6)
    )

    attempts: list[int] = []

    def exploding(messages: list[dict], schema: dict) -> dict:
        attempts.append(1)
        raise httpx.ReadTimeout("upstream timeout")

    fake_provider.responder = exploding

    body = client.post(RUN, json={"transaction_ids": [payroll.id, llm_only.id]}, headers=headers)
    assert body.json() == {"queued": 1}
    assert len(attempts) == 2  # initial attempt + exactly ONE retry

    session.refresh(llm_only)
    session.refresh(payroll)
    assert llm_only.category_id is None  # abandoned batch left uncategorized
    assert payroll.category_id == ruled_cat.id  # layer 1 unaffected by LLM failure
    assert session.exec(select(LlmSuggestion)).all() == []


def test_run_without_provider_degrades_gracefully(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """Provider construction failure -> layers 1-2 unaffected, no LLM calls."""
    headers, user_id, account_id = _setup(client)
    monkeypatch.setattr(
        categorization,
        "build_provider",
        lambda user, settings: ResolvedLlm(None, ""),
    )
    txn = make_transaction(session, user_id, account_id, "No Provider Here")
    body = client.post(RUN, json={"transaction_ids": [txn.id]}, headers=headers)
    assert body.status_code == 202
    assert body.json() == {"queued": 1}
    session.refresh(txn)
    assert txn.category_id is None  # stayed uncategorized, no crash


def test_date_range_scopes_transactions(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    """POST /suggestions/run with from/to only processes transactions in that range."""
    headers, user_id, account_id = _setup(client)

    # Create a rule matching "Groceries" payee
    cat = category_by_name(session, "groceries", user_id)
    session.add(
        Rule(
            user_id=user_id,
            match_type=RuleMatchType.contains,
            pattern="Groceries",
            priority=1,
            category_id=cat.id,
        )
    )
    session.commit()

    # Transactions across two years
    txn_2023 = make_transaction(
        session, user_id, account_id, "Groceries Jan 2023", day=date(2023, 1, 15)
    )
    txn_2024 = make_transaction(
        session, user_id, account_id, "Groceries Jun 2024", day=date(2024, 6, 15)
    )
    txn_2025 = make_transaction(
        session, user_id, account_id, "Groceries Mar 2025", day=date(2025, 3, 10)
    )

    # Run scoped to 2024 only
    resp = client.post(
        RUN,
        json={"from": "2024-01-01", "to": "2024-12-31"},
        headers=headers,
    )
    assert resp.status_code == 202
    assert resp.json()["queued"] == 0  # rule matched inline, no LLM needed

    session.refresh(txn_2023)
    session.refresh(txn_2024)
    session.refresh(txn_2025)
    assert txn_2023.category_id is None  # outside range — untouched
    assert txn_2024.category_id == cat.id  # inside range — rule applied
    assert txn_2025.category_id is None  # outside range — untouched


def test_paired_date_validation(client: TestClient) -> None:
    """POST /suggestions/run with only one of from/to returns 422."""
    headers, _user_id, _account_id = _setup(client)

    # Only from
    resp = client.post(RUN, json={"from": "2024-01-01"}, headers=headers)
    assert resp.status_code == 422

    # Only to
    resp = client.post(RUN, json={"to": "2024-12-31"}, headers=headers)
    assert resp.status_code == 422

    # Both present — succeeds (202 even with zero uncategorized)
    resp = client.post(RUN, json={"from": "2024-01-01", "to": "2024-12-31"}, headers=headers)
    assert resp.status_code == 202


def test_empty_body_backward_compat(
    client: TestClient, session: Session, background_session
) -> None:
    """POST /suggestions/run with empty body still works (all uncategorized)."""
    headers, user_id, account_id = _setup(client)
    make_transaction(session, user_id, account_id, "Unchanged")
    resp = client.post(RUN, json={}, headers=headers)
    assert resp.status_code == 202
    # txn has no rule match, so LLM batches it
    assert resp.json()["queued"] >= 1

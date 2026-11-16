"""Suggestion queue endpoints: joined listing, accept/reject flips, 409 not-pending."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.learned_payees import LearnedPayee
from app.models.llm_suggestions import LlmSuggestion, SuggestionStatus
from app.models.transactions import Transaction
from tests.test_cat_common import (
    auth,
    category_by_name,
    create_account,
    make_transaction,
    register_and_login,
)


def _seed_suggestion(client: TestClient, session: Session) -> tuple[dict, int, int, int]:
    body = register_and_login(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    account_id = create_account(client, headers)
    txn = make_transaction(session, user_id, account_id, "Fresh Mart")
    cat = category_by_name(session, "groceries", user_id)
    session.add(
        LlmSuggestion(
            user_id=user_id,
            transaction_id=txn.id,
            proposed_category_id=cat.id,
            confidence=0.82,
            model_used="fake-model",
        )
    )
    session.commit()
    suggestion = session.exec(select(LlmSuggestion)).one()
    return headers, suggestion.id, txn.id, user_id


def test_queue_listing_joins_payee_and_category_name(client: TestClient, session: Session) -> None:
    headers, suggestion_id, _, _ = _seed_suggestion(client, session)
    response = client.get("/api/v1/suggestions?status=suggested", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1 and len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == suggestion_id
    assert item["payee"] == "Fresh Mart"
    assert item["category_name"] == "groceries"
    assert item["status"] == "suggested"
    assert abs(item["confidence"] - 0.82) < 1e-9


def test_accept_applies_category_and_writes_learned_map(
    client: TestClient, session: Session
) -> None:
    headers, suggestion_id, txn_id, user_id = _seed_suggestion(client, session)
    accepted = client.post(f"/api/v1/suggestions/{suggestion_id}/accept", headers=headers)
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"

    refreshed = session.exec(select(Transaction).where(Transaction.id == txn_id)).one()
    assert refreshed.category_id == category_by_name(session, "groceries", user_id).id

    learned = session.exec(select(LearnedPayee)).one()
    assert learned.normalized_payee == "Fresh Mart" and learned.hit_count == 1


def test_repeat_accept_and_missing_suggestion_409(client: TestClient, session: Session) -> None:
    headers, suggestion_id, _, _ = _seed_suggestion(client, session)
    first = client.post(f"/api/v1/suggestions/{suggestion_id}/accept", headers=headers)
    assert first.status_code == 200
    second = client.post(f"/api/v1/suggestions/{suggestion_id}/accept", headers=headers)
    assert second.status_code == 409
    assert second.json()["code"] == "suggestion_not_pending"

    # Foreign/absent ids are indistinguishable by design: 404 not_found
    # (security-hardening; was 409).
    absent = client.post("/api/v1/suggestions/999999/accept", headers=headers)
    assert absent.status_code == 404
    assert absent.json()["code"] == "not_found"


def test_reject_flips_status_records_negative_signal(client: TestClient, session: Session) -> None:
    headers, suggestion_id, _, _ = _seed_suggestion(client, session)
    response = client.post(f"/api/v1/suggestions/{suggestion_id}/reject", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"

    row = session.exec(select(LlmSuggestion)).one()
    assert row.status == SuggestionStatus.rejected
    # Negative signal recorded; no learned entry written on reject.
    assert session.exec(select(LearnedPayee)).first() is None


def test_reject_after_accept_409(client: TestClient, session: Session) -> None:
    headers, suggestion_id, _, _ = _seed_suggestion(client, session)
    assert (
        client.post(f"/api/v1/suggestions/{suggestion_id}/accept", headers=headers).status_code
        == 200
    )
    rejected = client.post(f"/api/v1/suggestions/{suggestion_id}/reject", headers=headers)
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "suggestion_not_pending"


def test_suggestions_require_auth(client: TestClient) -> None:
    assert client.post("/api/v1/suggestions/run", json={}).status_code == 401
    assert client.get("/api/v1/suggestions").status_code == 401

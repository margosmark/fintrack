"""The learning loop: a human's correction teaches the learned rule (docs/04).

Kilter's model adopted in docs/04 §Learning loop — transactions arrive
categorized and the rules learn from the corrections. What this pins:

* a category correction writes/updates the `(payee -> category)` rule of
  layer 2 and stamps the edit receipt with `learned`, so the History drawer
  can say where the rule came from ("learned from you");
* re-running the pipeline over the corrected history applies that rule to the
  rows still waiting — the loop closes without a network call;
* a merchant correction teaches the rule for the row's CURRENT category;
* nothing that is not a correction teaches anything: clearing a category never
  invents a rule (and never wipes one either), and an unrelated edit never
  bumps `hit_count`.

No network: the pipeline's provider seam is the suite's fake, and every case
below asserts `queued == 0` / an empty `fake_provider.calls`, so the learned
map — not the LLM — does the work.
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.learned_payees import LearnedPayee
from tests.test_cat_common import (
    auth,
    background_session_fixture,
    category_by_name,
    create_account,
    fake_provider_fixture,
    make_transaction,
    register_and_login,
)

RUN = "/api/v1/suggestions/run"
NETFLIX_A = date(2026, 8, 1)
NETFLIX_B = date(2026, 8, 5)


@pytest.fixture(name="fake_provider")
def _fake_provider(monkeypatch):
    return fake_provider_fixture(monkeypatch)


@pytest.fixture(name="background_session")
def _background_session(monkeypatch, session: Session):
    return background_session_fixture(monkeypatch, session)


def _setup(client: TestClient) -> tuple[dict, int, int]:
    body = register_and_login(client)
    headers = auth(body["access_token"])
    return headers, body["user"]["id"], create_account(client, headers)


def _events(client: TestClient, transaction_id: int, headers: dict) -> list[dict]:
    body = client.get(f"/api/v1/transactions/{transaction_id}/events", headers=headers)
    assert body.status_code == 200, body.text
    payload = body.json()
    return payload["items"] if isinstance(payload, dict) else payload


def _rule(session: Session, user_id: int, payee: str) -> LearnedPayee | None:
    return session.exec(
        select(LearnedPayee).where(
            LearnedPayee.user_id == user_id, LearnedPayee.normalized_payee == payee
        )
    ).first()


def test_category_correction_writes_the_rule_and_stamps_the_receipt(
    client: TestClient, session: Session
) -> None:
    headers, user_id, account_id = _setup(client)
    subs = category_by_name(session, "entertainment", user_id)
    txn = make_transaction(session, user_id, account_id, "Netflix", day=NETFLIX_A)
    assert txn.category_id is None

    response = client.patch(
        f"/api/v1/transactions/{txn.id}", json={"category_id": subs.id}, headers=headers
    )
    assert response.status_code == 200, response.text

    learned = _rule(session, user_id, "Netflix")
    assert learned is not None
    assert learned.category_id == subs.id
    assert learned.hit_count == 1  # written once, by the correction itself

    # `record_edit_event` calls a moved category `categorized`, never `edited`
    # (docs/02 §transaction_events) — the receipt kind the drawer shows.
    receipts = [e for e in _events(client, txn.id, headers) if e["event_kind"] == "categorized"]
    assert receipts, "the correction must be receipted"
    assert receipts[0]["payload"]["learned"] == {
        "normalized_payee": "Netflix",
        "category_id": subs.id,
    }
    # The receipt stays a receipt: the marker rides ALONGSIDE the change, it
    # does not replace it.
    assert receipts[0]["payload"]["changes"] == {"category_id": subs.id}
    assert receipts[0]["payload"]["fields"] == ["category_id"]
    assert receipts[0]["actor"] is not None  # a HUMAN made this correction


def test_rerun_applies_the_learned_rule_to_the_corrected_history(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    """The closed loop: correct one row, the next run categorizes the rest."""
    headers, user_id, account_id = _setup(client)
    subs = category_by_name(session, "entertainment", user_id)
    already_seen = make_transaction(session, user_id, account_id, "Netflix", day=NETFLIX_A)
    still_waiting = make_transaction(session, user_id, account_id, "Netflix", day=NETFLIX_B)

    client.patch(
        f"/api/v1/transactions/{already_seen.id}",
        json={"category_id": subs.id},
        headers=headers,
    )

    response = client.post(RUN, json={"transaction_ids": [still_waiting.id]}, headers=headers)
    assert response.status_code == 202, response.text
    assert response.json() == {"queued": 0}  # layers 1-2 fully resolved it

    session.refresh(still_waiting)
    assert still_waiting.category_id == subs.id

    learned = _rule(session, user_id, "Netflix")
    assert learned is not None
    assert learned.hit_count == 2  # the pipeline's apply confirms the same rule

    # And the apply is receipted as a PIPELINE write (actor null, `via` learned)
    # — distinct from the human correction that taught it.
    applies = [
        e for e in _events(client, still_waiting.id, headers) if e["event_kind"] == "categorized"
    ]
    assert applies and applies[0]["actor"] is None
    assert applies[0]["payload"]["via"] == "learned"

    assert fake_provider.calls == []  # no LLM needed to honor a learned rule


def test_merchant_correction_teaches_the_rule_for_the_current_category(
    client: TestClient, session: Session
) -> None:
    headers, user_id, account_id = _setup(client)
    subs = category_by_name(session, "entertainment", user_id)
    txn = make_transaction(
        session, user_id, account_id, "Netfl1x", category_id=subs.id, day=NETFLIX_A
    )

    response = client.patch(
        f"/api/v1/transactions/{txn.id}", json={"normalized_payee": "Netflix"}, headers=headers
    )
    assert response.status_code == 200, response.text

    learned = _rule(session, user_id, "Netflix")
    assert learned is not None
    assert learned.category_id == subs.id
    assert learned.hit_count == 1


def test_clearing_a_category_never_writes_or_wipes_a_rule(
    client: TestClient, session: Session
) -> None:
    headers, user_id, account_id = _setup(client)
    subs = category_by_name(session, "entertainment", user_id)
    taught = make_transaction(session, user_id, account_id, "Netflix", day=NETFLIX_A)
    client.patch(
        f"/api/v1/transactions/{taught.id}", json={"category_id": subs.id}, headers=headers
    )

    cleared = make_transaction(session, user_id, account_id, "Hulu", day=NETFLIX_B)
    response = client.patch(
        f"/api/v1/transactions/{cleared.id}", json={"category_id": None}, headers=headers
    )
    assert response.status_code == 200, response.text

    # Nothing invented for the cleared row...
    assert _rule(session, user_id, "Hulu") is None
    # ...and the rule the user already taught survives untouched.
    learned = _rule(session, user_id, "Netflix")
    assert learned is not None
    assert learned.category_id == subs.id
    assert learned.hit_count == 1


def test_unrelated_edit_never_bumps_hit_count(client: TestClient, session: Session) -> None:
    """`hit_count` means "this mapping was confirmed", not "this row was touched"."""
    headers, user_id, account_id = _setup(client)
    subs = category_by_name(session, "entertainment", user_id)
    txn = make_transaction(session, user_id, account_id, "Netflix", day=NETFLIX_A)
    client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": subs.id}, headers=headers)
    learned = _rule(session, user_id, "Netflix")
    assert learned is not None
    assert learned.hit_count == 1

    response = client.patch(
        f"/api/v1/transactions/{txn.id}", json={"date": "2026-08-03"}, headers=headers
    )
    assert response.status_code == 200, response.text

    session.refresh(learned)
    assert learned.hit_count == 1

    # The row's receipts are newest-first: the date edit is the head, and IT
    # is the receipt that must carry no marker — the category correction
    # below it taught the rule and keeps its own.
    newest = _events(client, txn.id, headers)[0]
    assert newest["event_kind"] == "edited"
    assert newest["payload"]["fields"] == ["date"]
    assert "learned" not in newest["payload"], "a non-correction must claim it taught nothing"
    assert "learned" in _events(client, txn.id, headers)[1]["payload"]

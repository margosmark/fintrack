"""docs/57 §3b/§4, docs/59 §8 — every assistant write route is human-paced (F3).

Security t_108c24f3 (comment 907) F3: `POST /assistant/proposals` carried no
limiter at all while docs/59 §8 claimed it "inherits `POST /assistant/ask`'s
middleware posture" — slowapi decorates PER ROUTE and `app/main.py` has no
rate-limit middleware, so there was nothing to inherit. Measured on the
candidate: 30 rapid proposes → `{201: 30}`, zero `429`.

The fix gives the propose route and the three transitions their own
`human_paced_limit` (the same 12/minute ceiling as ask — one person, or one
connector acting for a person, does not apply a card thirteen times a
minute), and the docs now say exactly that.

Two follow-ups on the same seam (t_828c243a):

* **F3-a** — the three transitions sat in THREE per-route 12/minute buckets
  (36 writes a minute, and a loop needed three windows to exhaust the route)
  while every doc claimed one shared bucket. They now share one named scope.
* **F3-b** — the propose bucket was keyed on the client IP, and the MCP
  server's calls arrive from the platform's own container address, so every
  tenant on the instance shared ONE propose ceiling. It now also runs a
  per-authenticated-tenant bucket that no client-supplied header can reset.

Each test drives the real limiter (`limiter.enabled = True`), because a
decorator that is present but never enforced is the failure mode worth
preventing (tests/test_credential_rate_limits.py pattern).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.ratelimit import (
    ASSISTANT_PROPOSE_LIMIT,
    ASSISTANT_TRANSITION_LIMIT,
    limiter,
)
from tests.test_assistant import _account, _category, _headers, _txn, _user
from tests.test_byo_ai_connector import _body

API = "/api/v1"
PROPOSALS = f"{API}/assistant/proposals"
RATE_ENVELOPE = {"detail": "Too many attempts", "code": "rate_limited"}


@pytest.fixture
def rate_limited():
    limiter.enabled = True
    try:
        yield
    finally:
        limiter.enabled = False
        limiter.reset()


def _burst(limit: str) -> int:
    """Attempts allowed before the limiter answers 429 — parsed, not pinned,
    so bumping the ceiling in `core/ratelimit.py` cannot silently turn these
    tests into a 429-loop that fails for the wrong reason."""
    return int(limit.split("/")[0])


def _card(client: TestClient, session: Session, user: Any) -> int:
    """One real pending card FOR `user` (rows roll back with `session`)."""
    account = _account(session, user)
    txn = _txn(session, user, account)
    category = _category(session, user, "Groceries")
    resp = client.post(
        PROPOSALS,
        headers=_headers(user),
        json=_body(target=f"transaction:{txn.id}", fields={"category_id": category.id}),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_propose_route_is_rate_limited(client: TestClient, session: Session, rate_limited) -> None:
    """The finding itself: a burst of proposes must hit 429, not 30x201."""
    user = _user(session)
    account = _account(session, user)
    txn = _txn(session, user, account)
    category = _category(session, user, "Groceries")
    payload = _body(target=f"transaction:{txn.id}", fields={"category_id": category.id})

    statuses = []
    for _ in range(_burst(ASSISTANT_PROPOSE_LIMIT)):
        resp = client.post(PROPOSALS, headers=_headers(user), json=payload)
        statuses.append(resp.status_code)
    assert statuses == [201] * _burst(ASSISTANT_PROPOSE_LIMIT), statuses

    limited = client.post(PROPOSALS, headers=_headers(user), json=payload)
    assert limited.status_code == 429, limited.text
    assert limited.json() == RATE_ENVELOPE


def test_transition_routes_are_rate_limited(
    client: TestClient, session: Session, rate_limited
) -> None:
    """apply/dismiss/undo carry their own ceiling (defense in depth for F1:
    the transition body runs in a worker thread now, so a hammering client
    must not be able to occupy threads without a ceiling either)."""
    user = _user(session)
    proposal_id = _card(client, session, user)
    headers = _headers(user)

    statuses = []
    for _ in range(_burst(ASSISTANT_TRANSITION_LIMIT)):
        resp = client.post(f"{PROPOSALS}/{proposal_id}/apply", headers=headers)
        statuses.append(resp.status_code)
    # First wins the row (200); the rest are refused by the status check
    # (409) — every attempt still consumes the bucket, which is the point.
    assert statuses == [200] + [409] * (_burst(ASSISTANT_TRANSITION_LIMIT) - 1), statuses

    limited = client.post(f"{PROPOSALS}/{proposal_id}/apply", headers=headers)
    assert limited.status_code == 429, limited.text
    assert limited.json() == RATE_ENVELOPE


def test_transitions_share_one_bucket_across_all_three_routes(
    client: TestClient, session: Session, rate_limited
) -> None:
    """QA F4 / security F3-a (t_828c243a): ONE bucket for apply+dismiss+undo.

    Three per-route 12/minute buckets are 36 writes a minute through the UI
    and a loop needs three windows to exhaust the route. On the candidate the
    docs claimed a shared bucket while `apply` answered `429` and `dismiss`
    still returned `200` — the same defect QA and security both filed.
    """
    user = _user(session)
    headers = _headers(user)
    cap = _burst(ASSISTANT_TRANSITION_LIMIT)
    first = _card(client, session, user)
    other = _card(client, session, user)

    # Fill the bucket on `apply`: the first wins the card (200), the rest are
    # refused by the status check (409) — every attempt still consumes it.
    statuses = [
        client.post(f"{PROPOSALS}/{first}/apply", headers=headers).status_code for _ in range(cap)
    ]
    assert statuses == [200] + [409] * (cap - 1), statuses

    # The other two routes must be held by the SAME bucket now.
    dismiss = client.post(f"{PROPOSALS}/{other}/dismiss", headers=headers)
    assert dismiss.status_code == 429, dismiss.text
    assert dismiss.json() == RATE_ENVELOPE

    undo = client.post(f"{PROPOSALS}/{other}/undo", headers=headers)
    assert undo.status_code == 429, undo.text
    assert undo.json() == RATE_ENVELOPE


def test_propose_bucket_is_per_authenticated_tenant(
    client: TestClient, session: Session, rate_limited
) -> None:
    """Security F3-b (t_95eb8caf): one tenant's burst is not another's.

    The bucket was keyed on the client IP alone, and the MCP server calls the
    API from the platform's own container address, so every tenant on the
    instance shared ONE propose ceiling — a looping connector answered 429
    for everybody else's first propose (measured live in the gate's g6
    probe). The tenant key comes from the verified token, never a header.
    """
    a = _user(session)
    b = _user(session)
    cap = _burst(ASSISTANT_PROPOSE_LIMIT)

    def payload_for(user: Any) -> dict[str, Any]:
        account = _account(session, user)
        txn = _txn(session, user, account)
        category = _category(session, user, "Groceries")
        return _body(target=f"transaction:{txn.id}", fields={"category_id": category.id})

    payload_a = payload_for(a)
    payload_b = payload_for(b)

    statuses = [
        client.post(PROPOSALS, headers=_headers(a), json=payload_a).status_code for _ in range(cap)
    ]
    assert statuses == [201] * cap, statuses
    exhausted = client.post(PROPOSALS, headers=_headers(a), json=payload_a)
    assert exhausted.status_code == 429, exhausted.text

    # Tenant A is spent; tenant B still has a full budget of its own.
    other = client.post(PROPOSALS, headers=_headers(b), json=payload_b)
    assert other.status_code == 201, other.text


def test_propose_bucket_cannot_be_reset_with_a_fresh_forwarded_for(
    client: TestClient, session: Session, rate_limited
) -> None:
    """A fresh X-Forwarded-For per call resets the per-IP ceiling, never the tenant's.

    The per-client-IP key reads the trusted hop's value, so a caller minting
    a new hop per request used to get a brand-new 12/minute bucket every time
    (30 rapid proposes with a fresh XFF → 201 on the candidate). The
    authenticated-tenant bucket is keyed on the token and cannot move.
    """
    user = _user(session)
    account = _account(session, user)
    txn = _txn(session, user, account)
    category = _category(session, user, "Groceries")
    payload = _body(target=f"transaction:{txn.id}", fields={"category_id": category.id})
    cap = _burst(ASSISTANT_PROPOSE_LIMIT)

    statuses = []
    for i in range(cap + 1):
        headers = {**_headers(user), "X-Forwarded-For": f"198.51.100.{100 + i}"}
        statuses.append(client.post(PROPOSALS, headers=headers, json=payload).status_code)
    assert statuses == [201] * cap + [429], statuses

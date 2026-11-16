"""`GET /subscriptions` — RE-MOUNTED (M137 P1-2, docs/33 §Server-first computing).

M48-S3 deleted this router and had the browser compute the report over a
full-ledger download; `services/subscriptions.py` was never removed — it kept
serving `bill_reminders`' cash-flow calendar as the reference the TS port was
pinned to. This card deliberately REVERSES the removal (docs/33 A10): the
route is the same engine exposed again, so the answer is the server's.

The two behaviours that moved to `frontend/tests/s3ClientCompute.test.ts`
(`posting_date=None` falls back to `date`; `posting_date` still wins for gap
math) lived there until M160/P2-5 deleted the client twin with `s3Engine` — the
`subscriptions.detect` golden is now replayed against this route by
`tests/test_m160_replay_s3.py`. What is pinned HERE: the route
answers, needs auth, never writes, and is indifferent to `encryption_required`.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient


def _auth(client: TestClient) -> dict:
    email = f"subs-{datetime.now(UTC).timestamp()}@example.com"
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery"},
    )
    r = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_get_subscriptions_answers_with_the_report_shape(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get("/api/v1/subscriptions", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # The removed route's envelope, field for field (docs/08, docs/24).
    assert set(body) == {"subscriptions", "total_monthly", "total_annual"}
    assert isinstance(body["subscriptions"], list)
    assert isinstance(body["total_monthly"], str)
    assert isinstance(body["total_annual"], str)
    for sub in body["subscriptions"]:
        assert {"payee", "frequency", "avg_amount", "annual_cost", "occurrences"} <= set(sub)


def test_get_subscriptions_requires_auth(client: TestClient) -> None:
    # The route EXISTS now, so its auth dependency runs: 401, never 404.
    r = client.get("/api/v1/subscriptions")
    assert r.status_code == 401, r.text


def test_subscriptions_is_read_only(client: TestClient) -> None:
    """Re-mounting a GET must not hand out a write path."""
    headers = _auth(client)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        r = client.request(method, "/api/v1/subscriptions", json={}, headers=headers)
        assert r.status_code in (404, 405), f"{method} matched: {r.status_code} {r.text}"


@pytest.mark.parametrize("encryption_required", [False, True])
def test_subscriptions_is_indifferent_to_encryption_required(
    client: TestClient, encryption_required: bool
) -> None:
    """P1-5 pattern: the detection reads `normalized_payee` (mandatory
    plaintext), so the report must be byte-identical under the write guard."""
    headers = _auth(client)
    enabled = client.patch(
        "/api/v1/users/me",
        json={"encryption_required": encryption_required},
        headers=headers,
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["encryption_required"] is encryption_required

    r = client.get("/api/v1/subscriptions", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"subscriptions", "total_monthly", "total_annual"}
    # An empty tenant stays an empty report — never a lock-shaped refusal.
    assert body["subscriptions"] == []
    # The removed route's own roll-up: an empty sum stays an int, so an empty
    # tenant reads "0" monthly and "0.00" annual (quantized). Assert the
    # VALUE, not the spelling — this test pins the behaviour under
    # `encryption_required`, not the format of zero.
    assert Decimal(body["total_monthly"]) == 0
    assert Decimal(body["total_annual"]) == 0

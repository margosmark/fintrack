"""MCP token lifecycle contract (audit M12) + internal endpoint limits (M9).

Covers: 90-day expiry on create, `last_used_at` stamped on validate, expired
tokens rejected with the DISTINCT `mcp_token_expired` code, REVOCATION
(hard delete → validation fails closed, t_bb0033d2 AC), the 10-active
per-user cap (`mcp_token_cap`), creation rate limit, and rate limits on the
two internet-routed internal endpoints (validate-mcp-token,
internal-issue-token). Rate-limit tests opt the shared limiter in and reset
it, mirroring test_security_hardening.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.ratelimit import limiter
from app.models.mcp_tokens import McpToken, hash_mcp_token

REGISTER = "/api/v1/auth/register"
TOKENS = "/api/v1/auth/mcp-tokens"
VALIDATE = "/api/v1/auth/validate-mcp-token"
ISSUE = "/api/v1/auth/internal-issue-token"

EMAIL = "mcp@example.com"
PASSWORD = "correct-horse-battery"

RATE_ENVELOPE = {"detail": "Too many attempts", "code": "rate_limited"}


def _headers(client: TestClient) -> dict:
    client.post(REGISTER, json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create(client: TestClient, headers: dict, name: str = "cli") -> dict:
    response = client.post(TOKENS, json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# --- M12: expiry, last-used, exposure ---------------------------------------


def test_create_sets_90_day_expiry_and_never_reexposes_token(
    client: TestClient, session: Session
) -> None:
    headers = _headers(client)
    created = _create(client, headers)

    # `policy` joined the shape in M80 (docs/40 §1); `token` is still shown once.
    assert set(created) == {
        "id",
        "name",
        "token",
        "policy",
        "created_at",
        "expires_at",
        "last_used_at",
    }
    assert created["token"] and len(created["token"]) == 64
    assert created["last_used_at"] is None
    created_at = datetime.fromisoformat(created["created_at"])
    expires_at = datetime.fromisoformat(created["expires_at"])
    delta = expires_at - created_at
    # 90 days +/- leeway for app-clock vs DB-clock skew.
    assert timedelta(days=89) <= delta <= timedelta(days=91)

    # The list view exposes the lifecycle columns but never the raw token.
    listing = client.get(TOKENS, headers=headers)
    assert listing.status_code == 200
    item = listing.json()["items"][0]
    assert set(item) == {
        "id",
        "name",
        "policy",
        "created_at",
        "expires_at",
        "last_used_at",
    }
    assert item["expires_at"] is not None

    # The stored row keeps only the hash.
    row = session.exec(select(McpToken).where(McpToken.id == created["id"])).one()
    assert row.token_hash == hash_mcp_token(created["token"])


def test_validate_stamps_last_used_at(client: TestClient, session: Session) -> None:
    headers = _headers(client)
    created = _create(client, headers)
    assert created["last_used_at"] is None

    ok = client.post(VALIDATE, headers={"Authorization": f"Bearer {created['token']}"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["email"] == EMAIL

    row = session.exec(select(McpToken).where(McpToken.id == created["id"])).one()
    assert row.last_used_at is not None
    assert row.expires_at is not None  # still the original expiry

    # Missing / unknown bearer tokens stay 401 with their own codes.
    missing = client.post(VALIDATE)
    assert missing.status_code == 401
    assert missing.json()["code"] == "missing_token"
    unknown = client.post(VALIDATE, headers={"Authorization": "Bearer nonexistent"})
    assert unknown.status_code == 401
    assert unknown.json()["code"] == "invalid_mcp_token"


def test_expired_token_rejected_with_distinct_code(client: TestClient, session: Session) -> None:
    headers = _headers(client)
    created = _create(client, headers)

    row = session.exec(select(McpToken).where(McpToken.id == created["id"])).one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.add(row)
    session.commit()

    expired = client.post(VALIDATE, headers={"Authorization": f"Bearer {created['token']}"})
    assert expired.status_code == 401
    assert expired.json() == {
        "detail": "MCP token expired",
        "code": "mcp_token_expired",  # DISTINCT from invalid_mcp_token
    }

    # No usage stamped on rejection.
    session.refresh(row)
    assert row.last_used_at is None

    # Expired tokens can still be revoked by their owner.
    delete = client.delete(f"{TOKENS}/{created['id']}", headers=headers)
    assert delete.status_code == 204


def test_revoked_token_is_hard_deleted_and_never_validates_again(
    client: TestClient, session: Session
) -> None:
    """Revoke fails closed: the row is gone, so validation cannot resurrect it.

    t_bb0033d2 AC "revoke test" — a BYO-AI connector's token must die the
    moment the user revokes it, not at its 90-day expiry.
    """
    headers = _headers(client)
    created = _create(client, headers, name="byo-ai")

    ok = client.post(VALIDATE, headers={"Authorization": f"Bearer {created['token']}"})
    assert ok.status_code == 200, ok.text

    revoked = client.delete(f"{TOKENS}/{created['id']}", headers=headers)
    assert revoked.status_code == 204, revoked.text
    # Hard delete — no soft-revived row left for validation to find.
    assert session.get(McpToken, created["id"]) is None
    listing = client.get(TOKENS, headers=headers)
    assert listing.json() == {"items": [], "total": 0}

    rejected = client.post(VALIDATE, headers={"Authorization": f"Bearer {created['token']}"})
    assert rejected.status_code == 401
    assert rejected.json() == {
        "detail": "Invalid MCP token",
        # Not mcp_token_expired: the row itself is gone.
        "code": "invalid_mcp_token",
    }


def test_read_propose_token_mints_validates_and_stores_under_the_wider_check(
    client: TestClient, session: Session
) -> None:
    """The BYO-AI connector scope (docs/59 §1): mint → validate round trip.

    The insert itself proves migration 0060's widened CHECK accepts the
    value; validation echoes `read_propose` back — the one field the MCP
    seam filters the toolset from.
    """
    headers = _headers(client)
    minted = client.post(
        TOKENS, json={"name": "chatgpt", "policy": "read_propose"}, headers=headers
    )
    assert minted.status_code == 201, minted.text
    assert minted.json()["policy"] == "read_propose"

    ok = client.post(VALIDATE, headers={"Authorization": f"Bearer {minted.json()['token']}"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["policy"] == "read_propose"

    row = session.get(McpToken, minted.json()["id"])
    assert row is not None
    assert row.policy == "read_propose"


# --- M12: per-user active cap ------------------------------------------------


def test_active_token_cap_enforced_and_expired_rows_free_slots(
    client: TestClient, session: Session
) -> None:
    headers = _headers(client)
    ids = [_create(client, headers, name=f"cli-{i}")["id"] for i in range(10)]

    capped = client.post(TOKENS, json={"name": "one-too-many"}, headers=headers)
    assert capped.status_code == 400
    assert capped.json() == {
        "detail": "Active MCP token limit of 10 reached; revoke a token before creating another",
        "code": "mcp_token_cap",
    }

    # Expiring one row frees a slot (the cap counts ACTIVE tokens only).
    row = session.get(McpToken, ids[0])
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    session.add(row)
    session.commit()

    after = _create(client, headers, name="replacement")
    assert after["id"] not in ids


# --- M12/M9: rate limits ------------------------------------------------------


def test_create_rate_limit_engages(client: TestClient) -> None:
    headers = _headers(client)
    limiter.enabled = True
    try:
        for i in range(10):
            assert _create(client, headers, name=f"burst-{i}")["id"]
        limited = client.post(TOKENS, json={"name": "burst-11"}, headers=headers)
        assert limited.status_code == 429
        assert limited.json() == RATE_ENVELOPE
    finally:
        limiter.enabled = False
        limiter.reset()


def test_internal_endpoints_rate_limited(client: TestClient) -> None:
    """M9: both internet-routed internal endpoints are bounded (120/min each,
    separate per-path buckets) instead of unlimited."""
    limiter.enabled = True
    try:
        probe = {"Authorization": "Bearer nonexistent-token"}
        for _ in range(120):
            response = client.post(VALIDATE, headers=probe)
            assert response.status_code == 401  # oracle answers, bounded
        limited = client.post(VALIDATE, headers=probe)
        assert limited.status_code == 429
        assert limited.json() == RATE_ENVELOPE

        # internal-issue-token: fail-closed 403 until the bucket is spent.
        for _ in range(120):
            response = client.post(ISSUE, json={"user_id": 1})
            assert response.status_code == 403
            assert response.json()["code"] == "forbidden"
        limited = client.post(ISSUE, json={"user_id": 1})
        assert limited.status_code == 429
        assert limited.json() == RATE_ENVELOPE
    finally:
        limiter.enabled = False
        limiter.reset()

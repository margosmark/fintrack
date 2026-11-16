"""Self-service credential + shared-FX-write rate limits (docs/38, docs/07).

These are the two places where an AUTHENTICATED caller could reach an
expensive or unbounded write with no ceiling at all:

* `POST /users/me/change-password`, `PATCH /users/me/profile` and
  `DELETE /users/me` each run a full bcrypt-cost `verify_password` against the
  CURRENT password. Without a limit, a stolen-but-undetected access token buys
  an unthrottled oracle over the account's existing password — the exact hole
  the login limiter exists to close, left open on the far side of
  authentication.

* `fx_rates` is the only table with no `user_id` (docs/02): it is the shared
  cross-tenant cache that every tenant's valuations read. `POST /fx` is the
  only write path any tenant has into it, and it was unthrottled and accepted
  unbounded currency codes.

Each test drives the real limiter (`limiter.enabled = True`), because a
decorator that is present but never enforced is the failure mode worth
preventing.
"""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.ratelimit import CREDENTIAL_MUTATE_LIMIT, FX_WRITE_LIMIT, limiter

RATE_ENVELOPE = {"detail": "Too many attempts", "code": "rate_limited"}


@pytest.fixture
def rate_limited():
    limiter.enabled = True
    try:
        yield
    finally:
        limiter.enabled = False
        limiter.reset()


def _max_burst() -> int:
    """Attempts allowed before the limiter answers 429.

    Parsed from the limit string rather than hardcoded, so bumping the
    threshold in `core/ratelimit.py` does not silently turn this test into a
    429-loop that fails for the wrong reason.
    """
    return int(CREDENTIAL_MUTATE_LIMIT.split("/")[0])


def test_change_password_is_rate_limited(client: TestClient, rate_limited) -> None:
    from tests.test_deep_common import auth, register

    body = register(client)
    headers = auth(body["access_token"])

    # Wrong password every time: it still pays the full bcrypt cost, which is
    # exactly what the ceiling exists to bound.
    for _ in range(_max_burst()):
        r = client.post(
            "/api/v1/users/me/change-password",
            headers=headers,
            json={"current_password": "wrong-on-purpose", "new_password": "a-new-password-1234"},
        )
        assert r.status_code == 401, r.text
        assert r.json()["code"] == "invalid_credentials"

    limited = client.post(
        "/api/v1/users/me/change-password",
        headers=headers,
        json={"current_password": "wrong-on-purpose", "new_password": "a-new-password-1234"},
    )
    assert limited.status_code == 429
    assert limited.json() == RATE_ENVELOPE


def test_profile_update_is_rate_limited(client: TestClient, rate_limited) -> None:
    from tests.test_deep_common import auth, register

    body = register(client)
    headers = auth(body["access_token"])

    for _ in range(_max_burst()):
        # `email` MUST be present: the route only verifies a password when the
        # email actually changes, so a name-only patch never reaches the
        # expensive hash and would not test the thing this limit protects.
        r = client.patch(
            "/api/v1/users/me/profile",
            headers=headers,
            json={"email": "moved@example.com", "current_password": "wrong"},
        )
        assert r.status_code == 401, r.text
        assert r.json()["code"] == "invalid_credentials"

    limited = client.patch(
        "/api/v1/users/me/profile",
        headers=headers,
        json={"email": "moved@example.com", "current_password": "wrong"},
    )
    assert limited.status_code == 429
    assert limited.json() == RATE_ENVELOPE


def test_delete_account_is_rate_limited(client: TestClient, rate_limited) -> None:
    from tests.test_deep_common import auth, register

    body = register(client)
    headers = auth(body["access_token"])

    for _ in range(_max_burst()):
        r = client.request(
            "DELETE", "/api/v1/users/me", headers=headers, json={"current_password": "wrong"}
        )
        assert r.status_code == 401, r.text

    limited = client.request(
        "DELETE", "/api/v1/users/me", headers=headers, json={"current_password": "wrong"}
    )
    assert limited.status_code == 429
    assert limited.json() == RATE_ENVELOPE


def test_manual_fx_write_is_rate_limited(client: TestClient, rate_limited) -> None:
    """The shared cross-tenant cache needs a ceiling on its only write path."""
    from tests.test_deep_common import auth, register

    body = register(client)
    headers = auth(body["access_token"])

    burst = int(FX_WRITE_LIMIT.split("/")[0])
    # A distinct PAST date per call. `POST /fx` is create-once (first writer
    # wins, 409 on a repeat key), not an upsert — so reusing a date would 409
    # long before the limiter ever fired, and the test would pass for the
    # wrong reason.
    today = date.today()
    for offset in range(burst):
        r = client.post(
            "/api/v1/fx",
            headers=headers,
            json={
                "base": "USD",
                "quote": "CAD",
                "rate": "1.35",
                "date": (today - timedelta(days=offset + 1)).isoformat(),
            },
        )
        assert r.status_code == 201, r.text

    limited = client.post(
        "/api/v1/fx",
        headers=headers,
        json={
            "base": "USD",
            "quote": "CAD",
            "rate": "1.35",
            "date": (today - timedelta(days=burst + 1)).isoformat(),
        },
    )
    assert limited.status_code == 429
    assert limited.json() == RATE_ENVELOPE


def test_manual_fx_rejects_a_malformed_currency_code(client: TestClient) -> None:
    """`base`/`quote` are bounded like `AccountCreate.currency`.

    `fx_rates` has no `user_id`, so an unbounded string here becomes an
    unbounded set of distinct keys in a table every tenant reads.
    """
    from tests.test_deep_common import auth, register

    body = register(client)
    headers = auth(body["access_token"])

    for field in ("base", "quote"):
        r = client.post(
            "/api/v1/fx",
            headers=headers,
            json={
                "base": "USD",
                "quote": "CAD",
                "rate": "1.35",
                "date": "2026-01-05",
                field: "NOT-A-CURRENCY-CODE",
            },
        )
        assert r.status_code == 422, f"{field} must be length-bounded: {r.text}"

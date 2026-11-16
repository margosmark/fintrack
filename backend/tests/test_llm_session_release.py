"""Regression: LLM `provider.chat()` waits must not retain a DB pool slot.

Two request routes run a `chat()` that can wait up to 60 s (the egress
deadline) while `get_current_user`'s auth read has already autobegun a
transaction on THIS request's session — the M175 class (docs/01 §Sessions and
the event loop: *a `Session` is never held across an unbounded provider wait*).
On the default pool that pins one slot per probe, so a burst of "Test
connection" clicks (or of CSV uploads) drains it — the M148 shape.

Both routes are READ-ONLY across the wait (`llm-test` returns
`{ok, model, provider}`; `suggest-profile` returns LLM-parsed mappings), so
`release_before_provider_wait` has nothing of the caller's pending at the
release point.

Pattern follows `tests/test_sync_provider_session_release.py`: a one-slot
pool, a provider that blocks on events, and a second authenticated request
that must get a connection while the first one waits. RED before the fix with
`QueuePool limit of size 1 overflow 0 reached, connection timed out`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

import app.api.deps as deps
from app.main import app

PASSWORD = "correct-horse-battery"

#: What the blocked `chat()` answers once the test lets it through. The
#: `reasoning` marker is how `suggest-profile` proves the LLM path answered
#: instead of the heuristic fallback (`suggest_column_mapping` swallows every
#: exception and silently degrades).
_LLM_TEST_PAYLOAD: dict[str, Any] = {"ok": True}
_COLUMN_MAPPING_PAYLOAD: dict[str, Any] = {
    "columns": {
        "date": "Date",
        "raw_description": "Description",
        "amount_native": "Amount",
    },
    "date_format": "%Y-%m-%d",
    "decimal_comma": False,
    "credits_positive": True,
    "confidence": 0.9,
    "reasoning": "delayed-chat-payload",
}


@pytest.fixture(name="tiny_pool_engine")
def tiny_pool_engine_fixture() -> Iterator[Engine]:
    engine = create_engine(
        deps.get_engine().url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=2,
        pool_pre_ping=True,
    )
    previous = deps._engine
    deps._engine = engine
    yield engine
    deps._engine = previous
    engine.dispose()


class _DelayedChatProvider:
    """An LLM provider whose `chat()` parks until the test releases it."""

    def __init__(
        self,
        entered: threading.Event,
        release: threading.Event,
        payload: dict[str, Any],
        label: str,
    ) -> None:
        self._entered = entered
        self._release = release
        self._payload = payload
        self._label = label

    def chat(self, messages: list[dict], json_schema: dict) -> dict:
        self._entered.set()
        assert self._release.wait(5), f"test did not release the {self._label} chat wait"
        return dict(self._payload)


def _register_and_login(client: TestClient, email: str) -> tuple[str, dict[str, str]]:
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert registered.status_code == 200, registered.text
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


def _delete_tenant(token: str) -> None:
    with TestClient(app) as cleanup_client:
        deleted = cleanup_client.request(
            "DELETE",
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {token}"},
            json={"current_password": PASSWORD},
        )
        assert deleted.status_code == 204, deleted.text


def test_second_request_gets_connection_during_the_llm_test_chat(
    tiny_pool_engine: Engine, monkeypatch
) -> None:
    """`POST /users/me/llm-test` must not pin the pool across its chat wait.

    The route takes no `Session` of its own, but `get_current_user` already
    ran its `SELECT users…` on the request session from `deps.get_db`, so the
    connection is `idle in transaction` for the whole `provider.chat()`. With
    a one-slot pool a second authenticated read must still take that slot.
    """
    import app.api.users as users_mod

    entered = threading.Event()
    release = threading.Event()
    provider = _DelayedChatProvider(entered, release, _LLM_TEST_PAYLOAD, "llm-test")
    monkeypatch.setattr(users_mod, "make_provider", lambda *a, **k: provider)

    token: str | None = None
    worker = threading.Thread(target=lambda: None)  # placeholder for `finally`
    outcome: dict[str, Any] = {}
    try:
        with TestClient(app) as client:
            email = f"llm-test-wait-{threading.get_ident()}@example.com"
            token, headers = _register_and_login(client, email)

            def do_llm_test() -> None:
                try:
                    outcome["response"] = client.post(
                        "/api/v1/users/me/llm-test", json={}, headers=headers
                    )
                except Exception as exc:  # reported in the assertion below
                    outcome["error"] = exc

            worker = threading.Thread(target=do_llm_test, daemon=True)
            worker.start()
            assert entered.wait(3), "the llm-test provider chat was not reached"
            # The waiting request must have handed its only connection back to
            # this one-slot pool, allowing this authenticated read.
            second = client.get("/api/v1/users/me", headers=headers)
            assert second.status_code == 200, second.text
            release.set()
            worker.join(8)
            assert not worker.is_alive(), "the llm-test request did not finish"
            assert "error" not in outcome, outcome
            response = outcome["response"]
            assert response.status_code == 200, response.text
            body = response.json()
            assert set(body) == {"ok", "model", "provider"}, body
            assert body["ok"] is True, body
    finally:
        release.set()
        worker.join(8)
        if token is not None:
            _delete_tenant(token)


def test_second_request_gets_connection_during_the_suggest_profile_chat(
    tiny_pool_engine: Engine, monkeypatch
) -> None:
    """`POST /imports/suggest-profile` must not pin the pool across its chat.

    Same shape as the probe above, one layer deeper: the route only builds the
    provider (`build_provider`) and the wait happens in
    `services/column_mapping.py::_llm_suggest`, which is reached from a worker
    thread (`blocking(core)`) while the request session holds its auth read.
    """
    import app.api.imports as imports_mod

    entered = threading.Event()
    release = threading.Event()
    provider = _DelayedChatProvider(entered, release, _COLUMN_MAPPING_PAYLOAD, "suggest-profile")
    monkeypatch.setattr(
        imports_mod,
        "build_provider",
        lambda *a, **k: SimpleNamespace(provider=provider, model="test-model"),
    )

    token: str | None = None
    worker = threading.Thread(target=lambda: None)  # placeholder for `finally`
    outcome: dict[str, Any] = {}
    try:
        with TestClient(app) as client:
            email = f"suggest-profile-wait-{threading.get_ident()}@example.com"
            token, headers = _register_and_login(client, email)

            csv_bytes = (
                b"Date,Description,Amount\n2026-01-05,Coffee,-4.50\n2026-01-06,Groceries,-52.10\n"
            )

            def do_suggest_profile() -> None:
                try:
                    outcome["response"] = client.post(
                        "/api/v1/imports/suggest-profile",
                        headers=headers,
                        files={"file": ("statement.csv", csv_bytes, "text/csv")},
                    )
                except Exception as exc:  # reported in the assertion below
                    outcome["error"] = exc

            worker = threading.Thread(target=do_suggest_profile, daemon=True)
            worker.start()
            assert entered.wait(3), "the suggest-profile provider chat was not reached"
            # The waiting request must have handed its only connection back to
            # this one-slot pool, allowing this authenticated read.
            second = client.get("/api/v1/users/me", headers=headers)
            assert second.status_code == 200, second.text
            release.set()
            worker.join(8)
            assert not worker.is_alive(), "the suggest-profile request did not finish"
            assert "error" not in outcome, outcome
            response = outcome["response"]
            assert response.status_code == 200, response.text
            body = response.json()
            # The LLM answered — not the heuristic fallback, which is what a
            # swallowed chat failure would have produced.
            assert body["reasoning"] == "delayed-chat-payload", body
            assert body["columns"]["date"] == "Date", body
    finally:
        release.set()
        worker.join(8)
        if token is not None:
            _delete_tenant(token)

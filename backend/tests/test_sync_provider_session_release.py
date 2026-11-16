"""Regression: bank-sync provider waits must not retain a DB pool slot."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

import app.api.deps as deps
from app.main import app
from app.services.sync.provider import register_provider
from app.services.sync.stub import StubProvider, default_stub


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


@pytest.mark.parametrize("wait_method", ["create", "pull", "revoke"])
def test_second_request_gets_connection_during_provider_wait(
    tiny_pool_engine: Engine, wait_method: str
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class DelayedProvider(StubProvider):
        def _wait(self, method: str) -> None:
            if wait_method == method:
                entered.set()
                assert release.wait(5), f"test did not release provider {method} wait"

        def create_agreement(self, user, accounts):
            self._wait("create")
            return super().create_agreement(user, accounts)

        def pull_delta(self, user, since):
            self._wait("pull")
            return super().pull_delta(user, since)

        def revoke(self, user):
            self._wait("revoke")
            return super().revoke(user)

    provider = DelayedProvider(slug="stub")
    register_provider(provider)
    token: str | None = None
    try:
        with TestClient(app) as client:
            email = f"sync-wait-{wait_method}-{threading.get_ident()}@example.com"
            registered = client.post(
                "/api/v1/auth/register",
                json={"email": email, "password": "correct-horse-battery"},
            )
            assert registered.status_code == 200, registered.text
            login = client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "correct-horse-battery"},
            )
            assert login.status_code == 200, login.text
            token = login.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            inst = client.post("/api/v1/institutions", json={"name": "wait bank"}, headers=headers)
            assert inst.status_code == 201, inst.text
            account = client.post(
                "/api/v1/accounts",
                json={
                    "name": "checking",
                    "type": "chequing",
                    "currency": "CAD",
                    "institution_id": inst.json()["id"],
                },
                headers=headers,
            )
            assert account.status_code == 201, account.text
            agreement_id = None
            if wait_method != "create":
                created = client.post(
                    "/api/v1/sync/agreements",
                    json={"provider": "stub", "account_ids": [account.json()["id"]]},
                    headers=headers,
                )
                assert created.status_code == 201, created.text
                agreement_id = created.json()["id"]

            path = "/api/v1/sync/agreements"
            method = "post"
            kwargs = {
                "json": {"provider": "stub", "account_ids": [account.json()["id"]]},
                "headers": headers,
            }
            if wait_method == "pull":
                path = f"/api/v1/sync/agreements/{agreement_id}/pull"
                method = "post"
                kwargs = {"headers": headers}
            elif wait_method == "revoke":
                path = f"/api/v1/sync/agreements/{agreement_id}/revoke"
                method = "post"
                kwargs = {"headers": headers}

            outcome: dict[str, object] = {}

            def do_provider_request() -> None:
                try:
                    outcome["response"] = getattr(client, method)(path, **kwargs)
                except Exception as exc:  # reported in the assertion below
                    outcome["error"] = exc

            worker = threading.Thread(target=do_provider_request, daemon=True)
            worker.start()
            assert entered.wait(3), f"{wait_method} provider call was not reached"
            # The request that is waiting externally must have returned its only
            # connection to this one-slot pool, allowing this authenticated read.
            second = client.get("/api/v1/sync/agreements", headers=headers)
            assert second.status_code == 200, second.text
            release.set()
            worker.join(8)
            assert not worker.is_alive(), f"{wait_method} request did not finish"
            assert "error" not in outcome, outcome
            response = outcome["response"]
            assert response.status_code == (201 if wait_method == "create" else 200), response.text
    finally:
        release.set()
        register_provider(default_stub())
        if token is not None:
            with TestClient(app) as cleanup_client:
                deleted = cleanup_client.request(
                    "DELETE",
                    "/api/v1/users/me",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"current_password": "correct-horse-battery"},
                )
                assert deleted.status_code == 204, deleted.text


def test_second_request_gets_connection_during_the_institutions_provider_wait(
    tiny_pool_engine: Engine,
) -> None:
    """The institutions route must not pin the pool across its provider wait.

    `GET /sync/providers/{slug}/institutions` is a plain read, but
    `get_current_user` has already run its one query on THIS request session
    (deps.get_db), so the connection is `idle in transaction` when
    `provider.list_institutions()` goes out — the same shape M175 closed on
    every other seam. With a one-slot pool a second authenticated read must
    still be able to take that slot while the provider is blocked.
    """
    entered = threading.Event()
    release = threading.Event()

    class DelayedProvider(StubProvider):
        def list_institutions(self):
            entered.set()
            assert release.wait(5), "test did not release the institutions provider wait"
            return super().list_institutions()

    provider = DelayedProvider(slug="stub")
    register_provider(provider)
    token: str | None = None
    try:
        with TestClient(app) as client:
            email = f"sync-wait-institutions-{threading.get_ident()}@example.com"
            registered = client.post(
                "/api/v1/auth/register",
                json={"email": email, "password": "correct-horse-battery"},
            )
            assert registered.status_code == 200, registered.text
            login = client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "correct-horse-battery"},
            )
            assert login.status_code == 200, login.text
            token = login.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            outcome: dict[str, Any] = {}

            def do_provider_request() -> None:
                try:
                    outcome["response"] = client.get(
                        "/api/v1/sync/providers/stub/institutions", headers=headers
                    )
                except Exception as exc:  # reported in the assertion below
                    outcome["error"] = exc

            worker = threading.Thread(target=do_provider_request, daemon=True)
            worker.start()
            assert entered.wait(3), "the institutions provider call was not reached"
            # The waiting request must have handed its only connection back to
            # this one-slot pool, allowing this authenticated read.
            second = client.get("/api/v1/sync/agreements", headers=headers)
            assert second.status_code == 200, second.text
            release.set()
            worker.join(8)
            assert not worker.is_alive(), "the institutions request did not finish"
            assert "error" not in outcome, outcome
            response = outcome["response"]
            assert response.status_code == 200, response.text
            assert response.json(), response.text
    finally:
        release.set()
        register_provider(default_stub())
        if token is not None:
            with TestClient(app) as cleanup_client:
                deleted = cleanup_client.request(
                    "DELETE",
                    "/api/v1/users/me",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"current_password": "correct-horse-battery"},
                )
                assert deleted.status_code == 204, deleted.text

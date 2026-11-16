"""F1 (security t_108c24f3, comment 907): the proposal-row lock must never run on the event loop.

`apply`/`dismiss`/`undo` used to be `async def`, so their synchronous
`SELECT … FOR UPDATE` (S-2) executed ON the event loop. A row-lock wait
there then deadlocks the whole process: the loop is busy waiting for a
transaction that cannot be closed, because closing a request's session is
itself work the loop has to schedule. Security reproduced it live
(`probe_deadlock.py` on 24313d6): N=3 concurrent applies → exactly one
`200`, the rest hang forever, and afterwards `/api/v1/health`, `/docs` and
`/openapi.json` all stop answering until the process is killed.

The fix has two halves (docs/01 § "Sessions and the event loop"):

* the routes are sync `def`, so FastAPI runs them — and every blocking
  statement in them — in its worker threads; the loop stays free and the
  request/session close cycles always make progress;
* each transition transaction bounds its own lock wait
  (`services/assistant.py::LOCK_WAIT_LIMIT`), pinned separately in
  `tests/test_assistant_concurrency.py`.

This module pins the OUTCOME at the HTTP layer with the same shape as the
security probe, on the real ASGI stack over a real socket:

1. four concurrent transitions all ANSWER, fast: exactly one `200`, the
   rest `409 assistant_proposal_not_pending` — the lock still serializes
   them (atomicity preserved);
2. while they are in flight, `/docs` (no database, pure loop) is served
   within `LOOP_BUDGET_S` — the loop is not frozen;
3. after the burst, `/api/v1/health` answers `200` — the endpoint
   security's probe could no longer reach.

Template: `tests/test_m149_offloop_routes.py` (same live-server fixture,
same loop-budget reasoning).
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import uvicorn

from app.api import deps
from app.main import app

API = "/api/v1"
PASSWORD = "correct-horse-battery"

# How long the loop may take to answer /docs while transitions are running.
# Same value and same (deliberately unconditional) reasoning as M149's pin:
# a frozen loop never answers, so the budget must sit far below the stall it
# is meant to detect, and gating it would disable the freeze detector exactly
# when the box is busiest (docs/34 "what is NOT enforced").
LOOP_BUDGET_S = 0.5
# Health must ANSWER; the freeze signature is "never", not "slow". 2 s is a
# generous ceiling over the measured ~50 ms that also survives a loaded box.
HEALTH_BUDGET_S = 2.0
# Ceilings: they only ever stop a hung run from outliving the session.
CLIENT_TIMEOUT_S = 20
JOIN_S = 30

# Security's probe used N=3 and N=4; four gives the same shape with margin.
TRANSITIONS = 4


@pytest.fixture(scope="module")
def live_server() -> Iterator[str]:
    """The real ASGI stack on a real socket — uvicorn in a daemon thread."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn never started"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(30)
    assert not thread.is_alive(), "uvicorn did not shut down"
    # The live app opened its OWN engine (app.api.deps): drop its pooled
    # connections so nothing this module left open reaches into a later
    # module's assertions against the same suite database. Dispose is safe:
    # SQLAlchemy reconnects on demand.
    if deps._engine is not None:
        deps._engine.dispose()


@pytest.fixture(scope="module")
def tenant(live_server: str) -> Iterator[dict[str, Any]]:
    """A real tenant on the live stack: headers + an account + a transaction.

    Every row this commits is removed again in teardown: `POST /auth/register`
    commits through the live app's own engine, where the `session` fixture's
    rollback never reaches, and `conftest._guard_no_committed_tenant_survives`
    fails the whole RUN for a `users` row that outlives it (AGENTS §6).
    """
    with httpx.Client(base_url=live_server, timeout=CLIENT_TIMEOUT_S) as client:
        email = f"m149b-{uuid4().hex[:12]}@example.com"
        assert client.post(
            f"{API}/auth/register", json={"email": email, "password": PASSWORD}
        ).status_code in (200, 201, 409)
        login = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        account = client.post(
            f"{API}/accounts",
            headers=headers,
            json={"name": "F1 chequing", "type": "chequing", "currency": "CAD"},
        )
        assert account.status_code == 201, account.text

        transaction = client.post(
            f"{API}/transactions",
            headers=headers,
            json={
                "account_id": account.json()["id"],
                "date": "2026-08-01",
                "raw_description": "Loblaws",
                "amount_native": "-42.00",
            },
        )
        assert transaction.status_code == 201, transaction.text

    yield {
        "headers": headers,
        "account_id": account.json()["id"],
        "transaction_id": transaction.json()["id"],
    }

    with httpx.Client(base_url=live_server, timeout=CLIENT_TIMEOUT_S) as client:
        for path in (
            f"{API}/transactions/{transaction.json()['id']}",
            f"{API}/accounts/{account.json()['id']}",
        ):
            with contextlib.suppress(Exception):
                assert client.delete(path, headers=headers).status_code in (200, 204, 404)
        # …and the tenant itself, LAST, NOT suppressed: a failed delete here
        # IS the leak. `DELETE /users/me` wipes every row the user owns
        # (docs/20 cascade), including proposals and audits.
        deleted = client.request(
            "DELETE",
            f"{API}/users/me",
            headers=headers,
            json={"current_password": PASSWORD},
        )
        assert deleted.status_code == 204, deleted.text


def _create_proposal(live_server: str, tenant: dict[str, Any]) -> int:
    """One pending `transaction.update` card through the real REST path."""
    with httpx.Client(base_url=live_server, timeout=CLIENT_TIMEOUT_S) as client:
        resp = client.post(
            f"{API}/assistant/proposals",
            headers=tenant["headers"],
            json={
                "proposal_kind": "transaction.update",
                "proposal_target": f"transaction:{tenant['transaction_id']}",
                "proposal_summary": "Note this run",
                "proposal_fields": {"note": "f1-offloop"},
                "question": "Add a note",
                "answer": "The note field is empty; adding one is the change.",
            },
        )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_concurrent_transitions_answer_and_leave_the_loop_responsive(
    live_server: str, tenant: dict[str, Any]
) -> None:
    """≥3 concurrent transitions: all answer, loop live, health alive (F1)."""
    proposal_id = _create_proposal(live_server, tenant)

    outcomes: list[str] = []

    def apply(index: int) -> None:
        try:
            with httpx.Client(base_url=live_server, timeout=CLIENT_TIMEOUT_S) as client:
                resp = client.post(
                    f"{API}/assistant/proposals/{proposal_id}/apply",
                    headers=tenant["headers"],
                )
                outcomes.append(f"#{index} {resp.status_code}")
        except Exception as exc:
            outcomes.append(f"#{index} {type(exc).__name__}")

    threads = [
        threading.Thread(target=apply, args=(index,), daemon=True) for index in range(TRANSITIONS)
    ]
    for thread in threads:
        thread.start()

    # While ANY transition is in flight, /docs must be served. It takes no
    # database connection, so its latency IS the event loop's. On the broken
    # tree the loop is frozen here and the very first probe fails; on the
    # fixed tree the burst usually completes before this loop runs its first
    # iteration, which is the point.
    loop_failure: str | None = None
    deadline = time.monotonic() + JOIN_S
    while time.monotonic() < deadline and any(t.is_alive() for t in threads):
        started = time.perf_counter()
        try:
            probe = httpx.get(f"{live_server}/docs", timeout=2.0)
            elapsed = time.perf_counter() - started
            if probe.status_code not in (200, 401):
                loop_failure = f"/docs answered {probe.status_code}"
            elif elapsed > LOOP_BUDGET_S:
                loop_failure = (
                    f"/docs took {elapsed:.2f}s (budget {LOOP_BUDGET_S}s) while "
                    f"{TRANSITIONS} transitions were in flight"
                )
        except Exception as exc:
            loop_failure = f"{type(exc).__name__} probing /docs during the burst"
        if loop_failure:
            break
        time.sleep(0.02)
    assert loop_failure is None, (
        f"the event loop did not stay responsive during concurrent apply/dismiss: "
        f"{loop_failure}. The container runs one worker and one loop, so this is "
        "the whole API freezing behind one row-lock wait — docs/01 § "
        '"Sessions and the event loop", security F1 (t_108c24f3).'
    )

    for thread in threads:
        thread.join(JOIN_S)
    hung = [t for t in threads if t.is_alive()]
    assert not hung, f"{len(hung)} of {TRANSITIONS} transitions never answered: {outcomes}"

    statuses = sorted(
        int(part) for line in outcomes for part in [line.split()[-1]] if part.isdigit()
    )
    assert statuses == [200] + [409] * (TRANSITIONS - 1), outcomes

    # The exact endpoint security could no longer reach after the deadlock.
    started = time.perf_counter()
    health = httpx.get(f"{live_server}{API}/health", timeout=CLIENT_TIMEOUT_S)
    elapsed = time.perf_counter() - started
    assert health.status_code == 200, health.text
    assert elapsed < HEALTH_BUDGET_S, (
        f"/health answered in {elapsed:.2f}s (budget {HEALTH_BUDGET_S}s) — "
        "the loop is degraded after concurrent transitions"
    )

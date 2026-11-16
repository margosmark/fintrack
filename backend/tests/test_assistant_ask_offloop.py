"""F4 (security t_95eb8caf): the ask turn must never run its provider send on the event loop.

`POST /assistant/ask` was `async def` and called `service.ask(...)` directly,
so the synchronous `httpx.post(timeout=60)` in `app/llm/base.py` executed ON
the single-worker event loop (docs/01 § "Sessions and the event loop": *the
loop never does blocking work*). Any registered user can PATCH
`llm_base_url` to an endpoint of their choosing — a public host is accepted
with `200` — and every ask then holds the loop for as long as that host
takes to answer, up to the full 60 s timeout. Security's live proof on
8bbac9b: an 8.1 s ask → `/health` ReadTimeout; three asks → 9 of 10 health
polls dead over 23.6 s. One person's chat froze the whole API.

The fix is the hop the transitions already take (M149, `api/_offloop.blocking`):
the whole turn — context read, provider send, proposal write and response
shaping — runs in a worker thread and the loop only awaits it.

This module pins the OUTCOME over a real socket with a stub provider that
takes `STUB_SLEEP_S` to answer:

1. an ask through the real stack answers `200` with the stub's reply — the
   probe below is not vacuous: the ask really waited on the provider;
2. while that ask is in flight, `/api/v1/health` answers every poll within
   `HEALTH_BUDGET_S` — the exact endpoint security's probe could not reach;
3. after it, `/health` still answers `200`.

Template: `tests/test_assistant_transition_offloop.py`.
"""

from __future__ import annotations

import http.server
import json
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

# How long the stub provider takes to answer one completion. Long enough that
# the loop must answer MANY polls inside that window (so a freeze cannot hide
# behind a single unlucky probe), short enough to keep the test quick.
STUB_SLEEP_S = 2.5
# Health must ANSWER; the freeze signature is "blocked", not "slow". The
# budget sits far below the stall it detects and far above the measured ~50 ms
# (same reasoning as M149's pin): gating it would disable the detector exactly
# when the box is busiest (docs/34 "what is NOT enforced").
HEALTH_BUDGET_S = 1.0
# Give up on one probe after this long — a frozen loop never answers, so the
# timeout IS the detection on the broken tree.
PROBE_TIMEOUT_S = 1.5
PROBE_INTERVAL_S = 0.02
# Proofs that the sampling really happened inside the ask's window.
MIN_PROBES = 5
MIN_ASK_S = STUB_SLEEP_S * 0.8
# Ceilings: they only ever stop a hung run from outliving the session.
CLIENT_TIMEOUT_S = 30
JOIN_S = 40

# The stub's hit counter — the ask must really have reached the provider.
_STUB_HITS = {"n": 0}
_STUB_LOCK = threading.Lock()


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """One OpenAI-compatible completion, `STUB_SLEEP_S` late."""

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        with _STUB_LOCK:
            _STUB_HITS["n"] += 1
        time.sleep(STUB_SLEEP_S)
        content = json.dumps(
            {"answer": "Stub answer from the slow provider.", "proposal_kind": "none"}
        )
        body = json.dumps(
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "created": 0,
                "model": "stub-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any, **_kwargs: Any) -> None:
        pass  # one request per test; keep pytest output quiet


@pytest.fixture(scope="module")
def stub() -> Iterator[str]:
    """A loopback provider that is deliberately slow to answer."""
    _STUB_HITS["n"] = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(10)


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
def tenant(live_server: str, stub: str) -> Iterator[dict[str, Any]]:
    """A real tenant whose assistant points at the slow stub.

    `PATCH /users/me` is exactly how security's finding is weaponised: a
    registered user may set `llm_base_url` to an endpoint of their choosing
    (200). Everything this commits is removed again in teardown —
    `POST /auth/register` commits through the live app's own engine, where the
    `session` fixture's rollback never reaches, and
    `conftest._guard_no_committed_tenant_survives` fails the whole RUN for a
    `users` row that outlives it (AGENTS §6).
    """
    with httpx.Client(base_url=live_server, timeout=CLIENT_TIMEOUT_S) as client:
        email = f"f4-ask-{uuid4().hex[:12]}@example.com"
        assert client.post(
            f"{API}/auth/register", json={"email": email, "password": PASSWORD}
        ).status_code in (200, 201, 409)
        login = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        patched = client.patch(f"{API}/users/me", headers=headers, json={"llm_base_url": stub})
        assert patched.status_code == 200, patched.text
        assert patched.json()["llm_base_url"] == stub

    yield {"headers": headers}

    with httpx.Client(base_url=live_server, timeout=CLIENT_TIMEOUT_S) as client:
        # `DELETE /users/me` wipes every row the user owns (docs/20 cascade)
        # and is the LAST thing this fixture does, not suppressed: a failed
        # delete here IS the leak.
        deleted = client.request(
            "DELETE",
            f"{API}/users/me",
            headers=headers,
            json={"current_password": PASSWORD},
        )
        assert deleted.status_code == 204, deleted.text


def test_health_answers_while_an_ask_holds_the_provider(
    live_server: str, tenant: dict[str, Any]
) -> None:
    """An in-flight ask must not stop `/health` from answering (F4)."""
    hits_before = _STUB_HITS["n"]
    started_at = time.monotonic()
    outcomes: list[str] = []

    def ask() -> None:
        try:
            with httpx.Client(base_url=live_server, timeout=CLIENT_TIMEOUT_S) as client:
                resp = client.post(
                    f"{API}/assistant/ask",
                    headers=tenant["headers"],
                    json={"question": "What did I spend at the grocery store?"},
                )
                outcomes.append(f"{resp.status_code} {resp.text[:300]}")
        except Exception as exc:
            outcomes.append(f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=ask, daemon=True)
    thread.start()

    # While the ask is in flight, /health must answer. On the broken tree the
    # loop is blocked inside httpx.post for STUB_SLEEP_S, so the first probe
    # times out; on the fixed tree the loop answers every poll.
    failures: list[str] = []
    probes = 0
    deadline = time.monotonic() + JOIN_S
    while time.monotonic() < deadline and thread.is_alive():
        probe_started = time.perf_counter()
        try:
            health = httpx.get(f"{live_server}{API}/health", timeout=PROBE_TIMEOUT_S)
        except Exception as exc:
            failures.append(
                f"{type(exc).__name__} after {time.perf_counter() - probe_started:.2f}s"
            )
            break
        elapsed = time.perf_counter() - probe_started
        probes += 1
        if health.status_code != 200:
            failures.append(f"/health answered {health.status_code}")
            break
        if elapsed > HEALTH_BUDGET_S:
            failures.append(f"/health took {elapsed:.2f}s (budget {HEALTH_BUDGET_S}s)")
            break
        time.sleep(PROBE_INTERVAL_S)

    thread.join(JOIN_S)
    assert not thread.is_alive(), f"the ask never answered: {outcomes}"
    ask_wall = time.monotonic() - started_at

    assert failures == [], (
        f"/health did not stay responsive while an ask was in flight: {failures}. "
        "The container runs one worker and one loop, so a provider send ON the "
        'loop is the whole API freezing behind one chat — docs/01 § "Sessions '
        'and the event loop", security F4 (t_95eb8caf).'
    )
    assert probes >= MIN_PROBES, f"only {probes} health probes fit in {ask_wall:.2f}s"
    # The probe is only meaningful if the ask really waited on the provider.
    assert outcomes and outcomes[0].startswith("200 "), outcomes
    assert "Stub answer" in outcomes[0], outcomes
    assert _STUB_HITS["n"] > hits_before, "the ask never reached the provider"
    assert ask_wall >= MIN_ASK_S, (
        f"the ask returned in {ask_wall:.2f}s — the stub slept {STUB_SLEEP_S}s, so "
        "this run never held the provider wait it is meant to test"
    )

    health = httpx.get(f"{live_server}{API}/health", timeout=CLIENT_TIMEOUT_S)
    assert health.status_code == 200, health.text

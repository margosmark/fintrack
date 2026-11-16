"""M149 — the six upload/parse routes must not do blocking work on the loop.

docs/01 § "Sessions and the event loop": the container runs ONE uvicorn worker
and one event loop, so a route that blocks there stalls EVERY request in the
process, not just its own. Six `async def` routes needed `await file.read()`
and then kept going on the loop — CSV parse, a per-row duplicate query, an LLM
call of up to 60 s, a 2 MB base64 round-trip. None of them could self-deadlock
the way M148's auth query could, but each one froze the loop for its own
duration: one receipt upload stalled the dashboard, /health and every other tab.

The pin here is the MECHANISM, not the shape: make a route block for two
seconds on purpose and prove an unrelated request is still served while it
does. One route per affected router gets that treatment, each with a real
request body and a real response; the tripwire below keeps the other three
honest (a shape check, deliberately labelled as one).

Template: `tests/test_m148_pool_deadlock.py::test_pool_wait_does_not_freeze_the_event_loop`.
"""

from __future__ import annotations

import contextlib
import inspect
import socket
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
import uvicorn

from app.api import deps, imports, trades, transactions
from app.main import app

API = "/api/v1"
PASSWORD = "correct-horse-battery"

# The artificial stall, and how much of it the event loop may absorb. A frozen
# loop never answers, so the budget has to stay well under the stall itself.
STALL_S = 2.0
LOOP_BUDGET_S = 0.5
# Generous ceilings: they only ever stop a hung test from outliving the run.
WAIT_S = 20
#
# `LOOP_BUDGET_S` is DELIBERATELY not behind `latency_budget_blocker` (docs/34
# "what is NOT enforced", t_4980b7ed) — same reasoning as test_m148's, and for
# the same shape: this asserts the loop was not FROZEN, where frozen costs the
# injected STALL_S (2.0 s) against a 0.5 s budget. Both sides are slowed by a
# by a loaded box together, so the discriminator is a ratio, not a baseline.
# Measured across solo and loaded runs: the /docs probe took 7.1-18.4 ms
# (load 3-7 on 4 cores), 25-70x inside the budget, and it touches no database.
# Converting it would make the off-loop regression detector a skip exactly
# when the box is busiest — which is when a real freeze would hurt most.

# Minimal bodies with the RIGHT magic bytes — the server sniffs, so the content
# only has to be recognizable as an image, not a decodable one (M108's fixtures).
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 400

# A structured format needs no mapping profile, so the parse below exercises the
# real importer rather than a stub.
QIF = b"!Type:Bank\nD03/15/2026\nT-42.00\nPNODESC\n^\n"

# A CSV the heuristic mapper can read without an LLM (column_mapping falls back
# to its heuristic whenever the provider is None).
CSV = b"Date,Description,Amount\n2026-03-15,NODESC,-42.00\n"


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
    # The live app opened its OWN engine (app.api.deps), separate from the one
    # the test fixtures use. Drop its pooled connections so nothing this module
    # left open can reach into a later module's assertions against the same
    # suite database. Dispose is safe: SQLAlchemy reconnects on demand.
    if deps._engine is not None:
        deps._engine.dispose()


@pytest.fixture(scope="module")
def tenant(live_server: str) -> Iterator[dict[str, Any]]:
    """A real tenant on the live stack: headers, an account, a transaction."""
    with httpx.Client(base_url=live_server, timeout=60) as client:
        email = f"m149-{uuid4().hex[:12]}@example.com"
        assert client.post(
            f"{API}/auth/register", json={"email": email, "password": PASSWORD}
        ).status_code in (200, 201, 409)
        login = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        account = client.post(
            f"{API}/accounts",
            headers=headers,
            json={"name": "M149 chequing", "type": "chequing", "currency": "CAD"},
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

    # The receipt route is exercised for real, and
    # `tests/test_transaction_receipts.py` selects `transaction_receipts` with
    # NO tenant filter — so a row left behind here fails somebody else's suite
    # in a shared database. Take the image away again, then the row it hangs off.
    with httpx.Client(base_url=live_server, timeout=60) as client:
        for path in (
            f"{API}/transactions/{transaction.json()['id']}/receipt",
            f"{API}/transactions/{transaction.json()['id']}",
            f"{API}/accounts/{account.json()['id']}",
        ):
            with contextlib.suppress(Exception):
                assert client.delete(path, headers=headers).status_code in (200, 204, 404)
        # …and the tenant itself, LAST. `POST /auth/register` seeded this user's
        # 10 default categories through the live app's OWN engine, so those rows
        # (and the users row) are COMMITTED into the suite database — the
        # `session` fixture's rollback never sees them. Leaving them behind put a
        # second "groceries" in front of every later test that reads categories
        # without a user filter, which is what made
        # `tests/test_ynab_migration.py::test_commit_creates_accounts_categories_and_staged_rows`
        # flip on heap order (2 of ~8 full runs, 0 solo — t_a451f171).
        # NOT suppressed, unlike the loop above: a missing row there is fine,
        # a failed delete here IS the leak. `DELETE /users/me` wipes every row
        # the user owns (docs/20 cascade). httpx's `delete()` takes no body
        # argument, so `request("DELETE", …)` is the form that carries one.
        deleted = client.request(
            "DELETE",
            f"{API}/users/me",
            headers=headers,
            json={"current_password": PASSWORD},
        )
        assert deleted.status_code == 204, deleted.text


class Stall:
    """A patched seam: raise a flag, sleep, then call the real thing.

    The flag is what makes the probe below airtight — without it the unrelated
    request could finish before the stall ever began and the test would pass
    without having measured anything.
    """

    def __init__(self, seconds: float = STALL_S) -> None:
        self.seconds = seconds
        self.started = threading.Event()

    def wrap(self, fn: Callable[..., object]) -> Callable[..., object]:
        def call(*args: object, **kwargs: object) -> object:
            self.started.set()
            time.sleep(self.seconds)
            return fn(*args, **kwargs)

        return call


def _assert_loop_survives(
    slow: Callable[[], httpx.Response],
    stall: Stall,
    live_server: str,
) -> bytes:
    """Fire `slow` on a thread, then prove `/docs` is served while it blocks.

    `/docs` is a real request through the whole middleware + dependency stack
    and answers 401 without taking a database connection, so its latency IS the
    event loop's.
    """
    outcome: dict[str, object] = {}

    def call() -> None:
        try:
            response = slow()
            outcome["status"] = response.status_code
            outcome["body"] = response.text[:4000]
        except Exception as exc:  # the assertion reports it
            outcome["status"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    assert stall.started.wait(WAIT_S), "the request never reached the blocking seam"

    started = time.perf_counter()
    probe = httpx.get(f"{live_server}/docs", timeout=WAIT_S)
    loop_seconds = time.perf_counter() - started
    print(f"\nPERF loop-probe={loop_seconds * 1000:.1f}ms while a {STALL_S}s stall was in flight")

    assert probe.status_code == 401, probe.text
    assert loop_seconds < LOOP_BUDGET_S, (
        f"the event loop was blocked for {loop_seconds:.2f}s by an in-flight "
        f"upload/parse route (budget {LOOP_BUDGET_S}s). The container runs one "
        "worker and one loop, so this is the whole API freezing behind one "
        'receipt upload or one CSV import — docs/01 § "Sessions and the '
        'event loop".'
    )

    thread.join(WAIT_S)
    assert not thread.is_alive(), "the slow request never finished"
    status = outcome.get("status")
    assert status == 200, outcome
    return str(outcome.get("body", "")).encode()


def test_receipt_upload_stalls_off_loop(
    monkeypatch: pytest.MonkeyPatch, live_server: str, tenant: dict[str, Any]
) -> None:
    """A max-size receipt PUT must not freeze every other request (M108/M149).

    The seam is the magic-byte sniff — the first blocking thing the route does
    after it has read the body — and the route still answers 200 afterwards, so
    the hop carries the response back rather than swallowing it.
    """
    stall = Stall()
    monkeypatch.setattr(
        transactions, "_sniff_image_type", stall.wrap(transactions._sniff_image_type)
    )

    def slow() -> httpx.Response:
        with httpx.Client(base_url=live_server, timeout=WAIT_S) as client:
            return client.put(
                f"{API}/transactions/{tenant['transaction_id']}/receipt",
                headers=tenant["headers"],
                files={"file": ("receipt.png", PNG, "image/png")},
            )

    _assert_loop_survives(slow, stall, live_server)


def test_import_preview_stalls_off_loop(
    monkeypatch: pytest.MonkeyPatch, live_server: str, tenant: dict[str, Any]
) -> None:
    """The QIF/CSV parse + per-row duplicate pass must not freeze the loop."""
    stall = Stall()
    monkeypatch.setattr(imports, "_parse_file", stall.wrap(imports._parse_file))

    def slow() -> httpx.Response:
        with httpx.Client(base_url=live_server, timeout=WAIT_S) as client:
            return client.post(
                f"{API}/imports/preview",
                headers=tenant["headers"],
                data={"account_id": str(tenant["account_id"])},
                files={"file": ("statement.qif", QIF, "application/octet-stream")},
            )

    body = _assert_loop_survives(slow, stall, live_server)
    assert b"NODESC" in body, body


def test_suggest_profile_stalls_off_loop(
    monkeypatch: pytest.MonkeyPatch, live_server: str, tenant: dict[str, Any]
) -> None:
    """The LLM call — up to 60 s in production — must not freeze the loop.

    `build_provider` is the seam: it is the last thing the route does before
    handing bytes to the model, and returning a `None` provider sends the real
    heuristic mapper down the same path `test_column_mapping.py` already covers.
    """
    stall = Stall()

    def slow_build_provider(*args: object, **kwargs: object) -> SimpleNamespace:
        stall.started.set()
        time.sleep(stall.seconds)
        return SimpleNamespace(provider=None, model=None)

    monkeypatch.setattr(imports, "build_provider", slow_build_provider)

    def slow() -> httpx.Response:
        with httpx.Client(base_url=live_server, timeout=WAIT_S) as client:
            return client.post(
                f"{API}/imports/suggest-profile",
                headers=tenant["headers"],
                files={"file": ("statement.csv", CSV, "text/csv")},
            )

    body = _assert_loop_survives(slow, stall, live_server)
    assert b"columns" in body, body


# --- the tripwire: the three routes without a behavioral pin -----------------
#
# A shape check, and labelled as one — it cannot prove the hop works, only that
# nobody removed it. `inspect.getsource` unwraps slowapi's decorator, so it reads
# the route body itself.

_OFFLOOP_ROUTES = (
    ("POST /imports/suggest-profile", imports.suggest_profile),
    ("POST /imports/preview", imports.preview_import),
    ("POST /imports/upload", imports.upload),
    ("POST /trades/import/preview", trades.import_trades_preview),
    ("POST /trades/import", trades.import_trades),
    ("PUT /transactions/{id}/receipt", transactions.put_transaction_receipt),
)


@pytest.mark.parametrize(
    ("label", "route"), _OFFLOOP_ROUTES, ids=[label for label, _ in _OFFLOOP_ROUTES]
)
def test_route_hops_off_the_event_loop(label: str, route: Callable[..., object]) -> None:
    assert inspect.iscoroutinefunction(inspect.unwrap(route)), (
        f"{label} must stay `async def` — it needs `await file.read()` — which is "
        "exactly why its blocking half has to hop"
    )
    source = inspect.getsource(inspect.unwrap(route))
    assert "blocking(" in source, (
        f"{label} lost its off-loop hop: everything after `await file.read()` "
        "would be back on the event loop, freezing every other request for its "
        'duration (docs/01 § "Sessions and the event loop")'
    )


def test_blocking_refuses_a_coroutine_function() -> None:
    """The hop is for sync work; handing it a coroutine function would silently
    return an un-awaited coroutine instead of running anything."""
    from app.api._offloop import blocking

    async def already_async() -> None:  # pragma: no cover - never called
        return None

    with pytest.raises(TypeError, match="wraps SYNC work"):
        blocking(already_async)

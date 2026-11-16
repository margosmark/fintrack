"""M148 — a request waiting for a pool connection must not block the event loop.

Incident (2026-10-11 03:08 UTC, `fintrac.princessandmark.com`): 15 concurrent
`GET /forecast/run` requests checked out the entire SQLAlchemy pool
(`size 5 overflow 10`). Every request that then queued for a connection did
that wait ON THE EVENT LOOP — `get_current_user` is `async def` and called
`Session.get` directly, and a pool checkout blocks. A blocked loop cannot run
any other request, including the 15 handlers that already hold the pool and
need that same loop to send their response and close their session. Nothing
finished, nothing released a connection: 15 sessions sat `idle in
transaction` for two hours, `/health` answered nothing, and only
`docker restart` cleared it.

This pins the fix directly at the mechanism: hold every connection the pool
has, then make one authenticated request. It must wait for a connection, and
that wait must cost the EVENT LOOP nothing — an unrelated request is still
served immediately. Before the fix the loop froze for the whole
`pool_timeout`, which is exactly how one endpoint took the whole API down.
"""

from __future__ import annotations

import inspect
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import get_settings
from app.core.rls import attach_request_stamping
from app.core.security import create_access_token
from app.main import app
from app.models.users import User

API = "/api/v1"
RUN = f"{API}/forecast/run"

# The pool the incident exhausted, shrunk to the bone: two connections and a
# four-second checkout timeout, so the buggy behaviour costs ~4 s of frozen
# loop instead of the production 30 s. Four seconds is deliberate: the budget
# below has to stay generous enough for a loaded box (this suite shares 4
# cores) while still being a fraction of what a blocked loop costs.
POOL_SIZE = 1
POOL_OVERFLOW = 1
POOL_TIMEOUT = 4
POOL_TOTAL = POOL_SIZE + POOL_OVERFLOW

# A waiting-for-a-connection request may cost the loop a few tens of ms of
# scheduling noise; a frozen loop costs the full POOL_TIMEOUT.
LOOP_BUDGET_S = 1.0
#
# DELIBERATELY not behind `latency_budget_blocker` (docs/34 "what is NOT
# enforced", t_4980b7ed). This is a RATIO pin, not a latency budget: the
# assertion proves the loop was not frozen, and the frozen cost is the injected
# POOL_TIMEOUT above — 4 s versus a 1 s budget. Everything that loads the box
# slows both sides together (the probe and the stall it must beat), so the
# discriminator survives contention in a way an absolute first-paint number
# does not. Measured across solo and loaded runs: the /docs probe took
# 7.1-18.4 ms (load 3-7 on 4 cores), 25-140x inside the budget, and it touches
# no database (`require_docs_access` refuses an anonymous caller before any
# query). Gating it would turn a bug detector for the 2026-10-11 whole-API
# freeze into a skip — the one thing docs/34 says a conversion must not do.


@pytest.fixture(name="pinned_pool_engine")
def pinned_pool_engine_fixture() -> Iterator[Engine]:
    """The app's MAINTENANCE engine, replaced by a two-connection pool on the DB."""
    engine = create_engine(
        get_settings().database_url,
        pool_size=POOL_SIZE,
        max_overflow=POOL_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
        pool_pre_ping=True,
    )
    previous = deps._engine
    deps._engine = engine
    yield engine
    deps._engine = previous
    engine.dispose()


@pytest.fixture(name="pinned_request_pool")
def pinned_request_pool_fixture(pinned_pool_engine: Engine) -> Iterator[Engine]:
    """The pool `get_db` ACTUALLY checks out — the one this incident is about.

    That is `deps._engine` while `APP_DATABASE_URL` is unset, and a second,
    key-pointed engine once the key arms it (`get_request_engine()`), so
    starving only the maintenance pool starves a pool the request path never
    touches: `/health` then answered 200 and the pool-degradation contract
    went unverified for the app-role pool (QA t_592a6e1a F3). The pinned
    engine carries the app role's URL and the request stamping listener, so
    the boot-time role verification and the tenant stamp see exactly what
    production sees.
    """
    app_url = get_settings().app_database_url
    if not app_url:
        yield pinned_pool_engine
        return
    engine = create_engine(
        app_url,
        pool_size=POOL_SIZE,
        max_overflow=POOL_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
        pool_pre_ping=True,
    )
    attach_request_stamping(engine)
    previous_engine, previous_url = deps._request_engine, deps._request_engine_url
    deps._request_engine = engine
    deps._request_engine_url = app_url
    try:
        yield engine
    finally:
        deps._request_engine, deps._request_engine_url = previous_engine, previous_url
        engine.dispose()


@pytest.fixture(name="forecast_tenant")
def forecast_tenant_fixture(pinned_pool_engine: Engine) -> Iterator[dict[str, str]]:
    """A committed tenant with no rows — `/forecast/run` answers 200 on it."""
    with Session(pinned_pool_engine) as session:
        user = User(email="m148-deadlock@example.com", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        assert user.id is not None
        user_id = user.id
    yield {"Authorization": f"Bearer {create_access_token(user_id)}"}
    with Session(pinned_pool_engine) as session:
        session.execute(text("delete from users where id = :id"), {"id": user_id})
        session.commit()


@pytest.fixture(name="live_server")
def live_server_fixture(pinned_pool_engine: Engine, pinned_request_pool: Engine) -> Iterator[str]:
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


def _idle_in_transaction(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "select count(*) from pg_stat_activity "
                "where datname = current_database() and state = 'idle in transaction'"
            )
        ).scalar_one()


def test_pool_wait_does_not_freeze_the_event_loop(
    pinned_pool_engine, pinned_request_pool, forecast_tenant, live_server
) -> None:
    # Exhaust the pool `get_db` checks out: the maintenance engine in the
    # default config, the key-pointed request engine once APP_DATABASE_URL
    # arms it — holding the other one starves nothing and answers 200
    # (QA t_592a6e1a F3).
    held = [pinned_request_pool.connect() for _ in range(POOL_TOTAL)]
    assert len(held) == POOL_TOTAL

    outcome: dict[str, object] = {}

    def request_forecast() -> None:
        started = time.perf_counter()
        try:
            response = httpx.get(
                f"{live_server}{RUN}?horizon_months=24&granularity=day",
                headers=forecast_tenant,
                timeout=30,
            )
            outcome["status"] = response.status_code
        except Exception as exc:  # the assertion reports it
            outcome["status"] = f"{type(exc).__name__}: {exc}"
        outcome["seconds"] = round(time.perf_counter() - started, 3)

    try:
        # With the pool empty the API degrades to BOUNDED failures, it does
        # not hang: /health needs a connection, waits the pool timeout
        # off-loop, and answers 503 from its own probe.
        degraded = httpx.get(f"{live_server}{API}/health", timeout=30)
        assert degraded.status_code == 503
        assert degraded.json()["status"] == "unhealthy"

        waiter = threading.Thread(target=request_forecast, daemon=True)
        waiter.start()
        # Give the request time to reach its pool checkout and start waiting.
        time.sleep(0.5)

        # `/docs` is a real request through the whole middleware + dependency
        # stack, and it answers 401 without ever taking a connection
        # (`get_db` opens a Session lazily; `require_docs_access` refuses an
        # anonymous caller before any query). Its latency IS the loop's.
        started = time.perf_counter()
        health_of_the_loop = httpx.get(f"{live_server}/docs", timeout=30)
        loop_seconds = time.perf_counter() - started
        print(f"\nPERF loop-probe={loop_seconds * 1000:.1f}ms while pool was empty")

        assert health_of_the_loop.status_code == 401
        assert loop_seconds < LOOP_BUDGET_S, (
            f"the event loop was blocked for {loop_seconds:.2f}s by a request "
            f"waiting for a pool connection (budget {LOOP_BUDGET_S}s). Every "
            "other request — including the handlers holding the pool — shares "
            "that loop, so this is the 2026-10-11 outage: a saturated pool "
            "became a whole-API freeze."
        )

        for connection in held:
            connection.close()
        waiter.join(30)
    finally:
        for connection in held:
            connection.close()

    assert outcome["status"] == 200, outcome
    # Once the pool drains, the parked request finishes and closes its session.
    deadline = time.perf_counter() + 10
    while time.perf_counter() < deadline:
        if _idle_in_transaction(pinned_pool_engine) == 0:
            break
        time.sleep(0.2)
    assert _idle_in_transaction(pinned_pool_engine) == 0, (
        "sessions are still 'idle in transaction' after the pool drained"
    )


def test_forecast_run_answers_on_a_healthy_pool(
    pinned_pool_engine, forecast_tenant, live_server
) -> None:
    """The route itself is fine — the outage was the pool, not the forecast."""
    with httpx.Client(base_url=live_server, timeout=30) as client:
        for _ in range(POOL_TOTAL + 2):
            response = client.get(
                f"{RUN}?horizon_months=24&granularity=day", headers=forecast_tenant
            )
            assert response.status_code == 200
            assert response.json()["disclaimer"] == "Projection, not a prediction."
    assert _idle_in_transaction(pinned_pool_engine) == 0


def test_price_stream_parks_without_a_pooled_connection(pinned_pool_engine) -> None:
    """The SSE price stream must not hold a connection while it waits.

    `price_stream_generator` polls every 5 s and a stream stays open for as
    long as a tab does. Its session used to wrap the `await asyncio.sleep`
    AND the `yield`, so every open stream parked one pooled connection
    `idle in transaction` for its whole life — N open streams was N
    connections of a 15-connection pool, the same exhaustion as the outage.
    (docs/01 § "Sessions and the event loop".)
    """
    import asyncio
    import datetime as dt
    from decimal import Decimal

    from app.api._price_stream import price_stream_generator
    from app.models.prices import Price, PriceSource
    from app.models.securities import Security, SecurityKind

    with Session(pinned_pool_engine) as session:
        user = User(email="m148-stream@example.com", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        assert user.id is not None
        user_id = user.id
        security = Security(
            user_id=user_id,
            symbol="STREAM.TO",
            name="Stream Corp",
            kind=SecurityKind.etf,
            listing_currency="CAD",
            exchange="TSX",
        )
        session.add(security)
        session.commit()
        session.refresh(security)
        assert security.id is not None
        security_id = security.id
        session.add(
            Price(
                user_id=user_id,
                security_id=security_id,
                date=dt.date.today() - dt.timedelta(days=1),
                price=Decimal("12.34"),
                source=PriceSource.manual,
            )
        )
        session.commit()

    stream = price_stream_generator([security_id], user_id)  # kept alive on purpose
    try:
        first = asyncio.run(stream.__anext__())
        assert first.startswith("data: ")
        assert pinned_pool_engine.pool.checkedout() == 0, (
            "the price stream is parked at a yield while holding a pooled "
            "connection; every open stream would pin one until the tab closes"
        )
    finally:
        asyncio.run(stream.aclose())
        with Session(pinned_pool_engine) as session:
            for table in ("prices", "securities"):
                session.execute(text(f"delete from {table} where user_id = :id"), {"id": user_id})  # noqa: S608 -- table from fixed tuple in this test
            session.execute(text("delete from users where id = :id"), {"id": user_id})
            session.commit()


def test_get_db_stays_a_sync_generator() -> None:
    """`get_db` must stay a SYNC generator.

    FastAPI resolves sync generator dependencies in a worker thread, so the
    session is opened — and closed — off the event loop (docs/01
    § "Request lifecycle"). Making it `async def` would move that onto the
    loop and reopen the door this test guards.
    """
    assert inspect.isgeneratorfunction(deps.get_db), (
        "get_db must be a sync generator dependency, not an async one"
    )

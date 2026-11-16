"""Test fixtures: a fresh PER-SESSION test DB + one alembic upgrade head per session.

Runs against a REAL Postgres (docs/01) — never SQLite. The integration owner
provides the server; a missing/unreachable DB fails loudly on purpose.

The suite database is named for the SESSION that owns it (see
``_session_db_id`` below), never for the server: one Postgres serves every run
on the box, and a fixed name let two concurrent ``uv run pytest`` sessions drop
each other's database mid-run (t_00b6d06d).
"""

import os
import re
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlmodel import Session

# Fail-fast secret (#3): JWT_SECRET must be non-default BEFORE app.main below
# is imported (create_app builds Settings at import time). RATE_LIMIT_ENABLED
# keeps the auth limiter off unless a test opts back in, and the M59 delivery
# worker (docs/37) stays OFF: tests drive run_once explicitly so no background
# tick can race a fixture.
os.environ.setdefault("JWT_SECRET", "test-secret-not-default-0123456789abcd")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ["NOTIFY_WORKER_ENABLED"] = "false"
# M150: the FX rate-book refresh worker opens a BoC fetch; tests drive
# `fx_refresh.run_once` explicitly, so no background tick can fetch.
os.environ["FX_REFRESH_ENABLED"] = "false"
# M173: the benchmark-series refresh worker opens a yfinance fetch (docs/28).
os.environ["BENCHMARK_REFRESH_ENABLED"] = "false"
os.environ["DIVIDEND_REFRESH_ENABLED"] = "false"
# M187: the scheduled price-refresh worker opens a yfinance fetch on wall-
# clock ticks (docs/07 §Scheduled price refresh); tests drive
# `price_refresh.run_once` explicitly, so no background tick can fetch.
os.environ["PRICE_REFRESH_ENABLED"] = "false"

import app.api.deps as deps
from alembic import command
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import app
from app.models.categories import Category
from app.models.users import User

BACKEND_DIR = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://fintrack:fintrack@localhost:5432/fintrack",
)

# --- Per-session database identity -------------------------------------------------
# This box runs ONE Postgres, and every `uv run pytest` session used to derive
# the same fixed name from DATABASE_URL (`<base>_test`, plus `<suite>_demo` /
# `<suite>_perf` in the hermetic modules). Two sessions running at once therefore
# shared one database: the second one's `DROP DATABASE ... WITH (FORCE)` killed
# the first one's backends mid-run (`psycopg.errors.AdminShutdown`) and its
# `alembic upgrade` re-wrote the schema underneath it (`lock_timeout`, then
# `column users.forecast_scenarios does not exist`) — t_00b6d06d.
#
# So the suite database is named for the SESSION that owns it: TEST_DB_ID when
# you pin one, otherwise this process's pid. A pid cannot be reused while its
# owner lives, so two live sessions can never derive the same name — no lock, no
# coordination, and it still holds across worktrees (t_00b6d06d).
_MAX_DB_IDENT = 63  # Postgres truncates over-long identifiers instead of rejecting them
_TEST_MARKER = "_test"  # separates the (shortenable) base from the (never shortenable) id
_DERIVED_SUFFIXES = ("_demo", "_perf")  # what the hermetic modules append to it


def _session_db_id(wanted: str | None) -> str:
    """This run's database id: ``TEST_DB_ID`` if pinned, else the pid.

    Pin one (``TEST_DB_ID=t_a1b2c3d4 uv run pytest -q``) when you want a name
    you can find again after the run; leave it unset for the default, which
    needs no cooperation from whoever else is running the suite.
    """
    ident = re.sub(r"[^A-Za-z0-9_]", "_", wanted or "").strip("_")
    if not ident:
        return f"p{os.getpid()}"
    # The suite now runs under xdist by default (addopts `-n auto`): workers are
    # separate processes, so an UNPINNED run isolates for free (each worker is
    # its own pid), but one PINNED id would be handed to every worker at once
    # and they would collide on one database. Append the worker slot so a pin
    # stays unique per worker: `qa29_gw0`, `qa29_gw1`... A child run that must
    # own the bare pin pops PYTEST_XDIST_WORKER (see the isolation tests).
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    return f"{ident}_{worker}" if worker else ident


def _suite_db_name(base_db: str, db_id: str) -> str:
    """``<base>_test_<db_id>``, always inside Postgres' 63-byte identifier limit.

    The id goes on the END and the base is what gets shortened: truncating the
    id instead would quietly hand two sessions the same name again, which is the
    bug this naming exists to prevent. An id too long to fit is therefore
    REJECTED, never shortened: Postgres would truncate an over-long result
    silently and two distinct ids could land on one database. Room is left for
    the `_demo` / `_perf` databases the hermetic modules derive from this name.
    """
    tail = f"{_TEST_MARKER}_{db_id}"
    room = _MAX_DB_IDENT - max(len(suffix) for suffix in _DERIVED_SUFFIXES)
    if len(tail) > room:
        raise ValueError(
            f"database id {db_id!r} does not fit: {len(tail)} characters with its"
            f" {_TEST_MARKER!r} marker, {room} available.\n"
            "  Shortening it would let two long ids collide on one database, so it"
            f" is refused instead. Use a TEST_DB_ID of at most"
            f" {room - len(_TEST_MARKER) - 1} characters."
        )
    return f"{str(base_db)[: room - len(tail)]}{tail}"


TEST_DB_ID = _session_db_id(os.environ.get("TEST_DB_ID"))
TEST_DB_NAME = _suite_db_name(make_url(BASE_URL).database, TEST_DB_ID)
# The hermetic `_demo` / `_perf` names, for the same session: they are derived
# in their own modules from whatever DATABASE_URL points at, so they follow.
SESSION_DB_NAMES = (TEST_DB_NAME, *(f"{TEST_DB_NAME}{sfx}" for sfx in _DERIVED_SUFFIXES))
# The five modules that legitimately repoint DATABASE_URL at their own hermetic
# database for their own module scope. The leak guard stands down inside them
# and resumes on the NEXT test — which is exactly where a missed restore shows
# up as one obvious failure instead of a misleading cascade.
# NOTE: all five now restore exception-safely — everything from the env swap
# onward sits in try/finally (test_perf_budgets via t_435c2d14, the other four
# via t_0a0f56cf / t_17d025b2). The whitelist remains because the swap itself is
# still legitimate; it is the missing restore that is no longer possible.
_SWAP_MODULES = frozenset(
    {
        "test_perf_budgets.py",
        "test_cashflow_calendar_demo.py",
        "test_demo_seed.py",
        "test_export_zip_demo.py",
        # M136 wrote this one WITHOUT the swap (t_17d025b2): its module-wide
        # `seed_demo_account=True` made every TestClient LIFESPAN seed the whole
        # demo tenant into the SUITE database, committed, for the rest of the
        # session — 20 failures in five later modules. See the guard below.
        "test_locked_fixture_tenant.py",
        # docs/55: hermetic `<suite>_rlsrm` database for the upgrade →
        # downgrade → upgrade reversibility proof (same swap discipline).
        "test_rls_migration.py",
    }
)


def _url_with_db(db_name: str) -> str:
    url = make_url(BASE_URL)
    return url.set(database=db_name).render_as_string(hide_password=False)


# The one URL every test outside test_perf_budgets must see. Recomputed from
# BASE_URL rather than captured, so it can never drift from what _database sets.
SUITE_DATABASE_URL = _url_with_db(TEST_DB_NAME)


def _process_alive(pid: int) -> bool:
    """Is `pid` a live process on this host? (PermissionError = exists, not ours.)"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# Only names this scheme produces are ever candidates for reaping — a legacy
# `fintrack_test`, a hand-made database or another tool's schema does not match
# and is therefore untouchable by the reaper. The optional letters after the pid
# cover a pinned id like `p12345ann`, where the digits belong to the session
# that pinned it (alive while it runs, dead afterwards — either way the right
# answer).
_LEFTOVER_DB_RE = re.compile(r"^.+_test_p(?P<pid>\d+)[a-z]*(?:_(?:demo|perf))?$")


def _connections_to(conn, db_name: str) -> int:
    """Backends currently connected to `db_name` (0 when nobody is using it).

    EVERY row, deliberately: the reaper is not asking "is a peer session using
    this" but "may I safely DROP it", and a database an autovacuum worker is
    sitting on is not safe to drop either. The guard below asks a narrower
    question and therefore counts a narrower set — see `_counts_as_a_session`.
    """
    return int(
        conn.execute(
            text("SELECT count(*) FROM pg_stat_activity WHERE datname = :name"),
            {"name": db_name},
        ).scalar_one()
    )


# The guard may not judge a busy name on the first sample it takes. Two things
# are measured here rather than assumed (t_d06c85c7, "the connection guard
# counts a backend that is already gone"):
#
# 1. WHICH rows count. `pg_stat_activity` holds one row per backend ATTACHED to
#    a database, and an autovacuum worker or a parallel worker is Postgres's own
#    machinery, not a pytest run — counting it refuses a name nobody is using.
#    A 120 s sample of this cluster, read as superuser, caught two such rows on
#    live suite databases: `autovacuum worker`, role `unset`, ~0.2 s and ~0.6 s.
# 2. WHEN to decide. `Engine.dispose()` closes the socket in THIS process; the
#    server's backend still has to be scheduled to notice and take itself out of
#    `pg_stat_activity`. Probed on this box with 120 runs of the exact failing
#    sequence under 10 CPU hogs: 19/120 samples still saw the backend that had
#    just been disposed, every one of them `client backend` / `idle`, and every
#    one of them left by itself — measured 0-12 ms. So "busy" has to mean "busy
#    and still busy a bounded moment later": the deadline is ~150x the widest
#    measured window, which is also what absorbs a background worker of the
#    first kind. Both numbers are why this is a wait-then-recheck rather than a
#    filter alone — the stale row IS a client backend, so no row filter would
#    have removed it.
_GUARD_POLL = 0.01  # first re-sample: the measured staleness window is 0-12 ms
_GUARD_MAX_POLL = 0.25  # backoff cap, so a name that really is taken costs no CPU
_GUARD_DEADLINE = 2.0  # ~150x the widest measured window; a real collision still fails


def _counts_as_a_session(backend_type: str | None) -> bool:
    """Is this `pg_stat_activity` row a session that could be a peer pytest run?

    `None` is not "no backend" — it is a backend whose `backend_type` THIS ROLE
    may not read. Postgres blanks `state`/`backend_type` for rows owned by a
    role we have no privileges of (probed here: a `postgres`-role backend shows
    `backend_type IS NULL` while `usename`, `datname` and `usesysid` stay
    readable), which also covers a background worker whose role is unset.

    Those rows COUNT, deliberately. A peer session running as another role is
    exactly the collision this guard exists for, and skipping what we cannot
    read would trade a flake for a silent, far worse one: two runs would share
    a database again, which is the destruction this naming scheme was built to
    stop. The cost is the opposite kind of error — a background worker we
    cannot type may hold the name for the whole `deadline` and draw a refusal
    that names a session which is not there. That error is loud, it is bounded
    by `deadline`, and the measured workers (`autovacuum worker`, role `unset`,
    ~0.2-0.6 s in a 120 s cluster sample) sit well inside it. Loud and rare
    beats silent and destructive, so the unreadable case is never skipped.

    A readable non-session type (`autovacuum worker`, `parallel worker`, …) is
    not a pytest run and must not hold a name hostage at all.
    """
    return backend_type is None or backend_type == "client backend"


def _backend_types_on(conn, db_name: str) -> list[str | None]:
    """Every `backend_type` `pg_stat_activity` reports for `db_name`.

    `None` entries are backends whose type this role may not read (see
    `_counts_as_a_session`); the list is empty when nothing is attached.
    """
    rows = conn.execute(
        text("SELECT backend_type FROM pg_stat_activity WHERE datname = :name"),
        {"name": db_name},
    )
    return [row[0] for row in rows]


def _assert_databases_free(
    conn, db_names: tuple[str, ...], *, deadline: float = _GUARD_DEADLINE
) -> None:
    """Refuse to take over a database another live session is already using.

    A pid-derived name can only collide when TEST_DB_ID was pinned twice (two
    runs, same id) — and stealing it would recreate exactly the cross-run
    destruction this naming scheme exists to prevent, just less often. Fail
    loudly at session start instead of corrupting somebody's run mid-flight.

    Decides over `deadline`, not on the first sample: a backend whose client
    `dispose()`d is still in `pg_stat_activity` until the server schedules it
    to notice, so an instant verdict reports a name as taken by a session that
    has already gone (the t_d06c85c7 flake). Re-sampling cannot turn a real
    collision into a pass — a peer session holds its connection for the whole
    `deadline` — so the only cost of waiting is `deadline` before a genuine
    refusal, and the only thing the wait buys is the end of the race.
    """
    expires = time.monotonic() + deadline
    delay = _GUARD_POLL
    while True:
        busy = [
            name
            for name in db_names
            if any(_counts_as_a_session(t) for t in _backend_types_on(conn, name))
        ]
        if not busy:
            return
        remaining = expires - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "another pytest session is already using "
                + ", ".join(busy)
                + f"\n  TEST_DB_ID={TEST_DB_ID!r} is not unique on this host."
                "\n  Unset TEST_DB_ID for an automatic per-run name, or give this"
                "\n  run an id of its own."
            )
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, _GUARD_MAX_POLL)


def _unsignalable_blockers(conn, db_name: str) -> list[str]:
    """Backends attached to `db_name` that THIS role may not signal, as labels.

    Best effort diagnostics for a `drop_database_force` timeout, so a failure
    here must never mask the `DROP` error it is reporting (hence the guard).
    Same role is always signalable — `acl.c::has_privs_of_role` takes the
    `member == role` fast path — and so is a role we hold the privileges of;
    everything else (another role's session, or a worker whose role is not set
    yet, `usesysid IS NULL`) is not. `pg_stat_activity` shows us other
    sessions' `pid`/`usename` while hiding `state`/`backend_type`.
    """
    try:
        rows = conn.execute(
            text(
                "SELECT pid, coalesce(usename, 'unset role')"
                " FROM pg_stat_activity"
                " WHERE datname = :name AND pid <> pg_backend_pid()"
                "   AND NOT (usesysid IS NOT NULL AND ("
                "       usesysid = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
                "       OR pg_has_role(current_user, usesysid, 'USAGE')))"
            ),
            {"name": db_name},
        ).all()
    except SQLAlchemyError:
        return []
    return [f"pid {pid} ({who})" for pid, who in rows]


# --- FORCE-dropping a database --------------------------------------------------
# `DROP DATABASE … WITH (FORCE)` is the only way to drop a database somebody is
# still connected to, and it can fail for a reason that has nothing to do with
# this run. Postgres signals the attached backends only AFTER it has validated
# every one of them (`procarray.c::TerminateOtherDBBackends`), and a role that
# is neither superuser nor in `pg_signal_backend` may not signal a backend whose
# owning role it holds no privileges of — which includes a background worker
# whose role is still unset, because `has_privs_of_role(me, InvalidOid)` is
# false (`acl.c`). So one un-signalable process aborts the whole statement with
# `InsufficientPrivilege: permission denied to terminate process` and drops
# NOTHING: that is what failed 1 of 3 concurrent suite runs, on its own
# `<suite>_perf`, at `test_perf_harness` (t_00b6d06d, review round 1) —
# autovacuum attaches to a freshly seeded database at random. Such workers
# detach on their own, so wait for the statement to become runnable and retry;
# if it never does, say exactly what is still attached instead of hanging.
_FORCE_DROP_ERROR = "permission denied to terminate process"
_FORCE_DROP_POLL = 0.2  # first retry delay — the transient case settles here
_FORCE_DROP_MAX_POLL = 2.0  # …so a blocker that never leaves cannot flood the log


def drop_database_force(conn, db_name: str, *, deadline: float = 30.0) -> None:
    """`DROP DATABASE IF EXISTS "<db_name>" WITH (FORCE)`, retried until it runs.

    `conn` must be an AUTOCOMMIT connection — every caller opens its admin
    engine that way — because a failed statement must leave the connection
    usable for the next attempt instead of poisoning a transaction.
    """
    statement = text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    expires = time.monotonic() + deadline
    delay = _FORCE_DROP_POLL
    while True:
        try:
            conn.execute(statement)
            return
        except ProgrammingError as exc:
            if _FORCE_DROP_ERROR not in str(exc):
                raise  # some other problem: our retry would only hide it
            if time.monotonic() >= expires:
                blockers = _unsignalable_blockers(conn, db_name) or [
                    "nothing visible in pg_stat_activity from this role"
                ]
                raise RuntimeError(
                    f'DATABASE "{db_name}" survived DROP ... WITH (FORCE) for {deadline}s;'
                    "\n  "
                    + "\n  ".join(blockers)
                    + "\n  that backend cannot be signalled by this role — disconnect it,"
                    "\n  or grant its role pg_signal_backend, then run the suite again."
                ) from exc
        time.sleep(min(delay, max(expires - time.monotonic(), 0.0)))
        delay = min(delay * 2, _FORCE_DROP_MAX_POLL)


# --- wall-clock budgets on a shared box ----------------------------------------
# docs/34 settles this: a busy machine is not a stable baseline, so the QUERY
# COUNT is the enforced budget and latency is only reported. This box runs one
# Postgres and one CPU pool, and AGENTS §5 lets suites run CONCURRENTLY, so a
# test that asserts `elapsed < 1500` is measuring how loaded the machine is,
# not how indexed its query is. Two such pins already reded on contention alone
# (t_7100c168's 1.545 s detail first paint while five other agents' suites ran;
# t_428d4b99's 2355 ms drill at load 11 on 4 cores, in one of two simultaneous
# full suites, green solo in 6.75 s for the whole file).
#
# So a wall-clock pin has exactly ONE sanctioned shape: every plan / query-count
# / functional assertion stays enforced unconditionally, the clock is measured,
# and only the stopwatch assertions go behind `latency_budget_blocker()`. When
# the box cannot answer the clock, those assertions SKIP — with the measured
# numbers in the reason, so the evidence survives instead of vanishing into a
# red. A clock pin that skips here is still a pin on a quiet box; a clock pin
# that does NOT go through here is a flake generator.


def latency_budget_blocker() -> str | None:
    """Why a WALL-CLOCK budget cannot be answered on this box right now.

    `None` when the machine is quiet enough to answer (1-minute load average
    below the core count); otherwise the skip reason, naming the load and the
    cores, for the caller to hand to `pytest.skip`.

    Callers keep every non-clock assertion enforced on every run — this gates
    the stopwatch only (docs/34 "what is NOT enforced", AGENTS §4).
    """
    load1 = os.getloadavg()[0]
    cores = os.cpu_count() or 1
    if load1 >= cores:
        return (
            f"1-minute load average {load1:.1f} on {cores} cores — a wall-clock"
            " budget measures the box, not the query (docs/34: latency is"
            " reported, never enforced, on a shared machine)"
        )
    return None


def _reap_dead_run_databases(conn, keep: tuple[str, ...]) -> list[str]:
    """Drop databases left behind by pytest sessions that are no longer running.

    Nothing drops its own databases on teardown (they are worth keeping around
    while you debug a red run), and a killed session — SIGTERM from the
    dispatcher, `DROP DATABASE ... WITH (FORCE)` from an old-code run — would
    otherwise leak three ~11 MB databases per crash. So the NEXT session cleans
    up after every session that came before it:

    * `keep` is never touched — those are this run's own databases;
    * only names THIS ROLE owns are candidates. `pg_database` lists every
      role's databases, and this box runs agents under different postgres
      roles (t_84ce5704: QA's `fintrack_rlsqa_..._pNNNleak` child leftover
      matched the name scheme, its owner pid was dead, and the DROP died with
      `InsufficientPrivilege: must be owner of database` — failing a session
      that had nothing to do with that leftover);
    * only names this scheme generates are candidates (see `_LEFTOVER_DB_RE`);
    * a name is dropped only when its pid is dead AND nothing is connected to it
      right now. On this host a live run's pid is always alive, so a concurrent
      run's databases can never match; the connection check is the second,
      independent stop for a name whose pid came from another pid namespace.
    """
    names = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT datname FROM pg_database"
                " WHERE datdba = (SELECT oid FROM pg_roles WHERE rolname = CURRENT_USER)"
            )
        )
    ]
    reaped: list[str] = []
    for name in names:
        if name in keep:
            continue
        match = _LEFTOVER_DB_RE.match(name)
        if match is None or _process_alive(int(match["pid"])) or _connections_to(conn, name):
            continue
        drop_database_force(conn, name)  # may need to wait out an un-signalable backend
        reaped.append(name)
    return reaped


@pytest.fixture(scope="session", autouse=True)
def _database(request: pytest.FixtureRequest) -> Iterator[None]:
    """Create THIS session's test DB, stamp schema via alembic, aim settings at it."""
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    capture = request.config.pluginmanager.get_plugin("capturemanager")

    def announce(message: str) -> None:
        """Put `message` on the terminal even though we are inside a test.

        pytest captures stdout/stderr from conftest loading onward
        ("trigger conftest loading but while capturing", _pytest/capture.py), so
        a write during session setup lands in the capture buffer — which a
        PASSING run throws away. It escapes only when stdout is BLOCK-buffered:
        the flush then happens at interpreter shutdown, after capture has been
        put back, which is why "which database am I on" showed up in an
        interactive shell and vanished under a background runner
        (PYTHONUNBUFFERED=1). Lifting capture for the write makes it appear in
        both cases; `flush` forces it out before capture comes back.
        """
        if reporter is None:
            return
        if capture is not None:
            capture.suspend_global_capture()
        try:
            reporter.write_line(message)
            reporter.flush()
        finally:
            if capture is not None:
                capture.resume_global_capture()

    admin = create_engine(_url_with_db("postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            announce(f"suite database: {TEST_DB_NAME}  (TEST_DB_ID={TEST_DB_ID})")
            _assert_databases_free(conn, SESSION_DB_NAMES)
            for name in _reap_dead_run_databases(conn, SESSION_DB_NAMES):
                announce(f"reaped database left by a dead run: {name}")
            drop_database_force(conn, TEST_DB_NAME)
            conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
            # BUG-3 guard: background tasks open their OWN session and commit
            # while this session's outer transaction still holds the parent
            # rows (rollback-per-test design). In prod there is no spanning
            # transaction, so the FK wait can only happen here — and during
            # market hours a successful yfinance fetch made it wait forever
            # (suite wedged inside TestClient). lock_timeout turns that deadlock
            # into the wrappers' designed swallowed failure instead.
            conn.execute(text(f"ALTER DATABASE \"{TEST_DB_NAME}\" SET lock_timeout = '500ms'"))
    finally:
        admin.dispose()

    os.environ["DATABASE_URL"] = _url_with_db(TEST_DB_NAME)
    get_settings.cache_clear()
    deps._engine = None  # force deps to rebuild its engine against the test DB
    # Demo lifespan seeding would pollute every test DB (extra user + 10
    # categories break unfiltered asserts). test_demo_seed opts back in.
    os.environ["SEED_DEMO_ACCOUNT"] = "false"  # survives cache_clears (M9 lesson)
    os.environ["JWT_SECRET"] = "test-secret-not-default-0123456789abcd"
    os.environ["RATE_LIMIT_ENABLED"] = "false"
    os.environ["NOTIFY_WORKER_ENABLED"] = "false"  # M59: no background delivery ticks
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", _url_with_db(TEST_DB_NAME))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(name="session")
def session_fixture() -> Iterator[Session]:
    """Session bound to an outer transaction rolled back after each test."""
    from app.api.deps import get_engine

    engine = get_engine()
    with engine.connect() as conn:
        transaction = conn.begin()
        with Session(bind=conn, join_transaction_mode="create_savepoint") as session:
            yield session
        transaction.rollback()


@pytest.fixture(name="client")
def client_fixture(session: Session) -> Iterator[TestClient]:
    def override_get_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


# The single "today" every spending service reads (`today_et`) is imported BY
# NAME into each module, so freezing the clock means patching every binding.
# The date is deliberately mid-month: `compute_insights` sums the current month
# over `[first_of_month, today)`, a window that is EMPTY on the 1st of a month,
# so no seeding strategy can satisfy a current-month assertion on that date
# (this is what took CI red on 2026-10-01 — see tests/test_insights.py).
FROZEN_TODAY = date(2026, 6, 15)

_TODAY_BINDINGS = (
    "app.services.txn_filters.today_et",
    "app.services.insights.today_et",
    "app.services.budgets.today_et",
    "app.services.subscriptions.today_et",
    # M170: the confirmed-subscription query reads the clock itself (its own
    # default), so a frozen-clock test that reaches the calendar's
    # subscription series has to freeze THIS binding too — one "today" for
    # every reader (docs/29 F1d).
    "app.services.confirmed_subscriptions.today_et",
    "app.services.dividend_calendar.today_et",
    "app.api.reports.today_et",
    "app.api.forecast.today_et",
    "app.api.spending_alerts.today_et",
    "app.api.notifications.today_et",
)


@pytest.fixture(name="frozen_today")
def frozen_today_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[date]:
    """Pin the app's `today_et()` clock to FROZEN_TODAY for one test.

    Use it for any assertion over a CURRENT-month/current-period window —
    those windows shrink to nothing on the 1st and cannot be seeded. Tests
    that seed rows budget-style (window bounded below only) keep the real
    clock: `_TODAY.replace(day=...)` is already date-independent there.
    """
    for target in _TODAY_BINDINGS:
        monkeypatch.setattr(target, lambda: FROZEN_TODAY)
    yield FROZEN_TODAY


@pytest.fixture(autouse=True)
def _guard_database_url(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail LOUDLY on a leaked DATABASE_URL instead of letting it mislead.

    `test_perf_budgets` swaps DATABASE_URL at `<suite>_perf` for its module. It
    used to do that swap before its own setup could fail, with the restore
    sitting after a `yield` that was never reached — so a single setup error
    left the whole REST of the session pointed at the perf DB. Every later test
    then failed against a database that already held the demo tenant, and the
    reported symptoms (duplicate emails, wrong severity) named files that had
    nothing to do with the break (t_435c2d14).

    Checking on entry AND exit turns that into one obvious failure — at the
    first test that no longer expects the perf URL, or on the test that leaked
    it — with the offending value in the message.
    """
    if Path(str(getattr(request.node, "path", ""))).name in _SWAP_MODULES:
        yield  # that module's own fixture owns the swap and restores it
        return
    _assert_suite_database_url("before the test")
    yield
    _assert_suite_database_url("after the test")


def _assert_suite_database_url(when: str) -> None:
    current = os.environ.get("DATABASE_URL")
    assert current == SUITE_DATABASE_URL, (
        f"DATABASE_URL leaked {when}: expected the suite DB\n"
        f"  {SUITE_DATABASE_URL}\nbut found\n  {current}\n"
        "A fixture swapped DATABASE_URL and did not restore it — see the "
        "try/finally in tests/test_perf_budgets.py::_perf_database_gen."
    )


# --- the leak shape the URL guard cannot see ------------------------------------
# `_guard_database_url` above fails loudly when a fixture leaves DATABASE_URL
# pointed somewhere else. It CANNOT see the sibling shape: a seed that opens an
# ENGINE OF ITS OWN and commits, while every test rolls back through the
# `session` fixture's outer transaction. `app.main.lifespan` →
# `demo_seed.seed_demo_from_engine()` is exactly that (its own engine, its own
# session, real COMMIT) — legitimate in production, and invisible here until
# t_17d025b2, where `test_locked_fixture_tenant` opted back into
# `seed_demo_account` while a TestClient could still start: the FULL demo
# tenant (user + 10 categories + 357 transactions) landed in the suite database
# and failed 20 tests in FIVE later modules — duplicate `uq_users_email`, and
# `category_by_name("groceries")` finding two rows — while the report named
# every victim and none of the polluter.
#
# So check the one row that shape starts with, on the SUITE database directly
# (never via DATABASE_URL: the hermetic modules repoint that at their own
# database on purpose, and their committed demo rows are theirs to keep). The
# check runs AFTER the test, on a connection of its own, so it sees only what
# the test COMMITTED — the test session's connection is inside the rolled-back
# transaction and would see the row either way.
_DEMO_TENANT_EMAIL = "demo@fintracker.local"


@pytest.fixture(scope="session")
def _suite_probe_engine() -> Iterator[Engine]:
    """A second connection to the SUITE database — the only one that can see
    what earlier tests committed. Nothing else in the suite may commit at all,
    which is what makes "no committed demo tenant" a whole-session invariant.
    """
    engine = create_engine(SUITE_DATABASE_URL)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _guard_committed_demo_tenant(
    request: pytest.FixtureRequest, _suite_probe_engine: Engine
) -> Iterator[None]:
    """Fail the POLLUTING test, not the twenty tests it later breaks.

    Deliberately narrow: it asserts the demo tenant row and nothing else,
    because that is the row every module that opts back into
    `seed_demo_account` commits. Widen it when a different row proves it can
    leak the same way; the mechanism (own engine + COMMIT inside a
    rollback-per-test suite) does not change.
    """
    yield
    with _suite_probe_engine.connect() as conn:
        leaked = conn.execute(
            text("SELECT count(*) FROM users WHERE email = :email"),
            {"email": _DEMO_TENANT_EMAIL},
        ).scalar_one()
    assert leaked == 0, (
        f"{request.node.nodeid} COMMITTED the demo tenant into the suite database\n"
        f"  ({_DEMO_TENANT_EMAIL} — {leaked} row(s) now visible to every later test)\n"
        "This suite rolls every test back through the `session` fixture, so the\n"
        "only way to leave a row behind is an engine of your own: the lifespan's\n"
        "seed_demo_from_engine (app/main.py) opens one, which means a module that\n"
        "sets seed_demo_account while a TestClient can start seeds the demo for\n"
        "REAL. Make that module hermetic — repoint DATABASE_URL at `<suite>_demo`\n"
        "for the module scope and add it to conftest._SWAP_MODULES, exactly like\n"
        "tests/test_demo_seed.py — or stop the seed before TestClient startup.\n"
        "  tests/conftest.py::_guard_database_url cannot catch this shape: it\n"
        "  asserts the DATABASE_URL, not the rows already committed into the\n"
        "  CORRECT database."
    )


# --- the tenant-scoping CONTRACT (M178) ------------------------------------------
# Three waves, three incidents, ONE class — and each was fixed one leak at a
# time, which is why it kept coming back:
#
#   t_17d025b2  `test_locked_fixture_tenant` let every TestClient lifespan seed
#               the whole demo tenant into the SUITE database, committed. 20
#               tests in five LATER modules went red (6x duplicate uq_users_email,
#               13x MultipleResultsFound) and the report named every victim and
#               none of the polluter.
#   t_a451f171  `test_m149_offloop_routes` drives a LIVE server whose
#               `POST /auth/register` commits a user + the 10 DEFAULT_CATEGORIES
#               through the app's own engine; its teardown deleted the account
#               but not the identity, so a foreign "groceries" shadowed this
#               user's row in an unfiltered read. Heap order decides which of two
#               same-named rows a raw `select(Category)` returns, so the file
#               went red on 2 of ~8 full runs and never solo.
#   t_70d46e5e  the same leak, seen from the read side: a seed snapshot in
#               `test_ynab_migration` enumerated ALL users' categories.
#
# The mechanism never changes: a test opens an ENGINE OF ITS OWN (live server,
# lifespan seed, `get_engine()`) and COMMITS rows that the `session` fixture's
# rollback cannot reach. So the contract has two sides, and this harness now
# enforces both — a leak is a red test even when nothing happens to read it:
#
#   R1 WRITE SIDE — nothing a module commits may survive the module.
#      `_guard_module_left_no_tenant_state` snapshots the tenant footprint at
#      module start and fails at module end when any tenant table differs, so
#      the module that leaked is named while its own scope is still on screen.
#      `_guard_no_committed_tenant_survives` is the session-level backstop.
#   R2 READ SIDE — a read must be scoped to the test's own tenant, and the
#      suite database is DELIBERATELY dirty to prove it: `_decoy_tenant`
#      commits a foreign tenant (a user + the 10 DEFAULT_CATEGORIES, i.e. the
#      exact residue the incidents left) before the first test and removes it
#      after the last. An unscoped read therefore returns the wrong answer on
#      EVERY run — not only on the runs where a leak happened to land first.
#      That is what makes "the file passes alone" and "the file passes in the
#      suite" the same measurement instead of two different claims.
#
# A module may still hold committed rows ACROSS its own tests (m149, m155 and
# the auth-hardening races all do, on purpose) as long as its teardown takes
# them away again — which is exactly what "the footprint is unchanged at module
# end" asserts. Cost: one `count(*)` per tenant table, twice per module.
_TENANT_PRINCIPAL_TABLE = "users"  # the tenant row every other row hangs off
_TENANT_CHILD_MODELS = ("TransactionTag", "RuleTag", "BillReminderMatchTerm")
_TENANT_TABLE_CACHE: tuple[str, ...] | None = None


def tenant_table_names() -> tuple[str, ...]:
    """Every table a tenant's rows can live in — DERIVED, never hand-listed.

    Derived because the hand-listed version of this (the old guard watched ONE
    email in ONE table) is what let three leaks through in a row: a new table,
    or a new committer shape, silently stops being covered. Three parts:

    * the PRINCIPAL — `users`, which has no `user_id` of its own and is the row
      every other one hangs off. Watching only the user-`user_id` tables would
      miss the exact row the demo-seed and live-server leaks started with;
    * every table with a `user_id` column — tenanted by construction;
    * the three CHILD tables, which have none of their own and inherit tenancy
      through a parent FK that CASCADEs (docs/02): their rows are unreachable
      without a tenanted parent, so an orphan row in one is still residue.
    """
    global _TENANT_TABLE_CACHE
    if _TENANT_TABLE_CACHE is None:
        import app.models as domain

        names = set()
        for obj in vars(domain).values():
            table = getattr(obj, "__table__", None)
            if table is None or getattr(obj, "__module__", "").startswith("sqlmodel"):
                continue
            if "user_id" in table.c:
                names.add(table.name)
        names.add(_TENANT_PRINCIPAL_TABLE)
        for model_name in _TENANT_CHILD_MODELS:
            names.add(getattr(domain, model_name).__table__.name)
        _TENANT_TABLE_CACHE = tuple(sorted(names))
    return _TENANT_TABLE_CACHE


def tenant_footprint(conn: Connection) -> dict[str, int]:
    """Row count per tenant table — cheap, and enough to see residue."""
    return {
        name: int(conn.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one())  # noqa: S608 -- fixed table registry
        for name in tenant_table_names()
    }


def _footprint_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, tuple[int, int]]:
    return {name: (before[name], after[name]) for name in before if before[name] != after.get(name)}


@pytest.fixture(scope="session", autouse=True)
def _guard_no_committed_tenant_survives(
    _suite_probe_engine: Engine, _database: None
) -> Iterator[None]:
    """Fail the RUN that left tenant rows behind that the session started without.

    Compares the footprint taken once the schema is up against the one at
    session end rather than asserting emptiness: the decoy tenant below is a
    deliberate, constant part of that baseline, and a migration-seeded row is
    not residue. Everything else is.
    """
    with _suite_probe_engine.connect() as conn:
        before = tenant_footprint(conn)
    yield
    with _suite_probe_engine.connect() as conn:
        after = tenant_footprint(conn)
    delta = _footprint_delta(before, after)
    assert not delta, (
        "this run COMMITTED tenant rows into the suite database and left them:\n"
        + "\n".join(f"  {name}: {was} -> {now}" for name, (was, now) in delta.items())
        + "\nA test that needs rows another connection can see opens an engine of its\n"
        "own and COMMITS — the `session` fixture's rollback cannot reach those\n"
        "rows, and they answer every later test's UNFILTERED reads for the rest of\n"
        "the session (heap order decides which of two same-named rows wins:\n"
        "t_a451f171, t_17d025b2, t_70d46e5e).\n"
        "Delete the whole tenant in the fixture that made it — `DELETE /users/me`\n"
        "runs docs/20's cascade, or delete by `user_id` on a connection of your\n"
        "own: tests/test_m149_offloop_routes.py::tenant and\n"
        "tests/test_m155_price_session.py::tenant_fixture are the templates.\n"
        "tests/test_tenant_scoping_contract.py explains the whole contract."
    )


@pytest.fixture(scope="module", autouse=True)
def _guard_module_left_no_tenant_state(
    request: pytest.FixtureRequest, _suite_probe_engine: Engine, _database: None
) -> Iterator[None]:
    """Fail the MODULE that leaked, while its own scope is still on screen.

    The session guard can only name the run. By then the report is a list of
    victims two modules downstream and the polluter has scrolled away — the
    expensive part of every incident above. Module scope is the narrowest one
    that still lets a module hold committed rows across its own tests on
    purpose, so it is the granularity the contract is stated at.
    """
    with _suite_probe_engine.connect() as conn:
        before = tenant_footprint(conn)
    yield
    with _suite_probe_engine.connect() as conn:
        after = tenant_footprint(conn)
    delta = _footprint_delta(before, after)
    module = Path(str(getattr(request.node, "path", request.node.name))).name
    assert not delta, (
        f"{module} left committed tenant state in the suite database:\n"
        + "\n".join(f"  {name}: {was} -> {now}" for name, (was, now) in delta.items())
        + "\nRows committed through an engine of your own (a live server, a\n"
        "TestClient lifespan seed, `get_engine()`) outlive this module, and every\n"
        "later module that reads that table without a tenant filter inherits them\n"
        "(t_17d025b2 named five victim modules and no polluter). Take the tenant\n"
        "away again in the fixture that made it: `DELETE /users/me` runs docs/20's\n"
        "cascade — see tests/test_m149_offloop_routes.py::tenant."
    )


# --- the read side: the suite database is dirty ON PURPOSE ------------------------
# R1 stops the leaks this suite knows how to make. R2 is what makes the suite
# *faithful* to a database that has one: the decoy tenant below is committed
# before the first test, so any read that is not scoped to the test's own
# `user_id` sees a foreign row. That is the difference between "the suite was
# green today" and "the suite cannot be green by accident" — an unscoped read
# now fails deterministically (every run, whoever ran first) instead of failing
# on the 2-of-8 runs where a leaked row happened to be one row ahead.
#
# Its shape is the residue the incidents actually left, not a convenient one:
# a `users` row plus the 10 `DEFAULT_CATEGORIES` that `POST /auth/register` (and
# the demo seed) writes — so `category_by_name("groceries")` and
# `select(Category)` are ambiguous exactly as they were when m149 leaked.
# `ISOLATION_DECOY_TENANT=0` disables it, for a bisect of a scoping failure.
_DECOY_ENABLED = os.environ.get("ISOLATION_DECOY_TENANT", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
_DECOY_PASSWORD = "decoy-tenant-password"


@pytest.fixture(scope="session", autouse=True)
def _decoy_tenant(
    _guard_no_committed_tenant_survives: None, _suite_probe_engine: Engine
) -> Iterator[str | None]:
    """A foreign tenant, committed, present for the whole session.

    Requesting the session guard first is deliberate: it snapshots the database
    before this fixture plants (so the decoy is NOT residue and the guard's
    teardown, which runs after this one, compares equal). The module guard is
    module-scoped, so it always starts after this session fixture by
    construction and never sees a footprint it did not begin with.
    """
    if not _DECOY_ENABLED:
        yield None
        return

    from app.api.auth import DEFAULT_CATEGORIES
    from app.core.security import hash_password

    email = f"decoy-tenant-{os.getpid()}@example.com"
    with Session(_suite_probe_engine) as db:
        user = User(email=email, hashed_password=hash_password(_DECOY_PASSWORD))
        db.add(user)
        db.flush()
        user_id = user.id
        for name in DEFAULT_CATEGORIES:
            db.add(Category(user_id=user_id, name=name, is_system=True))
        db.commit()

    yield email

    # Take it away again: the session guard compares footprints, and a decoy
    # left behind would also be the last row standing in a database about to be
    # dropped — cheap to remove, and it keeps "no survivors" meaning what it says.
    with _suite_probe_engine.begin() as conn:
        conn.execute(text("DELETE FROM categories WHERE user_id = :uid"), {"uid": user_id})
        conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})


# --- provider_fund_registry: the coverage table COMMITTED itself ---------------
#
# M168 moved the fund-coverage registries out of module literals and into
# `provider_fund_registry` (docs/07 §Provider registry). Its reads and writes
# go through `services/provider_registry`, which owns a session OF ITS OWN on
# purpose: the provider chain must not hold a transaction open across a
# provider wait (docs/01 § Sessions and the event loop, M153). So a probe books
# its verdict with a COMMIT of its own — the one shape this rollback-per-test
# suite cannot undo by itself, and a `miss` row left behind would silently skip
# the probe a LATER test expects (the t_a451f171 class: heap order becomes the
# test's answer).
#
# The table is therefore snapshotted before every test and restored afterwards
# when anything moved: the migration's seed rows come back byte-identical,
# discovery rows vanish, and no test inherits another's verdict. One indexed
# SELECT over ~130 rows; the restore (DELETE + re-INSERT) runs only when the
# snapshot actually differs.
@pytest.fixture(autouse=True)
def _provider_registry_rollback(_suite_probe_engine: Engine) -> Iterator[None]:
    from app.models.provider_registry import ProviderFundRegistry

    table = ProviderFundRegistry.__table__
    with _suite_probe_engine.connect() as conn:
        before = [dict(row._mapping) for row in conn.execute(table.select().order_by(table.c.id))]
    yield
    with _suite_probe_engine.connect() as conn:
        after = [dict(row._mapping) for row in conn.execute(table.select().order_by(table.c.id))]
        if after == before:
            return
        conn.execute(table.delete())
        if before:
            conn.execute(table.insert(), before)
        # A Connection's transaction is NOT implicit-committed (SQLAlchemy 2.0
        # autobegins and rolls back on return) — without this the restore
        # would be a well-meant no-op and the next test would inherit the
        # verdict this one left behind.
        conn.commit()


# --- row-level security fixtures (docs/55) -------------------------------------------
# The app role (`fintrack_app`) is provisioned per CLUSTER, not by the suite:
# the enforcement tests SKIP with the provisioning command where it is absent,
# while test_rls_coverage.py's pure-SQL assertions still run everywhere.
# Lives here (not in rls_support.py) because pytest 9 does not register
# fixture objects imported from a helper module.
from rls_support import app_role_missing_reason, app_role_url  # noqa: E402


@pytest.fixture(scope="session")
def app_role_available() -> None:
    """Skip the requesting module when the cluster has no app role"""
    reason = app_role_missing_reason()
    if reason is not None:
        pytest.skip(reason)


@pytest.fixture(scope="module")
def app_engine(app_role_available: None) -> Iterator[Engine]:
    """The request engine exactly as production builds it (stamping attached)

    NullPool: each connection is freshly created and discarded, so one test
    can never inherit another test's pooled connection state.
    """
    from rls_support import create_engine_for_tests

    engine = create_engine_for_tests(app_role_url())
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def app_conn(app_engine) -> Iterator[Connection]:
    """One app-role connection inside an outer transaction, rolled back

    Every read and write a direct RLS test performs happens on the app role,
    in one transaction the fixture rolls back — the suite database is never
    touched. Tenant switches are transaction-local GUC writes, exactly what
    the production stamping does per request.
    """
    # Explicit begin/rollback, NOT `with conn.begin()`: a handler-level
    # Session.commit() (register does one) deactivates the connection's
    # transaction object, the context manager then skips its rollback, and the
    # open transaction is COMMITTED at disconnect — the rows outlive the test
    # (probed 2026-10-30; the suite's `session` fixture uses this same shape).
    conn = app_engine.connect()
    transaction = conn.begin()
    try:
        yield conn
    finally:
        transaction.rollback()
        conn.close()

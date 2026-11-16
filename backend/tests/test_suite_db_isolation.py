"""Per-session test database names — two concurrent suites must not collide (t_00b6d06d).

One Postgres serves every run on the box. When the suite database was a FIXED
`<base>_test`, a second `uv run pytest` session dropped the first one's database
mid-run (`AdminShutdown`), re-ran alembic underneath it (`lock_timeout`, then a
missing column) and destroyed it. These tests pin the five properties that keep
that from coming back:

1. the name is derived per SESSION (so a second session derives a different one);
2. the reaper that cleans up after dead sessions cannot reach a live one;
3. a session that somehow does collide FAILS at setup instead of stealing data;
4. `DROP DATABASE … WITH (FORCE)` survives a backend this role may not signal
   instead of killing the session that has to run it;
5. the scratch names THIS file creates are invisible to every other session's
   reaper, with or without a connection — otherwise the file that proves
   concurrency safety would not be concurrency-safe itself (review round 2).
6. how the guard DECIDES that a name is busy: over a bounded wait rather than
   one sample (a just-disposed backend is still in `pg_stat_activity` for
   0-12 ms), and only over backends that can be sessions — a readable
   background worker is ignored, an unreadable row never is (t_d06c85c7).

They exercise a real Postgres, like the rest of the suite (docs/01).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

import pytest
from conftest import (
    BACKEND_DIR,
    BASE_URL,
    SESSION_DB_NAMES,
    SUITE_DATABASE_URL,
    TEST_DB_ID,
    TEST_DB_NAME,
    _assert_databases_free,
    _connections_to,
    _process_alive,
    _reap_dead_run_databases,
    _session_db_id,
    _suite_db_name,
    _url_with_db,
    drop_database_force,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

# One cheap, DB-free test for the subprocess sessions below to select (a session
# fixture only runs when at least one test is selected).
_SINGLE_TEST = "test_session_id_defaults_to_the_pid_and_pins_when_asked"


def _admin():
    return create_engine(_url_with_db("postgres"), isolation_level="AUTOCOMMIT")


def _exists(conn, db_name: str) -> bool:
    return bool(
        conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}).first()
    )


def _scratch_db(kind: str, *, dead: bool = False) -> str:
    """A database name unique to THIS session.

    This file has to be concurrency-safe itself: two agents run the suite at the
    same time, so every name it creates is tagged with our pid. The default ends
    the name in OUR live pid, which makes it invisible to every OTHER session's
    reaper at any moment, connection or not — the property review round 2 found
    missing: a `dead=True` name sat unconnected in the gap between `CREATE
    DATABASE` and the first connection, all three of the reaper's conditions
    were true, and a peer's pass legitimately dropped it out from under the test.

    `dead=True` ends the name in a pid nobody can own instead. Use it ONLY for
    a name whose test proves what a dead reaper does and whose assertions stay
    true even if a peer drops the database first (see
    `test_reaper_drops_dead_runs_and_only_dead_runs`); never for a name a test
    must still hold later, because Postgres cannot create a database without a
    connection-free instant — even `ALTER DATABASE ... RENAME` refuses while
    anybody is connected — so no dead-pid name can be protected by a held
    connection across its whole life.
    """
    tail = f"_test_p{_dead_pid()}" if dead else f"_test_p{os.getpid()}"
    return f"fintrack_iso_{os.getpid()}_{kind}{tail}"


def _dead_pid() -> int:
    """A pid NOBODY can ever own, for naming a database nobody owns.

    `pid_max` itself is above the allocator's range, so it stays dead however
    many processes start during the test — a merely-unused pid could be handed
    to one and make the reaper (correctly) leave its database alone.
    """
    pid_max = int(Path("/proc/sys/kernel/pid_max").read_text())
    assert not _process_alive(pid_max), "this host can allocate pid_max; pick another"
    return pid_max


def _create(conn, db_name: str) -> None:
    drop_database_force(conn, db_name)
    conn.execute(text(f'CREATE DATABASE "{db_name}"'))


def _drop(conn, db_name: str) -> None:
    drop_database_force(conn, db_name)


# --- 1. the name itself -------------------------------------------------------------


def test_this_session_does_not_use_the_shared_name() -> None:
    """The regression: `<base>_test` is shared by every run, so it must be gone."""
    shared = f"{make_url(BASE_URL).database}_test"
    assert TEST_DB_NAME != shared
    assert TEST_DB_NAME.endswith(f"_test_{TEST_DB_ID}")
    assert re.fullmatch(r"[A-Za-z0-9_]+", TEST_DB_ID), "the id must be a safe identifier"


def test_two_sessions_derive_two_names() -> None:
    base = make_url(BASE_URL).database
    first, second = _suite_db_name(base, "p111"), _suite_db_name(base, "p222")
    assert first != second
    # ...and each one is stable for the session that derived it.
    assert first == _suite_db_name(base, "p111")


def test_session_id_defaults_to_the_pid_and_pins_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert _session_db_id(None) == f"p{os.getpid()}"
    assert _session_db_id("") == f"p{os.getpid()}"
    assert _session_db_id("___") == f"p{os.getpid()}", "an unusable pin must fall back"
    assert _session_db_id("t_a1b2c3d4") == "t_a1b2c3d4"
    assert _session_db_id("t/a b-c") == "t_a_b_c", "anything else is sanitised, never rejected"


def test_a_pin_is_suffixed_per_xdist_worker_so_parallel_runs_never_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default gate runs `-n auto`: one pinned id would reach every worker.

    Each worker is its own process, so an unpinned run isolates by pid for
    free — but a PINNED id is shared env, and two workers deriving the same
    name would fight over one database. The worker slot goes on the end, which
    keeps it out of the pid-shaped names the reaper can reap (a pin deliberately
    outlives the run) and out of the part `_suite_db_name` is allowed to eat.
    """
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    assert _session_db_id("qa29") == "qa29_gw0"
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    assert _session_db_id("qa29") == "qa29_gw3", "one pin, one database per worker"
    # An unpinned run still falls back to the worker's own pid, untouched.
    assert _session_db_id(None) == f"p{os.getpid()}"


def test_a_long_url_keeps_the_unique_part_of_the_name() -> None:
    """Postgres TRUNCATES a >63-byte identifier instead of rejecting it.

    So the shortening has to eat the base, never the id — otherwise two long
    URLs would truncate back to the same name and be silently shared again.
    """
    long_base = "b" * 80
    name = _suite_db_name(long_base, "p12345")
    assert name.endswith("_test_p12345")
    assert name != _suite_db_name(long_base, "p12346")
    for suffix in ("_demo", "_perf", ""):
        assert len(name + suffix) <= 63, f"{name + suffix!r} would be truncated by Postgres"


def test_a_long_id_is_refused_rather_than_truncated() -> None:
    """The id is the unique part, so it is never the part that gets shortened.

    An id that does not fit is REJECTED: shortening it hands two distinct
    pins the same database — silently, after Postgres truncates the name —
    which is the very collision this naming exists to prevent. (The first
    version shortened `tail` instead of refusing, so two 87-character pins
    both became `_test_home_hermes__hermes_kanban_boards…` — review round 1.)
    """
    fits = "e" * 52  # exactly the room the `_test_` marker + `_demo`/`_perf` leave
    assert _suite_db_name("fintrack", fits).endswith(f"_test_{fits}")

    too_long = "f" * 53
    with pytest.raises(ValueError, match="TEST_DB_ID"):
        _suite_db_name("fintrack", too_long)
    # …so two ids differing only past the limit never exist to be mapped onto
    # one database either.
    with pytest.raises(ValueError):
        _suite_db_name("fintrack", "z" * 60)


def test_hermetic_databases_track_the_suite_name() -> None:
    """The demo/perf modules derive these from DATABASE_URL; they must follow."""
    suite_db = SUITE_DATABASE_URL.rsplit("/", 1)[-1]
    assert SESSION_DB_NAMES == (suite_db, f"{suite_db}_demo", f"{suite_db}_perf")
    assert all(name.startswith(f"{TEST_DB_NAME}_") for name in SESSION_DB_NAMES[1:])


# --- 2. the reaper ------------------------------------------------------------------


def test_reaper_drops_dead_runs_and_only_dead_runs() -> None:
    """A leftover is dropped only if its owning pid is DEAD and it is unused.

    The live pid here is this very session's, which is exactly the case that
    would have destroyed a concurrent run's data had the reaper looked only at
    the name.
    """
    dead_suite = _scratch_db("dead", dead=True)
    dead_demo = f"{dead_suite}_demo"
    live_suite = _scratch_db("live")  # ends in OUR pid: alive right now
    mine = _scratch_db("mine", dead=True)  # dead-looking, but claimed via `keep`
    legacy = f"fintrack_iso_{os.getpid()}_legacy_test"  # old naming: not ours
    made = (dead_suite, dead_demo, live_suite, mine, legacy)

    admin = _admin()
    try:
        with admin.connect() as conn:
            for name in made:
                _create(conn, name)
            try:
                reaped = _reap_dead_run_databases(conn, keep=(mine,))
                # a leftover of a dead session does not survive a reaper...
                assert not _exists(conn, dead_suite) and not _exists(conn, dead_demo)
                # ...while a live run's database and any name the scheme never
                # generated are left alone.
                assert _exists(conn, live_suite)
                assert _exists(conn, legacy)
                # `mine` is asserted on what OUR reaper returned, not on its
                # existence: only this session's `keep` protects it here, so any
                # other session's reaper (keep=()) may legitimately drop it in the
                # instant between our reaper and an `_exists` check — which would
                # turn the file that proves concurrency safety into a flake.
                assert mine not in reaped, "a database inside our own `keep` was reaped"
            finally:
                for name in made:
                    _drop(conn, name)
    finally:
        admin.dispose()


def test_reaper_ignores_a_database_that_looks_dead_but_is_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second, independent stop: the pid says dead, the connections say live.

    The pid in the name has to READ as dead for that stop to be the one that
    decides — but a genuinely dead pid made the name a target for every PEER
    session's reaper in the connection-free gap between `CREATE DATABASE` and
    our first connection, which is what went red in review round 2. Postgres
    gives no way to close that gap: `CREATE DATABASE` starts life unconnected
    and `ALTER DATABASE ... RENAME` refuses while anybody is attached (both
    probed). So the name keeps OUR live pid — invisible to every peer at every
    instant — and this test fakes exactly the one judgement (`_process_alive`)
    for its own reaper call, which is what a foreign pid namespace used to
    produce. `keep` holds every other database, so the faked judgement's blast
    radius is exactly this one name; the two calls then pin both halves of the
    gate: connected -> skipped, disconnected -> reaped.
    """
    name = _scratch_db("busy")  # ends in OUR pid: no peer's reaper may touch it
    real_process_alive = _process_alive
    monkeypatch.setattr(
        "conftest._process_alive",
        lambda pid: False if pid == os.getpid() else real_process_alive(pid),
    )
    admin = _admin()
    engine = create_engine(_url_with_db(name))
    try:
        with admin.connect() as conn:
            _create(conn, name)
            try:
                others = [row[0] for row in conn.execute(text("SELECT datname FROM pg_database"))]
                keep = tuple(other for other in others if other != name)
                with engine.connect():  # a session is using it right now
                    reaped = _reap_dead_run_databases(conn, keep=keep)
                assert name not in reaped, "a connected database was dropped"
                assert _exists(conn, name)
                engine.dispose()  # the pool would otherwise hold it open
                reaped = _reap_dead_run_databases(conn, keep=keep)
                assert name in reaped, "the same database, now unused, must be reaped"
                assert not _exists(conn, name)
            finally:
                _drop(conn, name)
    finally:
        engine.dispose()
        admin.dispose()


def test_a_peer_reaper_cannot_take_a_connection_free_name() -> None:
    """A name of ours with ZERO connections survives a foreign reaper pass.

    Review round 2 went red exactly here: scratch names carried a dead pid, sat
    unconnected between `CREATE DATABASE` and the first connection, and a peer
    session's reaper (`keep=()`) legitimately dropped them — the file that
    exists to prove concurrency safety was itself not concurrency-safe. The
    default name carries our LIVE pid, which is what the reaper's liveness
    check exists for; this pins it against the same call a peer makes.
    """
    name = _scratch_db("peer")
    admin = _admin()
    try:
        with admin.connect() as conn:
            _create(conn, name)
            try:
                assert _connections_to(conn, name) == 0, "this test is about the gap"
                reaped = _reap_dead_run_databases(conn, keep=())  # a PEER's pass
                assert name not in reaped, "a connection-free live-pid name was reaped"
                assert _exists(conn, name)
            finally:
                _drop(conn, name)
    finally:
        admin.dispose()


# --- 3. colliding anyway -------------------------------------------------------------


def test_guard_refuses_a_database_someone_is_connected_to() -> None:
    # The default name ends in OUR live pid. The guard judges only connections,
    # so the pid is irrelevant to it — but a dead-pid name here would sit
    # unprotected in the gap between `CREATE DATABASE` and the first
    # connection, where any PEER session's reaper pass may drop it (the exact
    # failure of review round 2's pair 2).
    name = _scratch_db("gcheck")
    admin = _admin()
    engine = create_engine(_url_with_db(name))
    try:
        with admin.connect() as conn:
            _create(conn, name)
            try:
                with engine.connect():  # somebody is in there
                    with pytest.raises(RuntimeError, match="already using"):
                        _assert_databases_free(conn, (name,))
                engine.dispose()  # the pool would otherwise hold it open
                # `dispose()` closes OUR socket; the server-side backend needs
                # a moment to exit, and `pg_stat_activity` keeps listing it
                # until it does. Under load — or with a second suite running
                # concurrently, which AGENTS §5 explicitly allows — that
                # moment stretched long enough for this test's OWN scratch
                # backend to look like a stranger still connected (t_7100c168:
                # 1 of 4 concurrent-pair runs). Wait for it to actually leave;
                # the guard's verdict is still taken on a genuinely empty
                # database, and a backend that never leaves fails loudly here.
                deadline = time.monotonic() + 10.0
                while _connections_to(conn, name) and time.monotonic() < deadline:
                    time.sleep(0.1)
                assert _connections_to(conn, name) == 0, (
                    f"the scratch engine's own backend outlived dispose() on {name}"
                )
                _assert_databases_free(conn, (name,))  # empty again: free to take
            finally:
                _drop(conn, name)
    finally:
        engine.dispose()
        admin.dispose()


# --- 3b. what makes a name busy, and when the guard is allowed to say so -----------


class _Activity:
    """`pg_stat_activity` rows for the guard's own query, scripted per call.

    Like `_RefusingAdmin` further down, this fakes only the one read the code
    under test makes (`SELECT backend_type … WHERE datname = …`, once per
    round), so a test pins the DECISION — which rows count, and whether a busy
    row may be trusted at once — rather than the server behind it.
    """

    def __init__(self, script: list[list[tuple[str | None]]]) -> None:
        self.script = script
        self.calls = 0

    def execute(self, statement, params=None):
        sql = getattr(statement, "text", str(statement))
        assert "backend_type" in sql and "pg_stat_activity" in sql, (
            f"the guard must ask pg_stat_activity for backend_type, got:\n  {sql}"
        )
        rows = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return rows


def test_the_guard_rechecks_a_busy_name_before_reporting_it() -> None:
    """The t_d06c85c7 flake, pinned: a backend that has just gone is no collision.

    Reproduced here 19 times in 120 runs of the exact sequence under load: the
    first sample still showed `client backend` / `idle` for a database whose
    client had already `dispose()`d, and the guard called that a second pytest
    session. It has to look again before it says so.
    """
    stale_then_gone = _Activity(
        [
            [("client backend",)],  # first sample: still in pg_stat_activity
            [("client backend",)],  # …the server has not scheduled its exit yet
            [],  # …and now it is gone, so the name is free
        ]
    )
    _assert_databases_free(stale_then_gone, ("fintrack_guard_scripted",))
    assert stale_then_gone.calls == 3, "one busy sample must not decide the verdict"


def test_the_guard_ignores_a_backend_that_is_not_a_session() -> None:
    """Postgres's own machinery may sit on a name; it is not a pytest run.

    An autovacuum worker attached to a leftover pinned-id database would
    otherwise refuse a name no live session is using — and the reaper, which
    MUST respect such a backend (it may not drop a database somebody is
    attached to), keeps counting every row for exactly that reason.
    """
    for kind in ("autovacuum worker", "parallel worker", "walsender"):
        _assert_databases_free(_Activity([[(kind,)]]), ("fintrack_guard_scripted",))


def test_the_guard_refuses_a_backend_whose_type_it_cannot_read() -> None:
    """An unreadable `backend_type` is a peer session, not an absence.

    Postgres blanks that column for rows this role holds no privileges of
    (probed on this box: a `postgres`-role backend reads `backend_type IS
    NULL`). A filter of `backend_type = 'client backend'` matches none of
    them, so a second session running as another role would sail straight past
    the check this file exists for — the flake must not be paid for with a
    weaker guard.
    """
    with pytest.raises(RuntimeError, match="already using"):
        _assert_databases_free(_Activity([[(None,)]]), ("fintrack_guard_scripted",), deadline=0.2)


def test_a_second_session_fails_instead_of_taking_a_live_database() -> None:
    """End to end: a session pinned to OUR database dies at setup, with the reason.

    This is the failure mode we WANT when two runs are given the same id —
    loud, at session start, naming the database — instead of the old silent
    DROP DATABASE ... WITH (FORCE) in the middle of somebody's run.
    """
    # `p<our pid>taken`: unique to this session, and the parent holds a
    # connection to it for the whole child run, so no reaper can take it in
    # between. The trailing LETTERS matter: the reaper parses the digits after
    # `_test_p` as the owner pid, so a digit suffix would be swallowed into it
    # (`_test_p1234568` reads as dead pid 1234568) and leave the name reapable
    # in the gap before the parent connects — with letters the parsed pid stays
    # ours, alive for as long as this process runs.
    pinned_id = f"p{os.getpid()}taken"
    pinned_name = _suite_db_name(make_url(BASE_URL).database, pinned_id)
    admin = _admin()
    engine = create_engine(_url_with_db(pinned_name))
    try:
        with admin.connect() as conn:
            _create(conn, pinned_name)
        # The child pins TEST_DB_ID: it must be a plain serial session (-n0) with
        # coverage off, and it must not inherit this worker's xdist identity or the
        # parent run's COV_CORE_* data-file env (see test_tenant_scoping_contract).
        env = {**os.environ, "DATABASE_URL": BASE_URL, "TEST_DB_ID": pinned_id}
        env.pop("PYTEST_XDIST_WORKER", None)
        for _cov_key in [k for k in env if k.startswith("COV_CORE_")]:
            env.pop(_cov_key, None)
        with engine.connect():  # the "other session", as far as Postgres cares
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/test_suite_db_isolation.py",
                    "-q",
                    "-n0",
                    "--no-cov",
                    "-k",
                    _SINGLE_TEST,
                ],
                cwd=BACKEND_DIR,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )
        output = result.stdout + result.stderr
        assert result.returncode != 0, f"the colliding session passed?!:\n{output}"
        assert "already using" in output, f"no reason given:\n{output}"
        assert pinned_name in output, f"the database was not named:\n{output}"
    finally:
        engine.dispose()
        with admin.connect() as conn:
            _drop(conn, pinned_name)
        admin.dispose()


def test_a_pinned_session_announces_the_database_it_took() -> None:
    """The same session, alone: it gets its own database and says which one."""
    # `p<our pid>ann` names OUR live pid, so no other session's reaper can
    # mistake this database for a dead run's leftover while the child makes it.
    pinned_id = f"p{os.getpid()}ann"
    pinned_name = _suite_db_name(make_url(BASE_URL).database, pinned_id)
    # The child pins TEST_DB_ID: it must be a plain serial session (-n0) with
    # coverage off, and it must not inherit this worker's xdist identity or the
    # parent run's COV_CORE_* data-file env (see test_tenant_scoping_contract).
    env = {**os.environ, "DATABASE_URL": BASE_URL, "TEST_DB_ID": pinned_id}
    env.pop("PYTEST_XDIST_WORKER", None)
    for _cov_key in [k for k in env if k.startswith("COV_CORE_")]:
        env.pop(_cov_key, None)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_suite_db_isolation.py",
                "-q",
                "-n0",
                "--no-cov",
                "-k",
                _SINGLE_TEST,
            ],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, f"pinned session failed:\n{output}"
        assert f"suite database: {pinned_name}" in output, (
            f"did not announce {pinned_name}:\n{output}"
        )
    finally:
        admin = _admin()
        try:
            with admin.connect() as conn:
                _drop(conn, pinned_name)
        finally:
            admin.dispose()


# --- 4. FORCE-dropping past a backend this role may not signal ----------------------


class _Rows(list):
    """The slice of `conn.execute()`'s return value `drop_database_force` uses."""

    def all(self) -> list[tuple[int, str]]:
        return list(self)


class _RefusingAdmin:
    """An admin connection that refuses the first `failures` DROP statements.

    That is what Postgres does when an attached backend's owner is not one this
    role may signal: `TerminateOtherDBBackends` validates EVERY pid before it
    signals any, so the statement fails as a whole and nothing is dropped.
    `blockers` is what `pg_stat_activity` reports while that backend is there.
    """

    def __init__(
        self,
        failures: int,
        blockers: list[tuple[int, str]] | None = None,
        message: str = "permission denied to terminate process",
    ) -> None:
        self.failures = failures
        self.blockers = _Rows(blockers or [])
        self.message = message
        self.attempts = 0

    def execute(self, statement, params=None):
        if "pg_stat_activity" in getattr(statement, "text", str(statement)):
            return self.blockers  # the diagnostic query, not a DROP
        self.attempts += 1
        if self.attempts <= self.failures:
            raise ProgrammingError(str(statement), params, Exception(self.message))
        return None


def test_force_drop_retries_until_the_database_is_quiet() -> None:
    """One un-signalable backend must not kill the session (review round 1).

    That is the failure that made 2 of 3 concurrent pairs go red: the statement
    was issued once, refused, and `test_perf_harness` died with
    `InsufficientPrivilege` on its own `<suite>_perf`.
    """
    conn = _RefusingAdmin(failures=2)
    drop_database_force(conn, "fintrack_whatever", deadline=30.0)
    assert conn.attempts == 3, "it must wait and try again, not give up on the first refusal"


def test_force_drop_reports_what_is_stuck_instead_of_hanging() -> None:
    """A blocker that never leaves is a loud, bounded failure — never a hang."""
    conn = _RefusingAdmin(failures=10**9, blockers=[(4242, "postgres")])
    started = time.monotonic()
    with pytest.raises(RuntimeError) as excinfo:
        drop_database_force(conn, "fintrack_whatever", deadline=0.5)
    message = str(excinfo.value)
    assert "fintrack_whatever" in message, "the database must be named"
    assert "pid 4242 (postgres)" in message, "the un-signalable backend must be named"
    assert time.monotonic() - started < 10, "the deadline must bound the wait"


def test_force_drop_does_not_swallow_an_unrelated_failure() -> None:
    """Retrying is only ever for the one error this helper is built around."""
    conn = _RefusingAdmin(failures=10**9, message="permission denied to drop database")
    with pytest.raises(ProgrammingError, match="permission denied to drop database"):
        drop_database_force(conn, "fintrack_whatever", deadline=30.0)
    assert conn.attempts == 1, "an unrelated failure must surface on the first attempt"


_NEEDS_LOCAL_POSTGRES = (
    "needs `sudo -n -u postgres psql` (local peer auth) — a remote CI database cannot attach one"
)
_NOT_THE_LOCAL_POSTGRES = (
    "the suite's database is not on the server `sudo -n -u postgres psql` reaches — a backend "
    "can only be attached where the scratch database actually lives"
)


def _require_local_postgres() -> None:
    """Skip where a backend as ANOTHER role cannot be attached from a test.

    `fintrack` has no CREATEROLE, so the only way to make a backend this suite
    may not signal is to log in as somebody else; on this host that is
    `sudo -n -u postgres psql` over peer auth. Checked lazily, inside the test,
    so merely importing this file runs no subprocess.

    This only proves that A local Postgres answers peer auth. Which server the
    suite itself is on is `_require_same_server`'s job, after the scratch
    database exists to compare.
    """
    if not _sudo_psql_available():
        pytest.skip(_NEEDS_LOCAL_POSTGRES)


def _local_backend_oid(db_name: str, timeout: float = 15.0) -> str | None:
    """The oid `sudo -n -u postgres psql -d <db_name>` lands on, else None.

    That is the very connection `_attach_blocker` is about to open, so asking it
    for the oid of the database it reached answers both questions the blocker
    depends on: did the connection OPEN (a database the local server does not
    hold fails at once with `database "<name>" does not exist`), and is that
    database the one this session just created?
    """
    try:
        probe = subprocess.run(
            [
                "sudo",
                "-n",
                "-u",
                "postgres",
                "psql",
                "-d",
                db_name,
                "-Atc",
                "select oid from pg_database where datname = current_database()",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    oid = probe.stdout.strip()
    return oid if probe.returncode == 0 and oid else None


def _require_same_server(conn, db_name: str) -> None:
    """Skip unless `sudo -n -u postgres psql` reaches the server `db_name` was made on.

    `_sudo_psql_available()` cannot tell where the suite's DATABASE_URL points,
    and on the box's standard way of running the suite — a tunnel to another
    Postgres — it says yes while the scratch database is on the far server.
    The blocker then attached to the LOCAL one (peer auth, `local all all`), the
    far server's `pg_stat_activity` never showed it, and the test failed 15 s
    later with `the blocker never attached` instead of skipping (t_03722af5).

    Compare oids rather than URL strings: the same name on both servers IS the
    same database, which is the capability the test needs — a leftover from an
    old local run shares the name but not the oid.
    """
    ours = conn.execute(
        text("SELECT oid FROM pg_database WHERE datname = :n"), {"n": db_name}
    ).scalar()
    if ours is None or str(ours).strip() != _local_backend_oid(db_name):
        pytest.skip(_NOT_THE_LOCAL_POSTGRES)


def test_the_local_gate_judges_the_server_not_the_presence_of_sudo_psql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both halves of the gate, pinned from either server the suite may be on.

    Regression (t_03722af5): only `_require_local_postgres` existed, so a suite
    DATABASE_URL pointing away from the local cluster ran these tests anyway and
    failed on the blocker that could never attach. The oid comparison below is
    what must stay: a foreign server's oid skips, our own does not — the second
    half is what keeps coverage alive on the path the two FORCE-drop tests were
    written for, instead of a gate that quietly skips everywhere.
    """
    admin = _admin()
    try:
        with admin.connect() as conn:
            ours = conn.execute(
                text("SELECT oid FROM pg_database WHERE datname = 'postgres'")
            ).scalar()
            assert ours is not None, "every server has a `postgres` database"
            own_view = str(ours).strip()
            assert own_view.isdigit(), f"unexpected oid {ours!r}"
            monkeypatch.setattr(sys.modules[__name__], "_local_backend_oid", lambda _: own_view)
            _require_same_server(conn, "postgres")  # our own view: the test must run
            # a `postgres` of the same name carrying another oid: exactly what a
            # different server looks like to this gate.
            foreign_view = str(int(own_view) + 1)
            monkeypatch.setattr(sys.modules[__name__], "_local_backend_oid", lambda _: foreign_view)
            with pytest.raises(pytest.skip.Exception) as skipped:
                _require_same_server(conn, "postgres")
            assert _NOT_THE_LOCAL_POSTGRES in str(skipped.value)
    finally:
        admin.dispose()


@lru_cache(maxsize=1)
def _sudo_psql_available() -> bool:
    if not (shutil.which("sudo") and shutil.which("psql")):
        return False
    try:
        probe = subprocess.run(
            ["sudo", "-n", "-u", "postgres", "psql", "-d", "postgres", "-Atc", "select 1"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "1"


# Our blocker's `application_name`: `PGAPPNAME` beats psql's own fallback
# (`psql`, probed) and — unlike `backend_type`, `state` and `query` — it stays
# readable from a role that holds no privileges over the blocker's role.
_BLOCKER_APPNAME = "fintrack_test_blocker"


def _attach_blocker(db_name: str, seconds: float) -> subprocess.Popen:
    """A backend in `db_name` owned by role `postgres`, i.e. un-signalable here.

    Tagged with `_BLOCKER_APPNAME` so it can be picked out of pg_stat_activity
    by name (see `_wait_for_blocker`).
    """
    return subprocess.Popen(
        [
            "sudo",
            "-n",
            "-u",
            "postgres",
            "env",
            f"PGAPPNAME={_BLOCKER_APPNAME}",
            "psql",
            "-d",
            db_name,
            "-Atc",
            f"select pg_sleep({seconds})",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_blocker(conn, db_name: str, timeout: float = 15.0) -> int:
    """The pid of OUR blocker on `db_name` — not merely of some backend.

    "The first row in pg_stat_activity for this name" was the assumption here,
    and it is false: a scratch database is not empty. A transient background
    worker beat our psql to one in a full-suite run, `_wait_for_blocker`
    handed back its pid, and the assertion then looked for a pid that had
    already gone while the real blocker named in the failure was a different
    one (t_d06c85c7). Matching the tag `_attach_blocker` sets is what makes the
    pid ours; `application_name` is readable across roles, so the tag is
    visible exactly when `backend_type`/`state`/`query` are not.
    """
    expires = time.monotonic() + timeout
    while time.monotonic() < expires:
        rows = conn.execute(
            text(
                "SELECT pid FROM pg_stat_activity WHERE datname = :name AND application_name = :app"
            ),
            {"name": db_name, "app": _BLOCKER_APPNAME},
        ).all()
        if rows:
            return int(rows[0][0])
        time.sleep(0.05)
    raise AssertionError(f"the blocker never attached to {db_name}")


def _kill_blocker(pid: int) -> None:
    """End the blocker the only way this role can: as its own (superuser) role."""
    subprocess.run(
        [
            "sudo",
            "-n",
            "-u",
            "postgres",
            "psql",
            "-d",
            "postgres",
            "-Atc",
            f"select pg_terminate_backend({pid})",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )


# --- draining a scratch database before it is dropped ----------------------------
# The `finally`s below all end the same way: make sure whatever this test put on
# its scratch database is gone, then DROP it. Killing once off a snapshot of
# `pg_stat_activity` and dropping is what reded.
_DRAIN_POLL = 0.05
# The kills land in well under a second; this only has to outlast a client that
# is still being scheduled in on a loaded box (its `sudo`+`psql` start is what
# the red run was waiting on), and the DROP below still has its own deadline.
_DRAIN_DEADLINE = 20.0


def _attached(conn, db_name: str) -> list[int]:
    """Every backend pid `pg_stat_activity` shows on `db_name` right now."""
    return [
        int(row[0])
        for row in conn.execute(
            text("SELECT pid FROM pg_stat_activity WHERE datname = :n"), {"n": db_name}
        )
    ]


def _drain_then_drop(conn, db_name: str, *clients: subprocess.Popen | None) -> None:
    """Terminate every backend on `db_name`, wait until none is left, then DROP it.

    One pass of kills is NOT a cleanup: `pg_stat_activity` is a snapshot, and
    the untagged decoy psql this file spawns is a client that had not finished
    CONNECTING when the snapshot was taken — under load its `sudo`+`psql` start
    loses the race to the blocker's, which `_wait_for_blocker` had already
    confirmed. So the kill loop ran over the blocker alone, the decoy attached a
    moment later as `application_name='psql'`, and `DROP DATABASE … WITH (FORCE)`
    — which validates every attached backend before signalling any, and may not
    signal a `postgres`-role session from `fintrack` — spent its whole 30 s
    deadline on it and raised. Reproduced three times on this box: once in a
    solo run of this file and twice in five runs while two of them executed
    concurrently. The instrumentation that pinned it printed
    `pids at kill time: [<blocker>]` and then, five samples running,
    `still attached: [(<decoy>, 'psql')]`.

    So the drop waits for BOTH facts that make it safe, not one:

    * nothing is attached — re-checked every pass, so a client that attaches
      late is killed on the next one; and
    * every client process has exited — a client that has NOT connected yet
      would connect later, and no number of kills aimed at the backends already
      attached can stop that. Only an exited client can never attach again.

    A client that outlives the deadline is killed here (its `psql` child may
    still outlive the `sudo` parent, so this is a last resort, not a guarantee)
    and the DROP's own deadline reports what is left.
    """
    expires = time.monotonic() + _DRAIN_DEADLINE
    while True:
        pids = _attached(conn, db_name)
        if not pids and all(c is None or c.poll() is not None for c in clients):
            break
        past_deadline = time.monotonic() >= expires
        if past_deadline:
            for client in clients:
                if client is not None and client.poll() is None:
                    client.kill()
        for pid in pids:
            _kill_blocker(pid)
        if past_deadline:
            break
        time.sleep(_DRAIN_POLL)
    _drop(conn, db_name)


def _reap(*clients: subprocess.Popen | None) -> None:
    """Never leave a client the drain had to kill unreaped."""
    for client in clients:
        if client is None:
            continue
        try:
            client.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            client.kill()
            client.wait(timeout=5)


def test_force_drop_waits_out_a_backend_it_cannot_signal() -> None:
    """The review-round-1 failure, on a REAL database, end to end.

    A backend `fintrack` may not signal makes `DROP DATABASE … WITH (FORCE)`
    fail outright — pinned below with the raw statement, exactly as the red run
    issued it — and the helper must ride that out once the backend leaves,
    instead of letting the session die.
    """
    _require_local_postgres()
    name = _scratch_db("fdrop")  # ends in OUR pid: no peer's reaper may touch it
    admin = _admin()
    blocker: subprocess.Popen | None = None
    try:
        with admin.connect() as conn:
            _create(conn, name)
            _require_same_server(conn, name)
            blocker = _attach_blocker(name, seconds=3)
            _wait_for_blocker(conn, name)
            with pytest.raises(ProgrammingError) as raw:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            assert "permission denied to terminate process" in str(raw.value)

            drop_database_force(conn, name, deadline=30.0)
            assert not _exists(conn, name), "the database should be gone by now"
    finally:
        try:
            with admin.connect() as conn:
                # a 3 s sleeper: normally gone already, terminated if not — and
                # its client must have exited before the drop runs.
                _drain_then_drop(conn, name, blocker)  # no-op when the drop above already ran
        finally:
            _reap(blocker)
            admin.dispose()


def test_force_drop_says_which_backend_would_not_leave() -> None:
    """A backend that never leaves: bounded, loud, and naming the blocker."""
    _require_local_postgres()
    name = _scratch_db("fstuck")
    admin = _admin()
    blocker: subprocess.Popen | None = None
    pid: int | None = None
    try:
        with admin.connect() as conn:
            _create(conn, name)
            _require_same_server(conn, name)
            blocker = _attach_blocker(name, seconds=300)  # it is not leaving on its own
            pid = _wait_for_blocker(conn, name)
            with pytest.raises(RuntimeError) as excinfo:
                drop_database_force(conn, name, deadline=1.0)
            message = str(excinfo.value)
            assert name in message, "the database must be named"
            assert f"pid {pid} (" in message, "the un-signalable backend must be named"
    finally:
        try:
            with admin.connect() as conn:
                _drain_then_drop(conn, name, blocker)
        finally:
            _reap(blocker)
            admin.dispose()


# --- 5. the guard's blind spot, proven on a REAL backend of another role -----------


def test_wait_for_blocker_finds_its_own_backend_when_another_ones_there() -> None:
    """A scratch database is NOT empty — so "first row" is not "our blocker".

    The full-suite run of t_d06c85c7 failed on exactly this: a transient
    backend attached to a freshly created database before the blocker did, the
    helper handed back that pid, and the assertion then searched for a pid that
    had already gone while the real blocker was a different one. The decoy here
    attaches FIRST, as an untagged psql, so the old "any row will do" answer is
    the wrong one by construction.
    """
    _require_local_postgres()
    name = _scratch_db("wb")  # ends in OUR pid: no peer's reaper may touch it
    admin = _admin()
    decoy: subprocess.Popen | None = None
    blocker: subprocess.Popen | None = None
    try:
        with admin.connect() as conn:
            _create(conn, name)
            _require_same_server(conn, name)
            decoy = subprocess.Popen(
                [
                    "sudo",
                    "-n",
                    "-u",
                    "postgres",
                    "psql",
                    "-d",
                    name,
                    "-Atc",
                    "select pg_sleep(60)",  # no PGAPPNAME: never our blocker
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            blocker = _attach_blocker(name, seconds=60)
            pid = _wait_for_blocker(conn, name)
            tagged = conn.execute(
                text(
                    "SELECT application_name FROM pg_stat_activity WHERE datname = :n AND pid = :p"
                ),
                {"n": name, "p": pid},
            ).scalar()
            assert tagged == _BLOCKER_APPNAME, (
                f"pid {pid} is {tagged!r}, not our blocker — the decoy was picked"
            )
    finally:
        # Neither psql leaves on its own and neither is signalable by us, so
        # end them the way this role can — by backend pid, from the server side
        # — and wait for them to be GONE before the drop; a bare `Popen.kill()`
        # only ends the `sudo` parent, and the decoy's `psql` is still able to
        # attach (this is what reded) until its own process has exited.
        try:
            with admin.connect() as conn:
                _drain_then_drop(conn, name, blocker, decoy)
        finally:
            _reap(blocker, decoy)
            admin.dispose()


def test_the_guard_refuses_a_backend_whose_type_this_role_may_not_read() -> None:
    """The anti-weakening half of the fix, on a real database.

    `_Activity` above pins what the guard does with an unreadable
    `backend_type`; this pins that the column really IS unreadable for a peer
    running as another role — and that such a peer is still refused. Without
    that second half, `WHERE backend_type = 'client backend'` would look like
    the tidy fix for the flake and would let a second session straight past
    the check this whole file exists for.
    """
    _require_local_postgres()
    name = _scratch_db("grole")  # ends in OUR pid: no peer's reaper may touch it
    admin = _admin()
    blocker: subprocess.Popen | None = None
    pid: int | None = None
    try:
        with admin.connect() as conn:
            _create(conn, name)
            _require_same_server(conn, name)
            blocker = _attach_blocker(name, seconds=12)
            pid = _wait_for_blocker(conn, name)
            readable = conn.execute(
                text("SELECT backend_type FROM pg_stat_activity WHERE pid = :p"),
                {"p": pid},
            ).scalar()
            if readable is not None:
                pytest.skip(
                    f"this role reads backend_type={readable!r} for another role's"
                    " backend, so the unreadable case cannot be exercised here"
                )
            with pytest.raises(RuntimeError, match="already using"):
                _assert_databases_free(conn, (name,))
    finally:
        try:
            with admin.connect() as conn:
                _drain_then_drop(conn, name, blocker)
        finally:
            _reap(blocker)
            admin.dispose()

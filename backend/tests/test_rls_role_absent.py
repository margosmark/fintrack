"""Migration 0058 replays on a database whose cluster has NO app role (docs/55).

Every other RLS test in the suite runs against a provisioned database when
the cluster carries `fintrack_app`, and skips through `app_role_available`
(with the provisioning command) when it does not, so `_app_role_exists` was
always True in those runs and the "role missing" branch of `apply_policies`
had never executed — that is exactly why no gate caught it sending its
human-readable NOTICE to Postgres **as SQL**. On the 2026-10-31 production deploy of `b6cde5a2` the
role did not exist, and `alembic upgrade head` died with
`ProgrammingError: (psycopg.errors.SyntaxError) syntax error at or near
"NOTICE"`, crash-looping the api container (reproduced verbatim before this
fix on the throwaway cluster this module builds).

So this module builds a real cluster that has never had the role: a private
`initdb` data directory on a private port, hermetic and independent of the
shared cluster the rest of the suite runs on. The claims pinned here:

* `upgrade head` (0057 → 0058 → head) exits 0, lands the policies, and
  raises no ProgrammingError/SyntaxError;
* `apply_policies` executes **no statement at all** in the missing-role
  branch — it reports the condition on the logger instead (AC: the
  role-absent branch executes no SQL);
* 0058's `downgrade`/`revoke_rls` path is role-agnostic too, so
  upgrade → downgrade → upgrade round-trips on the role-less database.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, event, text
from sqlalchemy.engine import Engine, make_url

from alembic import command
from app.db.rls import APP_ROLE, apply_policies

_BACKEND = Path(__file__).resolve().parents[1]


def _head_revision() -> str:
    """Resolve the repo's current head — never pin one.

    This module pinned `0058_row_level_security`, which held only while 0058
    WAS head; every migration stacked on top (M188's 0059, M189's 0060, …)
    broke it. Rollbacks target `_BASE` (the revision before RLS) explicitly:
    `-1` only meant "past RLS" while 0058 was the tip.
    """
    cfg = Config(str(_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


_HEAD = _head_revision()
_BASE = "0057_price_fetched_at"
_SUPERUSER = "fintrack"


def _initdb() -> Path | None:
    """Locate the postgres server binaries (initdb/pg_ctl) on this host"""
    found = shutil.which("initdb")
    if found:
        return Path(found).parent
    # Debian-style multi-version layout (…/postgresql/17/bin/initdb)
    for candidate in sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb")):
        return candidate.parent
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _url(port: int, database: str) -> str:
    # auth=trust in the throwaway cluster, so the password is never checked
    return f"postgresql+psycopg://{_SUPERUSER}:fintrack@127.0.0.1:{port}/{database}"


@pytest.fixture(scope="module")
def _role_absent_cluster() -> Iterator[str]:
    """A fresh Postgres cluster where `fintrack_app` has never existed

    Yields the URL of its `fintrack` database. Skips (loudly) rather than
    silently passing on a host with no server binaries: a gate that cannot
    run this module has not verified the role-absent path.
    """
    bindir = _initdb()
    if bindir is None:
        pytest.skip(
            "postgres server binaries (initdb/pg_ctl) are not installed on this"
            " host — the role-absent migration replay cannot be verified here"
        )

    data_dir = Path(tempfile.mkdtemp(prefix="rls_role_absent_"))
    port = _free_port()
    log_path = data_dir / "server.log"
    subprocess.run(
        [
            str(bindir / "initdb"),
            "-U",
            _SUPERUSER,
            "--auth=trust",
            "--no-instructions",
            "-D",
            str(data_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            str(bindir / "pg_ctl"),
            "-D",
            str(data_dir),
            "-l",
            str(log_path),
            # fsync off: this cluster exists for seconds and holds no data
            "-o",
            f"-p {port} -k {data_dir} -c listen_addresses=127.0.0.1 -c fsync=off",
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    admin = create_engine(_url(port, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{_SUPERUSER}"'))
    finally:
        admin.dispose()

    try:
        with create_engine(_url(port, "postgres")).connect() as connection:
            provisioned = connection.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                {"role": APP_ROLE},
            ).scalar_one()
        assert not provisioned, (
            f"{APP_ROLE} unexpectedly exists in a fresh initdb cluster — this"
            " module would silently stop testing the role-absent path"
        )
        yield _url(port, _SUPERUSER)
    finally:
        subprocess.run(
            [str(bindir / "pg_ctl"), "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
            capture_output=True,
            text=True,
        )
        shutil.rmtree(data_dir, ignore_errors=True)


def _alembic_config(url: str) -> Config:
    cfg = Config(str(_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _state(engine: Engine) -> tuple[bool, int, int, int, str, bool]:
    """(RLS on transactions, policies, helpers, armed defaults, version, role)"""
    with engine.connect() as connection:
        return _state_on(connection)


def _state_on(connection: Connection) -> tuple[bool, int, int, int, str, bool]:
    rls = connection.execute(
        text("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass('public.transactions')")
    ).scalar_one()
    policies = connection.execute(
        text("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
    ).scalar_one()
    helpers = connection.execute(
        text(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'public' AND p.proname IN"
            " ('current_tenant_id', 'current_tenant_email', 'partner_visible_tenants',"
            " 'sharing_link_identity_matches')"
        )
    ).scalar_one()
    defaults = connection.execute(
        text(
            "SELECT count(*) FROM pg_default_acl"
            " WHERE defaclnamespace = 'public'::regnamespace"
            " AND array_to_string(defaclacl, ',') LIKE :role"
        ),
        {"role": f"%{APP_ROLE}%"},
    ).scalar_one()
    version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    role = connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"), {"role": APP_ROLE}
    ).scalar_one()
    return bool(rls), policies, helpers, defaults, version, role  # type: ignore[return-value]


def test_upgrade_head_succeeds_without_the_app_role(_role_absent_cluster: str) -> None:
    """0057 → 0058 → head lands the policies on a role-less database, exit 0

    This is the deploy step that crash-looped the api: the missing-role
    branch used to send its NOTICE string to Postgres as SQL.
    """
    command.upgrade(_alembic_config(_role_absent_cluster), _BASE)
    engine = create_engine(_role_absent_cluster)
    try:
        _rls, policies, _helpers, defaults, version, role = _state(engine)
        assert version == _BASE
        assert policies == 0, "0057 must predate every RLS policy"

        command.upgrade(_alembic_config(_role_absent_cluster), "head")
        rls, policies, helpers, defaults, version, role = _state(engine)
        assert version == _HEAD, "the upgrade died before reaching head"
        assert rls, "0058 did not enable RLS on transactions"
        assert policies > 0 and helpers == 4, (policies, helpers)
        assert defaults == 0, (
            "0058 armed default privileges for a role that does not exist —"
            " grants must be skipped entirely when the role is absent"
        )
        assert not role, "the fixture's precondition: the app role was never created"
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade_round_trip_without_the_app_role(
    _role_absent_cluster: str,
) -> None:
    """0058's downgrade is role-agnostic: `revoke_rls` never names the role"""
    engine = create_engine(_role_absent_cluster)
    try:
        # The previous test left the cluster at head; make that explicit so
        # this test's own claim does not depend on execution order.
        if _state(engine)[4] != _HEAD:
            command.upgrade(_alembic_config(_role_absent_cluster), "head")

        command.downgrade(_alembic_config(_role_absent_cluster), _BASE)
        rls, policies, helpers, defaults, version, _role = _state(engine)
        assert not rls and policies == 0 and helpers == 0, (rls, policies, helpers)
        assert defaults == 0, "the downgrade left default privileges armed"
        assert version == _BASE

        command.upgrade(_alembic_config(_role_absent_cluster), "head")
        rls, policies, helpers, defaults, version, _role = _state(engine)
        assert rls and policies > 0 and helpers == 4 and defaults == 0
        assert version == _HEAD
    finally:
        engine.dispose()


def test_missing_role_branch_executes_no_sql_and_logs_the_condition(
    _role_absent_cluster: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The degraded branch is a report, never a statement (the P0's shape)

    Records every statement `apply_policies` puts on the wire and asserts
    none of them is the NOTICE string — the assertion that fails on the
    pre-fix code even before Postgres turns it into a SyntaxError.
    """
    engine = create_engine(_role_absent_cluster)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, _params, _context, _many):  # type: ignore[no-untyped-def]
        statements.append(statement)

    try:
        with caplog.at_level(logging.WARNING, logger="app.db.rls.policies"):
            with engine.connect() as connection:
                apply_policies(connection)
    finally:
        engine.dispose()

    notices = [s for s in statements if s.lstrip().upper().startswith("NOTICE")]
    assert not notices, (
        "apply_policies sent its missing-role message to Postgres as SQL:"
        f" {notices[0][:120]!r} — a human-readable condition must be logged,"
        " never executed (it is a SyntaxError on every unprovisioned database)"
    )
    reported = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and APP_ROLE in record.getMessage()
    ]
    assert reported, (
        "the missing-role condition was neither executed nor reported — a"
        " database upgraded without the role must say so out loud"
    )
    assert "provision_rls_role" in reported[0].getMessage()


def test_at_head_database_stays_ungranted_until_0058_is_replayed(
    _role_absent_cluster: str,
) -> None:
    """Provisioning an at-head database arms nothing until `0058` is replayed

    The re-arm trap docs/55 §Rollout names: every per-database grant rides
    revision `0058`, so on a database that reached head while the role was
    absent — the state the tests above build, and the state the first deploy
    of `0058` to an unprovisioned cluster produces — creating the role and
    running `alembic upgrade head` grants nothing. The app then refuses to
    boot against that database, so the documented replay (`downgrade
    0057_price_fetched_at`,
    then `upgrade head`, as the schema owner) is the only path that arms it.

    Also the direct pin for `verify_request_role`'s UNGRANTED refusal
    (`app/core/rls.py`): the schema-owner refusal was the only branch with a
    direct test until now (`test_rls_isolation.py`).
    """
    from app.core.rls import verify_request_role

    engine = create_engine(_role_absent_cluster)
    app_engine: Engine | None = None
    try:
        rls, policies, helpers, _defaults, version, role = _state(engine)
        assert version == _HEAD, "this test needs the at-head state the module built"
        assert not role, "the module's precondition: the app role does not exist yet"
        assert rls and policies > 0 and helpers == 4, (rls, policies, helpers)

        # Provision, exactly as scripts/provision_rls_role.py does: a
        # CLUSTER-wide role. Nothing per-database is applied by that step.
        with engine.begin() as connection:
            connection.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_ROLE}'"))

        # `alembic upgrade head` on a database already at head is a no-op.
        command.upgrade(_alembic_config(_role_absent_cluster), "head")
        assert _state(engine)[4] == _HEAD, "upgrade head must not move a database at head"
        assert not _can_read_transactions(engine), (
            "the trap did not reproduce: creating the role must NOT re-apply"
            " the per-database grants — that is the state docs/55 §Rollout"
            " names the `0058` replay for"
        )

        # Arming the app engine at that database is refused, loudly — the
        # ungranted branch of the startup guard.
        app_engine = create_engine(
            make_url(_role_absent_cluster).set(username=APP_ROLE, password=APP_ROLE)
        )
        with pytest.raises(RuntimeError, match=r"cannot read public\.transactions"):
            verify_request_role(app_engine)

        # The documented replay is what arms it, and the same guard passes.
        command.downgrade(_alembic_config(_role_absent_cluster), _BASE)
        command.upgrade(_alembic_config(_role_absent_cluster), "head")
        assert _can_read_transactions(engine), "the `0058` replay re-applied no grants"
        verify_request_role(app_engine)  # logs the verified line, raises nothing

        # Leave the cluster as the module found it — at head, role absent —
        # so every claim in this module stays independent of test order.
        command.downgrade(_alembic_config(_role_absent_cluster), _BASE)
        with engine.begin() as connection:
            # DROP OWNED BY first: `revoke_rls` does not revoke the
            # per-database grants (CONNECT, schema/table privileges), and
            # DROP ROLE must not inherit them.
            connection.execute(text(f'DROP OWNED BY "{APP_ROLE}"'))
            connection.execute(text(f'DROP ROLE "{APP_ROLE}"'))
        command.upgrade(_alembic_config(_role_absent_cluster), "head")
        rls, policies, helpers, defaults, version, role = _state(engine)
        assert version == _HEAD and not role, (version, role)
        assert rls and policies > 0 and helpers == 4 and defaults == 0, (
            rls,
            policies,
            helpers,
            defaults,
        )
    finally:
        if app_engine is not None:
            app_engine.dispose()
        engine.dispose()


def _can_read_transactions(engine: Engine) -> bool:
    """Does the app role hold SELECT on public.transactions in this database?"""
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text("SELECT has_table_privilege(:role, 'public.transactions', 'SELECT')"),
                {"role": APP_ROLE},
            ).scalar_one()
        )

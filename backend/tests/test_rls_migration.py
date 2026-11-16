"""The RLS migration replays from scratch and reverses cleanly (docs/55).

Runs against a hermetic `<suite-db>_rlsrm` database (the test_demo_seed
pattern): upgrade → the policies are live → downgrade → they are gone →
upgrade again → they are back. The from-scratch half of the claim (every
existing migration still replays) is the fixture's own `upgrade head`,
which replays the full chain including 0058 on a brand-new database.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from conftest import drop_database_force
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alembic import command
from app.core.config import get_settings

_BACKEND = Path(__file__).resolve().parents[1]
_BASE = "0057_price_fetched_at"


def _head_revision() -> str:
    """Resolve the repo's current head instead of pinning one.

    This module pinned `0058_row_level_security`, which held only while 0058
    WAS head; every migration stacked on top (M188's 0059, M189's 0060, …)
    broke it. The claim here is always about the head of the chain, and the
    rollback target is always `_BASE` — the revision before RLS — because
    `-1` only meant "past RLS" while 0058 was the tip.
    """
    cfg = Config(str(_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


_HEAD = _head_revision()


@pytest.fixture(scope="module")
def _rls_database() -> Iterator[Engine]:
    """Hermetic database: full replay to head, URL swapped for the module"""
    base = os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://fintrack:fintrack@localhost:5432/fintrack"
    )
    suite_db = base.rsplit("/", 1)[-1]
    rls_db = f"{suite_db}_rlsrm"
    rls_url = base.rsplit("/", 1)[0] + f"/{rls_db}"
    admin = create_engine(base.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            drop_database_force(conn, rls_db)
            conn.execute(text(f'CREATE DATABASE "{rls_db}"'))
            # Same timeout the suite sets on itself (conftest). Without it a
            # `DROP POLICY` meeting a reader simply blocks, which is what made
            # the lock tests below pass vacuously: the retry in `drop_policies`
            # is only observable when a waited-on DDL statement gives up.
            conn.execute(text(f"ALTER DATABASE \"{rls_db}\" SET lock_timeout = '500ms'"))
    finally:
        admin.dispose()

    import app.api.deps as deps

    # Everything from the swap onward sits in try/finally: a generator
    # fixture that raises before its yield never restores (t_435c2d14).
    os.environ["DATABASE_URL"] = rls_url
    try:
        get_settings.cache_clear()
        deps._engine = None  # force deps to rebuild against the hermetic DB
        cfg = _alembic_config(rls_url)
        command.upgrade(cfg, "head")  # the from-scratch replay claim
        engine = create_engine(rls_url)
        try:
            yield engine
        finally:
            engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = base
        get_settings.cache_clear()
        deps._engine = None


def _alembic_config(url: str) -> Config:
    cfg = Config(str(_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _state(engine: Engine) -> tuple[bool, int, int, int, str]:
    """(RLS on transactions, policy count, helper count, armed defaults, head rev)"""
    with engine.connect() as conn:
        rls = conn.execute(
            text(
                "SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass('public.transactions')"
            )
        ).scalar_one()
        policies = conn.execute(
            text("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
        ).scalar_one()
        helpers = conn.execute(
            text(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'public' AND p.proname IN"
                " ('current_tenant_id', 'current_tenant_email', 'partner_visible_tenants',"
                " 'sharing_link_identity_matches')"
            )
        ).scalar_one()
        # `apply_policies` arms ALTER DEFAULT PRIVILEGES so a FUTURE migration's
        # table is born granted — an arm the downgrade must disarm, or the next
        # table the migrator RE-CREATES arrives holding privileges the registry
        # never declared (QA t_592a6e1a red 2: benchmark_prices).
        defaults = conn.execute(
            text(
                "SELECT count(*) FROM pg_default_acl"
                " WHERE defaclnamespace = 'public'::regnamespace"
                " AND array_to_string(defaclacl, ',') LIKE '%fintrack_app%'"
            )
        ).scalar_one()
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    return bool(rls), policies, helpers, defaults, version  # type: ignore[return-value]


def test_full_replay_enables_rls(app_role_available: None, _rls_database: Engine) -> None:
    """A from-scratch `upgrade head` lands the policies and the head revision"""
    rls, policies, helpers, defaults, version = _state(_rls_database)
    assert rls, "0058 did not enable RLS on transactions during a fresh replay"
    assert policies > 0 and helpers == 4, (policies, helpers)
    assert defaults > 0, "0058 did not arm the app role's default privileges"
    assert version == _HEAD


def test_downgrade_removes_policies_and_helpers_then_upgrade_restores_them(
    app_role_available: None,
    _rls_database: Engine,
) -> None:
    """The migration is reversible in both directions"""
    # NOT str(url): SQLAlchemy 2.x masks the password there
    url = _rls_database.url.render_as_string(hide_password=False)
    command.downgrade(_alembic_config(url), _BASE)

    rls, policies, helpers, defaults, version = _state(_rls_database)
    assert not rls, "downgrade left RLS enabled"
    assert policies == 0, f"downgrade left {policies} policies"
    assert helpers == 0, f"downgrade left {helpers} helper functions"
    assert defaults == 0, (
        "downgrade left the app role's ALTER DEFAULT PRIVILEGES armed: the next"
        " table the migrator re-creates is born with CRUD the registry never"
        " declared (m106's downgrade-to-0038 replay)"
    )
    assert version == "0057_price_fetched_at"

    command.upgrade(_alembic_config(url), "head")
    rls, policies, helpers, defaults, version = _state(_rls_database)
    assert rls and policies > 0 and helpers == 4 and defaults > 0
    assert version == _HEAD


def _open_reader(url: str) -> tuple[threading.Thread, Engine, threading.Event]:
    """A session that holds a plain read lock on `securities` until released

    Exactly the shape QA t_592a6e1a F1 found: `idle in transaction` with an
    ACCESS SHARE on the table, which is what `DROP POLICY` (ACCESS EXCLUSIVE)
    has to wait for. The caller decides when the reader leaves.
    """
    engine = create_engine(url)
    release = threading.Event()

    def hold() -> None:
        with engine.connect() as conn:
            conn.execute(text("SELECT id FROM public.securities LIMIT 1"))
            release.wait(timeout=120)
            conn.rollback()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    for _ in range(200):
        with engine.connect() as probe:
            waiting = probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database()"
                    " AND state = 'idle in transaction'"
                )
            ).scalar_one()
        if waiting:
            return thread, engine, release
        time.sleep(0.05)
    raise AssertionError("the blocking reader never reached the database")


READER_SECONDS = 1.2


def test_downgrade_rides_out_a_short_reader(
    app_role_available: None, _rls_database: Engine
) -> None:
    """A reader that releases inside the retry window does not fail 0058's rollback

    Each exclusive statement in `drop_policies` retries with a growing pause,
    so a live-but-transient reader is waited out instead of aborting the
    downgrade at the database's lock_timeout (QA t_592a6e1a F1).
    """
    url = _rls_database.url.render_as_string(hide_password=False)
    thread, blocker, release = _open_reader(url)
    timer = threading.Timer(READER_SECONDS, release.set)
    timer.start()
    try:
        command.downgrade(_alembic_config(url), _BASE)
    finally:
        timer.cancel()
        release.set()
        thread.join(timeout=30)
        blocker.dispose()

    rls, policies, helpers, defaults, version = _state(_rls_database)
    assert not rls and policies == 0 and helpers == 0 and defaults == 0
    assert version == "0057_price_fetched_at"

    command.upgrade(_alembic_config(url), "head")
    rls, policies, helpers, defaults, version = _state(_rls_database)
    assert rls and policies > 0 and helpers == 4 and defaults > 0
    assert version == _HEAD


def test_downgrade_fails_loudly_when_the_reader_will_not_leave(
    _rls_database: Engine,
) -> None:
    """A wedged reader ends the downgrade in an error, never in a silent skip

    Tolerating a busy table by skipping it would let the migration report a
    rollback it did not perform, so the sweep raises after its retries, and
    the failed migration leaves the schema exactly where it was.
    """
    url = _rls_database.url.render_as_string(hide_password=False)
    thread, blocker, release = _open_reader(url)
    try:
        with pytest.raises(Exception, match="still blocked after"):
            command.downgrade(_alembic_config(url), _BASE)
    finally:
        release.set()
        thread.join(timeout=30)
        blocker.dispose()

    # The failed migration rolled back whole: the policies are still live.
    rls, policies, helpers, _defaults, version = _state(_rls_database)
    assert rls and policies > 0 and helpers == 4 and version == _HEAD

"""Coverage guards for row-level security (docs/55, modelled on Lumina's
tests/test_rls_coverage.py @ 7d6e740c370cae33f48e9ac4d9bd72617a4bb664).

The registry in app/db/rls/policies.py is the single source of truth for
how each table is protected; this file is what keeps it true as tables are
added:

* every public table is classified (secured / child / auth / global-read) —
  an unclassified table fails here by name, which is the forcing function
  for a new table's migration to call `secure_registered_table` /
  `grant_auth_table` / `grant_global_read_table`;
* the classified state matches the database (RLS enabled where registered,
  helpers present with the right SECURITY DEFINER shape);
* the app role holds exactly the declared privileges — no more.

The role-privilege assertions need the provisioned app role and skip (with
the provisioning command) where it is absent; the schema assertions run
everywhere.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from rls_support import app_role_missing_reason
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.api.deps import get_engine
from app.db.rls import APP_ROLE, AUTH_TABLES, CHILD_TABLES, GLOBAL_READ_TABLES, SECURED_TABLES
from app.db.rls.functions import (
    CURRENT_TENANT_EMAIL,
    CURRENT_TENANT_ID,
    PARTNER_VISIBLE_TENANTS,
    SHARING_LINK_IDENTITY_MATCHES,
)

# Privileges the app role is allowed to hold, per class — "only what it
# needs" (docs/55 §Approach). A NEW privilege on a registered table means
# this file and the policy registry must both say so.
_EXPECTED_TABLE_PRIVILEGES: dict[str, set[str]] = {
    **{table: {"SELECT", "INSERT", "UPDATE", "DELETE"} for table, _, _ in SECURED_TABLES},
    **{table: {"SELECT", "INSERT", "UPDATE", "DELETE"} for table, _ in CHILD_TABLES},
    **{table: {"SELECT", "INSERT", "UPDATE", "DELETE"} for table in AUTH_TABLES},
    **{table: {"SELECT"} for table in GLOBAL_READ_TABLES},
    "fx_rates": {"SELECT", "INSERT", "UPDATE"},
}

# SECURITY DEFINER shapes: (name, identity args, is_definer)
_HELPERS = (
    (CURRENT_TENANT_ID, "()", False),
    (CURRENT_TENANT_EMAIL, "()", True),
    (PARTNER_VISIBLE_TENANTS, "()", True),
    (SHARING_LINK_IDENTITY_MATCHES, "(bigint, bigint, text, text)", True),
)


@pytest.fixture(scope="module")
def owner_conn() -> Iterator[Connection]:
    """The suite database as its schema owner (the role migrations run as)"""
    with get_engine().connect() as conn:
        yield conn


def _public_tables(conn: Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    }


def _classified_tables() -> set[str]:
    secured = {table for table, _, _ in SECURED_TABLES}
    child = {table for table, _ in CHILD_TABLES}
    return secured | child | set(AUTH_TABLES) | set(GLOBAL_READ_TABLES)


# Alembic's bookkeeping table: not application data, never touched by the
# app role (alembic runs as the schema owner), so it belongs to no class.
_NON_APP_TABLES = {"alembic_version"}


def test_every_public_table_is_classified(owner_conn: Connection) -> None:
    """No table may exist without a declared RLS class (docs/55 §Approach)

    This is the forcing function: a migration that adds a table must also
    register it — secured via `secure_registered_table`, auth via
    `grant_auth_table`, global-read via `grant_global_read_table` — or this
    test fails naming it.
    """
    tables = _public_tables(owner_conn) - _NON_APP_TABLES
    classified = _classified_tables()
    unclassified = tables - classified
    stale = classified - tables  # registered but absent (partial replay) is a registry bug
    assert not unclassified, (
        "public tables with no RLS classification in app/db/rls/policies.py:"
        f" {sorted(unclassified)} — register each one (secure_registered_table /"
        " grant_auth_table / grant_global_read_table) in its migration"
    )
    assert not stale, f"classified tables missing from the schema: {sorted(stale)}"


def test_secured_and_child_tables_have_rls_and_policies(owner_conn: Connection) -> None:
    """Every registered tenant/child table is protected by a live policy"""
    secured = {table for table, _, _ in SECURED_TABLES}
    child = {table for table, _ in CHILD_TABLES}
    for table in sorted(secured | child):
        row = owner_conn.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:name)"),
            {"name": f"public.{table}"},
        ).one()
        assert row[0], f"ROW LEVEL SECURITY is not enabled on {table}"
        policies = {
            r[0]
            for r in owner_conn.execute(
                text(
                    "SELECT policyname FROM pg_policies WHERE schemaname='public' AND tablename=:t"
                ),
                {"t": table},
            )
        }
        assert policies, f"no policy registered on {table}"


def test_auth_and_global_tables_have_no_rls(owner_conn: Connection) -> None:
    """Auth queries run pre-identity; globals are shared caches (docs/55)"""
    for table in sorted(set(AUTH_TABLES) | set(GLOBAL_READ_TABLES)):
        row = owner_conn.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:name)"),
            {"name": f"public.{table}"},
        ).one()
        assert not row[0], f"{table} must stay outside RLS (docs/55 §Approach)"
        policies = owner_conn.execute(
            text("SELECT count(*) FROM pg_policies WHERE schemaname='public' AND tablename=:t"),
            {"t": table},
        ).scalar_one()
        assert policies == 0, f"{table} must carry no policy"


def test_helper_functions_exist_with_the_right_shape(owner_conn: Connection) -> None:
    """The four helpers exist, with matching signatures and definer flags"""
    for name, identity_args, expect_definer in _HELPERS:
        # oidvectortypes() renders just the argument TYPES ("bigint, text"),
        # which is the drop-signature form the registry pairs with each helper.
        row = owner_conn.execute(
            text(
                "SELECT p.prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname='public' AND p.proname = :name"
                " AND oidvectortypes(p.proargtypes) = :args"
            ),
            {"name": name.split(".")[-1], "args": identity_args.lstrip("(").rstrip(")")},
        ).first()
        assert row is not None, f"missing helper {name}{identity_args}"
        assert row[0] is expect_definer, (
            f"{name} expected SECURITY DEFINER={expect_definer}, got {row[0]}"
        )


def test_no_orphaned_helper_signatures(owner_conn: Connection) -> None:
    """No leftover helper from a retired signature lingers in the schema"""
    from app.db.rls.functions import _OBSOLETE_SIGNATURES

    for signature in _OBSOLETE_SIGNATURES:
        # "public.name(types)" — parse back to name for pg_proc lookup
        bare = signature.split("(")[0]
        args = signature.split("(", 1)[1].rstrip(")")
        row = owner_conn.execute(
            text(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname='public' AND p.proname = :name"
                " AND oidvectortypes(p.proargtypes) = :args"
            ),
            {"name": bare.split(".")[-1], "args": args},
        ).scalar_one()
        assert row == 0, f"obsolete helper {signature} still exists"


@pytest.fixture()
def app_role_conn(app_role_available: None) -> Iterator[Connection]:
    from rls_support import app_role_url
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    # Function-scoped + NullPool: a failed statement in one test can never
    # hand an aborted transaction to the next one.
    engine = create_engine(app_role_url(), poolclass=NullPool, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            yield conn
    finally:
        engine.dispose()


def test_app_role_privileges_match_the_registry_exactly(app_role_conn: Connection) -> None:
    """The app role holds the declared privileges and nothing else"""
    actual: dict[str, set[str]] = {}
    rows = app_role_conn.execute(
        text(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants"
            " WHERE grantee = :role AND table_schema = 'public'"
        ),
        {"role": APP_ROLE},
    )
    for table_name, privilege_type in rows:
        actual.setdefault(table_name, set()).add(privilege_type)

    expected = {table: privs for table, privs in _EXPECTED_TABLE_PRIVILEGES.items()}
    assert set(actual) == set(expected), (
        "app role table set differs from the registry."
        f" extra={sorted(set(actual) - set(expected))}"
        f" missing={sorted(set(expected) - set(actual))}"
    )
    for table in sorted(expected):
        assert actual[table] == expected[table], (
            f"app role privileges on {table}: {sorted(actual[table])} != {sorted(expected[table])}"
        )


def test_app_role_can_use_the_schema_sequences_and_helpers(app_role_conn: Connection) -> None:
    """INSERT needs sequence USAGE; policy evaluation needs EXECUTE"""
    assert app_role_conn.execute(
        text("SELECT has_schema_privilege(CAST(:role AS name), 'public', 'USAGE')"),
        {"role": APP_ROLE},
    ).scalar_one()
    sequence = app_role_conn.execute(
        text(
            "SELECT format('%I.%I', n.nspname, c.relname)"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind = 'S' LIMIT 1"
        )
    ).scalar_one_or_none()
    if sequence is not None:
        # 3-arg form: (user name, schema-qualified sequence text, privilege)
        assert app_role_conn.execute(
            text("SELECT has_sequence_privilege(CAST(:role AS name), CAST(:seq AS text), 'USAGE')"),
            {"role": APP_ROLE, "seq": sequence},
        ).scalar_one(), f"app role lacks USAGE on {sequence}"
    for name, identity_args, _ in _HELPERS:
        assert app_role_conn.execute(
            text(
                "SELECT has_function_privilege("
                "CAST(:role AS name), CAST(:fn AS regprocedure), 'EXECUTE')"
            ),
            {"role": APP_ROLE, "fn": f"{name}{identity_args}"},
        ).scalar_one(), f"app role cannot execute {name}"


def test_helpers_are_not_executable_by_public(owner_conn: Connection) -> None:
    """EXECUTE on the definer helpers belongs to the app role, never PUBLIC

    A fresh no-privilege role inherits exactly `PUBLIC`'s privileges (and on
    PG16+ also holds `CONNECT`), so "PUBLIC has no EXECUTE" IS the claim that
    such a role reports `has_function_privilege(...) = false` — without this
    suite needing CREATEROLE to mint one. Before the revoke, `proacl` carried
    `=X/<owner>` on all four helpers and any cluster role could call
    `current_tenant_email()` against a tenant it stamped for itself
    (docs/55 §Residual risks).
    """
    for name, identity_args, _ in _HELPERS:
        granted_to_public = owner_conn.execute(
            text(
                "SELECT count(*) FROM information_schema.routine_privileges"
                " WHERE routine_schema = 'public' AND routine_name = :name"
                " AND grantee = 'PUBLIC' AND privilege_type = 'EXECUTE'"
            ),
            {"name": name.split(".")[-1]},
        ).scalar_one()
        assert granted_to_public == 0, (
            f"PUBLIC still holds EXECUTE on {name}{identity_args} — any cluster"
            " role that can reach this database could call the SECURITY DEFINER"
            " helper against a tenant of its own choosing (docs/55 §Residual"
            " risks)"
        )


def test_app_role_cannot_bypass_row_level_security(app_role_conn: Connection) -> None:
    """The role the request path uses is not superuser / BYPASSRLS / owner"""
    is_superuser, bypasses_rls, owns_public = app_role_conn.execute(
        text(
            "SELECT current_setting('is_superuser') = 'on',"
            " (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user),"
            " EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public'"
            " AND tableowner = current_user)"
        )
    ).one()
    assert not (is_superuser or bypasses_rls or owns_public), (
        "the request role can bypass RLS — startup verification"
        " (app.core.rls.verify_request_role) would refuse to serve"
    )


def test_role_absence_is_reported_not_silently_skipped() -> None:
    """The skip path must name the provisioning command, never pass quietly"""
    reason = app_role_missing_reason()
    assert reason is None or "provision_rls_role.py" in reason

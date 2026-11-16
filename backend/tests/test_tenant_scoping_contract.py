"""The M178 tenant-scoping contract: a red suite, not a lucky one.

Three waves red with the same class — t_17d025b2 (the lifetime-seeded demo
tenant), t_a451f171 (a live server's committed registration), t_70d46e5e (the
seed snapshot that read it). Each was fixed one leak at a time, and each fix
left the *next* leak possible, because the suite's immunity depended on nothing
having leaked yet. `tests/conftest.py` now states the contract and enforces
both sides of it; this file pins the enforcement itself, because a guard nobody
tests is a comment:

* R1 (write) — nothing a module commits may outlive the module. Proven here by
  running the leak ON PURPOSE in a child session and requiring the MODULE guard
  to fail it, naming the table that grew.
* R2 (read) — the suite database is deliberately dirty: a foreign decoy tenant
  is committed for the whole session, so an unscoped read is wrong on EVERY
  run rather than on the runs where a leak happened to land first.

Run it like any other backend test (docs/01): a real Postgres is required.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import _DECOY_ENABLED, BACKEND_DIR, SUITE_DATABASE_URL, tenant_table_names
from sqlalchemy import create_engine, text
from sqlmodel import Session, select

from app.api.auth import DEFAULT_CATEGORIES
from app.core.security import hash_password
from app.models.categories import Category
from app.models.users import User

#: Tables that deliberately have NO tenant, each with its reason in
#: `tests/test_models.py::NO_USER_ID` — the footprint must NOT watch these,
#: because their rows are shared facts (a close, a rate, an issuer verdict)
#: rather than somebody's data. `User` is NOT one of them: it has no `user_id`
#: for the same reason, but it IS the principal row of every tenant, and the
#: leaks all started with one (see `_TENANT_PRINCIPAL_TABLE`).
_OPERATOR_MODELS = ("FxRate", "ProviderFundRegistry", "BenchmarkPrice")
_PRINCIPAL_MODEL = "User"
_CHILD_MODELS = ("TransactionTag", "RuleTag", "BillReminderMatchTerm")

#: `_suite_probe_engine`'s own URL: the SUITE database, even inside the
#: hermetic modules that repoint DATABASE_URL at a database of their own.
_PROBE_URL = SUITE_DATABASE_URL

#: R2 is off only while somebody bisects a scoping failure
#: (`ISOLATION_DECOY_TENANT=0`). The two pins that read the decoy SKIP in that
#: mode with this reason, instead of going red and looking like the bisect's
#: result — the contract is simply not enforced while the flag is off.
_decoy_required = pytest.mark.skipif(
    not _DECOY_ENABLED,
    reason="ISOLATION_DECOY_TENANT=0: R2 is deliberately off (bisect only, never a release run)",
)


def test_the_registry_is_derived_from_the_models_not_hand_listed() -> None:
    """Guard the guard: the footprint must cover every tenanted table.

    The old guard watched `users` and one email — which is why three incidents
    in a row got through it. This pins the derivation: every model with a
    `user_id` column is watched, the operator-level tables are not, and the
    child tables (tenancy inherited through a parent FK) are.
    """
    import app.models as domain

    watched = set(tenant_table_names())
    with_user_id = {
        obj.__table__.name
        for obj in vars(domain).values()
        if getattr(obj, "__table__", None) is not None
        and not getattr(obj, "__module__", "").startswith("sqlmodel")
        and "user_id" in obj.__table__.c
    }
    assert with_user_id, "the model package stopped exposing tables — introspection broke"
    assert with_user_id <= watched, (
        f"these tenanted tables are NOT in the residue footprint: {sorted(with_user_id - watched)}"
    )
    for model_name in _OPERATOR_MODELS:
        table = getattr(domain, model_name).__table__.name
        assert table not in watched, (
            f"{model_name} ({table}) is operator-level, not tenant data — the "
            "footprint must not require it to be empty"
        )
    principal = getattr(domain, _PRINCIPAL_MODEL).__table__.name
    assert principal in watched, (
        f"{_PRINCIPAL_MODEL} ({principal}) is missing from the footprint — every "
        "leak this contract exists for begins with a committed tenant row, and the "
        "user-id tables alone cannot see it"
    )
    for model_name in _CHILD_MODELS:
        table = getattr(domain, model_name).__table__.name
        assert table in watched, f"{model_name} ({table}) inherits tenancy — watch it"
    # Measured 2026-10-12: `users` + 44 tables carrying `user_id` + 3 that
    # inherit tenancy through a parent FK.
    assert len(watched) >= 48, (
        f"the footprint covers only {len(watched)} tables ({sorted(watched)}) — "
        "the derivation is not seeing the model package"
    )


@_decoy_required
def test_the_decoy_tenant_is_committed_for_the_whole_session() -> None:
    """R2 is LIVE, not documentation: a foreign tenant is really there.

    Read through an engine of its own, like any other connection would: the
    decoy is committed state, so the `session` fixture sees it — and so does a
    leaked row from a sibling module.
    """
    engine = create_engine(_PROBE_URL)
    try:
        with engine.connect() as conn:
            decoys = (
                conn.execute(
                    text("SELECT id FROM users WHERE email LIKE 'decoy-tenant-%@example.com'")
                )
                .scalars()
                .all()
            )
            assert len(decoys) == 1, (
                "exactly one decoy tenant is expected in the suite database; found "
                f"{len(decoys)}. ISOLATION_DECOY_TENANT disables it (conftest); a "
                "disabled decoy means an unscoped read can pass by luck again."
            )
            categories = conn.execute(
                text("SELECT count(*) FROM categories WHERE user_id = :uid"),
                {"uid": decoys[0]},
            ).scalar_one()
    finally:
        engine.dispose()
    assert categories == len(DEFAULT_CATEGORIES), (
        "the decoy must look like the residue the incidents left — a user plus the "
        f"{len(DEFAULT_CATEGORIES)} default categories; it has {categories}"
    )


@_decoy_required
def test_an_unscoped_category_read_is_ambiguous_while_the_suite_is_dirty(
    session: Session,
) -> None:
    """The t_17d025b2 / t_a451f171 failure, reproduced on purpose, every run.

    A tenant seeded by the suite's own session also owns a "Groceries"; the
    decoy owns one too. `select(Category).where(name == ...).one()` — the shape
    `tests/test_cat_common.py::category_by_name` used to have — is then
    `MultipleResultsFound`, and a bare `select(Category)` count is off by ten.
    That is why every read in this suite is scoped by `user_id`.
    """
    from app.models.accounts import Account, AccountType

    email = "contract-unscoped-read@example.com"
    user = User(email=email, hashed_password=hash_password("contract-password"))
    session.add(user)
    session.flush()
    account = Account(
        user_id=user.id, name="Contract chequing", type=AccountType.chequing, currency="CAD"
    )
    session.add(account)
    for name in DEFAULT_CATEGORIES:
        session.add(Category(user_id=user.id, name=name, is_system=True))
    session.flush()

    mine = select(Category).where(Category.user_id == user.id)
    assert len(session.exec(mine).all()) == len(DEFAULT_CATEGORIES)

    unscoped = session.exec(select(Category)).all()
    assert len(unscoped) >= 2 * len(DEFAULT_CATEGORIES), (
        "the suite database is supposed to carry a foreign tenant's categories "
        "on top of this test's own — an unscoped read that looks scoped is how "
        "the ynab seed snapshot went red on 2 of 8 full runs (t_a451f171)"
    )


# --- R1: the module guard, proven firing ------------------------------------------
# The probe below is the leak itself, env-gated so it never runs as part of the
# suite: it commits a tenant through an engine of its own and leaves no teardown
# (exactly what `test_m149_offloop_routes` did before t_a451f171, and what
# `test_locked_fixture_tenant` did before t_17d025b2). The test under it runs
# that probe in a CHILD session and requires the module guard to fail it.
_PROBE_ENV = "M178_PROBE_LEAK"
_PROBE_NODE = "probe_leak"


@pytest.mark.skipif(
    os.environ.get(_PROBE_ENV) != "1",
    reason="leak probe — selected only by test_the_module_guard_fails_a_leaking_module",
)
def test_probe_leak_commits_a_tenant_that_outlives_this_module() -> None:
    """The leak, on purpose: a real COMMIT, no cleanup. Never selected normally."""
    engine = create_engine(_PROBE_URL)
    try:
        with Session(engine) as db:
            db.add(
                User(
                    email=f"m178-probe-leak-{os.getpid()}@example.com",
                    hashed_password=hash_password("probe-password"),
                )
            )
            db.commit()
    finally:
        engine.dispose()


def _run_child_session(*, leak: bool) -> subprocess.CompletedProcess[str]:
    """One child pytest session over this file, on its own database."""
    env = {
        **os.environ,
        "DATABASE_URL": _PROBE_URL,
        "TEST_DB_ID": f"p{os.getpid()}{'leak' if leak else 'clean'}",
    }
    env.pop(_PROBE_ENV, None)
    # The child must not inherit this worker's xdist/coverage identity: -n0 below
    # makes it a plain serial session (its pinned TEST_DB_ID would collide across
    # parallel workers), --no-cov plus the COV_CORE_* pop keep it from appending
    # into the parent's per-worker coverage data file, and dropping
    # PYTEST_XDIST_WORKER keeps conftest from suffixing the pinned id.
    env.pop("PYTEST_XDIST_WORKER", None)
    for _cov_key in [k for k in env if k.startswith("COV_CORE_")]:
        env.pop(_cov_key, None)
    if leak:
        env[_PROBE_ENV] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(Path(__file__).resolve().relative_to(BACKEND_DIR)),
            "-q",
            "-n0",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "-k",
            f"{_PROBE_NODE} or test_the_registry_is_derived",
        ],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_the_module_guard_fails_a_leaking_module() -> None:
    """R1 end to end: the polluter is named, in its own module, every run."""
    result = _run_child_session(leak=True)
    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        "a session that commits a tenant and never cleans it up PASSED — the "
        f"module guard is not armed:\n{output}"
    )
    assert "left committed tenant state in the suite database" in output, (
        f"the child failed for some other reason:\n{output}"
    )
    assert "users:" in output, f"the guard did not name the table that grew:\n{output}"


def test_a_module_that_cleans_up_is_not_flagged() -> None:
    """The negative control: the same child, probe not selected, is green.

    Without this half, "the guard fails a leak" is indistinguishable from "the
    guard fails everything".
    """
    result = _run_child_session(leak=False)
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"a clean session was flagged:\n{output}"
    assert "left committed tenant state" not in output

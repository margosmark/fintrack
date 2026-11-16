"""Isolation behaviour of row-level security (docs/55, modelled on Lumina's
tests/test_rls_isolation.py @ 7d6e740c370cae33f48e9ac4d9bd72617a4bb664).

Every test here runs as the UNPRIVILEGED app role on the suite database,
inside one transaction the fixture rolls back. The central claim the card
exists for: **a query that omits the tenant filter returns nothing rather
than another tenant's rows** — the database is the second lock, and it does
not care whether the application remembered its WHERE clause.

The sync write-path test at the bottom is the deliberate exception: it arms
`APP_DATABASE_URL` and drives the production Engine-bound request path with
real commits, so it wipes its own tenant in a `finally` (the suite's
footprint guard is what would otherwise fail the run on it).
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from rls_support import app_role_url, seed_link, seed_tenant, stamp
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from app.core.rls import bind_request_tenant, clear_request_tenant, verify_request_role


@pytest.fixture(autouse=True)
def _clean_tenant_context() -> Iterator[None]:
    """Every test starts and ends with no request tenant bound to this thread"""
    clear_request_tenant()
    yield
    clear_request_tenant()


def _is_rls_violation(exc: DBAPIError) -> bool:
    return "row-level security" in str(exc.orig)


# --- the central claim -----------------------------------------------------------------


def test_unfiltered_query_returns_only_the_stamped_tenant(app_conn) -> None:
    """A forgotten tenant filter reads ONE tenant, never two (docs/55 §Why)"""
    alice = seed_tenant(app_conn, "rls-alice@example.com")
    bob = seed_tenant(app_conn, "rls-bob@example.com")
    assert alice.id is not None and bob.id is not None and alice.id != bob.id

    stamp(app_conn, alice.id)
    seen = set(app_conn.execute(text("SELECT user_id FROM transactions")).scalars())
    assert seen == {alice.id}, "a filterless read leaked rows across tenants"

    stamp(app_conn, bob.id)
    seen = set(app_conn.execute(text("SELECT user_id FROM transactions")).scalars())
    assert seen == {bob.id}


def test_unstamped_connection_sees_nothing(app_conn) -> None:
    """No identity ⇒ no rows: fail closed, never fail open"""
    seed_tenant(app_conn, "rls-ghost@example.com")
    stamp(app_conn, None)
    assert app_conn.execute(text("SELECT count(*) FROM transactions")).scalar_one() == 0
    assert app_conn.execute(text("SELECT count(*) FROM accounts")).scalar_one() == 0
    assert app_conn.execute(text("SELECT count(*) FROM categories")).scalar_one() == 0


def test_inserting_another_tenants_row_is_refused(app_conn) -> None:
    """WITH CHECK refuses a cross-tenant write (the insert never lands)"""
    alice = seed_tenant(app_conn, "rls-ins-a@example.com")
    bob = seed_tenant(app_conn, "rls-ins-b@example.com")
    assert alice.id is not None and bob.id is not None
    account_id = app_conn.execute(
        text("SELECT id FROM accounts WHERE user_id = :uid"), {"uid": bob.id}
    ).scalar_one()
    stamp(app_conn, alice.id)
    with pytest.raises(DBAPIError) as excinfo:
        app_conn.execute(
            text(
                "INSERT INTO transactions"
                " (user_id, account_id, date, raw_description, amount_native)"
                " VALUES (:uid, :acct, '2026-10-02', 'forged', -1.00)"
            ),
            {"uid": bob.id, "acct": account_id},
        )
    assert _is_rls_violation(excinfo.value)


def test_update_and_delete_of_another_tenants_rows_touch_nothing(app_conn) -> None:
    """The other tenant's rows are invisible: 0 rows updated, 0 deleted"""
    alice = seed_tenant(app_conn, "rls-upd-a@example.com")
    bob = seed_tenant(app_conn, "rls-upd-b@example.com")
    assert alice.id is not None and bob.id is not None
    stamp(app_conn, alice.id)
    updated = app_conn.execute(
        text("UPDATE transactions SET amount_native = 0 WHERE user_id = :uid"), {"uid": bob.id}
    ).rowcount
    deleted = app_conn.execute(
        text("DELETE FROM transactions WHERE user_id = :uid"), {"uid": bob.id}
    ).rowcount
    assert (updated, deleted) == (0, 0)
    stamp(app_conn, bob.id)
    remaining = app_conn.execute(
        text("SELECT amount_native FROM transactions WHERE user_id = :uid"), {"uid": bob.id}
    ).scalar_one()
    assert remaining == Decimal("-12.34")


# --- household sharing (docs/43): the one sanctioned cross-tenant surface -------------


def test_partner_seat_reads_the_owner_but_cannot_write(app_conn) -> None:
    """An active sharing seat widens READS to the owner — and only reads"""
    owner = seed_tenant(app_conn, "rls-share-owner@example.com")
    partner = seed_tenant(app_conn, "rls-share-partner@example.com")
    assert owner.id is not None and partner.id is not None
    seed_link(app_conn, owner, "rls-share-partner@example.com", partner=partner)

    stamp(app_conn, partner.id)
    seen = set(app_conn.execute(text("SELECT user_id FROM transactions")).scalars())
    # The partner's OWN rows plus the owner's: the seat widens reads, it does
    # not replace the caller's tenant.
    assert seen == {owner.id, partner.id}, "the seated partner must read the owner's rows"

    with pytest.raises(DBAPIError) as excinfo:
        app_conn.execute(
            text("UPDATE transactions SET amount_native = 0 WHERE user_id = :uid"),
            {"uid": owner.id},
        )
    assert _is_rls_violation(excinfo.value), "the partner seat must be read-only"


def test_seated_partner_cannot_repoint_a_link_at_a_third_party(app_conn) -> None:
    """Consent is unforgeable: the identity helper pins owner/email/token"""
    owner = seed_tenant(app_conn, "rls-forge-owner@example.com")
    partner = seed_tenant(app_conn, "rls-forge-partner@example.com")
    victim = seed_tenant(app_conn, "rls-forge-victim@example.com")
    assert owner.id is not None and partner.id is not None and victim.id is not None
    link_id = seed_link(app_conn, owner, "rls-forge-partner@example.com", partner=partner)

    stamp(app_conn, partner.id)
    # Each expected violation runs inside a SAVEPOINT that is rolled back
    # explicitly: a WITH CHECK violation aborts the transaction in PostgreSQL,
    # and pytest.raises swallows the error — so SQLAlchemy would try to RELEASE
    # a poisoned savepoint. The second check must still see a live transaction.
    # Re-point the owner: refused (the identity helper pins the stored owner).
    savepoint = app_conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as excinfo:
            app_conn.execute(
                text("UPDATE sharing_links SET user_id = :victim WHERE id = :lid"),
                {"victim": victim.id, "lid": link_id},
            )
        assert _is_rls_violation(excinfo.value)
    finally:
        savepoint.rollback()
    # Re-point the seat at a third party: refused (a partner cannot hand
    # someone else access to the owner's data).
    savepoint = app_conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as excinfo:
            app_conn.execute(
                text("UPDATE sharing_links SET partner_user_id = :victim WHERE id = :lid"),
                {"victim": victim.id, "lid": link_id},
            )
        assert _is_rls_violation(excinfo.value)
    finally:
        savepoint.rollback()
    # The stored link is untouched.
    stamp(app_conn, owner.id)
    row = app_conn.execute(
        text("SELECT user_id, partner_user_id FROM sharing_links WHERE id = :lid"), {"lid": link_id}
    ).one()
    assert (row[0], row[1]) == (owner.id, partner.id)


def test_pending_invite_is_visible_to_its_addressee_only(app_conn) -> None:
    """The accept flow's token lookup: the addressee sees the pending row"""
    owner = seed_tenant(app_conn, "rls-inv-owner@example.com")
    invitee = seed_tenant(app_conn, "rls-invitee@example.com")
    stranger = seed_tenant(app_conn, "rls-inv-stranger@example.com")
    assert owner.id is not None and invitee.id is not None and stranger.id is not None
    link_id = seed_link(app_conn, owner, "rls-invitee@example.com", status="pending")

    stamp(app_conn, invitee.id)
    seen = app_conn.execute(text("SELECT id FROM sharing_links")).scalars().all()
    assert list(seen) == [link_id], "the addressee must see the invite addressed to them"

    stamp(app_conn, stranger.id)
    assert app_conn.execute(text("SELECT count(*) FROM sharing_links")).scalar_one() == 0

    # The TTL materialization arm: the addressee may mark their own expired
    # invite `expired` (the accept flow does), and it grants no data access.
    app_conn.execute(
        text("UPDATE sharing_links SET status = 'expired' WHERE id = :lid"), {"lid": link_id}
    )
    assert (
        app_conn.execute(
            text("SELECT count(*) FROM transactions WHERE user_id = :uid"), {"uid": owner.id}
        ).scalar_one()
        == 0
    ), "an expired, unseated invite must grant no data access"


def test_sharing_audit_attributes_partner_receipts_to_the_owner(app_conn) -> None:
    """The accept receipt is the owner's row, written by the partner (D7)"""
    owner = seed_tenant(app_conn, "rls-audit-owner@example.com")
    partner = seed_tenant(app_conn, "rls-audit-partner@example.com")
    unrelated = seed_tenant(app_conn, "rls-audit-other@example.com")
    assert owner.id is not None and partner.id is not None and unrelated.id is not None
    seed_link(app_conn, owner, "rls-audit-partner@example.com", partner=partner)

    receipt = (
        "INSERT INTO sharing_audit"
        " (user_id, actor_user_id, event, args_hash, response_code, duration_ms)"
        " VALUES (:owner, :actor, 'invite_accept', 'hash', 200, 1)"
    )
    stamp(app_conn, partner.id)
    app_conn.execute(
        text(receipt),
        {"owner": owner.id, "actor": partner.id},
    )
    stamp(app_conn, unrelated.id)
    with pytest.raises(DBAPIError) as excinfo:
        app_conn.execute(
            text(receipt),
            {"owner": owner.id, "actor": unrelated.id},
        )
    assert _is_rls_violation(excinfo.value)


# --- join tables, reference tables, auth tables ----------------------------------------


def test_join_table_rows_require_a_visible_parent(app_conn) -> None:
    """transaction_tags is scoped through its transaction, not a user_id column"""
    alice = seed_tenant(app_conn, "rls-tag-a@example.com")
    bob = seed_tenant(app_conn, "rls-tag-b@example.com")
    assert alice.id is not None and bob.id is not None
    ids = {}
    for user in (alice, bob):
        stamp(app_conn, user.id)
        ids[user.id] = app_conn.execute(
            text(
                "SELECT t.id, g.id FROM transactions t, tags g"
                " WHERE t.user_id = :uid AND g.user_id = :uid"
            ),
            {"uid": user.id},
        ).one()

    stamp(app_conn, alice.id)
    own_txn, own_tag = ids[alice.id]
    app_conn.execute(
        text("INSERT INTO transaction_tags (transaction_id, tag_id) VALUES (:t, :g)"),
        {"t": own_txn, "g": own_tag},
    )
    other_txn, _ = ids[bob.id]
    with pytest.raises(DBAPIError) as excinfo:
        app_conn.execute(
            text("INSERT INTO transaction_tags (transaction_id, tag_id) VALUES (:t, :g)"),
            {"t": other_txn, "g": own_tag},
        )
    assert _is_rls_violation(excinfo.value)


def test_reference_tables_are_readable_without_an_identity(app_conn) -> None:
    """The shared caches every tenant reads (docs/02) stay global-read"""
    stamp(app_conn, None)
    for table in ("fx_rates", "benchmark_prices", "provider_fund_registry"):
        app_conn.execute(
            text(f"SELECT count(*) FROM public.{table}")  # noqa: S608 -- fixed tuple above
        ).scalar_one()


def test_auth_tables_are_readable_without_an_identity(app_conn) -> None:
    """Login and token flows look rows up before any identity exists (docs/55)"""
    stamp(app_conn, None)
    for table in (
        "users",
        "refresh_tokens",
        "mcp_tokens",
        "totp_factors",
        "webauthn_credentials",
        "recovery_codes",
        "oidc_links",
    ):
        app_conn.execute(
            text(f"SELECT count(*) FROM public.{table}")  # noqa: S608 -- fixed tuple above
        ).scalar_one()


def test_account_deletion_keeps_working_under_the_app_role(app_conn) -> None:
    """The docs/20 deletion path (`delete_user_rows`) works as the app role

    Account deletion is an app-ordered child->parent wipe, not an FK cascade
    (the tenant FKs are plain, by design — docs/20). Every step of it runs on
    the request session under the deleter's own stamp: the junction deletes
    read the user's own transactions/rules through their policies, each
    per-table delete passes USING for the deleter's own rows, and the final
    `users` delete carries no policy. If any step stops working under RLS,
    the first statement the database refuses is where this test fails.
    """
    from rls_support import orm

    from app.api.users import delete_user_rows
    from app.models.users import User as UserModel

    user = seed_tenant(app_conn, "rls-delete@example.com")
    assert user.id is not None
    stamp(app_conn, user.id)
    session = orm(app_conn)
    db_user = session.get(UserModel, user.id)
    assert db_user is not None
    delete_user_rows(session, db_user)
    session.delete(db_user)
    session.flush()

    def _count(clause: str) -> int:
        return app_conn.execute(
            text(f"SELECT count(*) FROM {clause}"),  # noqa: S608 -- clause is a literal here
            {"uid": user.id},
        ).scalar_one()

    assert _count("users WHERE id = :uid") == 0
    for table in ("transactions", "accounts", "categories", "tags"):
        assert _count(f"{table} WHERE user_id = :uid") == 0, (
            f"{table} rows survived delete_user_rows"
        )


# --- the request-context machinery -----------------------------------------------------


def test_begin_stamp_is_transaction_local_and_dies_with_the_transaction(
    app_role_available: None,
) -> None:
    """A pooled connection can never carry one request's tenant to the next

    Single-connection pool: the second session reuses the SAME physical
    connection the first one released, which is exactly the leak shape the
    transaction-local stamp exists to prevent.
    """
    engine = create_engine(app_role_url(), pool_size=1, max_overflow=0, pool_pre_ping=True)
    from app.core.rls import attach_request_stamping

    attach_request_stamping(engine)
    try:
        bind_request_tenant(424242)
        with Session(engine) as session:
            stamped = session.execute(
                text("SELECT current_setting('app.tenant_id', true)")
            ).scalar()
        assert stamped == "424242", "the begin listener must stamp the bound tenant"

        clear_request_tenant()
        with Session(engine) as session:
            residue = session.execute(
                text("SELECT current_setting('app.tenant_id', true)")
            ).scalar()
        assert not residue, "the released connection carried the previous request's tenant"
    finally:
        engine.dispose()


def test_verify_request_role_refuses_a_connection_that_owns_the_tables() -> None:
    """The startup guard rejects the schema owner as the request role"""
    from app.api.deps import get_engine

    with pytest.raises(RuntimeError, match="Refusing to start"):
        verify_request_role(get_engine())


# --- end to end through the API, on the app role ---------------------------------------


@pytest.fixture()
def app_role_client(app_engine: Engine):
    """A TestClient whose get_db is an app-role session (production shape)"""
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.main import app

    # Explicit begin/rollback (see conftest app_conn): the handler's
    # db.commit() must not become a real commit of this transaction.
    conn = app_engine.connect()
    transaction = conn.begin()
    try:
        with Session(bind=conn, join_transaction_mode="create_savepoint") as session:

            def override_get_db():
                yield session

            app.dependency_overrides[get_db] = override_get_db
            try:
                # raise_server_exceptions=False: the no-stamp test ASSERTS the
                # 500 envelope the database violation produces.
                with TestClient(app, raise_server_exceptions=False) as client:
                    yield client, session
            finally:
                app.dependency_overrides.clear()
    finally:
        transaction.rollback()
        conn.close()


def test_register_seeds_its_tenant_through_the_app_role(app_role_client) -> None:
    """POST /auth/register works under RLS because the handler stamps its new tenant"""
    client, session = app_role_client
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "rls-e2e-register@example.com", "password": "Str0ngPass!123"},
    )
    assert response.status_code == 200, response.text
    uid = session.execute(
        text("SELECT id FROM users WHERE email = :email"),
        {"email": "rls-e2e-register@example.com"},
    ).scalar_one()
    categories = session.execute(
        text("SELECT count(*) FROM categories WHERE user_id = :uid"), {"uid": uid}
    ).scalar_one()
    assert categories == 10, "the default categories must land in the new tenant"


def test_register_without_the_tenant_stamp_is_refused_by_the_database(
    app_role_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a forgotten stamp: the second lock refuses the write (500, not a leak)"""
    client, _session = app_role_client
    import app.api.auth as auth_module

    monkeypatch.setattr(auth_module, "set_tenant_now", lambda _session, _user_id: None)
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "rls-e2e-nostamp@example.com", "password": "Str0ngPass!123"},
    )
    assert response.status_code == 500, response.text
    assert response.json()["code"] == "internal_error"


# --- the sync write path (docs/55 §Per-request tenant context) ----------------------


@pytest.fixture()
def armed_request_engine(
    app_role_available: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Engine]:
    """`APP_DATABASE_URL` armed: the request engine IS the app role (docs/55)

    This is the production shape — `get_db` binds handlers to an Engine, not
    to the test harness's outer-transaction `Connection` — and therefore the
    `_write_session` arm that used to fall back to the owner engine.
    """
    import app.api.deps as deps
    from app.core.config import get_settings

    monkeypatch.setenv("APP_DATABASE_URL", app_role_url())
    get_settings.cache_clear()
    previous_engine, previous_url = deps._request_engine, deps._request_engine_url
    deps._request_engine = None
    deps._request_engine_url = None
    built: Engine | None = None
    try:
        built = deps.get_request_engine()
        assert built is not deps.get_engine(), "APP_DATABASE_URL did not arm a second engine"
        yield built
    finally:
        deps._request_engine = previous_engine
        deps._request_engine_url = previous_url
        if built is not None and built is not previous_engine:
            built.dispose()
        monkeypatch.delenv("APP_DATABASE_URL", raising=False)
        get_settings.cache_clear()


def _wipe_committed_tenant(email: str) -> None:
    """Take away every row this test committed for `email` (docs/20 wipe)

    The suite rolls tests back through the `session` fixture; a test that
    needs real commits (this one drives the production request path) opens
    engines of its own, and the footprint guard fails the RUN on whatever it
    leaves behind — so the wipe runs unconditionally, in a `finally`.
    """
    from app.api.deps import get_engine
    from app.api.users import delete_user_rows
    from app.models.users import User as UserModel

    with Session(get_engine(), expire_on_commit=False) as cleanup:
        row = cleanup.exec(select(UserModel).where(UserModel.email == email)).first()
        if row is None:
            return
        delete_user_rows(cleanup, row)
        cleanup.delete(row)
        cleanup.commit()


def test_sync_pull_write_session_is_bound_to_the_request_engine(
    armed_request_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/55 F1: the sync write path follows the REQUEST engine, not the owner

    With `APP_DATABASE_URL` armed, `POST /sync/agreements/{id}/pull` must open
    its write session on the very engine `get_db` handed the handler — the
    `fintrack_app` role the policies constrain — and connect as that role.
    The old `SQLModelSession(get_engine())` fallback wrote request-path
    commits through the schema owner, which RLS never touches.
    """
    from fastapi.testclient import TestClient

    import app.api.deps as deps
    from app.main import app
    from app.services.sync import service

    request_engine = armed_request_engine
    assert request_engine.url.username == "fintrack_app"

    # (write-session bind, request-session bind, DB-side role of the writer)
    observed: list[tuple[object, object, str]] = []
    real_write_session = service._write_session

    def _spy(db: Session) -> Session:
        writer = real_write_session(db)
        # The writer's own connection, opened before service touches it — and
        # service commits through this very session, so this IS the role the
        # sync writes run as.
        role = writer.execute(text("SELECT current_user")).scalar_one()
        observed.append((writer.get_bind(), db.get_bind(), role))
        return writer

    monkeypatch.setattr(service, "_write_session", _spy)

    email = f"rls-sync-write-{uuid4().hex[:12]}@example.com"
    password = "correct-horse-battery"
    with TestClient(app) as client:
        try:
            registered = client.post(
                "/api/v1/auth/register", json={"email": email, "password": password}
            )
            assert registered.status_code == 200, registered.text
            login = client.post("/api/v1/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            institution = client.post(
                "/api/v1/institutions", json={"name": "RLS write bank"}, headers=headers
            )
            assert institution.status_code == 201, institution.text
            account = client.post(
                "/api/v1/accounts",
                json={
                    "name": "RLS chequing",
                    "type": "chequing",
                    "currency": "CAD",
                    "institution_id": institution.json()["id"],
                },
                headers=headers,
            )
            assert account.status_code == 201, account.text
            agreement = client.post(
                "/api/v1/sync/agreements",
                json={"provider": "stub", "account_ids": [account.json()["id"]]},
                headers=headers,
            )
            assert agreement.status_code == 201, agreement.text
            observed.clear()  # the claim is about PULL's write session, not create's
            pull = client.post(
                f"/api/v1/sync/agreements/{agreement.json()['id']}/pull", headers=headers
            )
            assert pull.status_code == 200, pull.text
        finally:
            _wipe_committed_tenant(email)

    assert observed, "POST /sync/agreements/{id}/pull never opened a sync write session"
    write_bind, request_bind, role = observed[-1]
    assert request_bind is request_engine, "the request session is not on the request engine"
    assert write_bind is request_engine, "the sync write session left the request engine"
    assert write_bind is not deps.get_engine(), (
        "the sync write path fell back to the maintenance engine — request-path"
        " commits would bypass row-level security (docs/55 §Roles)"
    )
    assert role == "fintrack_app", f"sync writes ran as {role!r}, not as the constrained app role"

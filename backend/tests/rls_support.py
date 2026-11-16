"""Shared helpers for the row-level-security test modules (docs/55).

Plain functions only: the fixtures live in tests/conftest.py (pytest 9 does
not register fixture objects imported from a helper module). The app role is
provisioned per CLUSTER (backend/scripts/provision_rls_role.py), not by the
suite, so the conftest fixtures SKIP with the exact command when the role is
absent — loudly, never silently — while the pure-SQL coverage assertions in
test_rls_coverage.py still run everywhere.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.pool import NullPool
from sqlmodel import Session

from app.api.deps import get_engine
from app.core.rls import attach_request_stamping
from app.db.rls import APP_ROLE
from app.models.accounts import Account, AccountType
from app.models.categories import Category
from app.models.tags import Tag
from app.models.transactions import Transaction
from app.models.users import User


def app_role_url() -> str:
    """The suite database URL rewritten to the app role

    `render_as_string(hide_password=False)`, NOT `str(url)`: SQLAlchemy 2.x
    masks the password in `str()`, and a `***` password fails auth in a way
    that reads like a provisioning bug.
    """
    base = make_url(os.environ["DATABASE_URL"])
    password = os.environ.get("APP_ROLE_PASSWORD") or base.password or ""
    return base.set(username=APP_ROLE, password=password).render_as_string(hide_password=False)


def app_role_missing_reason() -> str | None:
    """Why the app role cannot be exercised here, or None when it can"""
    with get_engine().connect() as conn:
        exists = conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"), {"role": APP_ROLE}
        ).scalar_one()
    if exists:
        return None
    return (
        f"app role {APP_ROLE!r} is not provisioned on this cluster — run"
        " `cd backend && uv run python scripts/provision_rls_role.py` (docs/55)."
        " The policies are installed (test_rls_coverage asserts that without"
        " the role); only the enforcement behaviour needs the role."
    )


def create_engine_for_tests(url: str, **kwargs) -> Engine:
    """An app-role engine with the production begin-stamp listener attached"""
    engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True, **kwargs)
    attach_request_stamping(engine)
    return engine


def stamp(conn: Connection, tenant_id: int | None) -> None:
    """Set the request tenant the way production does (transaction-local)"""
    conn.execute(
        text("SELECT set_config('app.tenant_id', :tenant, true)"),
        {"tenant": "" if tenant_id is None else str(tenant_id)},
    )


def orm(conn: Connection) -> Session:
    """A session joined to the connection's outer transaction"""
    return Session(bind=conn, join_transaction_mode="create_savepoint")


def seed_tenant(conn: Connection, email: str) -> User:
    """A minimal tenant: user + category + account + transaction + tag

    Stamps the new tenant between the `users` insert (no RLS) and the
    tenant-row writes, mirroring the register path.
    """
    session = orm(conn)
    user = User(email=email, hashed_password="x" * 60)
    session.add(user)
    session.flush()
    assert user.id is not None
    stamp(conn, user.id)
    session.add(Category(user_id=user.id, name="Groceries", is_system=True))
    account = Account(user_id=user.id, name="Chequing", type=AccountType.chequing, currency="CAD")
    session.add(account)
    session.flush()
    assert account.id is not None
    session.add(
        Transaction(
            user_id=user.id,
            account_id=account.id,
            date=date(2026, 10, 1),
            raw_description="seed row",
            normalized_payee="seed row",  # NOT NULL (docs/03 payee normalization)
            currency="CAD",  # NOT NULL
            amount_native=Decimal("-12.34"),
            dedup_hash="rls-seed-dedup",  # NOT NULL; unique per account
        )
    )
    session.add(Tag(user_id=user.id, name="seed-tag"))
    session.flush()
    return user


def seed_link(
    conn: Connection,
    owner: User,
    invite_email: str,
    *,
    partner: User | None = None,
    status: str = "active",
) -> int:
    """A sharing link from `owner` to `invite_email` (docs/43), stamped as owner"""
    stamp(conn, owner.id)
    session = orm(conn)
    link = _link_row(owner, invite_email, status)
    session.add(link)
    session.flush()
    assert link.id is not None
    if partner is not None:
        link.partner_user_id = partner.id
        session.add(link)
        session.flush()
    return link.id


def _link_row(owner: User, invite_email: str, status: str):
    from app.models.sharing import SharingLink, SharingLinkStatus

    return SharingLink(
        user_id=owner.id,  # type: ignore[arg-type]
        email=invite_email,
        # deterministic stand-in for hash_invite_token(raw)
        token_hash="a" * 64,
        status=SharingLinkStatus(status),
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )

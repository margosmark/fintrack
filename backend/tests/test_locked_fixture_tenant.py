"""M136 §0-a — the locked-fixture tenant's OWN seeded run (docs/33).

This is the fixture the whole wave's gates stand on: a tenant that is locked
by construction (`encryption_required=TRUE`, cipher rows, NO vault envelope),
so a QA browser session — or a test — can exercise every locked path without
an enrollment dance.

The contract it pins, in the same order the card states it:

  1. the tenant EXISTS with `encryption_required=TRUE` — the G1-G3 write
     guards are live against it, so "the fixture's own seeded run" is a real
     run of the enforcement, not a mock of it;
  2. it has cipher rows AND no envelope — `useVaultEnrollment()` answers
     "enrolled" (the write guard alone proves a vault story) while every
     browser starts LOCKED, which is the property that makes the fixture
     usable without per-device setup;
  3. every write the pre-emptive frontend gates defend is refused server-side
     with the SAME `encryption_required` envelope — the defense-in-depth
     half of docs/33 rule 7d: the gate exists so the user is never SHOWN a
     doomed control, and the 422 exists so a future regression cannot slip a
     plaintext write into an encrypted tenant.

This module opts back into `seed_demo_account`, so — like `tests/test_demo_seed.py`
and `tests/test_export_zip_demo.py` — it runs against its own hermetic
`<suite-db>_demo` database rather than the suite database the other modules
roll back against. Committed rows are the point of a seed, and a seed here
rides the app's own lifespan engine, which really commits (t_17d025b2).
"""

import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from conftest import drop_database_force
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlmodel import Session, select

from alembic import command
from app.core.config import get_settings
from app.models.accounts import Account
from app.models.transactions import Transaction
from app.models.users import User
from app.models.vault import VaultEnvelope
from app.services.locked_fixture import FIXTURE_EMAIL, ensure_locked_fixture_tenant

_BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fixture_seed_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest disables lifespan seeding globally (`SEED_DEMO_ACCOUNT=false`);
    this suite opts back in the same way `test_demo_seed.py` does, because the
    fixture tenant rides the SAME kill switch — and proving the switch is
    honored is part of the contract, not an obstacle (docs/33 §locked-fixture
    tenant: never seeded in a production-shaped environment)."""
    monkeypatch.setattr(get_settings(), "seed_demo_account", True)


@pytest.fixture(scope="module", autouse=True)
def _fixture_database() -> Iterator[None]:
    """Hermetic `<suite-db>_demo` DB, same pattern as `test_demo_seed.py`.

    This module opts back into `seed_demo_account`, so a TestClient's LIFESPAN
    seeds the whole demo tenant through `seed_demo_from_engine()` — an engine of
    its OWN, which COMMITS. Against the suite database that committed tenant
    survived the session and broke every later module that seeds or looks up
    `demo@fintracker.local` / a category named `groceries` (t_17d025b2: 20
    failures in five modules, none of them this one). Committed rows belong in
    this module's own database, which the suite rolls nothing back against.
    """
    base = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://fintrack:fintrack@localhost:5432/fintrack_test",
    )
    suite_db = base.rsplit("/", 1)[-1]
    demo_db = f"{suite_db}_demo"
    demo_url = base.rsplit("/", 1)[0] + f"/{demo_db}"
    admin = create_engine(base.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        drop_database_force(conn, demo_db)
        conn.execute(text(f'CREATE DATABASE "{demo_db}"'))
    admin.dispose()

    import app.api.deps as deps

    # From the swap onward everything runs under try/finally: a generator
    # fixture that raises BEFORE its `yield` never executes the code after it,
    # so a skipped restore would leave the REST of the session pointed at this
    # database (t_435c2d14).
    os.environ["DATABASE_URL"] = demo_url
    try:
        get_settings().seed_demo_account = True
        get_settings.cache_clear()
        deps._engine = None
        cfg = Config(str(_BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
        cfg.set_main_option("sqlalchemy.url", demo_url)
        command.upgrade(cfg, "head")
        yield
    finally:
        os.environ["DATABASE_URL"] = base
        get_settings.cache_clear()
        deps._engine = None


def _login(client: TestClient, email: str, password: str) -> dict:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_fixture_seeds_a_locked_enrolled_tenant(client: TestClient, session: Session) -> None:
    """The seed produces the tenant's two halves: enrolled (write guard on)
    and locked (no envelope anywhere)."""
    user = ensure_locked_fixture_tenant(session)
    assert user is not None
    assert user.encryption_required is True

    # Cipher rows exist — the surfaces have real ciphertext to withhold.
    rows = session.exec(select(Transaction).where(Transaction.user_id == user.id)).all()
    assert len(rows) == 8
    ciphered = [r for r in rows if r.payee_cipher is not None]
    assert len(ciphered) == 7
    # The one plaintext row is deliberate (docs/33 A16 mixed-mode): a locked
    # surface has something the lock does NOT withhold, so the honest partial
    # state ("Showing 1 of N") has a real row to show.
    plaintext = [r for r in rows if r.payee_cipher is None]
    assert len(plaintext) == 1
    assert all(r.amount_cipher is not None for r in ciphered)

    # NO envelope: this is what makes the fixture load locked in EVERY browser.
    envelopes = session.exec(select(VaultEnvelope).where(VaultEnvelope.user_id == user.id)).all()
    assert envelopes == []


def test_fixture_is_idempotent(client: TestClient, session: Session) -> None:
    """A second seed is a no-op — the same re-seed guard the demo has."""
    first = ensure_locked_fixture_tenant(session)
    assert first is not None
    rows_after_first = session.exec(
        select(Transaction).where(Transaction.user_id == first.id)
    ).all()
    second = ensure_locked_fixture_tenant(session)
    assert second is not None
    assert second.id == first.id
    rows_after_second = session.exec(
        select(Transaction).where(Transaction.user_id == second.id)
    ).all()
    assert len(rows_after_second) == len(rows_after_first) == 8


def test_fixture_login_and_me_reflect_the_write_guard(client: TestClient, session: Session) -> None:
    """The fixture is a real, login-able tenant whose `/users/me` tells the
    frontend the write guard is on — which is what arms `useEncryptionRequired()`."""
    ensure_locked_fixture_tenant(session)
    from app.services.locked_fixture import FIXTURE_PASSWORD

    auth = _login(client, FIXTURE_EMAIL, FIXTURE_PASSWORD)
    me = client.get("/api/v1/users/me", headers=_headers(auth["access_token"]))
    assert me.status_code == 200
    assert me.json()["encryption_required"] is True


def test_g1_split_save_is_refused_with_the_documented_envelope(
    client: TestClient, session: Session
) -> None:
    """G1's server half: a split part without `amount_cipher` is refused with
    `encryption_required` (transactions.py step 7). The frontend gate exists so
    the user never SEES this control while locked; the 422 is the
    defense-in-depth that keeps the contract true if the gate ever regresses.
    """
    ensure_locked_fixture_tenant(session)
    from app.services.locked_fixture import FIXTURE_PASSWORD

    auth = _login(client, FIXTURE_EMAIL, FIXTURE_PASSWORD)
    headers = _headers(auth["access_token"])
    account = session.exec(select(Account).where(Account.name == "Fixture Chequing")).first()
    assert account is not None

    # A parent row, written through the API WITH its ciphers (the fixture's
    # own rows were seeded directly, so use the API for a clean parent).
    parent = client.post(
        "/api/v1/transactions",
        json={
            "account_id": account.id,
            "date": "2026-09-28",
            "raw_description": "SPLIT PARENT",
            "amount_native": "-100.00",
            "payee_cipher": "v1.p",
            "amount_cipher": "v1.a",
        },
        headers=headers,
    )
    assert parent.status_code == 201, parent.text
    parent_id = parent.json()["id"]

    # A split WITHOUT per-part amount_cipher → the documented 422.
    refused = client.post(
        f"/api/v1/transactions/{parent_id}/split",
        json={
            "children": [
                {"amount_native": "-60.00"},
                {"amount_native": "-40.00"},
            ]
        },
        headers=headers,
    )
    assert refused.status_code == 422
    assert refused.json()["code"] == "encryption_required"

    # WITH per-part amount_cipher → accepted (the same route, the honest path).
    accepted = client.post(
        f"/api/v1/transactions/{parent_id}/split",
        json={
            "children": [
                {"amount_native": "-60.00", "amount_cipher": "v1.a1"},
                {"amount_native": "-40.00", "amount_cipher": "v1.a2"},
            ]
        },
        headers=headers,
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["sum_ok"] is True


def test_g3_ynab_commit_is_refused_with_the_documented_envelope(
    client: TestClient, session: Session
) -> None:
    """G3's server half: `POST /imports/ynab/commit` refuses an enrolled tenant
    outright (ynab.py) — the importer server-side produces no cipher fields.
    """
    ensure_locked_fixture_tenant(session)
    from app.services.locked_fixture import FIXTURE_PASSWORD

    auth = _login(client, FIXTURE_EMAIL, FIXTURE_PASSWORD)
    body = client.post(
        "/api/v1/imports/ynab/commit",
        headers=_headers(auth["access_token"]),
        files={
            "file": (
                "ynab_budget.json",
                b'{"data": {"budget": {"name": "x"}}}',
                "application/json",
            )
        },
    )
    assert body.status_code == 422
    assert body.json()["code"] == "encryption_required"


def test_fixture_rows_sum_and_are_tenant_scoped(client: TestClient, session: Session) -> None:
    """A sanity check that the seeded rows are coherent financial data, not
    just cipher-shaped noise: the signed amounts sum to the expected total and
    every row belongs to the fixture tenant (the tenancy invariant, docs/02)."""
    user = ensure_locked_fixture_tenant(session)
    assert user is not None
    rows = session.exec(select(Transaction).where(Transaction.user_id == user.id)).all()
    total = sum((r.amount_native for r in rows), Decimal("0"))
    # 8 rows: -84.20 -56.00 -91.05 -42.10 +3200.00 +500.00 +1.42 +0.01
    assert total == Decimal("3428.08")
    assert all(r.user_id == user.id for r in rows)


def test_kill_switch_honored(client: TestClient, session: Session) -> None:
    """With `SEED_DEMO_ACCOUNT` off — the default in this suite — the fixture
    seed refuses to run. A fixture tenant must never exist in a
    production-shaped environment (docs/33 §locked-fixture tenant)."""
    monkey_off = get_settings()
    monkey_off.seed_demo_account = False
    try:
        assert ensure_locked_fixture_tenant(session) is None
        orphans = session.exec(select(User).where(User.email == FIXTURE_EMAIL)).all()
        assert orphans == []
    finally:
        monkey_off.seed_demo_account = True

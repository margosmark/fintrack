"""Demo-seed contract tests (w8-contract pin 6).

Runs against a REAL Postgres (its own `<suite-db>_demo` database, derived from
the isolated suite DB): every pin-6
invariant — endpoint shape + idempotency, disabled flag → 404 demo_disabled,
and the seeded-dataset invariants. Seeding happens ONCE through the real
engine so rows persist across tests (the `session` fixture rolls back).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
from alembic.config import Config
from conftest import drop_database_force, latency_budget_blocker
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlmodel import Session, func, select

from alembic import command
from app.api.deps import get_engine
from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.accounts import Account, AccountType
from app.models.fx_rates import FxRate, FxSource
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.models.transactions import Transaction
from app.models.users import User
from app.services import fx as fx_service
from app.services.demo_seed import (
    DEMO_EMAIL,
    ensure_demo_account,
    seed_demo_from_engine,
    seed_fingerprint,
)


def _valet_down(*_args: object, **_kwargs: object) -> dict:
    """Stand in for an unreachable Bank of Canada (docs/07 retry exhaustion)."""
    raise fx_service.ValetUnavailable("simulated: no network in this suite")


@pytest.fixture(autouse=True)
def _demo_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest disables lifespan seeding globally; this suite opts back in."""
    monkeypatch.setattr(get_settings(), "seed_demo_account", True)


_BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def _offline_valet() -> Iterator[None]:
    """Keep BoC unreachable for the WHOLE module — seeding included.

    The demo's fx gaps are empty BY DESIGN (docs/30: `fx_rates` is never
    seeded), and that is what every `422 fx_rate_missing` pin below asserts.
    M115 taught the ACB sweep to backfill on a miss, so without this the live
    Bank of Canada would fill those gaps DURING the one-shot seed and turn the
    envelope pins into 200s — silently, and differently on every host.
    """
    patcher = mock.patch.object(fx_service, "fetch_valet_series", _valet_down)
    patcher.start()
    yield
    patcher.stop()


@pytest.fixture(scope="module", autouse=True)
def _demo_database() -> Iterator[None]:
    """Hermetic `<suite-db>_demo` DB: committed demo rows must not leak into the
    shared session database that later test modules consume."""
    base = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://fintrack:fintrack@localhost:5432/fintrack_test",
    )
    # Named from the SUITE database (conftest points DATABASE_URL at
    # `<db>_test`), never a fixed global name: two agents running this file
    # concurrently would otherwise DROP each other's database mid-run, which
    # surfaces as an AdminShutdown deep inside SQLAlchemy — a database
    # collision that reads like a code failure.
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
    # fixture that raises BEFORE its `yield` never executes the code after
    # it, so the restore used to be skipped and every later test in the
    # session kept running against `<suite>_demo` (t_435c2d14).
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


D = Decimal


def _today() -> date:
    """The clock, read at CALL time — never captured at import (t_435c2d14)."""
    return date.today()


def _from() -> date:
    """First of the month 11 months before today."""
    today = _today()
    total = today.year * 12 + today.month - 1 - 11
    return date(total // 12, total % 12 + 1, 1)


@pytest.fixture(name="demo_user")
def demo_user_fixture() -> User:

    with Session(get_engine(), expire_on_commit=False) as session:
        user = ensure_demo_account(session)
    assert user is not None
    return user


# --- pin 2: endpoint ---------------------------------------------------------


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _account(session: Session, user_id: int, type_: AccountType) -> Account:
    account = session.exec(
        select(Account).where(Account.user_id == user_id, Account.type == type_)
    ).one()
    return account


# --- pin 2: endpoint ---------------------------------------------------------


def test_demo_endpoint_login_shape_and_idempotent(client: TestClient, session: Session) -> None:
    first = client.post("/api/v1/auth/demo")
    assert first.status_code == 200
    body = first.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == DEMO_EMAIL
    # Access token in the body; the refresh token rides ONLY on the httpOnly
    # cookie (schemas/auth.py TokenResponse `exclude=True`, audit M1).
    assert body["access_token"]
    assert "refresh_token" not in body
    assert first.cookies.get("refresh_token")
    user_id = body["user"]["id"]

    txns_before = session.exec(
        select(func.count()).where(Transaction.user_id == user_id)  # type: ignore[arg-type]
    ).one()

    second = client.post("/api/v1/auth/demo")
    assert second.status_code == 200
    body2 = second.json()
    assert body2["user"]["id"] == user_id  # stable user id
    txns_after = session.exec(
        select(func.count()).where(Transaction.user_id == user_id)  # type: ignore[arg-type]
    ).one()
    assert txns_after == txns_before  # nothing duplicated


def test_demo_disabled_returns_404_demo_disabled(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "seed_demo_account", False)
    response = client.post("/api/v1/auth/demo")
    assert response.status_code == 404
    assert response.json()["code"] == "demo_disabled"


# --- H7: fx_rates is NEVER seeded --------------------------------------------


def _fx_count(session: Session) -> int:
    count = session.exec(select(func.count()).select_from(FxRate)).one()  # type: ignore[arg-type]
    return int(count)


def test_no_fx_rows_from_boot_seed_or_demo_endpoint(
    client: TestClient, session: Session, demo_user: User
) -> None:
    """H7: neither the boot-lifespan seed path nor POST /auth/demo writes fx_rates."""
    # The fresh `ensure_demo_account` behind `demo_user` ran the full dataset
    # seed — the same code path the boot lifespan uses — with fx empty after.
    assert _fx_count(session) == 0
    assert seed_demo_from_engine() is not None  # boot helper re-run, idempotent
    assert _fx_count(session) == 0
    response = client.post("/api/v1/auth/demo")
    assert response.status_code == 200
    assert _fx_count(session) == 0  # the demo endpoint seeds no fx rows either


# --- pin 4/6: dataset invariants --------------------------------------------


def test_seeded_dataset_invariants(client: TestClient, session: Session, demo_user: User) -> None:
    uid = demo_user.id

    # ≥300 transactions, ≥95% categorized.
    txn_count = session.exec(
        select(func.count()).where(Transaction.user_id == uid)  # type: ignore[arg-type]
    ).one()
    assert txn_count >= 300
    categorized = session.exec(
        select(func.count()).where(  # type: ignore[arg-type]
            Transaction.user_id == uid, Transaction.category_id.is_not(None)
        )
    ).one()
    assert categorized / txn_count >= D("0.95")

    # Positions positive for RY.TO (TFSA), VTI and NVDA (margin).

    margin = _account(session, uid, AccountType.brokerage_non_registered)
    tfsa = _account(session, uid, AccountType.tfsa)

    def position(account_id: int, symbol: str) -> Decimal:
        sec = session.exec(
            select(Security).where(Security.user_id == uid, Security.symbol == symbol)
        ).one()
        qty = session.exec(
            select(func.coalesce(func.sum(Trade.quantity), 0)).where(  # type: ignore[arg-type]
                Trade.user_id == uid,
                Trade.account_id == account_id,
                Trade.security_id == sec.id,
                Trade.action.in_([TradeAction.buy, TradeAction.sell]),
            )
        ).one()
        return D(str(qty))

    assert position(tfsa.id, "RY.TO") > 0
    assert position(margin.id, "VTI") > 0
    assert position(margin.id, "NVDA") > 0

    # Card balance negative (current-month spend not yet paid off).
    card = _account(session, uid, AccountType.credit_card)
    card_balance = session.exec(
        select(func.coalesce(func.sum(Transaction.amount_native), 0)).where(  # type: ignore[arg-type]
            Transaction.account_id == card.id
        )
    ).one()
    assert D(str(card_balance)) < 0
    net = client.get(
        f"/api/v1/reports/net-worth?from={_from()}&to={_today()}&granularity=month",
        headers=_headers(demo_user),
    )
    assert net.status_code == 200
    buckets = net.json()["buckets"]
    assert len(buckets) in range(12, 15)
    assert all(D(str(b["net_cad"])) != 0 for b in buckets)

    # Tax/report endpoints must not crash while fx gaps are empty (H7): they
    # answer the documented 422 fx_rate_missing envelope (docs/06), never a 500.
    perf = client.get(
        f"/api/v1/reports/investment-performance?from={_from()}&to={_today()}",
        headers=_headers(demo_user),
    )
    assert perf.status_code == 422
    assert perf.json()["code"] == "fx_rate_missing"
    cg = client.get(
        f"/api/v1/reports/capital-gains?year={_today().year}", headers=_headers(demo_user)
    )
    assert cg.status_code == 422
    assert cg.json()["code"] == "fx_rate_missing"


def test_tax_reports_recover_once_fx_gaps_are_filled(
    client: TestClient, session: Session, demo_user: User
) -> None:
    """Gaps stay empty until a rate exists (docs/06/07): a manual gap-fill per
    USD trade date brings the tax engine and the ACB pool cache back."""
    uid = demo_user.id
    usd_trades = session.exec(
        select(Trade)
        .join(Security, Trade.security_id == Security.id)
        .where(Trade.user_id == uid, Security.listing_currency == "USD")  # type: ignore[arg-type]
        .distinct()
    ).all()
    usd_days = sorted({trade.date for trade in usd_trades})
    assert usd_days  # the demo does trade USD-listed securities
    for day in usd_days:
        session.add(
            FxRate(base="USD", quote="CAD", date=day, rate=D("1.35"), source=FxSource.manual)
        )
    session.commit()
    headers = _headers(demo_user)

    # Investment performance: realized > 0; unrealized present for non-registered.
    perf = client.get(
        f"/api/v1/reports/investment-performance?from={_from()}&to={_today()}", headers=headers
    )
    assert perf.status_code == 200, perf.text
    items = {i["symbol"]: i for i in perf.json()["items"]}
    assert D(str(perf.json()["totals"]["realized_cad"])) > 0
    assert items["VTI"]["unrealized_cad"] is not None
    assert items["VTI"]["acb_pool_cad"] is not None
    # Registered holdings carry no pool (reference-only cost, docs/05).
    assert items["RY.TO"]["unrealized_cad"] is None

    # Capital gains: the partial VTI sell shows up in its year with a gain.
    sell_year = session.exec(
        select(Trade.date).where(  # type: ignore[arg-type]
            Trade.user_id == uid,
            Trade.action == TradeAction.sell,
            Trade.security_id
            == session.exec(
                select(Security.id).where(Security.user_id == uid, Security.symbol == "VTI")
            ).one(),
        )
    ).first()
    assert sell_year is not None
    cg = client.get(f"/api/v1/reports/capital-gains?year={sell_year.year}", headers=headers)
    assert cg.status_code == 200, cg.text
    cg_items = {i["symbol"]: i for i in cg.json()["items"]}
    assert "VTI" in cg_items
    assert D(str(cg_items["VTI"]["gain_loss_cad"])) > 0

    # The pool cache deferred at seed time repopulates via the sanctioned path.
    rebuild = client.post("/api/v1/acb/rebuild", headers=headers)
    assert rebuild.status_code == 200, rebuild.text
    assert rebuild.json()["pools"] > 0


def test_seed_is_deterministic_and_fast(client: TestClient, session: Session) -> None:
    """Warm re-seed guard is a no-op query; a cold seed must fit the <3s budget.

    The cold-path timing is exercised by test_seeded_dataset_invariants on a
    fresh DB; here we pin that a second ensure call is cheap and stable.

    Determinism (`again.id == first.id`) is a FUNCTIONAL claim and is enforced
    unconditionally; the 3s budget is the wall-clock claim, so it goes last,
    behind `latency_budget_blocker` — this path is CPU + shared-Postgres, and
    AGENTS §5 lets two full suites share these cores (docs/34, t_4980b7ed).
    """
    import time

    started = time.perf_counter()
    with Session(get_engine(), expire_on_commit=False) as s:
        first = ensure_demo_account(s)
        again = ensure_demo_account(s)
    elapsed = time.perf_counter() - started
    assert first is not None and again.id == first.id
    print(f"\nPERF ensure_demo_account x2={elapsed * 1000:.1f}ms")

    blocker = latency_budget_blocker()
    if blocker is not None:
        pytest.skip(
            "3s warm re-seed budget not measurable on this box: "
            f"{blocker}; measured {elapsed:.3f}s for two ensure_demo_account"
            " calls (determinism assertion above enforced regardless)"
        )
    assert elapsed < 3.0, f"two ensure calls took {elapsed:.3f}s (budget 3.0s)"


_EXPECTED_LIFESTYLE = {"bills": 5, "budgets": 3, "debts": 2, "goals": 2, "rules": 2}


def _lifestyle_counts(session: Session, uid: int) -> dict[str, int]:
    from app.models.bill_reminders import BillReminder
    from app.models.budgets import Budget
    from app.models.debts import Debt
    from app.models.goals import Goal
    from app.models.rules import Rule

    out = {}
    for model, key in (
        (BillReminder, "bills"),
        (Budget, "budgets"),
        (Debt, "debts"),
        (Goal, "goals"),
        (Rule, "rules"),
    ):
        out[key] = session.exec(
            select(func.count()).where(model.user_id == uid)  # type: ignore[arg-type]
        ).one()
    return out


def test_lifestyle_tables_seeded(session: Session, demo_user: User) -> None:
    """M45 (docs/30): fresh demo carries the pinned lifestyle rows."""
    counts = _lifestyle_counts(session, demo_user.id)
    assert counts == _EXPECTED_LIFESTYLE


def test_lifestyle_backfills_pre_m45_demo(demo_user: User) -> None:
    """A demo with transactions but empty lifestyle tables gets exactly them.

    NOTE: deletes/reads go through independent committed sessions — the
    shared `session` fixture rolls everything back, which would hide the
    deletes from ensure_demo_account's connection.
    """
    from app.models.bill_reminders import BillReminder
    from app.models.budgets import Budget
    from app.models.debts import Debt
    from app.models.goals import Goal
    from app.models.rules import Rule

    uid = demo_user.id
    with Session(get_engine(), expire_on_commit=False) as s:
        for model in (BillReminder, Budget, Debt, Goal, Rule):
            for row in s.exec(select(model).where(model.user_id == uid)):
                s.delete(row)
        s.commit()
        assert all(v == 0 for v in _lifestyle_counts(s, uid).values())

        again = ensure_demo_account(s)
        assert again is not None and again.id == uid

    with Session(get_engine(), expire_on_commit=False) as s:
        assert _lifestyle_counts(s, uid) == _EXPECTED_LIFESTYLE

        # Second run is a no-op.
        ensure_demo_account(s)
    with Session(get_engine(), expire_on_commit=False) as s:
        assert _lifestyle_counts(s, uid) == _EXPECTED_LIFESTYLE


def test_lifestyle_visible_through_api(client: TestClient, demo_user: User) -> None:
    """Seeded lifestyle rows round-trip through the list endpoints."""
    headers = _headers(demo_user)
    for path, key, expected in (
        ("/api/v1/bills", None, 5),
        ("/api/v1/budgets", None, 3),
        ("/api/v1/debts", None, 2),
        ("/api/v1/goals", None, 2),
        ("/api/v1/rules", "items", 2),
    ):
        r = client.get(path, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        items = body[key] if key else body
        assert len(items) == expected, path


# --- M50: POST /auth/demo/reset -----------------------------------------------

RESET = "/api/v1/auth/demo/reset"
# The shipped exemption set — four exact paths, one of them this route (docs/20).
# M81 added `/api/v1/auth/mcp-tokens` (demo mint; revoke lives in the separate
# DEMO_MUTATION_EXEMPT_PREFIXES tuple, untouched by `_guard_gap`). M157 added
# `/api/v1/projections/run` (pure request-body math, no `Session` in the
# handler). This is a deliberate LOCAL copy of the shipped set, so the three
# assertions that use it below fail loudly the next time the guard grows an
# exemption — keep it in step with `DEMO_MUTATION_EXEMPT_PATHS`.
SHIPPED_EXEMPTIONS = frozenset(
    {
        "/api/v1/acb/rebuild",
        "/api/v1/auth/demo/reset",
        "/api/v1/auth/mcp-tokens",
        "/api/v1/projections/run",
    }
)
ARTIFACT_URL = "/api/v1/backup/artifacts"
VAULT_URL = "/api/v1/vault/envelopes"
# The damage paths `_guard_gap` widens the demo write guard to. The backup
# artifact upload is here because M63's defect lives on a table the shared
# cascade never reaches (M63, docs/11 § Demo reset); the vault enrolment POST
# because M63b decided the same fate for `vault_envelopes` (docs/33).
MUTATION_PATHS = (
    "/api/v1/transactions",
    "/api/v1/budgets",
    "/api/v1/bills",
    ARTIFACT_URL,
    VAULT_URL,
)


def _counts(session: Session, uid: int) -> dict[str, int]:
    """Row counts of the three tables the M50 regression has to cover."""
    from app.models.bill_reminders import BillReminder
    from app.models.budgets import Budget

    out: dict[str, int] = {}
    for model, key in (
        (Transaction, "transactions"),
        (Budget, "budgets"),
        (BillReminder, "bills"),
    ):
        count = session.exec(select(func.count()).where(model.user_id == uid)).one()  # type: ignore[arg-type]
        out[key] = int(count)
    return out


@contextmanager
def _guard_gap() -> Iterator[None]:
    """Widen the demo write guard to the `MUTATION_PATHS`, then restore it.

    Deliberately NOT `monkeypatch`: the function-scoped `monkeypatch` instance
    is shared with this module's autouse `_demo_enabled` fixture, so
    `monkeypatch.undo()` would also roll back `seed_demo_account = True` and
    turn the reset that follows into a 404.
    """
    import app.api.deps as deps

    original = deps.DEMO_MUTATION_EXEMPT_PATHS
    deps.DEMO_MUTATION_EXEMPT_PATHS = SHIPPED_EXEMPTIONS.union(MUTATION_PATHS)
    try:
        yield
    finally:
        deps.DEMO_MUTATION_EXEMPT_PATHS = original


def test_reset_restores_the_seed_fingerprint_after_api_damage(
    client: TestClient,
    session: Session,
    demo_user: User,
) -> None:
    """M50 acceptance: damage the demo through the API, reset, fingerprint == fresh seed."""
    import app.api.deps as deps

    uid = demo_user.id
    assert uid is not None
    headers = _headers(demo_user)
    fresh = seed_fingerprint(session, uid)
    counts_before = _counts(session, uid)

    accounts = client.get("/api/v1/accounts", headers=headers).json()["items"]
    categories = client.get("/api/v1/categories", headers=headers).json()["items"]
    chequing = next(a["id"] for a in accounts if a["type"] == "chequing")
    groceries = next(c["id"] for c in categories if c["name"] == "groceries")
    housing = next(c["id"] for c in categories if c["name"] == "housing")

    writes = [
        (
            "/api/v1/transactions",
            {
                "account_id": chequing,
                "date": _today().isoformat(),
                "raw_description": "VISITOR DAMAGE",
                "amount_native": "-123.45",
            },
        ),
        # `housing` is the one seeded category with no budget (groceries/
        # dining/entertainment are taken) — otherwise this would 409.
        ("/api/v1/budgets", {"category_id": housing, "amount": "999.99"}),
        (
            "/api/v1/bills",
            {
                "name": "Visitor Damage Bill",
                "amount": "55.00",
                "category_id": groceries,
                "frequency": "monthly",
                "next_due_date": _today().isoformat(),
            },
        ),
    ]

    # (a) The shipped boundary refuses every one of these writes — the
    # exemption this test leans on below does NOT cover them.
    for path, payload in writes:
        refused = client.post(path, json=payload, headers=headers)
        assert refused.status_code == 403, (path, refused.text)
        assert refused.json()["code"] == "demo_account_restricted"

    # (b) Apply the gap the reset exists for (a future hole in the guard), then
    # cause the damage through the real API. `_guard_gap` restores the guard.
    with _guard_gap():
        for path, payload in writes:
            created = client.post(path, json=payload, headers=headers)
            assert created.status_code == 201, (path, created.text)

    damaged_counts = _counts(session, uid)
    assert damaged_counts["transactions"] == counts_before["transactions"] + 1
    assert damaged_counts["budgets"] == counts_before["budgets"] + 1
    assert damaged_counts["bills"] == counts_before["bills"] + 1
    damaged = seed_fingerprint(session, uid)
    assert damaged != fresh  # the fingerprint really does see the damage

    # (c) The SHIPPED exemption set is back in force; the reset goes through
    # its own exempt path exactly as it would in production.
    assert deps.DEMO_MUTATION_EXEMPT_PATHS == SHIPPED_EXEMPTIONS

    reset = client.post(RESET, headers=headers)
    assert reset.status_code == 204, reset.text
    assert reset.content == b""

    assert seed_fingerprint(session, uid) == fresh
    assert _counts(session, uid) == counts_before
    # Same tenant afterwards: the user row (and this session) survived the wipe.
    assert client.get("/api/v1/users/me", headers=headers).json()["email"] == DEMO_EMAIL


def test_reset_disabled_returns_404_demo_disabled(
    client: TestClient, demo_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "seed_demo_account", False)
    response = client.post(RESET, headers=_headers(demo_user))
    assert response.status_code == 404
    assert response.json()["code"] == "demo_disabled"


def test_reset_requires_authentication(client: TestClient) -> None:
    """An unauthenticated wipe would hand the public demo door a destroy button."""
    response = client.post(RESET)
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


def test_reset_is_forbidden_for_every_other_tenant(
    client: TestClient, session: Session, demo_user: User
) -> None:
    client.post(
        "/api/v1/auth/register",
        json={"email": "m50-foreign@example.com", "password": "correct-horse-1"},
    )
    registered = client.post(
        "/api/v1/auth/login",
        json={"email": "m50-foreign@example.com", "password": "correct-horse-1"},
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    uid = demo_user.id
    assert uid is not None
    fresh = seed_fingerprint(session, uid)
    response = client.post(RESET, headers=headers)

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Only the demo account can reset the demo tenant",
        "code": "forbidden",
    }
    assert seed_fingerprint(session, uid) == fresh  # nothing was wiped


def test_reset_is_rate_limited_like_demo_login(client: TestClient) -> None:
    """5/minute, same bucket size as `POST /auth/demo`: 6th call → 429.

    The caller is a foreign tenant so each allowed attempt is stopped by the
    handler (403) instead of paying for a full re-seed; the limiter sits in
    front of the handler, which is the property under test.
    """
    from app.core.ratelimit import limiter

    client.post(
        "/api/v1/auth/register",
        json={"email": "m50-rl@example.com", "password": "correct-horse-2"},
    )
    registered = client.post(
        "/api/v1/auth/login",
        json={"email": "m50-rl@example.com", "password": "correct-horse-2"},
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    limiter.enabled = True
    try:
        statuses: list[int] = []
        bodies: list[dict] = []
        for _ in range(6):
            attempt = client.post(RESET, headers=headers)
            statuses.append(attempt.status_code)
            bodies.append(attempt.json())
        assert statuses[:5] == [403, 403, 403, 403, 403], statuses
        assert statuses[5] == 429, statuses
        assert bodies[5] == {"detail": "Too many attempts", "code": "rate_limited"}
    finally:
        limiter.enabled = False
        limiter.reset()


# --- M63/M63b: demo reset vs the tables the cascade cannot reach --------------

_ARTIFACT_PAYLOAD = {
    # A real daily key (`_period_is_real`) and a well-formed
    # `v1.<b64(iv)>.<b64 ciphertext>` envelope; the content is never decoded
    # server-side, so any opaque blob of the right shape is enough.
    "ciphertext": "v1.aXY=.Y2lwaGVydGV4dA==",
}

_ENVELOPE_PAYLOAD = {
    # `device_id` matches `^[0-9a-f]{32}$` (schema) and the blob is opaque to
    # the server (docs/33 K3/K6): only SIZE and SHAPE are ever validated, so
    # any well-formed bundle is enough to make the row real.
    "device_id": "3f2a1b7c9d0e5f60718293a4b5c6d7e8",
    "label": "Chrome · Linux",
    "wrapped_blob": "eyJ2IjoxLCJzYWx0IjoiYQ",
}


def _artifact_count(session: Session, uid: int) -> int:
    from app.models.backup import BackupArtifact

    count = session.exec(select(func.count()).where(BackupArtifact.user_id == uid)).one()
    return int(count)  # type: ignore[arg-type]


def _envelope_count(session: Session, uid: int) -> int:
    from app.models.vault import VaultEnvelope

    count = session.exec(select(func.count()).where(VaultEnvelope.user_id == uid)).one()
    return int(count)  # type: ignore[arg-type]


def test_reset_clears_stale_backup_artifacts(
    client: TestClient,
    session: Session,
    demo_user: User,
) -> None:
    """M63: `backup_artifacts` is invisible to the cascade — the reset must still clear it.

    The table sits outside `_USER_OWNED_MODELS` on purpose (a restore must not
    delete the artifacts it restores from, docs/11) and its FK only cascades
    when the `users` row goes — which `POST /auth/demo/reset` deliberately does
    not do. Without the reset's own wipe (`_RESET_EXTRA_MODELS`) the rows are
    stranded across resets, and nothing in `seed_fingerprint` used to see them.
    """
    import app.api.deps as deps
    from app.services.demo_seed import _RESET_EXTRA_MODELS

    uid = demo_user.id
    assert uid is not None
    headers = _headers(demo_user)
    payload = {**_ARTIFACT_PAYLOAD, "period": _today().isoformat()}
    fresh = seed_fingerprint(session, uid)

    # (a) The shipped boundary refuses the upload like any other demo write —
    # the table is only reachable through a guard gap.
    refused = client.post(ARTIFACT_URL, json=payload, headers=headers)
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "demo_account_restricted"
    assert _artifact_count(session, uid) == 0

    # (b) Through the real API with the guard widened (the gap the reset exists
    # for); `_guard_gap` restores the shipped set on the way out.
    with _guard_gap():
        created = client.post(ARTIFACT_URL, json=payload, headers=headers)
    assert created.status_code == 201, created.text
    assert _artifact_count(session, uid) == 1
    # The fingerprint reads the table now, so the M50 fingerprint-equality
    # acceptance sees this damage class without a bespoke assertion.
    assert seed_fingerprint(session, uid) != fresh

    # (c) Shipped guard back in force; the reset runs through its own exempt path.
    assert deps.DEMO_MUTATION_EXEMPT_PATHS == SHIPPED_EXEMPTIONS
    assert "backup_artifacts" in {model.__tablename__ for model in _RESET_EXTRA_MODELS}

    reset = client.post(RESET, headers=headers)
    assert reset.status_code == 204, reset.text
    assert _artifact_count(session, uid) == 0  # no stale rows survive
    assert seed_fingerprint(session, uid) == fresh


def test_reset_clears_stale_vault_envelopes(
    client: TestClient,
    session: Session,
    demo_user: User,
) -> None:
    """M63b: `vault_envelopes` is invisible to the cascade — the reset must still clear it.

    Same defect class as M63: the table sits outside `_USER_OWNED_MODELS`
    (a *restore* must never replay device enrolment — docs/11), its FK only
    cascades when the `users` row goes, and `POST /auth/demo/reset` keeps that
    row by design. So a stray envelope used to outlive every reset *and* stay
    invisible to `seed_fingerprint`, which reads only what the reset wipes.

    The shipped boundary refuses enrolment (docs/33 keeps demo enrolment
    local-only), so this is defence-in-depth, not an observed leak: the row is
    created through the real API with the guard widened — the gap the reset
    exists for — and must be gone after it.
    """
    import app.api.deps as deps
    from app.services.demo_seed import _RESET_EXTRA_MODELS

    uid = demo_user.id
    assert uid is not None
    headers = _headers(demo_user)
    fresh = seed_fingerprint(session, uid)

    # (a) The shipped boundary refuses enrolment like any other demo write.
    refused = client.post(VAULT_URL, json=_ENVELOPE_PAYLOAD, headers=headers)
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "demo_account_restricted"
    assert _envelope_count(session, uid) == 0

    # (b) Through the real API with the guard widened; `_guard_gap` restores
    # the shipped set on the way out.
    with _guard_gap():
        created = client.post(VAULT_URL, json=_ENVELOPE_PAYLOAD, headers=headers)
    assert created.status_code == 201, created.text
    assert _envelope_count(session, uid) == 1
    # The fingerprint reads the table now, so the M50 fingerprint-equality
    # acceptance sees this damage class without a bespoke assertion.
    assert seed_fingerprint(session, uid) != fresh

    # (c) Shipped guard back in force; the reset runs through its own exempt path.
    assert deps.DEMO_MUTATION_EXEMPT_PATHS == SHIPPED_EXEMPTIONS
    assert "vault_envelopes" in {model.__tablename__ for model in _RESET_EXTRA_MODELS}

    reset = client.post(RESET, headers=headers)
    assert reset.status_code == 204, reset.text
    assert _envelope_count(session, uid) == 0  # no stale rows survive
    assert seed_fingerprint(session, uid) == fresh


def test_every_user_owned_table_is_classified_for_demo_reset() -> None:
    """M63/M63b: no silent behaviour — every `user_id` table has a documented reset fate.

    The buckets, all in `app/services/demo_seed.py`:
    `_USER_OWNED_MODELS` (wiped by the shared cascade, credential tables
    excepted), `_RESET_EXTRA_MODELS` (wiped by the reset itself, because the
    cascade deliberately skips them) and `_DELIBERATELY_SURVIVING_MODELS`
    (kept, each with a written reason). A user-owned table that reaches the
    model registry without joining one of them fails here — which is exactly
    how `backup_artifacts` went stale.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.api.users import _USER_OWNED_MODELS
    from app.services.demo_seed import (
        _CREDENTIAL_MODELS,
        _DELIBERATELY_SURVIVING_MODELS,
        _RESET_EXTRA_MODELS,
    )

    cascade = {model.__tablename__ for model in _USER_OWNED_MODELS} - {
        model.__tablename__ for model in _CREDENTIAL_MODELS
    }
    wiped = cascade | {model.__tablename__ for model in _RESET_EXTRA_MODELS}
    kept = {model.__tablename__ for model in _CREDENTIAL_MODELS} | {
        model.__tablename__ for model in _DELIBERATELY_SURVIVING_MODELS
    }

    user_owned = sorted(
        mapper.class_.__tablename__
        for mapper in sa_inspect(User).registry.mappers
        if mapper.local_table is not None and "user_id" in mapper.local_table.columns
    )
    # Introspection really sees both extra tables, or the check below is vacuous.
    assert {"backup_artifacts", "vault_envelopes"} <= set(user_owned), user_owned

    unclassified = sorted(set(user_owned) - wiped - kept)
    assert not unclassified, (
        "user-owned table(s) with no documented reset fate — classify them in "
        "app/services/demo_seed.py (_USER_OWNED_MODELS / _RESET_EXTRA_MODELS / "
        f"_DELIBERATELY_SURVIVING_MODELS): {unclassified}"
    )
    assert not wiped & kept, sorted(wiped & kept)
    # The M63/M63b decisions themselves, pinned as data: both tables the shared
    # cascade cannot reach are wiped by the reset (M63 `backup_artifacts`,
    # M63b `vault_envelopes`), and whatever a reset DOES keep carries a written
    # reason rather than a "someone should decide" note.
    assert "backup_artifacts" in wiped
    assert "vault_envelopes" in wiped
    assert all(
        isinstance(reason, str) and reason.strip()
        for reason in _DELIBERATELY_SURVIVING_MODELS.values()
    )

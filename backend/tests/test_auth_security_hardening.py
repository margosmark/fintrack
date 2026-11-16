"""Security-hardening regression tests (one focused file, three fixes):

1. GLOBAL demo mutation guard — the shared demo tenant (`demo@fintracker.local`)
   is re-seeded by `POST /auth/demo`, so any mutation an anonymous visitor can
   perform destroys the dataset every other visitor is looking at. The guard
   lives in the shared auth dependency (`app.api.deps.get_current_user`), so
   every ordinary mutation router inherits it: POST/PUT/PATCH/DELETE → 403
   `demo_account_restricted`, while demo login and every read endpoint keep
   working.
2. Health probe disclosure — `GET /api/v1/health` is unauthenticated, so a
   failing DB check must not echo the driver exception (host, port, DSN
   fragment, credentials) to the caller. Detail belongs in the server log.
3. Refresh-token handling — `POST /auth/refresh` is COOKIE-ONLY (a body token
   is no longer honoured), and rotation is atomic: a conditional UPDATE means
   two concurrent refreshes for one token cannot both win, and the loser
   follows the documented reuse contract (all live tokens revoked, 401
   `invalid_or_revoked_refresh_token`).

Run: cd backend && uv run pytest tests/test_auth_security_hardening.py -q
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.api.auth import _issue_pair
from app.api.deps import get_engine
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
)
from app.models.accounts import Account, AccountType
from app.models.categories import Category
from app.models.fx_rates import FxRate
from app.models.refresh_tokens import RefreshToken
from app.models.rules import Rule
from app.models.securities import Security, SecurityKind
from app.models.users import User
from app.services.demo_seed import DEMO_EMAIL

ACCOUNTS = "/api/v1/accounts"
TRANSACTIONS = "/api/v1/transactions"
TRADES = "/api/v1/trades"
RULES = "/api/v1/rules"
CATEGORIES = "/api/v1/categories"
SECURITIES = "/api/v1/securities"
FX = "/api/v1/fx"
ACB_REBUILD = "/api/v1/acb/rebuild"
MCP_TOKENS = "/api/v1/auth/mcp-tokens"
PROJECTIONS_RUN = "/api/v1/projections/run"
ME = "/api/v1/users/me"
CHANGE_PW = "/api/v1/users/me/change-password"
LLM_TEST = "/api/v1/users/me/llm-test"
BUDGETS = "/api/v1/budgets"
BILLS = "/api/v1/bills"
LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
REFRESH = "/api/v1/auth/refresh"
REGISTER = "/api/v1/auth/register"
HEALTH = "/api/v1/health"

PASSWORD = "correct-horse-battery"


# --- shared fixtures ---------------------------------------------------------


@contextmanager
def _cookies_over_http() -> Iterator[None]:
    """Relax `Secure` so httpx will actually send the cookie over http.

    TestClient talks plain http, and httpx drops Secure cookies on a non-TLS
    request — with the production default (Secure) on, every cookie-based
    assertion would silently exercise the "no cookie" path instead.
    """
    settings = get_settings()
    original = settings.cookie_secure
    settings.cookie_secure = False
    try:
        yield
    finally:
        settings.cookie_secure = original


def _make_user(session: Session, email: str) -> User:
    user = User(email=email, hashed_password=hash_password(PASSWORD))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _seed_demo_tenant(session: Session) -> dict[str, Any]:
    """A minimal demo tenant: user + category + account + security.

    The guard is enforced in the auth DEPENDENCY, so these rows only have to
    make the mutation payloads valid — a 403 proves the guard fired, while
    the non-demo control below proves the endpoints themselves still work.
    """
    user = _make_user(session, DEMO_EMAIL)
    category = Category(user_id=user.id, name="groceries", is_system=True)
    account = Account(
        user_id=user.id, name="Demo Chequing", type=AccountType.chequing, currency="CAD"
    )
    # Trades require an investment account, so seed one too (the trade probe
    # would otherwise fail validation before the guard could be observed).
    brokerage = Account(
        user_id=user.id,
        name="Demo Brokerage",
        type=AccountType.brokerage_non_registered,
        currency="CAD",
    )
    security = Security(
        user_id=user.id,
        symbol="RY.TO",
        name="Royal Bank of Canada",
        kind=SecurityKind.stock,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add_all([category, account, brokerage, security])
    session.commit()
    session.refresh(category)
    session.refresh(account)
    session.refresh(brokerage)
    session.refresh(security)
    return {
        "user": user,
        "category_id": category.id,
        "account_id": account.id,
        "brokerage_id": brokerage.id,
        "security_id": security.id,
        "headers": _auth(user),
    }


# --- 1. global demo mutation guard -------------------------------------------

# Every ordinary mutation router, one representative call each: the guard is
# inherited from the shared dependency, so a new router is covered for free.
# `id_keys` names the tenant keys substituted into zero-valued payload fields;
# `trade` payloads use `brokerage_id` (trades reject non-investment accounts).
_MUTATIONS = [
    # method, path, payload template, payload keys filled from the tenant
    ("POST", ACCOUNTS, {"name": "Sneaky", "type": "chequing", "currency": "CAD"}, ()),
    ("PATCH", f"{ACCOUNTS}/{{account_id}}", {"name": "Renamed"}, ()),
    ("DELETE", f"{ACCOUNTS}/{{account_id}}", None, ()),
    (
        "POST",
        CATEGORIES,
        {"name": "Sneaky", "parent_id": None},
        (),
    ),
    (
        "POST",
        TRANSACTIONS,
        {
            "account_id": 0,
            "date": "2020-01-15",
            "raw_description": "TAMPER",
            "amount_native": "-1.00",
        },
        ("account_id",),
    ),
    (
        "POST",
        TRADES,
        {
            "account_id": 0,
            "security_id": 0,
            "action": "buy",
            "date": "2020-01-15",
            "quantity": "1",
            "price_native": "10.00",
        },
        ("account_id:brokerage_id", "security_id"),
    ),
    # The three tables the M50 demo-reset regression damages through the API
    # (docs/20) — they must stay blocked here for that test to mean anything.
    ("POST", BUDGETS, {"category_id": 0, "amount": "10.00"}, ("category_id",)),
    (
        "POST",
        BILLS,
        {
            "name": "TAMPER BILL",
            "amount": "10.00",
            "category_id": 0,
            "frequency": "monthly",
            "next_due_date": "2030-01-15",
        },
        ("category_id",),
    ),
    (
        "POST",
        RULES,
        {"match_type": "contains", "pattern": "TAMPER", "priority": 1, "category_id": 0},
        ("category_id",),
    ),
    (
        "POST",
        SECURITIES,
        {"symbol": "TAMP", "name": "TAMP", "kind": "stock", "listing_currency": "CAD"},
        (),
    ),
    (
        "PUT",
        f"{SECURITIES}/{{security_id}}/fundamentals",
        {"pe_ratio": "12.5", "source": "manual"},
        (),
    ),
    ("POST", FX, {"base": "USD", "quote": "CAD", "date": "2020-01-02", "rate": "1.35"}, ()),
    # auth-scoped mutations on the demo tenant's own rows. MCP token mint and
    # revoke are DELIBERATELY absent (M81): the demo tenant is exempt on those
    # two paths — see `test_demo_mcp_token_routes_are_the_only_new_exemption`.
    ("POST", LLM_TEST, {}, ()),
    ("POST", CHANGE_PW, {"current_password": PASSWORD, "new_password": "another-horse"}, ()),
]


@pytest.mark.parametrize(
    ("method", "path", "body", "id_keys"),
    _MUTATIONS,
    ids=[f"{m} {p}" for m, p, _b, _k in _MUTATIONS],
)
def test_demo_mutation_is_rejected(
    client: TestClient,
    session: Session,
    method: str,
    path: str,
    body: dict | None,
    id_keys: tuple[str, ...],
) -> None:
    """POST/PUT/PATCH/DELETE as the demo tenant → 403 demo_account_restricted."""
    tenant = _seed_demo_tenant(session)
    payload = dict(body or {})
    for key in id_keys:
        # "field:tenant_key" lets one payload field pull from a different
        # tenant row (trades must target the brokerage account).
        field, _, tenant_key = key.partition(":")
        payload[field] = tenant[tenant_key or field]
    path = path.format(
        account_id=tenant["account_id"],
        security_id=tenant["security_id"],
        category_id=tenant["category_id"],
    )

    response = client.request(method, path, json=payload, headers=tenant["headers"])

    assert response.status_code == 403, response.text
    assert response.json() == {
        "detail": "The demo account is read-only; changes are not allowed",
        "code": "demo_account_restricted",
    }


def test_demo_mutation_guard_writes_nothing(client: TestClient, session: Session) -> None:
    """The guard fires in the dependency, so no handler body ever runs."""
    tenant = _seed_demo_tenant(session)
    uid = tenant["user"].id

    before = {
        "accounts": len(session.exec(select(Account).where(Account.user_id == uid)).all()),
        "rules": len(session.exec(select(Rule).where(Rule.user_id == uid)).all()),
        "securities": len(session.exec(select(Security).where(Security.user_id == uid)).all()),
    }
    attempts = [
        ("POST", ACCOUNTS, {"name": "Sneaky", "type": "chequing", "currency": "CAD"}),
        (
            "POST",
            RULES,
            {"match_type": "contains", "pattern": "X", "priority": 1, "category_id": 0},
        ),
        ("PUT", f"{SECURITIES}/{tenant['security_id']}/fundamentals", {"pe_ratio": "1.0"}),
    ]
    for method, path, body in attempts:
        body = {**body}
        if "category_id" in body:
            body["category_id"] = tenant["category_id"]
        response = client.request(method, path, json=body, headers=tenant["headers"])
        assert response.status_code == 403, response.text

    after = {
        "accounts": len(session.exec(select(Account).where(Account.user_id == uid)).all()),
        "rules": len(session.exec(select(Rule).where(Rule.user_id == uid)).all()),
        "securities": len(session.exec(select(Security).where(Security.user_id == uid)).all()),
    }
    assert after == before


def test_demo_write_exemptions_are_enumerated_never_inherited() -> None:
    """M50/M81/M157: the guard's escape hatches are enumerated, never inherited.

    `POST /auth/demo/reset` is the THIRD exemption overall (docs/20) — the
    first, `POST /auth/demo`, sits outside this dependency entirely — the
    MCP credential routes are the FOURTH and FIFTH (M81, docs/40 §1: the demo
    tenant's own token mint + revoke), and `POST /projections/run` is the SIXTH
    (M157/K-12, docs/12 §7: pure request-body math, no `Session` in the
    handler). The sets pinned below hold exactly the routes that reach the
    guard. A new route never inherits an exemption, and nothing joins either
    set without the justification comment sitting next to it.
    """
    from app.api.deps import DEMO_MUTATION_EXEMPT_PATHS, DEMO_MUTATION_EXEMPT_PREFIXES

    assert DEMO_MUTATION_EXEMPT_PATHS == frozenset(
        {
            "/api/v1/acb/rebuild",
            "/api/v1/auth/demo/reset",
            "/api/v1/auth/mcp-tokens",
            "/api/v1/projections/run",
        }
    )
    # Revoke carries the id segment, so it can only be matched by prefix.
    assert DEMO_MUTATION_EXEMPT_PREFIXES == ("/api/v1/auth/mcp-tokens/",)


def test_demo_mcp_token_routes_are_the_only_new_exemption(
    client: TestClient, session: Session
) -> None:
    """M81: the demo tenant mints READ-ONLY tokens, refuses explicit `full`,
    and can revoke again — every other demo mutation stays 403."""
    tenant = _seed_demo_tenant(session)
    headers = tenant["headers"]

    minted = client.post(MCP_TOKENS, json={"name": "demo-agent"}, headers=headers)
    assert minted.status_code == 201, minted.text
    assert minted.json()["policy"] == "read_only"

    # An explicit `full` is REFUSED, never silently downgraded (docs/40 §1).
    refused = client.post(MCP_TOKENS, json={"name": "nope", "policy": "full"}, headers=headers)
    assert refused.status_code == 422
    assert refused.json() == {
        "detail": "The demo tenant can only mint read-only MCP tokens",
        "code": "demo_mcp_read_only",
    }

    # Same for the BYO-AI connector scope — demo is query-only, nothing else
    # (docs/59 §1: the guard refuses anything that is not read_only).
    refused_propose = client.post(
        MCP_TOKENS, json={"name": "byo-ai", "policy": "read_propose"}, headers=headers
    )
    assert refused_propose.status_code == 422
    assert refused_propose.json() == {
        "detail": "The demo tenant can only mint read-only MCP tokens",
        "code": "demo_mcp_read_only",
    }

    # The token validates as read_only + is_demo — the two fields the MCP seam
    # filters the toolset and picks the refusal wording from (docs/40 §2).
    validated = client.post(
        "/api/v1/auth/validate-mcp-token",
        headers={"Authorization": f"Bearer {minted.json()['token']}"},
    )
    assert validated.status_code == 200, validated.text
    assert validated.json()["policy"] == "read_only"
    assert validated.json()["is_demo"] is True

    # Revoke must work too: otherwise one visitor fills MCP_TOKEN_ACTIVE_CAP
    # with tokens no visitor can delete for 90 days.
    revoked = client.delete(f"{MCP_TOKENS}/{minted.json()['id']}", headers=headers)
    assert revoked.status_code == 204, revoked.text
    listing = client.get(MCP_TOKENS, headers=headers)
    assert listing.status_code == 200
    assert listing.json() == {"items": [], "total": 0}


def test_projections_run_is_the_only_new_exemption(client: TestClient, session: Session) -> None:
    """M157/K-12: the demo tenant CAN run projections — and only that.

    `POST /projections/run` is the SIXTH demo-write exemption and the first
    one that touches no row at all (the handler takes no `Session`; the run is
    a pure function of the request body). Restoring it server-side is what
    lets the Reports calculator be server-computed for every tenant, the demo
    one included. The control pair below is the point: the same demo session
    that runs a projection is still refused on an ordinary mutating route, so
    the exemption widened nothing else.
    """
    tenant = _seed_demo_tenant(session)
    headers = tenant["headers"]
    payload = {
        "start_value_cad": "10000",
        "annual_contribution_cad": "1200",
        "years": 2,
        "inflation_pct": "2",
        "scenarios": [{"label": "Balanced", "annual_return_pct": "6.5"}],
    }

    run = client.post(PROJECTIONS_RUN, json=payload, headers=headers)
    assert run.status_code == 200, run.text
    assert run.json()["scenarios"][0]["label"] == "Balanced"

    # The guard itself is untouched: the same session, one ordinary route over.
    refused = client.post(
        ACCOUNTS,
        json={"name": "Sneaky", "type": "chequing", "currency": "CAD"},
        headers=headers,
    )
    assert refused.status_code == 403
    assert refused.json() == {
        "detail": "The demo account is read-only; changes are not allowed",
        "code": "demo_account_restricted",
    }


def test_shared_cache_is_not_writable_by_the_demo_tenant(
    client: TestClient, session: Session
) -> None:
    """`fx_rates` is the one shared, user-less table (docs/07 H7) — the
    global guard is what keeps a demo visitor from poisoning it."""
    tenant = _seed_demo_tenant(session)
    before = len(session.exec(select(FxRate)).all())

    response = client.post(
        FX,
        json={"base": "USD", "quote": "CAD", "date": "2020-01-02", "rate": "1.35"},
        headers=tenant["headers"],
    )

    assert response.status_code == 403
    assert response.json()["code"] == "demo_account_restricted"
    assert len(session.exec(select(FxRate)).all()) == before


def test_demo_tenant_reads_still_work(client: TestClient, session: Session) -> None:
    """The guard is method-scoped: every read endpoint stays available."""
    tenant = _seed_demo_tenant(session)
    for path in (
        ACCOUNTS,
        TRANSACTIONS,
        TRADES,
        RULES,
        CATEGORIES,
        SECURITIES,
        ACB_REBUILD.replace("/rebuild", ""),
        ME,
    ):
        response = client.get(path, headers=tenant["headers"])
        assert response.status_code == 200, (path, response.text)


def test_acb_rebuild_stays_available_to_the_demo_tenant(
    client: TestClient, session: Session
) -> None:
    """The oldest guard exemption (one of the four literal paths in
    `DEMO_MUTATION_EXEMPT_PATHS`; the others are `POST /auth/demo/reset`, M50,
    the demo tenant's MCP token mint, M81, and `POST /projections/run`, M157).

    `POST /acb/rebuild` recomputes `acb_pools` — a derived cache for THIS user
    only, written solely by app/tax/acb.py::rebuild_user_pools from that user's
    own trades. It cannot alter a seeded transaction, trade, price or balance,
    and docs/30 relies on it to populate the demo's pool cache after fx gaps
    are filled. Blocking it would remove a repair path and protect nothing.
    """
    tenant = _seed_demo_tenant(session)
    response = client.post(ACB_REBUILD, json={}, headers=tenant["headers"])
    assert response.status_code == 200, response.text
    # A different tenant is unaffected by the exemption either way.
    other = _make_user(session, f"rebuild-ok-{uuid.uuid4().hex}@example.com")
    assert client.post(ACB_REBUILD, json={}, headers=_auth(other)).status_code == 200


def test_demo_login_and_session_continue_to_work(client: TestClient, session: Session) -> None:
    """The guard sits behind auth: signing in as the demo tenant is allowed."""
    tenant = _seed_demo_tenant(session)
    response = client.post(LOGIN, json={"email": DEMO_EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == DEMO_EMAIL
    assert "refresh_token" not in body
    assert client.cookies.get("refresh_token")
    # The issued access token can still read.
    assert client.get(ME, headers=_auth(tenant["user"])).status_code == 200


def test_non_demo_tenant_mutations_are_not_blocked(client: TestClient, session: Session) -> None:
    """Control: the guard must not over-block ordinary tenants."""
    user = _make_user(session, f"guard-ok-{uuid.uuid4().hex}@example.com")
    headers = _auth(user)

    created = client.post(
        ACCOUNTS, json={"name": "Mine", "type": "chequing", "currency": "CAD"}, headers=headers
    )
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]
    assert (
        client.patch(
            f"{ACCOUNTS}/{account_id}", json={"name": "Renamed"}, headers=headers
        ).status_code
        == 200
    )
    assert client.delete(f"{ACCOUNTS}/{account_id}", headers=headers).status_code == 204

    rule = client.post(
        RULES,
        json={"match_type": "contains", "pattern": "COFFEE", "priority": 1, "category_id": None},
        headers=headers,
    )
    # No category in this tenant's fixture — the guard must not be the blocker.
    assert rule.status_code != 403, rule.text


# --- 2. health probe must not disclose the driver error ----------------------


class _ExplodingSession:
    """Stands in for a DB session whose `SELECT 1` fails with a leaky error."""

    DETAIL = (
        'connection to server at "db.internal", port 5432 failed: FATAL: '
        'password authentication failed for user "fin"'
    )

    def execute(self, *args: object, **kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception(self.DETAIL))


def test_health_failure_response_is_generic(client: TestClient) -> None:
    """503 to the caller, with no driver/DSN detail in the body."""
    from app.api.deps import get_db
    from app.main import app

    def override_get_db():
        yield _ExplodingSession()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = client.get(HEALTH)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
    assert data["checks"]["database"]["status"] == "unhealthy"
    # The key stays for monitoring; the value no longer leaks.
    assert "error" in data["checks"]["database"]
    for secret in ("db.internal", "5432", "password authentication", "fin", "OperationalError"):
        assert secret not in response.text, secret


def test_health_failure_logs_the_driver_detail(
    client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    """The detail is retained in the server log, never in the response."""
    from app.api.deps import get_db
    from app.main import app

    # alembic's fileConfig() sets disable_existing_loggers=True under pytest,
    # which would silently drop app.api.health records.
    monkeypatch.setattr(logging.getLogger("app.api.health"), "disabled", False)

    def override_get_db():
        yield _ExplodingSession()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with caplog.at_level(logging.ERROR, logger="app.api.health"):
            response = client.get(HEALTH)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    # The operator still gets the full driver message (and traceback) in the
    # log — caplog.text renders the exc_info block, getMessage() would not.
    assert _ExplodingSession.DETAIL in caplog.text
    assert "OperationalError" in caplog.text
    # ...and the caller still saw nothing of it.
    assert _ExplodingSession.DETAIL not in response.text


# --- 3. refresh is cookie-only and rotation is atomic ------------------------


def test_refresh_ignores_a_body_token(client: TestClient) -> None:
    """A body-carried refresh token is no longer honoured (cookie-only)."""
    email = f"cookie-only-{uuid.uuid4().hex}@example.com"
    with _cookies_over_http():
        # M87 (docs/29 F15): register answers 200 and opens NO session — the
        # refresh cookie these three tests exercise comes from the sign-in.
        assert client.post(REGISTER, json={"email": email, "password": PASSWORD}).status_code == 200
        assert client.post(LOGIN, json={"email": email, "password": PASSWORD}).status_code == 200
        issued = client.cookies.get("refresh_token")
        assert issued

        # Cookie jar cleared → the body token alone must not authenticate.
        client.cookies.clear()
        rejected = client.post(REFRESH, json={"refresh_token": issued})
        assert rejected.status_code == 401
        assert rejected.json()["code"] == "missing_refresh_token"

        # The genuine cookie still rotates.
        assert client.post(LOGIN, json={"email": email, "password": PASSWORD}).status_code == 200
        cookie_token = client.cookies.get("refresh_token")
        assert cookie_token and cookie_token != issued
        rotated = client.post(REFRESH, json={})
        assert rotated.status_code == 200, rotated.text
        assert "refresh_token" not in rotated.json()
        assert client.cookies.get("refresh_token") not in (None, issued, cookie_token)


def test_logout_ignores_a_body_token(client: TestClient) -> None:
    """Logout is cookie-only too: a body token cannot revoke a session."""
    email = f"logout-body-{uuid.uuid4().hex}@example.com"
    with _cookies_over_http():
        # M87 (docs/29 F15): register answers 200 and opens NO session — the
        # refresh cookie these three tests exercise comes from the sign-in.
        assert client.post(REGISTER, json={"email": email, "password": PASSWORD}).status_code == 200
        assert client.post(LOGIN, json={"email": email, "password": PASSWORD}).status_code == 200
        token = client.cookies.get("refresh_token")
        assert token

        client.cookies.clear()
        response = client.post(LOGOUT, json={"refresh_token": token})
        assert response.status_code == 401
        assert response.json()["code"] == "missing_refresh_token"

        # The token was NOT revoked: it still works from a real cookie.
        client.cookies.set("refresh_token", token, path="/api/v1/auth")
        assert client.post(REFRESH, json={}).status_code == 200


def _committed_user_with_token(email: str) -> tuple[int, str]:
    """A user + one live refresh token, COMMITTED on its own connection.

    The concurrency tests need rows another connection can see, so they cannot
    ride the shared `session` fixture (everything there is rolled back).
    """
    with Session(get_engine(), expire_on_commit=False) as s:
        user = User(email=email, hashed_password=hash_password(PASSWORD))
        s.add(user)
        s.commit()
        s.refresh(user)
        uid = user.id
        assert uid is not None
        pair = _issue_pair(s, user)
        return uid, pair.refresh_token


def _drop_user(user_id: int) -> None:
    with Session(get_engine()) as s:
        s.exec(delete(RefreshToken).where(RefreshToken.user_id == user_id))
        s.exec(delete(User).where(User.id == user_id))
        s.commit()


def test_concurrent_rotation_has_exactly_one_winner() -> None:
    """Two racing claims for ONE token: the conditional UPDATE lets exactly
    one win, so a token can never be spent twice into two live sessions."""
    from app.api.auth import _claim_refresh_token

    user_id, raw_token = _committed_user_with_token(f"race-{uuid.uuid4().hex}@example.com")
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def claim() -> None:
        with Session(get_engine()) as s:
            barrier.wait(timeout=15)
            won = _claim_refresh_token(s, hash_refresh_token(raw_token), datetime.now(UTC))
        with lock:
            results.append(won)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert sorted(results) == [False, True], f"expected exactly one winner, got {results}"
    finally:
        _drop_user(user_id)


def test_losing_claim_revokes_the_winners_successor() -> None:
    """Documented reuse contract: the loser invalidates every live token of
    the user — including the successor the winner was just handed."""
    from app.api.auth import (
        _claim_refresh_token,
        _lookup_refresh_row,
        _revoke_all_refresh_tokens,
    )

    user_id, raw_token = _committed_user_with_token(f"loser-{uuid.uuid4().hex}@example.com")
    now = datetime.now(UTC)
    try:
        with Session(get_engine(), expire_on_commit=False) as s:
            assert _claim_refresh_token(s, hash_refresh_token(raw_token), now) is True
            user = s.get(User, user_id)
            assert user is not None
            successor = _issue_pair(s, user)

        # Loser replays the spent token → claim fails → reuse detection fires.
        with Session(get_engine()) as s:
            assert _claim_refresh_token(s, hash_refresh_token(raw_token), now) is False
            _revoke_all_refresh_tokens(s, user_id, now)

        with Session(get_engine()) as s:
            row = _lookup_refresh_row(s, successor.refresh_token)
            assert row is not None and row.revoked_at is not None
            live = s.exec(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
                )
            ).all()
            assert live == []
    finally:
        _drop_user(user_id)


def test_claim_is_idempotent_only_for_one_holder() -> None:
    """The same live token can be claimed exactly once."""
    from app.api.auth import _claim_refresh_token

    user_id, raw_token = _committed_user_with_token(f"single-{uuid.uuid4().hex}@example.com")
    now = datetime.now(UTC)
    try:
        with Session(get_engine()) as s:
            assert _claim_refresh_token(s, hash_refresh_token(raw_token), now) is True
        with Session(get_engine()) as s:
            assert _claim_refresh_token(s, hash_refresh_token(raw_token), now) is False
    finally:
        _drop_user(user_id)


def test_refresh_reuse_over_http_revokes_every_token(client: TestClient) -> None:
    """HTTP-level reuse detection: replaying a spent token kills the chain,
    successor included."""
    email = f"reuse-{uuid.uuid4().hex}@example.com"
    with _cookies_over_http():
        # M87 (docs/29 F15): register answers 200 and opens NO session — the
        # refresh cookie these three tests exercise comes from the sign-in.
        assert client.post(REGISTER, json={"email": email, "password": PASSWORD}).status_code == 200
        assert client.post(LOGIN, json={"email": email, "password": PASSWORD}).status_code == 200
        spent = client.cookies.get("refresh_token")
        assert spent

        assert client.post(REFRESH, json={}).status_code == 200
        successor = client.cookies.get("refresh_token")
        assert successor and successor != spent

        # Replay the spent token: 401, and the successor is revoked too.
        client.cookies.set("refresh_token", spent, path="/api/v1/auth")
        replay = client.post(REFRESH, json={})
        assert replay.status_code == 401
        assert replay.json()["code"] == "invalid_or_revoked_refresh_token"

        client.cookies.set("refresh_token", successor, path="/api/v1/auth")
        after = client.post(REFRESH, json={})
        assert after.status_code == 401
        assert after.json()["code"] == "invalid_or_revoked_refresh_token"


def test_expired_refresh_token_does_not_revoke_siblings(
    client: TestClient, session: Session
) -> None:
    """Expiry keeps its own signal — only REUSE revokes the whole chain."""
    user = _make_user(session, f"expiry-{uuid.uuid4().hex}@example.com")
    uid = user.id
    assert uid is not None
    stale_raw, _ = new_refresh_token()
    sibling_raw, sibling_hash = new_refresh_token()
    expired = RefreshToken(
        user_id=uid,
        token_hash=hash_refresh_token(stale_raw),
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    sibling = RefreshToken(
        user_id=uid, token_hash=sibling_hash, expires_at=datetime.now(UTC) + timedelta(days=1)
    )
    session.add_all([expired, sibling])
    session.commit()
    session.refresh(sibling)

    with _cookies_over_http():
        client.cookies.set("refresh_token", stale_raw, path="/api/v1/auth")
        response = client.post(REFRESH, json={})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_or_revoked_refresh_token"

    session.refresh(sibling)
    assert sibling.revoked_at is None
    # ...and the sibling itself is still usable from its own cookie.
    with _cookies_over_http():
        client.cookies.set("refresh_token", sibling_raw, path="/api/v1/auth")
        assert client.post(REFRESH, json={}).status_code == 200


def test_expired_refresh_token_retry_does_not_revoke_siblings(
    client: TestClient, session: Session
) -> None:
    """A RETRY of an expired token must not cascade either.

    The first presentation of an expired token stamps `revoked_at` (the claim is
    the reuse check). Without the expiry guard, a SECOND presentation — a stale
    browser tab retrying, or a proxy replaying — finds the row already stamped,
    reads that as "this token was spent", and burns every other session for the
    user. A self-inflicted logout of all devices, from a client that merely
    retried. Reuse detection must fire on a real replay, never on an expired
    token being tried twice.
    """
    user = _make_user(session, f"expiry-retry-{uuid.uuid4().hex}@example.com")
    uid = user.id
    assert uid is not None
    stale_raw, _ = new_refresh_token()
    sibling_raw, sibling_hash = new_refresh_token()
    session.add_all(
        [
            RefreshToken(
                user_id=uid,
                token_hash=hash_refresh_token(stale_raw),
                expires_at=datetime.now(UTC) - timedelta(days=1),
            ),
            RefreshToken(
                user_id=uid,
                token_hash=sibling_hash,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
        ]
    )
    session.commit()

    sibling = session.exec(
        select(RefreshToken).where(RefreshToken.token_hash == sibling_hash)
    ).one()
    sibling_id = sibling.id

    for attempt in (1, 2, 3):
        with _cookies_over_http():
            client.cookies.set("refresh_token", stale_raw, path="/api/v1/auth")
            response = client.post(REFRESH, json={})
        assert response.status_code == 401, f"attempt {attempt}"
        assert response.json()["code"] == "invalid_or_revoked_refresh_token"
        # The sibling session survives EVERY retry of the expired token.
        still_live = session.get(RefreshToken, sibling_id)
        assert still_live is not None
        assert still_live.revoked_at is None, f"sibling revoked on attempt {attempt}"

    # And the sibling is genuinely still usable.
    with _cookies_over_http():
        client.cookies.set("refresh_token", sibling_raw, path="/api/v1/auth")
        assert client.post(REFRESH, json={}).status_code == 200


def test_concurrent_http_refresh_has_exactly_one_winner(client: TestClient) -> None:
    """TWO `POST /auth/refresh` requests carrying ONE cookie, in flight together.

    This is the client-side race that killed the demo session 39 s into the
    M58b walkthrough (docs/09 "demo-session rotation"): the browser presents
    the same httpOnly cookie twice — AuthContext's session probe and
    usePriceStream's SSE reconnect each used to start their own refresh — and
    the server must still answer with exactly one winner. Two 200s would mean
    one cookie minted two live sessions (rotation forked); the loser instead
    takes the documented reuse path (401 `invalid_or_revoked_refresh_token`,
    chain-wide revocation, docs/29), and afterwards the chain never holds more
    than one live token.

    The client-side half of that fix is the single-flight refresh in
    `frontend/src/api/client.ts` (pinned by
    `frontend/tests/refreshSingleFlight.test.ts`); this pins the OTHER half —
    that the claim stays atomic under real HTTP concurrency, not just when
    `_claim_refresh_token` is called directly.
    """
    user_id, raw_token = _committed_user_with_token(f"http-race-{uuid.uuid4().hex}@example.com")
    barrier = threading.Barrier(2)
    answers: list[tuple[int, str | None]] = []
    lock = threading.Lock()

    def hammer() -> None:
        one_shot = TestClient(fastapi_app)
        one_shot.cookies.set("refresh_token", raw_token, path="/api/v1/auth")
        barrier.wait(timeout=15)
        response = one_shot.post(REFRESH, json={})
        body = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        with lock:
            answers.append((response.status_code, body.get("code")))

    # conftest points `get_db` at ONE session shared by every request so tests
    # can inspect it — two concurrent requests would then drive that single
    # Session from two threads ("concurrent operations are not permitted").
    # This test is about real concurrency, so it takes the production wiring
    # (a Session per request) for its own duration and puts the override back.
    from app.api.deps import get_db
    from app.main import app as fastapi_app

    shared_session_override = fastapi_app.dependency_overrides.pop(get_db, None)
    threads = [threading.Thread(target=hammer) for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads), "a refresh request hung"

        statuses = sorted(status for status, _ in answers)
        assert statuses == [200, 401], f"one cookie must yield one winner: {answers}"
        loser_code = next(code for status, code in answers if status == 401)
        assert loser_code == "invalid_or_revoked_refresh_token", answers

        with Session(get_engine()) as s:
            live = s.exec(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.revoked_at.is_(None),
                )
            ).all()
        assert len(live) <= 1, f"rotation forked the chain: {len(live)} live tokens"
    finally:
        if shared_session_override is not None:
            fastapi_app.dependency_overrides[get_db] = shared_session_override
        _drop_user(user_id)

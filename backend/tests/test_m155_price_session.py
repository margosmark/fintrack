"""M155 — the PRICE paths must not hold a session across their fetch.

The price-path half of the class M153 fixed on the enrichment path
(`tests/test_m153_enrichment_session.py`, which reads the same two questions
from the same moment). Every seam here READS first — `session.get`, a
`select` — and then waits on the provider; SQLAlchemy autobegins on that
read, so without a release the connection sits `idle in transaction` for the
whole fetch. That is the row M148 drained the pool with and the row M152's
prod sweep caught on a healthy operator run:

* `api/securities._run_backfill` — BackgroundTask, own session out of
  `deps.get_engine()`: `session.get` autobegins, `backfill_range`'s first
  act is `fetch_history`.
* `api/portfolio._run_ensure_recent_price` — same shape, `fetch_latest_close`.
* `GET /securities/{id}/overview` and `POST /securities/{id}/refresh` —
  REQUEST sessions: `owned_or_404` autobegins and the fetch follows.
* `valuation.compute_positions` — the `/portfolio/positions` read path, and
  the one that makes the seam indivisible: it calls `ensure_recent_price`
  ONCE PER HELD SECURITY, and each call's own `newest_price` SELECT reopens
  the transaction the previous iteration left behind. A release at the
  caller can only sit outside the loop; the release that matters has to sit
  between `newest_price` and `fetch_latest_close`, i.e. inside
  `ensure_recent_price`.

Both questions are asked from inside a monkeypatched provider call, exactly
as M153 does:

1. no session the caller opened is in a transaction at that instant, and
2. no client backend on this database is `idle in transaction`.

Question 2 needs a database with nothing else parked on it, so these tests
drive a REALLY-COMMITTED tenant through `deps.get_engine()` — the same
engine the background tasks and `get_db` draw from — rather than the
`session` fixture, whose outer transaction is by construction `idle in
transaction` for the whole test.

And the deliberate EXCEPTION, pinned so it cannot be "fixed" by accident:
`services/symbol_repair.repair_symbol` reaches `backfill_range`'s fetch with
flushed, uncommitted writes (the corrected symbol plus the stale closes it
just deleted) and a dry-run contract that depends on `session.rollback()`
discarding them. A release inside `backfill_range` would commit an
operator's `--apply`-less dry run, so the release for that seam lives at its
CALLERS and `repair_symbol` deliberately holds its transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlmodel import Session as SQLModelSession

import app.api.portfolio as portfolio_api
import app.api.securities as securities_api
import app.services.market_data as market_data
from app.api.deps import get_db, get_engine
from app.core.security import create_access_token
from app.main import app
from app.models.accounts import Account, AccountType
from app.models.prices import Price, PriceSource
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.services import valuation
from app.services.symbol_repair import repair_symbol

API = "/api/v1"
D = Decimal

#: Two held securities, neither with a cached close — the state that makes
#: BOTH price seams actually wait.
_HELD = ("M155A", "M155B")

#: ``(label, symbol)`` of every provider call, and whether anything was
#: parked in a transaction at the moment it was made.
_PROBES: list[tuple[str, str]] = []
_BUSY: list[bool] = []
#: Every session the code under test opened — the objects whose state the
#: pin reads. `get_db`'s override below records the request session.
_SESSIONS: list[Session] = []

#: The same question M153 asked, unchanged.
_IDLE_IN_TRANSACTION = (
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() "
    "AND backend_type = 'client backend' "
    "AND state = 'idle in transaction' "
    "AND pid <> pg_backend_pid()"
)


def _at_the_wait(label: str, symbol: str) -> None:
    """The pin, asked at the moment the provider would be awaited."""
    _PROBES.append((label, symbol))
    with get_engine().connect() as conn:
        parked = int(conn.execute(text(_IDLE_IN_TRANSACTION)).scalar() or 0)
    _BUSY.append(parked > 0 or any(session.in_transaction() for session in _SESSIONS))


@pytest.fixture(name="probes", autouse=True)
def probes_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every provider seam on the price paths becomes a recorder.

    Real code runs around them — `backfill_range` really books the verdict,
    `_cache_closes` really inserts and commits — so the run exercises the
    write path while every network hop records whether the pool connection
    was parked at that instant.
    """
    _PROBES.clear()
    _BUSY.clear()
    _SESSIONS.clear()

    def _probe_history(symbol: str, start_date: date, end_date: date) -> dict[date, Decimal]:
        _at_the_wait("fetch_history", symbol)
        return {end_date: D("11.00")}

    def _probe_close(symbol: str, today: date | None = None) -> tuple[date, Decimal] | None:
        _at_the_wait("fetch_latest_close", symbol)
        return (today or date.today(), D("12.00"))

    def _probe_fundamentals(symbol: str) -> dict[str, object] | None:
        _at_the_wait("fetch_fundamentals", symbol)
        return None

    monkeypatch.setattr(market_data, "fetch_history", _probe_history)
    monkeypatch.setattr(market_data, "fetch_latest_close", _probe_close)
    monkeypatch.setattr(market_data, "fetch_fundamentals", _probe_fundamentals)
    yield


@pytest.fixture(name="tenant")
def tenant_fixture() -> Iterator[dict[str, Any]]:
    """A really-committed tenant: two held securities with NO cached close.

    The background tasks open their OWN session out of `deps.get_engine()`,
    which can only see COMMITTED rows — the suite fixture's outer transaction
    is invisible to it (the house pattern, docs/13) — so the tenant is
    committed for real through that very engine and deleted again in teardown.
    """
    engine = get_engine()
    with SQLModelSession(engine, expire_on_commit=False) as setup:
        user = User(email="m155-price@example.com", hashed_password="x")
        setup.add(user)
        setup.commit()
        setup.refresh(user)
        assert user.id is not None
        account = Account(
            user_id=user.id,
            name="M155 account",
            type=AccountType.tfsa,
            currency="CAD",
            taxable=False,
        )
        setup.add(account)
        setup.commit()
        setup.refresh(account)
        ids: dict[str, int] = {}
        for symbol in _HELD:
            security = Security(
                user_id=user.id,
                symbol=symbol,
                name=f"{symbol} Inc.",
                kind=SecurityKind.stock,
                listing_currency="CAD",
                exchange="TSX",
            )
            setup.add(security)
            setup.commit()
            setup.refresh(security)
            assert security.id is not None
            ids[symbol] = security.id
            setup.add(
                Trade(
                    user_id=user.id,
                    account_id=account.id,
                    security_id=security.id,
                    action=TradeAction.buy,
                    date=date.today(),
                    quantity=D("10"),
                    price_native=D("50"),
                )
            )
        setup.commit()
    try:
        yield {"user_id": user.id, "account_id": account.id, "securities": ids, "engine": engine}
    finally:
        with SQLModelSession(engine) as teardown:
            for table in ("prices", "trades", "securities", "accounts"):
                teardown.execute(
                    text(f"DELETE FROM {table} WHERE user_id = :uid"),  # noqa: S608 -- table from fixed tuple in this test
                    {"uid": user.id},
                )
            teardown.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user.id})
            teardown.commit()


@pytest.fixture(name="headers")
def headers_fixture(tenant: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(tenant['user_id'])}"}


@pytest.fixture(name="client")
def client_fixture(tenant: dict[str, Any]) -> Iterator[TestClient]:
    """A TestClient whose request session is a REAL one — the exact shape of
    `deps.get_db` (same engine, `expire_on_commit=False`) — recorded in
    `_SESSIONS` so the probes can ask question 1 about it."""

    def override_get_db() -> Iterator[Session]:
        with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
            _SESSIONS.append(session)
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _detached_security(tenant: dict[str, Any], symbol: str) -> Security:
    """The row as a background task would hold it: loaded through its own
    session, then let go — every attribute the seams read stays loaded."""
    engine: Engine = tenant["engine"]
    session = SQLModelSession(engine, expire_on_commit=False)
    try:
        security = session.get(Security, tenant["securities"][symbol])
        assert security is not None, f"{symbol} is invisible to a committed-read session"
        return security
    finally:
        session.close()


# --- the two background tasks ------------------------------------------------


def test_the_backfill_task_releases_before_its_fetch(tenant: dict[str, Any]) -> None:
    """`_run_backfill` is the cheap half of the card: `session.get`
    autobegins and `backfill_range`'s first act is `fetch_history`, so the
    pool connection sat `idle in transaction` for the whole year of bars."""
    securities_api._run_backfill(_detached_security(tenant, "M155A"))

    assert _PROBES == [("fetch_history", "M155A")], (
        f"the backfill path did not exercise its fetch — the pin saw {_PROBES!r}"
    )
    assert _BUSY == [False], (
        "the background backfill task held its session in a transaction while "
        "it waited on yfinance — the M152 row, on a serving path"
    )


def test_the_price_task_releases_before_its_fetch(tenant: dict[str, Any]) -> None:
    """The market-hours fan-out's own body: `session.get` then
    `ensure_recent_price(force=True)`."""
    portfolio_api._run_ensure_recent_price(_detached_security(tenant, "M155A"), force=True)

    assert _PROBES == [("fetch_latest_close", "M155A")]
    assert _BUSY == [False], (
        "the background price task held its session in a transaction across the quote"
    )


# --- the request paths -------------------------------------------------------


def test_the_overview_route_releases_before_both_of_its_waits(
    client: TestClient, headers: dict[str, str], tenant: dict[str, Any]
) -> None:
    """`GET /securities/{id}/overview` is a REQUEST session: `owned_or_404`
    autobegins, then `ensure_recent_price` and `ensure_fundamentals` each
    wait on a provider. M153 already releases the second one; this pins the
    first."""
    body = client.get(f"{API}/securities/{tenant['securities']['M155A']}/overview", headers=headers)
    assert body.status_code == 200, body.text

    # The requested security retains its overview refresh path; the shared
    # valuation pass is cache-only and does not refresh the second holding.
    assert _PROBES == [
        ("fetch_latest_close", "M155A"),
        ("fetch_fundamentals", "M155A"),
    ], f"unexpected overview provider calls — the pin saw {_PROBES!r}"
    assert _BUSY == [False, False], (
        "the overview request held ITS request session in a transaction across a provider wait"
    )


def test_the_refresh_route_releases_before_its_backfill(
    client: TestClient, headers: dict[str, str], tenant: dict[str, Any]
) -> None:
    """`POST /securities/{id}/refresh` — the manual "Try refresh": same
    request-session shape, and the wait is a 365-day `fetch_history`."""
    body = client.post(f"{API}/securities/{tenant['securities']['M155A']}/refresh", headers=headers)
    assert body.status_code == 200, body.text

    assert ("fetch_history", "M155A") in _PROBES, (
        f"the refresh route never reached its backfill — the pin saw {_PROBES!r}"
    )
    index = _PROBES.index(("fetch_history", "M155A"))
    assert _BUSY[index] is False, (
        "the refresh request held its request session in a transaction across the 365-day backfill"
    )


def test_the_positions_path_releases_before_each_of_its_fetches(
    tenant: dict[str, Any],
) -> None:
    """The shared valuation service returns cached values without provider waits."""
    with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
        _SESSIONS.append(session)
        rows = valuation.compute_positions(session, tenant["user_id"])

    assert {row["symbol"] for row in rows} == set(_HELD)
    assert _PROBES == []
    assert _BUSY == []


# --- what the release costs the caller (docs/07 says it in numbers) ----------


def test_one_cache_only_positions_call_performs_no_provider_or_commit(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached portfolio valuation performs no per-security provider/commit work."""
    commits: list[str] = []
    real_commit = Session.commit

    def _spy(self: Session, *args: object, **kwargs: object) -> Any:
        commits.append(self.get_bind().url.database or "?")
        return real_commit(self, *args, **kwargs)

    monkeypatch.setattr(Session, "commit", _spy)

    with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
        _SESSIONS.append(session)
        valuation.compute_positions(session, tenant["user_id"])

    assert _PROBES == []
    assert commits == []


def test_a_positions_call_with_fresh_closes_commits_nothing(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of that claim: a close already dated today is never
    re-fetched, so the release never fires and the request costs no commits
    at all — the freshness window is what keeps a page load off yfinance."""
    with SQLModelSession(tenant["engine"], expire_on_commit=False) as setup:
        for symbol, value in (("M155A", "10.00"), ("M155B", "20.00")):
            setup.add(
                Price(
                    user_id=tenant["user_id"],
                    security_id=tenant["securities"][symbol],
                    date=date.today(),
                    price=D(value),
                    source=PriceSource.manual,
                )
            )
        setup.commit()

    commits: list[object] = []
    real_commit = Session.commit

    def _spy(self: Session, *args: object, **kwargs: object) -> Any:
        commits.append(1)
        return real_commit(self, *args, **kwargs)

    monkeypatch.setattr(Session, "commit", _spy)

    with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
        _SESSIONS.append(session)
        valuation.compute_positions(session, tenant["user_id"])

    assert _PROBES == [], f"a fresh close must never be re-fetched — the pin saw {_PROBES!r}"
    assert commits == [], (
        "a positions request that fetched nothing committed something — the "
        "release is only ever paid for by a security that actually waited"
    )


# --- the deliberate exception ------------------------------------------------


def test_the_symbol_repair_keeps_its_transaction_across_its_backfill(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`repair_symbol` is the caller `backfill_range`'s release must NOT be
    sprinkled into.

    It repoints the symbol and drops the stale closes INSIDE one transaction
    and then backfills — and `scripts/repair_symbol.py` rolls that whole
    transaction back for a dry run (`--apply` is the write). A release inside
    `backfill_range` would commit an operator's read-only dry run and turn
    `--apply` into a formality, so the seam for that function lives at its
    callers instead and this caller deliberately holds its transaction.

    Pinned both ways: the wait really is busy (the writes are pending), and
    the rollback really does undo them.
    """
    old_symbol = "M155OLD"
    with SQLModelSession(tenant["engine"], expire_on_commit=False) as setup:
        security = Security(
            user_id=tenant["user_id"],
            symbol=old_symbol,
            name="M155 old spelling",
            kind=SecurityKind.stock,
            listing_currency="CAD",
            exchange="TSX",
        )
        setup.add(security)
        setup.commit()
        setup.refresh(security)
        assert security.id is not None
        repair_id = security.id

    def _resolve(symbol: str, **_kwargs: object) -> dict[str, Any]:
        return {
            "match": "direct",
            "result": {
                "symbol": "M155NEW.TO",
                "exchange": "TSX",
                "kind": SecurityKind.stock,
                "name": "M155 new spelling",
                "listing_currency": "CAD",
            },
            "candidates": [],
        }

    monkeypatch.setattr(market_data, "resolve_symbol", _resolve)

    # A backfill that brings back NOTHING is the sharp case: nothing
    # self-commits on the way (`_cache_closes` commits only `if inserted`), so
    # the repair's own writes are still pending at the wait — which is exactly
    # the state an internal release would commit out from under a dry run.
    def _empty_history(symbol: str, start_date: date, end_date: date) -> dict[date, Decimal]:
        _at_the_wait("fetch_history", symbol)
        return {}

    monkeypatch.setattr(market_data, "fetch_history", _empty_history)

    with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
        _SESSIONS.append(session)
        target = session.get(Security, repair_id)
        assert target is not None
        repair_symbol(session, target, backfill=True)

    assert _PROBES == [("fetch_history", "M155NEW.TO")], (
        f"the repair never reached its backfill — the pin saw {_PROBES!r}"
    )
    assert _BUSY == [True], (
        "the symbol repair now releases its transaction before the backfill — "
        "that commits the caller's pending symbol change and stale-price "
        "deletes, which `scripts/repair_symbol.py` rolls back for a dry run"
    )

    # The other half of the same contract: a dry run writes nothing.
    with SQLModelSession(tenant["engine"], expire_on_commit=False) as dry_run:
        target = dry_run.get(Security, repair_id)
        assert target is not None
        repair_symbol(dry_run, target, backfill=True)
        assert target.symbol == "M155NEW.TO", "the repair did not apply in-session"
        dry_run.rollback()
        target = dry_run.get(Security, repair_id)
        assert target is not None and target.symbol == old_symbol, (
            "a dry run left the corrected symbol behind — the repair's "
            "transaction is the rollback boundary the operator relies on"
        )

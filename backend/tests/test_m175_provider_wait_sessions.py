"""M175 — the held-session class, asked from INSIDE every provider wait it releases.

M148/M152/M153/M155/M161 fixed four leaks piecemeal; M175 is the exhaustive
pass over the whole class (`tests/test_provider_wait_ledger.py` is the guard
that keeps it exhaustive). This file is the behavioural half: for every path
whose release M175 ADDED — and for the two callers that deliberately do not get
one — it asks the M153 questions from inside a monkeypatched provider call:

1. no session the caller opened is in a transaction at that instant, and
2. no client backend on this database is `idle in transaction`.

Question 2 needs a database with nothing else parked on it, so these tests
drive a REALLY-COMMITTED tenant through `deps.get_engine()` — the same engine
the workers, the background tasks and `get_db` draw from — rather than the
`session` fixture, whose outer transaction is by construction `idle in
transaction` for the whole test (the house pattern, tests/test_m155_price_session.py).

The paths, and the shape each one had:

* `deep_backfill.backfill_security` — a SWEEP (`scripts/deep_price_backfill.py`,
  dry run by DEFAULT) that read `_prefetch`/`trade_evidence` and then fetched
  per row, holding one transaction across every row's wait.
* `benchmarks.backfill_symbol` — the benchmark worker's own read-then-fetch.
* `fx.latest_rate` / `fx.get_rates_between` — Valet fetch-on-miss; portfolio
  positions now batch cached FX and never call this fetch-on-miss path.
* `fx_refresh.run_once` — the scheduled pair loop.
* `split_repair.repair_split_window` — the split repair's re-cache.
* `categorization._process_one_batch` — the app's LONGEST wait (60 s LLM egress,
  retried once) with the batch's reads already on the session.
* `notify_delivery._dispatch` — SMTP/webhook sends, one wait per delivery in the
  batch, with the batch's writes uncommitted on the session.
* `GET /securities/lookup`, `GET /securities/quote-check`,
  `POST /securities/{id}/holdings/import`, `POST /sharing/invites` — request sessions,
  where the auth read has the connection `idle in transaction` before the wait.
* `services.dividend_refresh.refresh_one` — own session per tenant/security job,
  closed before fetch and reopened to store.

And the two deliberate exceptions, pinned so a future pass cannot "fix" them
into a half-committed import or an unrollback-able operator dry run:
`trades._resolve_security` (the staging import holds a FLUSHED, uncommitted
`import_batches` row) and `fx.get_rates_between(commit=False)` (the ACB sweep's
rate backfill, which must ride `rebuild_user_pools`' never-committing
transaction).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlmodel import Session as SQLModelSession
from sqlmodel import select

import app.api.deps as deps
import app.api.sharing as sharing_api
import app.api.trades as trades_api
import app.services.benchmarks as benchmarks
import app.services.categorization as categorization
import app.services.deep_backfill as deep_backfill
import app.services.dividend_refresh as dividend_refresh
import app.services.fx as fx
import app.services.fx_refresh as fx_refresh
import app.services.market_data as market_data
import app.services.notify_delivery as notify_delivery
import app.services.split_repair as split_repair
from app.api.deps import get_db, get_engine
from app.core.security import create_access_token
from app.main import app
from app.models.accounts import Account, AccountType
from app.models.categories import Category
from app.models.dividend_history import (
    DividendHistory,
    DividendHistoryRefresh,
    DividendHistoryState,
)
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.notifications import NotificationDelivery
from app.models.prices import Price, PriceSource
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.transactions import Transaction
from app.models.users import User

API = "/api/v1"
D = Decimal

#: Two held securities — enough to prove "once per row/position", which is the
#: loop shape every one of these leaks came in.
_HELD = ("M175A", "M175B")

#: ``(label, subject)`` of every provider call, and whether anything was parked
#: in a transaction at the moment it was made.
_PROBES: list[tuple[str, str]] = []
_BUSY: list[bool] = []
#: Every session the code under test opened — the objects question 1 reads.
#: `get_db`'s override below records the request session.
_SESSIONS: list[Session] = []

_IDLE_IN_TRANSACTION = (
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() "
    "AND backend_type = 'client backend' "
    "AND state = 'idle in transaction' "
    "AND pid <> pg_backend_pid()"
)


def _at_the_wait(label: str, subject: str) -> None:
    """The pin, asked at the moment the provider would be awaited."""
    _PROBES.append((label, subject))
    with get_engine().connect() as conn:
        parked = int(conn.execute(text(_IDLE_IN_TRANSACTION)).scalar() or 0)
    _BUSY.append(parked > 0 or any(session.in_transaction() for session in _SESSIONS))


@pytest.fixture(name="probes", autouse=True)
def probes_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every M175 provider seam becomes a recorder.

    The returns are deliberately EMPTY: an empty answer is the case that leaves
    a write-less path with its read transaction still open (nothing commits on
    a skip), which is exactly the shape the release has to survive.
    """
    _PROBES.clear()
    _BUSY.clear()
    _SESSIONS.clear()

    def _close(symbol: str, today: date | None = None) -> None:
        _at_the_wait("fetch_latest_close", symbol)
        return None

    def _fundamentals(symbol: str) -> None:
        _at_the_wait("fetch_fundamentals", symbol)
        return None

    def _candidates(symbol: str, start_date: date, end_date: date) -> list[Any]:
        _at_the_wait("fetch_history_candidates", symbol)
        return []

    def _history(symbol: str, start_date: date, end_date: date) -> dict[date, Decimal]:
        _at_the_wait("fetch_history", symbol)
        return {}

    def _benchmark(ticker: str, start_date: date, end_date: date) -> tuple[dict, Any]:
        _at_the_wait("fetch_benchmark_history", ticker)
        return ({}, market_data.QuoteOutcome.unavailable)

    def _valet(base: str, quote: str, start_date: date, end_date: date) -> dict[date, Decimal]:
        _at_the_wait("fetch_valet_series", f"{base}/{quote}")
        return {}

    def _composition(symbol: str) -> None:
        _at_the_wait("fetch_fund_composition", symbol)
        return None

    def _dividends(symbol: str) -> list[dict[str, object]]:
        _at_the_wait("fetch_dividend_history", symbol)
        return []

    def _resolve(symbol: str) -> dict[str, Any]:
        _at_the_wait("resolve_symbol", symbol)
        return {"match": "none", "result": None, "candidates": []}

    def _quote(symbol: str) -> tuple[Any, None]:
        _at_the_wait("probe_quote", symbol)
        return (market_data.QuoteOutcome.ok, None)

    def _lookup(symbol: str, **kwargs: Any) -> None:
        _at_the_wait("lookup_symbol", symbol)
        return None

    def _send(payload: dict, channel: str, *args: Any, **kwargs: Any) -> Any:
        _at_the_wait("send", channel)
        return notify_delivery.SendResult("sent", 200, None, None)

    def _invite_email(target: str, accept_url: str, settings: Any) -> bool:
        _at_the_wait("send_invite_email", target)
        return False

    monkeypatch.setattr(market_data, "fetch_history_candidates", _candidates)
    monkeypatch.setattr(market_data, "fetch_history", _history)
    monkeypatch.setattr(market_data, "fetch_latest_close", _close)
    monkeypatch.setattr(market_data, "fetch_fundamentals", _fundamentals)
    monkeypatch.setattr(market_data, "fetch_benchmark_history", _benchmark)
    monkeypatch.setattr(fx, "fetch_valet_series", _valet)
    monkeypatch.setattr(market_data, "fetch_fund_composition", _composition)
    monkeypatch.setattr(market_data, "fetch_dividend_history", _dividends)
    monkeypatch.setattr(market_data, "resolve_symbol", _resolve)
    monkeypatch.setattr(market_data, "probe_quote", _quote)
    monkeypatch.setattr(market_data, "lookup_symbol", _lookup)
    monkeypatch.setattr(notify_delivery, "send", _send)
    monkeypatch.setattr(sharing_api, "send_invite_email", _invite_email)
    yield


@pytest.fixture(name="tenant")
def tenant_fixture() -> Iterator[dict[str, Any]]:
    """A really-committed tenant: an investment account, two held securities.

    Committed for real through `deps.get_engine()` because the workers and the
    background tasks open their OWN sessions out of it — the suite fixture's
    outer transaction is invisible to them (docs/13).
    """
    engine = get_engine()
    today = datetime.now(UTC).date()
    with SQLModelSession(engine, expire_on_commit=False) as setup:
        user = User(email="m175-waits@example.com", hashed_password="x")
        setup.add(user)
        setup.commit()
        setup.refresh(user)
        assert user.id is not None
        account = Account(
            user_id=user.id,
            name="M175 account",
            type=AccountType.brokerage_non_registered,
            currency="CAD",
            taxable=True,
        )
        setup.add(account)
        setup.commit()
        setup.refresh(account)
        ids: dict[str, int] = {}
        for index, symbol in enumerate(_HELD):
            security = Security(
                user_id=user.id,
                symbol=symbol,
                name=f"{symbol} Inc.",
                kind=SecurityKind.stock,
                listing_currency="CAD" if index == 0 else "USD",
                exchange="TSX" if index == 0 else "NASDAQ",
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
                    date=today - timedelta(days=30),
                    quantity=D("10"),
                    price_native=D("50"),
                )
            )
            # A close dated TODAY: `ensure_recent_price` serves it without a
            # fetch (docs/07 freshness window), which is what lets the
            # valuation test reach the FX lookup on the foreign listing.
            setup.add(
                Price(
                    user_id=user.id,
                    security_id=security.id,
                    date=today,
                    price=D("50") if index == 0 else D("20"),
                    source=PriceSource.manual,
                )
            )
        category = Category(user_id=user.id, name="Groceries")
        setup.add(category)
        setup.commit()
        setup.refresh(category)
    tenant = {
        "user_id": user.id,
        "account_id": account.id,
        "securities": ids,
        "category_id": category.id,
        "engine": engine,
    }
    try:
        yield tenant
    finally:
        with SQLModelSession(engine) as teardown:
            for table in (
                "notification_delivery_attempts",
                "notification_deliveries",
                "dividend_history_refresh",
                "dividend_history",
                "sharing_audit",
                "sharing_links",
                "transaction_events",
                "transactions",
                "prices",
                "trades",
                "watchlist",
                "securities",
                "accounts",
                "categories",
                "import_batches",
            ):
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
    """A TestClient whose request session is a REAL one, recorded for question 1."""

    def override_get_db() -> Iterator[Session]:
        with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
            _SESSIONS.append(session)
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


# --- the sweeps ---------------------------------------------------------------


def test_the_deep_price_sweep_releases_between_its_rows(tenant: dict[str, Any]) -> None:
    """`scripts/deep_price_backfill.py` is dry run by DEFAULT and walks every
    held row with ONE session: without the release, one operator run parked a
    transaction across every provider wait for minutes (the M152 shape)."""
    with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
        _SESSIONS.append(session)
        results = deep_backfill.deep_backfill_all(session, user_id=tenant["user_id"], apply=False)

    assert len(results) == len(_HELD), f"the sweep did not walk every held row: {results!r}"
    assert _PROBES == [("fetch_history_candidates", symbol) for symbol in _HELD], (
        f"the sweep did not wait once per row — the pin saw {_PROBES!r}"
    )
    assert _BUSY == [False, False], (
        "the deep-price sweep held its session in a transaction while it waited on the "
        "provider — and it does so once per row, so one session carried the whole run"
    )


def test_the_benchmark_sweep_releases_before_each_symbol(tenant: dict[str, Any]) -> None:
    """`services/benchmark_refresh.py::run_once` reads `symbols_behind` and then
    walks the four picks — the read transaction used to be open across the
    first fetch, and across every symbol whose fetch ended in `not_found`."""
    today = datetime.now(UTC).date()
    with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
        _SESSIONS.append(session)
        benchmarks.backfill_all(session, today=today, symbols=["SPY", "XIU"])

    assert _PROBES == [
        ("fetch_benchmark_history", "SPY"),
        ("fetch_benchmark_history", "XIU.TO"),
    ], f"the benchmark sweep did not fetch its picks — the pin saw {_PROBES!r}"
    assert _BUSY == [False, False], (
        "the benchmark sweep held its session in a transaction across a provider wait"
    )


def test_the_fx_worker_releases_before_its_pair_loop(tenant: dict[str, Any]) -> None:
    """The scheduled Valet refresh: `privacy_blocks`/`pairs_behind` read, then
    one fetch per pair behind the cache."""
    today = datetime.now(UTC).date()
    with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
        _SESSIONS.append(session)
        stats = fx_refresh.run_once(session, today)

    assert len(_PROBES) == len(fx.CURATED_FX_BASES), (
        f"the worker did not reach every pair ({stats!r}) — the pin saw {_PROBES!r}"
    )
    assert set(_BUSY) == {False}, (
        "the scheduled fx refresh held its session in a transaction across a pair's "
        "Valet call (10 s x 3 retries)"
    )


# --- the Valet fetch-on-miss --------------------------------------------------


def test_latest_rate_releases_before_its_fetch(tenant: dict[str, Any]) -> None:
    """`fx.latest_rate` is called ONCE PER FOREIGN-LISTING POSITION by
    `valuation.compute_positions`, so a cold cache parked the request session
    once per holding — the same per-iteration shape M161 fixed for prices."""
    today = datetime.now(UTC).date()
    with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
        _SESSIONS.append(session)
        assert fx.latest_rate(session, "USD", "CAD", today) is None

    assert _PROBES == [("fetch_valet_series", "USD/CAD")], f"the pin saw {_PROBES!r}"
    assert _BUSY == [False], "`latest_rate` held the session across its Valet fetch"


def test_get_rates_between_releases_on_its_committing_side(tenant: dict[str, Any]) -> None:
    """The range seam's `commit=True` side (the `GET /fx` route) releases; the
    `commit=False` side (the ACB sweep) must NOT — it rides a transaction that
    docs/02 says never commits, and this pins the asymmetry."""
    today = datetime.now(UTC).date()
    start = today - timedelta(days=5)
    with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
        _SESSIONS.append(session)
        fx.get_rates_between(session, "USD", "CAD", start, today)
    assert _BUSY == [False], "the commit=True rate range held its session across the Valet call"

    _PROBES.clear()
    _BUSY.clear()
    with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
        _SESSIONS.append(session)
        fx.get_rates_between(session, "EUR", "CAD", start, today, commit=False)
    assert _BUSY == [True], (
        "the ACB side of the rate range must KEEP its transaction: `rebuild_user_pools` "
        "never commits (docs/02), so a release there would leave a half-applied sweep"
    )


def test_valuation_read_is_cache_only_and_does_not_hold_a_provider_session(
    tenant: dict[str, Any],
) -> None:
    """The positions read only uses batch-loaded cache rows; refresh is separate."""
    from app.services import valuation

    with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
        _SESSIONS.append(session)
        rows = valuation.compute_positions(session, tenant["user_id"])

    assert {row["symbol"] for row in rows} == set(_HELD)
    assert _PROBES == [], f"cache-only valuation reached provider seams: {_PROBES!r}"
    assert _BUSY == [], f"cache-only valuation held a session across provider I/O: {_BUSY!r}"


# --- the repair and the LLM batch ---------------------------------------------


def test_the_split_repair_releases_before_its_refetch(tenant: dict[str, Any]) -> None:
    """The repair re-caches one window through the normalizing seam; the fetch
    is its first act and both callers (CLI, route) reach it read-only."""
    today = datetime.now(UTC).date()
    with SQLModelSession(tenant["engine"], expire_on_commit=False) as session:
        _SESSIONS.append(session)
        security = session.get(Security, tenant["securities"]["M175A"])
        assert security is not None
        result = split_repair.repair_split_window(
            session, security, today - timedelta(days=10), today, apply=False
        )

    assert result.refused == "no_closes"
    assert _PROBES == [("fetch_history", "M175A")], f"the pin saw {_PROBES!r}"
    assert _BUSY == [False], "the split repair held its session across its re-cache fetch"


def test_the_llm_batch_releases_before_its_chat(tenant: dict[str, Any]) -> None:
    """The 60 s one: `_process_one_batch` reads its batch, accounts and few-shot
    examples, then waits on the LLM — retried once, so two minutes of
    `idle in transaction` on the worker's connection."""
    engine = tenant["engine"]
    with SQLModelSession(engine, expire_on_commit=False) as session:
        _SESSIONS.append(session)
        user = session.get(User, tenant["user_id"])
        assert user is not None
        transaction = Transaction(
            user_id=user.id,
            account_id=tenant["account_id"],
            date=datetime.now(UTC).date(),
            raw_description="M175 GROCER",
            normalized_payee="m175 grocer",
            amount_native=D("-12.00"),
            currency="CAD",
            dedup_hash="m175-llm-batch-row",
        )
        session.add(transaction)
        session.flush()
        assert transaction.id is not None
        categories = list(
            session.exec(categorization.select(Category).where(Category.user_id == user.id)).all()
        )
        category_names = {c.id: c.name for c in categories}

        def _chat(messages: list[dict], schema: dict) -> dict:
            _at_the_wait("provider.chat", "batch")
            return {"suggestions": []}

        provider = SimpleNamespace(chat=_chat)

        categorization._process_one_batch(
            session,
            user,
            provider,
            "m175-test",
            [transaction.id],
            categories,
            category_names,
            set(category_names),
        )
        session.rollback()

    assert _PROBES == [("provider.chat", "batch")], f"the batch never reached the LLM: {_PROBES!r}"
    assert _BUSY == [False], (
        "the LLM batch worker held its session in a transaction across the provider call "
        "(60 s deadline, retried once)"
    )


# --- the notification dispatcher ----------------------------------------------


def test_the_notification_dispatch_releases_before_every_send(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch of sends, each an SMTP/webhook wait, with the batch's decisions
    (cancelled rows, previous attempts) uncommitted on the session.

    `_ineligible_reason` is stubbed to `None` on purpose: the subject here is
    the SEND's session state, not the eligibility predicate (which has its own
    coverage in the delivery suite).
    """
    monkeypatch.setattr(notify_delivery, "_ineligible_reason", lambda *args, **kwargs: None)
    engine = tenant["engine"]
    now = datetime.now(UTC)
    with SQLModelSession(engine, expire_on_commit=False) as session:
        _SESSIONS.append(session)
        for index in range(2):
            session.add(
                NotificationDelivery(
                    user_id=tenant["user_id"],
                    channel="email",
                    alert_type="budget_threshold",
                    severity="warning",
                    item_key=f"m175-{index}",
                    link="m175",
                    status="pending",
                    occurred_at=now,
                    next_attempt_at=now - timedelta(minutes=1),
                )
            )
        session.commit()
        notified = notify_delivery._dispatch(session, user_id=tenant["user_id"])

    assert notified["sent"] == 2, f"the dispatcher did not send both deliveries: {notified!r}"
    assert _PROBES == [("send", "email"), ("send", "email")], f"the pin saw {_PROBES!r}"
    assert _BUSY == [False, False], (
        "the notification dispatcher sent with its session parked in a transaction — "
        "once per delivery in the batch"
    )


# --- the request routes --------------------------------------------------------


def test_the_lookup_route_releases_before_its_lookup(
    client: TestClient, headers: dict[str, str]
) -> None:
    """`GET /securities/lookup` is read-only, but the auth read is what has the
    request session `idle in transaction` before the yfinance lookup."""
    response = client.get(f"{API}/securities/lookup", params={"symbol": "M175X"}, headers=headers)

    assert response.status_code == 404, response.text
    assert _PROBES == [("resolve_symbol", "M175X")], f"the pin saw {_PROBES!r}"
    assert _BUSY == [False], "the lookup route held its request session across the provider call"


def test_the_quote_check_route_releases_before_its_probe(
    client: TestClient, headers: dict[str, str]
) -> None:
    response = client.get(
        f"{API}/securities/quote-check", params={"symbol": "M175X"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert _PROBES == [("probe_quote", "M175X")], f"the pin saw {_PROBES!r}"
    assert _BUSY == [False], "the quote-check route held its request session across the probe"


def test_the_holdings_import_route_releases_before_the_provider_chain(
    client: TestClient, headers: dict[str, str], tenant: dict[str, Any]
) -> None:
    """The one route that runs the provider chain INLINE (issuer CSV, Vanguard,
    yfinance) — its first act is the fetch, so the request session used to be
    parked for the whole chain."""
    response = client.post(
        f"{API}/securities/{tenant['securities']['M175A']}/holdings/import", headers=headers
    )

    assert response.status_code == 503, response.text
    assert _PROBES == [("fetch_fund_composition", "M175A")], f"the pin saw {_PROBES!r}"
    assert _BUSY == [False], "the holdings import route held its request session across the chain"


def test_the_dividend_suggestions_route_queues_without_waiting_for_provider(
    client: TestClient, headers: dict[str, str]
) -> None:
    """The request reads cached events and reports pending refresh state, without yfinance I/O."""
    response = client.get(f"{API}/trades/dividend-suggestions", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == []
    state = response.headers["X-Dividend-History-State"]
    parsed_state = json.loads(state)
    assert parsed_state["pending"] == len(_HELD)
    assert parsed_state["stale"] == 0  # no successful history is pending, not stale
    assert _PROBES == [], f"foreground route reached provider seams: {_PROBES!r}"
    assert _BUSY == [], f"foreground route held a session across provider I/O: {_BUSY!r}"


def test_dividend_refresh_failure_preserves_history_and_empty_success_clears_it(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    security_id = tenant["securities"]["M175A"]
    last_success = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
    with SQLModelSession(tenant["engine"]) as session:
        session.add(
            DividendHistoryRefresh(
                user_id=tenant["user_id"],
                security_id=security_id,
                state=DividendHistoryState.available,
                last_success_at=last_success,
            )
        )
        session.add(
            DividendHistory(
                user_id=tenant["user_id"],
                security_id=security_id,
                ex_date=date(2026, 1, 15),
                amount=Decimal("0.25"),
            )
        )
        session.commit()

    monkeypatch.setattr(market_data, "fetch_dividend_history", lambda symbol: None)
    assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is False
    with SQLModelSession(tenant["engine"]) as session:
        refresh = session.exec(
            select(DividendHistoryRefresh).where(
                DividendHistoryRefresh.user_id == tenant["user_id"],
                DividendHistoryRefresh.security_id == security_id,
            )
        ).one()
        events = session.exec(
            select(DividendHistory).where(
                DividendHistory.user_id == tenant["user_id"],
                DividendHistory.security_id == security_id,
            )
        ).all()
        assert refresh.state == DividendHistoryState.unavailable
        assert refresh.last_success_at == last_success
        assert len(events) == 1 and events[0].amount == Decimal("0.25")

    def quote_not_found(symbol: str) -> None:
        market_data._last_quote_outcome.set(market_data.QuoteOutcome.not_found)
        market_data._last_quote_reason.set("Quote not found for symbol: M175A")
        return None

    monkeypatch.setattr(market_data, "fetch_dividend_history", quote_not_found)
    assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is False
    with SQLModelSession(tenant["engine"]) as session:
        refresh = session.exec(
            select(DividendHistoryRefresh).where(
                DividendHistoryRefresh.user_id == tenant["user_id"],
                DividendHistoryRefresh.security_id == security_id,
            )
        ).one()
        security = session.get(Security, security_id)
        events = session.exec(
            select(DividendHistory).where(
                DividendHistory.user_id == tenant["user_id"],
                DividendHistory.security_id == security_id,
            )
        ).all()
        assert security is not None and security.quote_miss_count == 1
        assert refresh.state == DividendHistoryState.unavailable
        assert refresh.last_success_at == last_success
        assert len(events) == 1 and events[0].amount == Decimal("0.25")

    monkeypatch.setattr(
        market_data,
        "fetch_dividend_history",
        lambda symbol: [{"date": "2026-01-15", "amount": "NaN"}],
    )
    assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is False
    with SQLModelSession(tenant["engine"]) as session:
        refresh = session.exec(
            select(DividendHistoryRefresh).where(
                DividendHistoryRefresh.user_id == tenant["user_id"],
                DividendHistoryRefresh.security_id == security_id,
            )
        ).one()
        events = session.exec(
            select(DividendHistory).where(
                DividendHistory.user_id == tenant["user_id"],
                DividendHistory.security_id == security_id,
            )
        ).all()
        assert refresh.state == DividendHistoryState.unavailable
        assert refresh.error_code == "invalid_provider_payload"
        assert refresh.last_success_at == last_success
        assert len(events) == 1 and events[0].amount == Decimal("0.25")

    monkeypatch.setattr(market_data, "fetch_dividend_history", lambda symbol: [])
    assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is True
    with SQLModelSession(tenant["engine"]) as session:
        refresh = session.exec(
            select(DividendHistoryRefresh).where(
                DividendHistoryRefresh.user_id == tenant["user_id"],
                DividendHistoryRefresh.security_id == security_id,
            )
        ).one()
        events = session.exec(
            select(DividendHistory).where(
                DividendHistory.user_id == tenant["user_id"],
                DividendHistory.security_id == security_id,
            )
        ).all()
        assert refresh.state == DividendHistoryState.empty
        assert refresh.last_success_at is not None and refresh.last_success_at > last_success
        assert events == []


def test_dividend_refresh_age_and_retry_backoff_are_pinned(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 13, 12, 0, 0)
    monkeypatch.setattr(dividend_refresh, "_now", lambda: now)
    security_ids = [tenant["securities"][symbol] for symbol in _HELD]
    with SQLModelSession(tenant["engine"]) as session:
        young_security = Security(
            user_id=tenant["user_id"],
            symbol="M175-AGE",
            name="Refresh age fixture",
            kind=SecurityKind.stock,
            listing_currency="CAD",
            exchange="TSX",
        )
        session.add(young_security)
        session.flush()
        assert young_security.id is not None
        security_ids.append(young_security.id)
        session.add_all(
            [
                DividendHistoryRefresh(
                    user_id=tenant["user_id"],
                    security_id=security_ids[0],
                    state=DividendHistoryState.available,
                    last_success_at=now - timedelta(hours=23),
                    last_attempt_at=now - timedelta(minutes=16),
                ),
                DividendHistoryRefresh(
                    user_id=tenant["user_id"],
                    security_id=security_ids[1],
                    state=DividendHistoryState.available,
                    last_success_at=now - timedelta(hours=25),
                    last_attempt_at=now - timedelta(minutes=14),
                ),
                DividendHistoryRefresh(
                    user_id=tenant["user_id"],
                    security_id=security_ids[2],
                    state=DividendHistoryState.available,
                    last_success_at=now - timedelta(hours=25),
                    last_attempt_at=now - timedelta(minutes=16),
                ),
            ]
        )
        session.commit()

        rows = dividend_refresh.enqueue_due(session, tenant["user_id"], set(security_ids))

        assert rows[security_ids[0]].state == DividendHistoryState.available
        assert rows[security_ids[1]].state == DividendHistoryState.available
        assert rows[security_ids[2]].state == DividendHistoryState.pending
        assert rows[security_ids[2]].last_attempt_at == now


def test_dividend_worker_tick_has_a_finite_serial_batch(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = tenant["engine"]
    with SQLModelSession(engine) as session:
        extras: list[Security] = []
        for index in range(dividend_refresh.MAX_JOBS_PER_TICK + 2):
            security = Security(
                user_id=tenant["user_id"],
                symbol=f"M175B{index}",
                name=f"Bounded worker {index}",
                kind=SecurityKind.stock,
                listing_currency="CAD",
                exchange="TSX",
            )
            session.add(security)
            extras.append(security)
        session.flush()
        session.add_all(
            [
                DividendHistoryRefresh(
                    user_id=tenant["user_id"],
                    security_id=security.id,
                    state=DividendHistoryState.pending,
                )
                for security in extras
            ]
        )
        session.commit()

    calls: list[str] = []
    monkeypatch.setattr(
        market_data, "fetch_dividend_history", lambda symbol: calls.append(symbol) or []
    )

    assert dividend_refresh._run_pending_batch() == dividend_refresh.MAX_JOBS_PER_TICK
    assert len(calls) == dividend_refresh.MAX_JOBS_PER_TICK
    assert len(set(calls)) == len(calls)
    with SQLModelSession(engine) as session:
        pending = session.exec(
            select(DividendHistoryRefresh).where(
                DividendHistoryRefresh.user_id == tenant["user_id"],
                DividendHistoryRefresh.state == DividendHistoryState.pending,
            )
        ).all()
        assert len(pending) == 2


def test_dividend_worker_recovers_a_stale_refresh_after_restart(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    security_id = tenant["securities"]["M175A"]
    with SQLModelSession(tenant["engine"]) as session:
        session.add(
            DividendHistoryRefresh(
                user_id=tenant["user_id"],
                security_id=security_id,
                state=DividendHistoryState.refreshing,
                last_attempt_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=6),
            )
        )
        session.commit()

    calls: list[str] = []
    monkeypatch.setattr(
        market_data, "fetch_dividend_history", lambda symbol: calls.append(symbol) or []
    )

    assert dividend_refresh._run_pending_batch() == 1
    assert calls == ["M175A"]
    with SQLModelSession(tenant["engine"]) as session:
        refresh = session.exec(
            select(DividendHistoryRefresh).where(
                DividendHistoryRefresh.user_id == tenant["user_id"],
                DividendHistoryRefresh.security_id == security_id,
            )
        ).one()
        assert refresh.state == DividendHistoryState.empty
        assert refresh.last_success_at is not None


def test_dividend_refresh_rechecks_privacy_before_provider_call(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    security_id = tenant["securities"]["M175A"]
    with SQLModelSession(tenant["engine"]) as session:
        user = session.get(User, tenant["user_id"])
        assert user is not None
        user.privacy_mode = True
        session.add(
            DividendHistoryRefresh(
                user_id=tenant["user_id"],
                security_id=security_id,
                state=DividendHistoryState.pending,
            )
        )
        session.commit()
    calls: list[str] = []
    monkeypatch.setattr(market_data, "fetch_dividend_history", lambda symbol: calls.append(symbol))

    assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is False

    with SQLModelSession(tenant["engine"]) as session:
        refresh = session.exec(
            select(DividendHistoryRefresh).where(
                DividendHistoryRefresh.user_id == tenant["user_id"],
                DividendHistoryRefresh.security_id == security_id,
            )
        ).one()
        assert calls == []
        assert refresh.state == DividendHistoryState.unavailable
        assert refresh.error_code == "privacy_or_ownership"


def test_dividend_refresh_is_single_flight_across_concurrent_workers(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    security_id = tenant["securities"]["M175A"]
    with SQLModelSession(tenant["engine"]) as session:
        session.add(
            DividendHistoryRefresh(
                user_id=tenant["user_id"],
                security_id=security_id,
                state=DividendHistoryState.pending,
            )
        )
        session.commit()
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_provider(symbol: str) -> list[dict[str, str]]:
        calls.append(symbol)
        entered.set()
        assert release.wait(timeout=5)
        return []

    monkeypatch.setattr(market_data, "fetch_dividend_history", slow_provider)
    result: list[bool] = []
    first = threading.Thread(
        target=lambda: result.append(dividend_refresh.refresh_one(tenant["user_id"], security_id))
    )
    first.start()
    try:
        assert entered.wait(timeout=5)
        assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is False
    finally:
        release.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert result == [True]
    assert calls == ["M175A"]


def test_dividend_provider_wait_releases_a_tiny_pool_connection(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    security_id = tenant["securities"]["M175A"]
    with SQLModelSession(tenant["engine"]) as session:
        session.add(
            DividendHistoryRefresh(
                user_id=tenant["user_id"],
                security_id=security_id,
                state=DividendHistoryState.pending,
            )
        )
        session.commit()

    tiny_engine = create_engine(
        tenant["engine"].url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.5,
    )
    monkeypatch.setattr(deps, "_engine", tiny_engine)
    entered = threading.Event()
    release = threading.Event()

    def slow_provider(symbol: str) -> list[dict[str, str]]:
        entered.set()
        assert release.wait(timeout=5)
        return []

    monkeypatch.setattr(market_data, "fetch_dividend_history", slow_provider)
    result: list[bool] = []
    worker = threading.Thread(
        target=lambda: result.append(dividend_refresh.refresh_one(tenant["user_id"], security_id))
    )
    worker.start()
    try:
        assert entered.wait(timeout=5)
        # This second session must acquire the one and only connection while
        # the first thread is still blocked inside provider I/O.
        with SQLModelSession(tiny_engine) as session:
            refresh = session.exec(
                select(DividendHistoryRefresh).where(
                    DividendHistoryRefresh.user_id == tenant["user_id"],
                    DividendHistoryRefresh.security_id == security_id,
                )
            ).one()
            assert refresh.state == DividendHistoryState.refreshing
    finally:
        release.set()
        worker.join(timeout=5)
        tiny_engine.dispose()

    assert not worker.is_alive()
    assert result == [True]


def test_the_invite_route_releases_before_the_smtp_send(
    client: TestClient, headers: dict[str, str]
) -> None:
    """`POST /sharing/invites` sends the mail AFTER the invite is committed
    (the send's outcome is reported, never rolled back) — `db.refresh` is what
    re-opened the transaction the SMTP wait then sat in."""
    response = client.post(
        f"{API}/sharing/invites", json={"email": "m175-invite@example.com"}, headers=headers
    )

    assert response.status_code == 201, response.text
    assert _PROBES == [("send_invite_email", "m175-invite@example.com")], f"the pin saw {_PROBES!r}"
    assert _BUSY == [False], "the invite route held its request session across the SMTP send"


# --- the deliberate exceptions -------------------------------------------------


def test_the_import_staging_keeps_its_transaction_across_its_lookups(
    tenant: dict[str, Any],
) -> None:
    """`trades._resolve_security` is the class's deliberate NON-change.

    The investment-CSV staging path flushes the `import_batches` row (and every
    security it has already created) BEFORE it looks a new symbol up, so a
    release here would commit half an import the user never committed — the
    same shape as `symbol_repair.repair_symbol`. So: the session IS in a
    transaction at the wait, and the flushed row is STILL not visible to
    another connection afterwards, which is what proves nothing was committed.
    """
    engine = tenant["engine"]
    with SQLModelSession(engine, expire_on_commit=False) as session:
        _SESSIONS.append(session)
        batch = ImportBatch(
            user_id=tenant["user_id"],
            account_id=tenant["account_id"],
            institution="m175.csv",
            file_name="m175.csv",
            status=ImportBatchStatus.pending_review,
        )
        session.add(batch)
        session.flush()
        marker_id = batch.id
        assert marker_id is not None

        trades_api._resolve_security(session, tenant["user_id"], "M175NEW", "New Co", "CAD")

        session.rollback()

    assert _PROBES == [
        ("lookup_symbol", "M175NEW"),
        # The lookup answered nothing, so the row's exchange/currency probe
        # follows (`fetch_latest_close` on the `.TO` form) — a SECOND wait on
        # the same held transaction, which is why the release decision here is
        # the caller's and not the seam's.
        ("fetch_latest_close", "M175NEW.TO"),
    ], f"the pin saw {_PROBES!r}"
    assert _BUSY == [True, True], (
        "the staging import's lookups are expected to KEEP their transaction — a release "
        "there would commit a flushed, uncommitted import batch"
    )
    with SQLModelSession(engine) as other:
        visible = other.exec(
            trades_api.select(ImportBatch).where(ImportBatch.id == marker_id)
        ).first()
        assert visible is None, (
            "the flushed import batch became visible to another connection — something "
            "committed the staging import's transaction"
        )

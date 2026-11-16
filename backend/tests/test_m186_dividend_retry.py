"""M186 — quote-miss observability and a BOUNDED retry policy for dividend refresh.

Baseline `4887221` preserved historical dividends on a quote miss: the refresh
fails, cached events and `last_success_at` survive, and the M141 verdict
(`quote_miss_count` / `dead_symbol`) advances on the security. What it left
open, and what this file pins:

1. **The retry was flat and unbounded in TIME** — every failure was
   retry-eligible after the same 15 minutes, forever, so a symbol that stopped
   resolving was re-fetched every 15 minutes for the rest of its life (the
   dead-symbol flag caps the HARD-miss fan-out at three, but a row already
   flagged, or a symbol flaking between verdicts, kept cycling).
   `dividend_refresh.retry_age` now backs a failure streak off
   15m → 30m → 1h → … capped at `MAX_RETRY_AGE` (6h), driven by the persisted
   `dividend_history_refresh.consecutive_failures`. The cap is the bound.

2. **Recovery is one success** — `refresh_one` resets `consecutive_failures`
   to 0 on any successful fetch (including a quote-verified empty), so a
   source that heals returns to the flat cadence immediately instead of
   waiting out a stale backoff.

3. **A hard quote miss is not a network failure** — the refresh row now books
   `error_code="quote_not_found"` (distinct from `provider_unavailable`) and
   `GET /trades/dividend-suggestions` counts those rows (plus `dead_symbol`
   rows) in the additive `X-Dividend-History-State.quote_miss`, so the UI can
   say "no public quote for the symbol (possibly delisted)" instead of a
   generic "unavailable" that implies retrying will help.

The worker's other bounds (MAX_JOBS_PER_TICK, STALE_JOB_AGE, single-flight
row lock) are pinned in `test_m175_provider_wait_sessions.py`; this file owns
the backoff arithmetic, its persistence through `enqueue_due`, and the
observability surface.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlmodel import Session as SQLModelSession
from sqlmodel import select

import app.services.dividend_refresh as dividend_refresh
import app.services.market_data as market_data
from app.api.deps import get_engine
from app.models.accounts import Account, AccountType
from app.models.dividend_history import (
    DividendHistory,
    DividendHistoryRefresh,
    DividendHistoryState,
)
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User

API = "/api/v1"
D = Decimal


@pytest.fixture(name="tenant")
def tenant_fixture() -> Iterator[dict[str, Any]]:
    """A really-committed tenant: one user, one security (docs/13).

    `refresh_one` opens its own session through `deps.get_engine()`, so rows
    must be committed for real — the suite fixture's outer transaction is
    invisible to it.
    """
    engine = get_engine()
    today = datetime.now(UTC).date()
    with SQLModelSession(engine, expire_on_commit=False) as setup:
        user = User(email="m186-retry@example.com", hashed_password="x")
        setup.add(user)
        setup.commit()
        setup.refresh(user)
        assert user.id is not None
        account = Account(
            user_id=user.id,
            name="M186 account",
            type=AccountType.brokerage_non_registered,
            currency="CAD",
            taxable=True,
        )
        setup.add(account)
        setup.commit()
        setup.refresh(account)
        security = Security(
            user_id=user.id,
            symbol="M186A",
            name="M186 fixture",
            kind=SecurityKind.stock,
            listing_currency="CAD",
            exchange="TSX",
        )
        setup.add(security)
        setup.commit()
        setup.refresh(security)
        assert security.id is not None
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
        setup.commit()
    tenant = {
        "user_id": user.id,
        "account_id": account.id,
        "security_id": security.id,
        "engine": engine,
    }
    try:
        yield tenant
    finally:
        with SQLModelSession(engine) as teardown:
            for table in (
                "dividend_history_refresh",
                "dividend_history",
                "trades",
                "watchlist",
                "securities",
                "accounts",
            ):
                teardown.execute(
                    text(f"DELETE FROM {table} WHERE user_id = :uid"),  # noqa: S608 -- table from fixed tuple in this test
                    {"uid": user.id},
                )
            teardown.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user.id})
            teardown.commit()


def _refresh_row(tenant: dict[str, Any]) -> DividendHistoryRefresh:
    with SQLModelSession(tenant["engine"]) as session:
        return session.exec(
            select(DividendHistoryRefresh).where(
                DividendHistoryRefresh.user_id == tenant["user_id"],
                DividendHistoryRefresh.security_id == tenant["security_id"],
            )
        ).one()


def _set_state(tenant: dict[str, Any], **values: Any) -> None:
    with SQLModelSession(tenant["engine"]) as session:
        session.exec(
            update(DividendHistoryRefresh)
            .where(DividendHistoryRefresh.user_id == tenant["user_id"])
            .values(**values)
        )
        session.commit()


def _prime_pending(tenant: dict[str, Any]) -> None:
    with SQLModelSession(tenant["engine"]) as session:
        session.add(
            DividendHistoryRefresh(
                user_id=tenant["user_id"],
                security_id=tenant["security_id"],
                state=DividendHistoryState.pending,
            )
        )
        session.commit()


# --- 1. The backoff arithmetic, pinned as pure behaviour ----------------------


def test_retry_age_doubles_per_failure_and_caps_at_six_hours() -> None:
    """15m flat → 30m → 1h → 2h → 4h → 6h cap; the cap is the bound."""
    assert dividend_refresh.retry_age(0) == timedelta(minutes=15)
    assert dividend_refresh.retry_age(1) == timedelta(minutes=30)
    assert dividend_refresh.retry_age(2) == timedelta(hours=1)
    assert dividend_refresh.retry_age(3) == timedelta(hours=2)
    assert dividend_refresh.retry_age(4) == timedelta(hours=4)
    assert dividend_refresh.retry_age(5) == timedelta(hours=6)  # 8h → capped
    assert dividend_refresh.retry_age(9) == dividend_refresh.MAX_RETRY_AGE
    assert dividend_refresh.retry_age(10_000) == dividend_refresh.MAX_RETRY_AGE
    # A never-failed / legacy NULL row keeps the flat cadence.
    assert dividend_refresh.retry_age(None) == timedelta(minutes=15)


def test_enqueue_due_honours_the_backoff_window(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 3-failure streak must NOT re-enqueue at 16 minutes — but does at 2h01."""
    now = datetime(2026, 10, 15, 12, 0, 0)
    monkeypatch.setattr(dividend_refresh, "_now", lambda: now)
    security_id = tenant["security_id"]
    with SQLModelSession(tenant["engine"]) as session:
        session.add(
            DividendHistoryRefresh(
                user_id=tenant["user_id"],
                security_id=security_id,
                state=DividendHistoryState.unavailable,
                error_code="provider_unavailable",
                consecutive_failures=3,  # backoff = 2h
                last_attempt_at=now - timedelta(minutes=16),
            )
        )
        session.commit()
        rows = dividend_refresh.enqueue_due(session, tenant["user_id"], {security_id})
    assert rows[security_id].state == DividendHistoryState.unavailable

    _set_state(tenant, last_attempt_at=now - timedelta(hours=2, minutes=1))
    with SQLModelSession(tenant["engine"]) as session:
        rows = dividend_refresh.enqueue_due(session, tenant["user_id"], {security_id})
    assert rows[security_id].state == DividendHistoryState.pending


def test_failure_streak_climbs_then_recovers_on_success(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two provider failures → streak 2; a good fetch → streak 0, state available."""
    security_id = tenant["security_id"]
    _prime_pending(tenant)

    def outage(symbol: str) -> None:
        market_data._last_quote_outcome.set(market_data.QuoteOutcome.unavailable)
        market_data._last_quote_reason.set("HTTP Error 503")
        return None

    monkeypatch.setattr(market_data, "fetch_dividend_history", outage)
    assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is False
    row = _refresh_row(tenant)
    assert row.consecutive_failures == 1
    assert row.error_code == "provider_unavailable"
    assert row.state == DividendHistoryState.unavailable

    _set_state(tenant, state=DividendHistoryState.pending)
    assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is False
    row = _refresh_row(tenant)
    assert row.consecutive_failures == 2

    # RECOVERY: the provider answers again. One success ends the streak — the
    # row returns to the flat 15-minute retry window, not the 1h backoff.
    def recovered(symbol: str) -> list[dict[str, object]]:
        market_data._last_quote_outcome.set(market_data.QuoteOutcome.ok)
        market_data._last_quote_reason.set(None)
        return [{"date": "2026-09-15", "amount": "0.30"}]

    monkeypatch.setattr(market_data, "fetch_dividend_history", recovered)
    _set_state(tenant, state=DividendHistoryState.pending)
    assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is True
    row = _refresh_row(tenant)
    assert row.consecutive_failures == 0
    assert row.error_code is None
    assert row.state == DividendHistoryState.available
    assert row.last_success_at is not None
    with SQLModelSession(tenant["engine"]) as session:
        events = session.exec(
            select(DividendHistory).where(DividendHistory.security_id == security_id)
        ).all()
    assert len(events) == 1 and events[0].amount == Decimal("0.30")


def test_a_recovered_row_is_due_at_the_flat_window_not_the_backoff(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The healed row re-queues after RETRY_AGE (16 min); a streak-2 row at the
    same age would wait an hour. The backoff only ever DELAYS a retry — the
    24h freshness gate still owns the happy path."""
    now = datetime(2026, 10, 15, 12, 0, 0)
    monkeypatch.setattr(dividend_refresh, "_now", lambda: now)
    security_id = tenant["security_id"]
    _prime_pending(tenant)
    _set_state(
        tenant,
        state=DividendHistoryState.available,
        consecutive_failures=0,
        last_success_at=now - timedelta(days=2),
        last_attempt_at=now - timedelta(minutes=16),
    )
    with SQLModelSession(tenant["engine"]) as session:
        rows = dividend_refresh.enqueue_due(session, tenant["user_id"], {security_id})
    assert rows[security_id].state == DividendHistoryState.pending


def test_empty_success_also_clears_the_streak(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quote-verified empty is a SUCCESS (docs/07) — it resets the backoff."""
    security_id = tenant["security_id"]
    _prime_pending(tenant)
    _set_state(tenant, consecutive_failures=4, error_code="provider_unavailable")

    monkeypatch.setattr(market_data, "fetch_dividend_history", lambda symbol: [])
    assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is True
    row = _refresh_row(tenant)
    assert row.state == DividendHistoryState.empty
    assert row.consecutive_failures == 0
    assert row.error_code is None


# --- 3. Quote-miss observability ----------------------------------------------


def test_hard_quote_miss_books_quote_not_found_and_counts_the_streak(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Yahoo 404 is `quote_not_found`, not `provider_unavailable` — and it
    still preserves history (the baseline 4887221 contract) while advancing
    the security's M141 verdict."""
    security_id = tenant["security_id"]
    _prime_pending(tenant)
    with SQLModelSession(tenant["engine"]) as session:
        session.add(
            DividendHistory(
                user_id=tenant["user_id"],
                security_id=security_id,
                ex_date=date(2026, 1, 15),
                amount=Decimal("0.25"),
            )
        )
        session.commit()

    def quote_not_found(symbol: str) -> None:
        market_data._last_quote_outcome.set(market_data.QuoteOutcome.not_found)
        market_data._last_quote_reason.set("Quote not found for symbol: M186A")
        return None

    monkeypatch.setattr(market_data, "fetch_dividend_history", quote_not_found)
    assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is False

    row = _refresh_row(tenant)
    assert row.error_code == "quote_not_found"
    assert row.state == DividendHistoryState.unavailable
    assert row.consecutive_failures == 1
    with SQLModelSession(tenant["engine"]) as session:
        security = session.get(Security, security_id)
        events = session.exec(
            select(DividendHistory).where(DividendHistory.security_id == security_id)
        ).all()
        assert security is not None and security.quote_miss_count == 1
        assert len(events) == 1 and events[0].amount == Decimal("0.25")


def test_invalid_payload_keeps_its_own_error_code(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed body is `invalid_provider_payload` — the quote-miss override
    must not swallow it (the fetch SUCCEEDED; only the payload was bad)."""
    security_id = tenant["security_id"]
    _prime_pending(tenant)
    monkeypatch.setattr(
        market_data,
        "fetch_dividend_history",
        lambda symbol: [{"date": "2026-01-15", "amount": "NaN"}],
    )
    assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is False
    row = _refresh_row(tenant)
    assert row.error_code == "invalid_provider_payload"
    assert row.consecutive_failures == 1


def test_dead_symbol_rows_back_off_too(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flagged row still climbs the streak (it is re-queued and re-refused),
    so the worker stops poking it every 15 minutes."""
    security_id = tenant["security_id"]
    _prime_pending(tenant)
    with SQLModelSession(tenant["engine"]) as session:
        security = session.get(Security, security_id)
        assert security is not None
        security.dead_symbol = True
        session.add(security)
        session.commit()

    assert dividend_refresh.refresh_one(tenant["user_id"], security_id) is False
    row = _refresh_row(tenant)
    assert row.error_code == "dead_symbol"
    assert row.consecutive_failures == 1


def _register(client: TestClient) -> tuple[dict[str, str], int]:
    email = f"m186-{time.time_ns()}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    me = client.get(f"{API}/users/me", headers=headers).json()
    return headers, me["id"]


def _open_holding(
    client: TestClient, session: SQLModelSession, user_id: int, headers: dict[str, str], symbol: str
) -> int:
    """An investment account + security + buy, committed through the API."""
    account = session.exec(
        select(Account).where(
            Account.user_id == user_id, Account.type == AccountType.brokerage_non_registered
        )
    ).first()
    if account is None:
        account = Account(
            user_id=user_id,
            name=f"M186 {symbol}",
            type=AccountType.brokerage_non_registered,
            currency="CAD",
            taxable=True,
        )
        session.add(account)
        session.commit()
        session.refresh(account)
    assert account is not None and account.id is not None
    sec = client.post(
        f"{API}/securities",
        json={
            "symbol": symbol,
            "name": f"{symbol} Fund",
            "kind": "etf",
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
        headers=headers,
    )
    assert sec.status_code == 201, sec.text
    security_id = sec.json()["id"]
    trade = client.post(
        f"{API}/trades",
        json={
            "account_id": account.id,
            "security_id": security_id,
            "action": "buy",
            "date": "2026-01-02",
            "quantity": "10",
            "price_native": "50",
        },
        headers=headers,
    )
    assert trade.status_code in (200, 201), trade.text
    return security_id


def test_header_counts_quote_misses_separately_from_transient_failures(
    client: TestClient, session: SQLModelSession
) -> None:
    """X-Dividend-History-State.quote_miss = quote_not_found + dead_symbol rows;
    the generic unavailable count keeps every unavailable row (additive key)."""
    headers, user_id = _register(client)
    security_id = _open_holding(client, session, user_id, headers, "M186H")
    security2_id = _open_holding(client, session, user_id, headers, "M186I")

    session.add(
        DividendHistoryRefresh(
            user_id=user_id,
            security_id=security_id,
            state=DividendHistoryState.unavailable,
            error_code="quote_not_found",
            # Recent attempt + success keep `enqueue_due` from re-queueing the
            # row to `pending` before the handler counts it (backoff + the
            # 24h freshness gate both say "not due").
            last_attempt_at=datetime.now(UTC).replace(tzinfo=None),
            last_success_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    session.add(
        DividendHistoryRefresh(
            user_id=user_id,
            security_id=security2_id,
            state=DividendHistoryState.unavailable,
            error_code="provider_unavailable",
            last_attempt_at=datetime.now(UTC).replace(tzinfo=None),
            last_success_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    session.commit()

    response = client.get(f"{API}/trades/dividend-suggestions", headers=headers)
    assert response.status_code == 200
    state = json.loads(response.headers["X-Dividend-History-State"])
    assert state["unavailable"] == 2
    assert state["quote_miss"] == 1


def test_header_zero_state_includes_quote_miss(client: TestClient) -> None:
    """The no-open-positions early return carries the additive key too."""
    headers, _user_id = _register(client)
    response = client.get(f"{API}/trades/dividend-suggestions", headers=headers)
    assert response.status_code == 200
    state = json.loads(response.headers["X-Dividend-History-State"])
    # Both additive waves of the header ride on the early return: M186's
    # `quote_miss` and docs/07 §Dividend-view provenance's `missing` /
    # `source` / `age_days` — same key set as the populated path, all zeros.
    assert state == {
        "pending": 0,
        "unavailable": 0,
        "empty": 0,
        "stale": 0,
        "quote_miss": 0,
        "missing": 0,
        "source": "yfinance",
        "as_of": None,
        "age_days": None,
    }

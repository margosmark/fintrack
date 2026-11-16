"""M150 — the rate book keeps itself current, and says when it cannot (docs/07).

The defect this pins: `fx.fx_rate_cad` is cache-only and the ≤7-day
carry-forward (M115) means a book that stopped being fetched still *resolves*
every date — Wednesday's USD cash leg reads Tuesday's rate, nothing errors and
nothing logs. The gap re-prices silently. Two halves close it, and both are
asserted here:

- **the scheduled refresh** (`services.fx_refresh`) fetches the curated pairs
  when the cache is behind, does NOTHING when it is not (a current book costs
  zero network calls), fails soft per pair, and sends nothing at all while a
  privacy-mode user exists (docs/11);
- **`GET /fx/status`** reports the book's age for the currencies this tenant
  actually holds — cache-only, so a page load can never become a Valet call.

M151 (the follow-up this file's second half exposed): the curated set must
contain only bases BoC actually publishes a series for, or `pairs_behind`
reports an unfetchable pair as behind forever and the tick spends a call and
a warning on a 404 every pass. DKK is the worked case — the pins live at the
bottom of this file.

Every network seam is monkeypatched: no test in this file may reach Valet.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select
from test_tax_acb import _user, add_trade, make_security

import app.services.fx as fx_service
from app.core.config import get_settings
from app.models.accounts import Account, AccountType
from app.models.fx_rates import FxRate, FxSource
from app.models.trades import TradeAction
from app.models.users import User
from app.services import fx_refresh, fx_status

D = Decimal


def _today() -> date:
    """Derived at call time — never a literal (AGENTS §6 date-lock trap)."""
    return datetime.now(UTC).date()


def _seed_rate(
    session: Session,
    base: str,
    day: date,
    rate: str = "1.3700",
    source: FxSource = FxSource.boc_valet,
) -> None:
    session.add(FxRate(base=base, quote="CAD", date=day, rate=D(rate), source=source))
    session.commit()


def _account(session: Session, user: User, currency: str, name: str = "Main") -> Account:
    account = Account(
        user_id=user.id,
        name=name,
        type=AccountType.brokerage_non_registered,
        currency=currency,
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _auth(client: TestClient, session: Session) -> tuple[dict[str, str], User]:
    """Register + log in, and hand back the USER ROW behind the token.

    Seeded rows must belong to the AUTHENTICATED user — /fx/status is
    tenant-scoped, so seeding one user and asserting on another tenant's answer
    proves nothing.
    """
    email = f"m150-{datetime.now(UTC).timestamp()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    user = session.exec(select(User).where(User.email == email)).one()
    return {"Authorization": f"Bearer {body.json()['access_token']}"}, user


def _usd_trade(session: Session, user: User, currency: str = "USD") -> None:
    """One committed trade whose ROW currency is `currency` (M132)."""
    account = _account(session, user, "CAD")
    security = make_security(session, user, "MSFT", "USD")
    trade = add_trade(
        session, user, account, security, TradeAction.buy, _today().isoformat(), "1", "100.00"
    )
    trade.currency = currency
    session.add(trade)
    session.commit()


def _no_network(*_args: object, **_kwargs: object) -> dict:
    raise AssertionError("a cache-only path opened a Valet fetch")


# --- the scheduled refresh ----------------------------------------------------


def test_refresh_only_touches_pairs_the_cache_is_behind(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One fresh pair is left alone; every other curated pair is refreshed."""
    today = _today()
    _seed_rate(session, "USD", today)
    calls: list[tuple[str, str, date]] = []

    def fake_refresh(sess, base, quote, end_date, **_kwargs):
        calls.append((base, quote, end_date))
        return 2

    monkeypatch.setattr(fx_service, "refresh_pair", fake_refresh)
    stats = fx_refresh.run_once(session, today)

    assert stats.privacy_blocked is False
    assert [base for base, _quote, _end in calls] == stats.refreshed
    assert "USD" not in stats.refreshed, "a pair dated today is already current"
    assert set(stats.refreshed) | {"USD"} == set(fx_service.CURATED_FX_BASES)
    assert all(quote == "CAD" for _base, quote, _end in calls)
    assert all(end == today for _base, _quote, end in calls)
    assert stats.rows_added == 2 * len(calls)
    assert stats.failed == []


def test_min_age_window_is_respected(session: Session) -> None:
    """Yesterday's row is current (age 1 ≤ min age); two days old is behind."""
    today = _today()
    _seed_rate(session, "EUR", today - timedelta(days=1))
    _seed_rate(session, "GBP", today - timedelta(days=2))

    behind = fx_refresh.pairs_behind(session, today)

    assert "EUR" not in behind, "a one-day-old row is inside the min-age window"
    assert "GBP" in behind


def test_a_current_book_costs_zero_network_calls(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every curated pair cached today → the tick fetches nothing at all."""
    today = _today()
    for base in fx_service.CURATED_FX_BASES:
        _seed_rate(session, base, today)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a current book must not fetch")

    monkeypatch.setattr(fx_service, "refresh_pair", boom)
    stats = fx_refresh.run_once(session, today)

    assert stats.refreshed == []
    assert stats.failed == []
    assert stats.rows_added == 0
    assert stats.current == list(fx_service.CURATED_FX_BASES)


def test_one_pair_failing_does_not_stop_the_others(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ValetUnavailable is per-pair and soft (docs/07 fail-soft discipline)."""
    today = _today()

    def flaky(sess, base, quote, end_date, **_kwargs):
        if base == "USD":
            raise fx_service.ValetUnavailable("boom")
        return 1

    monkeypatch.setattr(fx_service, "refresh_pair", flaky)
    stats = fx_refresh.run_once(session, today)

    assert stats.failed == ["USD"]
    assert len(stats.refreshed) == len(fx_service.CURATED_FX_BASES) - 1


def test_privacy_mode_user_silences_the_whole_tick(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One privacy-mode user means zero packets leave the machine (docs/11)."""
    user = _user(session)
    _usd_trade(session, user)
    user.privacy_mode = True
    session.add(user)
    session.commit()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("privacy mode must block the fetch entirely")

    monkeypatch.setattr(fx_service, "refresh_pair", boom)
    stats = fx_refresh.run_once(session, _today())

    assert stats.privacy_blocked is True
    assert stats.refreshed == []
    assert stats.current == []
    assert stats.rows_added == 0


def test_refresh_pair_writes_empty_dates_only(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual gap-fill survives a refresh with its rate untouched (docs/07)."""
    today = _today()
    _seed_rate(session, "USD", today - timedelta(days=1), "9.9900", FxSource.manual)
    payload = {today - timedelta(days=1): D("1.3700"), today: D("1.3800")}

    monkeypatch.setattr(fx_service, "fetch_valet_series", lambda *_a, **_k: payload)
    added = fx_service.refresh_pair(session, "USD", "CAD", today)

    assert added == 1, "only the empty date is written"
    rows = {
        row.date: (row.rate, row.source)
        for row in session.exec(select(FxRate).where(FxRate.base == "USD")).all()
    }
    assert rows[today - timedelta(days=1)] == (D("9.9900"), FxSource.manual)
    assert rows[today] == (D("1.3800"), FxSource.boc_valet)


def test_worker_start_honours_the_settings_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """FX_REFRESH_ENABLED=false is a real off switch (the test suite relies on it).

    The enabled case runs inside a loop because `start_worker` calls
    `asyncio.create_task` — the same shape `lifespan` gives it in production.
    """

    async def round_trip() -> None:
        monkeypatch.setenv("FX_REFRESH_ENABLED", "true")
        get_settings.cache_clear()
        task = fx_refresh.start_worker()
        assert task is not None
        await fx_refresh.stop_worker(task)

    try:
        monkeypatch.setenv("FX_REFRESH_ENABLED", "false")
        get_settings.cache_clear()
        assert fx_refresh.start_worker() is None
        asyncio.run(round_trip())
    finally:
        get_settings.cache_clear()


# --- GET /fx/status -----------------------------------------------------------


def test_status_reports_the_pairs_that_price_this_tenant(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fx_service, "fetch_valet_series", _no_network)
    today = _today()
    headers, user = _auth(client, session)
    _usd_trade(session, user)
    _seed_rate(session, "USD", today)

    body = client.get("/api/v1/fx/status", headers=headers).json()

    assert body["reporting_currency"] == "CAD"
    assert [pair["base"] for pair in body["pairs"]] == ["USD"]
    usd = body["pairs"][0]
    assert usd["quote"] == "CAD"
    assert usd["as_of"] == today.isoformat()
    assert usd["age_days"] == 0
    assert usd["stale"] is False
    assert usd["source"] == "boc_valet"
    assert body["as_of"] == today.isoformat()
    assert body["age_days"] == 0
    assert body["stale"] is False


def test_status_unions_row_account_and_listing_currencies(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/07: the trade row's code, a pre-M132 row's account code, listings."""
    monkeypatch.setattr(fx_service, "fetch_valet_series", _no_network)
    headers, user = _auth(client, session)
    _account(session, user, "GBP")  # what a NULL-currency trade row reads in
    make_security(session, user, "RY", "CAD")  # the reporting currency: excluded
    _usd_trade(session, user)

    body = client.get("/api/v1/fx/status", headers=headers).json()

    assert [pair["base"] for pair in body["pairs"]] == ["GBP", "USD"]
    assert all(pair["stale"] is True for pair in body["pairs"])
    assert body["as_of"] is None and body["age_days"] is None
    assert body["stale"] is True, "a pair with no rate at all is stale by definition"


def test_status_stale_boundary_is_the_carry_forward_window(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7 days old is still carried forward (current); 8 is past the window.

    Seeded oldest-first: the stale answer is what the book said BEFORE the
    fresher row landed, which is exactly the state the chip exists to show.
    """
    monkeypatch.setattr(fx_service, "fetch_valet_series", _no_network)
    today = _today()
    headers, user = _auth(client, session)
    _usd_trade(session, user)
    _seed_rate(session, "USD", today - timedelta(days=8))

    body = client.get("/api/v1/fx/status", headers=headers).json()
    assert body["pairs"][0]["age_days"] == 8
    assert body["pairs"][0]["stale"] is True
    assert body["stale"] is True
    assert body["as_of"] == (today - timedelta(days=8)).isoformat()

    _seed_rate(session, "USD", today - timedelta(days=7))
    body = client.get("/api/v1/fx/status", headers=headers).json()
    assert body["pairs"][0]["age_days"] == 7
    assert body["pairs"][0]["stale"] is False
    assert body["stale"] is False


def test_status_reports_the_worst_covered_pair(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Top-level age describes the pair that decides whether numbers drift."""
    monkeypatch.setattr(fx_service, "fetch_valet_series", _no_network)
    today = _today()
    headers, user = _auth(client, session)
    _usd_trade(session, user)
    make_security(session, user, "RY", "EUR")
    _seed_rate(session, "USD", today)
    _seed_rate(session, "EUR", today - timedelta(days=3))

    body = client.get("/api/v1/fx/status", headers=headers).json()

    assert body["as_of"] == (today - timedelta(days=3)).isoformat()
    assert body["age_days"] == 3
    assert body["stale"] is False


def test_status_is_empty_when_no_foreign_currency_is_held(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No FX dependency → no pairs, not stale, and nothing for the chip."""
    monkeypatch.setattr(fx_service, "fetch_valet_series", _no_network)
    headers, user = _auth(client, session)
    account = _account(session, user, "CAD")
    security = make_security(session, user, "RY", "CAD")
    add_trade(
        session, user, account, security, TradeAction.buy, _today().isoformat(), "1", "100.00"
    )

    body = client.get("/api/v1/fx/status", headers=headers).json()

    assert body["pairs"] == []
    assert body["as_of"] is None and body["age_days"] is None
    assert body["stale"] is False


def test_status_never_fetches_and_needs_auth(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache-only (docs/12): a privacy-mode user gets the same 200, no 403."""
    monkeypatch.setattr(fx_service, "fetch_valet_series", _no_network)
    headers, user = _auth(client, session)
    _usd_trade(session, user)
    user.privacy_mode = True
    session.add(user)
    session.commit()

    assert client.get("/api/v1/fx/status").status_code in (401, 403)
    assert client.get("/api/v1/fx/status", headers=headers).status_code == 200


def test_currencies_in_use_is_tenant_scoped(session: Session) -> None:
    """Another tenant's listing currency is never this tenant's dependency."""
    mine = _user(session)
    _usd_trade(session, mine)
    other = User(email="m150-other@example.com", hashed_password="x")
    session.add(other)
    session.commit()
    session.refresh(other)
    make_security(session, other, "SHOP", "CAD")

    codes = fx_status.currencies_in_use(session, mine.id, "CAD")  # type: ignore[arg-type]

    assert codes == ["USD"]


# --- M151: a base BoC cannot price ------------------------------------------
#
# M150's first live tick found DKK in CURATED_FX_BASES while BoC publishes no
# FXDKKCAD series (404), so pairs_behind called it behind on EVERY pass and
# the refresh burned a call plus a WARNING on a series that can never answer —
# for a board row GET /fx/board could never render anyway. Fix = the
# membership rule (docs/07 §Curated board membership): the base leaves the
# constant, and because pairs_behind iterates that constant, "never fetched"
# needs no special case at all.


def test_the_tick_never_spends_a_call_on_a_pair_that_cannot_answer(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DKK is behind an empty cache and still not fetchable — because absent.

    The set is empty here (rollback-per-test), so every curated base is
    behind and every one of them is fetched. DKK being outside the constant
    is the whole fix: it appears in neither list, on any cache state.
    """
    today = _today()
    calls: list[str] = []

    def record(sess, base, quote, end_date, **_kwargs):
        calls.append(base)
        return 1

    monkeypatch.setattr(fx_service, "refresh_pair", record)

    behind = fx_refresh.pairs_behind(session, today)
    assert "DKK" not in behind, "a base with no BoC series must never be reported behind"
    assert behind == list(fx_service.CURATED_FX_BASES), "an empty cache: all 17 are behind"

    stats = fx_refresh.run_once(session, today)

    assert "DKK" not in calls, "the tick must not spend a call on a series that cannot answer"
    assert set(calls) == set(fx_service.CURATED_FX_BASES)
    assert "DKK" not in stats.failed
    assert "DKK" not in stats.current


def test_status_says_rates_missing_for_a_currency_the_board_cannot_price(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DKK-only tenant still gets its FX dependency reported (docs/07).

    DKK is off the curated board, so no tick will ever fill it — but the
    status read is driven by the currencies in USE, not by the board, and
    "nobody will ever fill this" is precisely when silence is worst. The
    answer is the documented worst state: one pair, no row, stale.
    """
    monkeypatch.setattr(fx_service, "fetch_valet_series", _no_network)
    headers, user = _auth(client, session)
    make_security(session, user, "NOVO-B", "DKK")  # a Copenhagen listing

    body = client.get("/api/v1/fx/status", headers=headers).json()

    assert [pair["base"] for pair in body["pairs"]] == ["DKK"]
    dkk = body["pairs"][0]
    assert dkk["quote"] == "CAD"
    assert dkk["as_of"] is None and dkk["age_days"] is None
    assert dkk["stale"] is True and dkk["source"] is None
    assert body["as_of"] is None and body["age_days"] is None
    assert body["stale"] is True, "a pair with no row at all is stale by definition"


def test_dkk_cash_legs_keep_working_through_the_pair_explicit_paths(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping DKK from the board never touched GET/POST /fx (docs/07).

    Those paths are pair-explicit and never read CURATED_FX_BASES: with no
    series behind DKK the lookup answers the documented `fx_unavailable`
    envelope instead of a 5xx, and a manual gap-fill makes the leg priceable
    again — which is the whole reason a DKK holder is not stuck.
    """
    today = _today()

    def no_series(*_args: object, **_kwargs: object) -> dict:
        raise fx_service.ValetUnavailable("404 Series FXDKKCAD not found")

    monkeypatch.setattr(fx_service, "fetch_valet_series", no_series)
    headers, _user_row = _auth(client, session)

    miss = client.get("/api/v1/fx", params={"base": "DKK", "quote": "CAD"}, headers=headers)
    assert miss.status_code == 404
    assert miss.json()["code"] == "fx_unavailable"

    made = client.post(
        "/api/v1/fx",
        json={
            "base": "DKK",
            "quote": "CAD",
            "date": today.isoformat(),
            "rate": "4.9000",
        },
        headers=headers,
    )
    assert made.status_code == 201
    assert made.json()["source"] == "manual"

    hit = client.get(
        "/api/v1/fx",
        params={"base": "DKK", "quote": "CAD", "date": today.isoformat()},
        headers=headers,
    )
    assert hit.status_code == 200
    assert D(hit.json()["rate"]) == D("4.9000")

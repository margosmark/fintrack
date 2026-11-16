"""Privacy-mode toggle validation + external-fetch lockdown (docs/11 steps 5-6).

Covers w6-contract BackendPrivacy pins 1, 2 and 4:
- PATCH /users/me rejects privacy_mode=true combined with llm_provider=openai_compatible
  (409 privacy_mode_requires_local_llm), both same-request and stored-provider.
- With privacy_mode on, every external seam answers
  403 {"code": "privacy_mode_blocks_external_api"}: BoC Valet fetches behind GET /fx
  (cache HITs still serve), yfinance lookup behind GET /securities/lookup, and the
  portfolio valuation fetch-on-miss seam. Manual entries and cached reads keep working.
"""

import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.fx_rates import FxRate, FxSource
from app.models.securities import Security

USERS_URL = "/api/v1/users/me"
FX_URL = "/api/v1/fx"


def _auth(client: TestClient) -> dict[str, str]:
    email = f"{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "secret123"})
    body = client.post("/api/v1/auth/login", json={"email": email, "password": "secret123"})
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _enable_privacy(client: TestClient, headers: dict[str, str]) -> None:
    response = client.patch(USERS_URL, json={"privacy_mode": True}, headers=headers)
    assert response.status_code == 200, response.text


def _forbid_network(monkeypatch: Any) -> None:
    """Any Valet HTTP call or real yfinance import is a test failure."""

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("External network attempted while privacy mode is on")

    monkeypatch.setattr("app.services.fx.requests.get", explode)

    # The double REFUSES instead of ANSWERING. `history` returning an empty
    # frame is not "no answer": docs/07 reads that shape as yfinance's hard
    # "quote not found" verdict, and three of them flag the row dead (M141) —
    # which THIS file books against its own XIC row from a single positions read
    # (creation backfill + the read itself + the market-hours background
    # refresh, the last only when the clock lands inside 09:30-20:00 ET). The
    # flag then has `ensure_recent_price` serve the cache, so the privacy seam
    # these assertions pin is never reached: 200 where they want 403. An
    # exception that is NOT a "ticker missing" one is `_classify_history_error`'s
    # NETWORK verdict (`unavailable`), which never advances the counter — the
    # double never asked Yahoo, so it must never vote on whether XIC exists.
    def unreachable(**kwargs: Any) -> Any:
        raise TimeoutError("yfinance double: provider unreachable in this test")

    fake_yf = SimpleNamespace(Ticker=lambda symbol: SimpleNamespace(info={}, history=unreachable))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    # `_yf_session` is a process-wide lru_cache over the REAL `yfinance.data`,
    # and a COLD one raises (`import yfinance.data` against the fake module
    # above) BEFORE `Ticker.history` is ever called. So the single-file run
    # answered `unavailable` while the full suite — an earlier test having
    # warmed the cache — reached the double and answered `not_found`. That is
    # the whole reason this file was green alone and red in a full run at an
    # open-market clock: pin the cache off so both runs exercise one seam.
    monkeypatch.setattr("app.services.market_data._yf_session", lambda: None)
    # The refusal costs MAX_ATTEMPTS x backoff per candidate, and waiting on a
    # double that is scripted to refuse is pure latency.
    monkeypatch.setattr("app.services.market_data.MAX_ATTEMPTS", 1)
    # M142: the name→ticker SEARCH fallback (docs/07) is this module's other
    # external seam. Pinned to "no hit" so a lookup degrades to 404 here
    # instead of asking Yahoo — what these tests pin is the privacy envelope
    # versus graceful degradation, never Yahoo's answer.
    monkeypatch.setattr("app.services.market_data._search_equity_quotes", lambda query: [])


def _weekday_rates(session: Session, start_date: date, end_date: date) -> list[date]:
    days = []
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() < 5:
            session.add(
                FxRate(
                    base="EUR",
                    quote="CAD",
                    rate=Decimal("1.47"),
                    date=cursor,
                    source=FxSource.manual,
                )
            )
            days.append(cursor)
        cursor += timedelta(days=1)
    session.commit()
    return days


# --- docs/11 step 5: PATCH /users/me validation -------------------------------


def test_patch_rejects_privacy_mode_with_cloud_provider_in_same_request(client: TestClient) -> None:
    headers = _auth(client)
    response = client.patch(
        USERS_URL, json={"privacy_mode": True, "llm_provider": "openai_compatible"}, headers=headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "privacy_mode_requires_local_llm"
    me = client.get(USERS_URL, headers=headers).json()
    assert me["privacy_mode"] is False  # conflicting patch mutates nothing
    assert me["llm_provider"] == "ollama"


def test_patch_rejects_cloud_provider_while_privacy_already_on(client: TestClient) -> None:
    headers = _auth(client)
    _enable_privacy(client, headers)
    response = client.patch(USERS_URL, json={"llm_provider": "openai_compatible"}, headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "privacy_mode_requires_local_llm"
    me = client.get(USERS_URL, headers=headers).json()
    assert me["llm_provider"] == "ollama"


def test_patch_allows_privacy_mode_with_ollama_then_disabling_restores_cloud(
    client: TestClient,
) -> None:
    headers = _auth(client)
    response = client.patch(
        USERS_URL, json={"privacy_mode": True, "llm_provider": "ollama"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["privacy_mode"] is True
    response = client.patch(
        USERS_URL,
        json={"privacy_mode": False, "llm_provider": "openai_compatible"},
        headers=headers,
    )
    assert response.status_code == 200
    me = client.get(USERS_URL, headers=headers).json()
    assert me["privacy_mode"] is False
    assert me["llm_provider"] == "openai_compatible"


# --- docs/11 step 6: fx seam (Valet blocked, cache + manual usable) -----------


def test_fx_cache_hit_served_without_fetch_under_privacy(
    client: TestClient, session: Session, monkeypatch: Any
) -> None:
    _forbid_network(monkeypatch)
    today = datetime.now(UTC).date()
    session.add(
        FxRate(base="USD", quote="CAD", rate=Decimal("1.25"), date=today, source=FxSource.manual)
    )
    session.commit()

    headers = _auth(client)
    _enable_privacy(client, headers)

    response = client.get(
        FX_URL, params={"base": "USD", "quote": "CAD", "date": today.isoformat()}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "manual"
    assert Decimal(str(body["rate"])) == Decimal("1.25")


def test_fx_cache_miss_returns_403_envelope_under_privacy(
    client: TestClient, monkeypatch: Any
) -> None:
    _forbid_network(monkeypatch)
    headers = _auth(client)
    _enable_privacy(client, headers)

    response = client.get(FX_URL, params={"base": "EUR", "quote": "CAD"}, headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "privacy_mode_blocks_external_api"


def test_fx_range_fully_cached_served_under_privacy(
    client: TestClient, session: Session, monkeypatch: Any
) -> None:
    _forbid_network(monkeypatch)
    monday = datetime.now(UTC).date() - timedelta(days=datetime.now(UTC).date().weekday())
    _weekday_rates(session, monday, monday + timedelta(days=4))  # Mon..Fri seeded

    headers = _auth(client)
    _enable_privacy(client, headers)

    response = client.get(
        FX_URL,
        params={
            "base": "EUR",
            "quote": "CAD",
            "start_date": monday.isoformat(),
            "end_date": (monday + timedelta(days=4)).isoformat(),
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 5


def test_fx_range_that_would_fetch_blocked_under_privacy(
    client: TestClient, session: Session, monkeypatch: Any
) -> None:
    _forbid_network(monkeypatch)
    monday = datetime.now(UTC).date() - timedelta(days=datetime.now(UTC).date().weekday())
    session.add(
        FxRate(base="EUR", quote="CAD", rate=Decimal("1.47"), date=monday, source=FxSource.manual)
    )
    session.commit()  # only Monday covered; Tue-Fri would require a fetch

    headers = _auth(client)
    _enable_privacy(client, headers)

    response = client.get(
        FX_URL,
        params={
            "base": "EUR",
            "quote": "CAD",
            "start_date": monday.isoformat(),
            "end_date": (monday + timedelta(days=4)).isoformat(),
        },
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "privacy_mode_blocks_external_api"


def test_manual_rate_entry_still_works_under_privacy(client: TestClient) -> None:
    headers = _auth(client)
    _enable_privacy(client, headers)

    response = client.post(
        FX_URL,
        json={
            "base": "EUR",
            "quote": "CAD",
            "date": datetime.now(UTC).date().isoformat(),
            "rate": "1.47",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["source"] == "manual"


# --- docs/11 step 6: securities lookup seam -----------------------------------


def test_securities_lookup_blocked_under_privacy(client: TestClient, monkeypatch: Any) -> None:
    _forbid_network(monkeypatch)
    headers = _auth(client)
    _enable_privacy(client, headers)

    response = client.get("/api/v1/securities/lookup", params={"symbol": "AAPL"}, headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "privacy_mode_blocks_external_api"


def test_securities_lookup_unaffected_without_privacy(client: TestClient, monkeypatch: Any) -> None:
    _forbid_network(monkeypatch)
    headers = _auth(client)

    response = client.get("/api/v1/securities/lookup", params={"symbol": "AAPL"}, headers=headers)
    # The fake yfinance returns an empty payload -> graceful degradation, NOT the
    # privacy envelope: blocking is strictly tied to privacy_mode.
    assert response.status_code == 404
    assert response.json()["code"] == "lookup_unavailable"


# --- docs/11 step 6: valuation fetch-on-miss seam -----------------------------


def _security_with_price(
    client: TestClient, headers: dict[str, str], age_days: int
) -> tuple[int, date]:
    today = datetime.now(UTC).date()
    security = client.post(
        "/api/v1/securities",
        json={
            "symbol": "XIC",
            "name": "iShares Core S&P/TSX Capped Composite",
            "kind": "etf",
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
        headers=headers,
    )
    assert security.status_code == 201, security.text
    price_day = today - timedelta(days=age_days)
    price = client.post(
        "/api/v1/prices",
        json={"security_id": security.json()["id"], "date": price_day.isoformat(), "price": "30"},
        headers=headers,
    )
    assert price.status_code in (200, 201), price.text
    return security.json()["id"], price_day


def _buy_position(
    client: TestClient, headers: dict[str, str], account_id: int, security_id: int
) -> None:
    trade = client.post(
        "/api/v1/trades",
        json={
            "account_id": account_id,
            "security_id": security_id,
            "action": "buy",
            "date": datetime.now(UTC).date().isoformat(),
            "quantity": "10",
            "price_native": "30",
        },
        headers=headers,
    )
    assert trade.status_code in (200, 201), trade.text


def _account(client: TestClient, headers: dict[str, str]) -> int:
    institution = client.post("/api/v1/institutions", json={"name": "Questrade"}, headers=headers)
    assert institution.status_code == 201, institution.text
    account = client.post(
        "/api/v1/accounts",
        json={
            "institution_id": institution.json()["id"],
            "name": "TFSA",
            "type": "tfsa",
            "currency": "CAD",
        },
        headers=headers,
    )
    assert account.status_code == 201, account.text
    return account.json()["id"]


def test_valuation_stale_price_fetch_blocked_under_privacy(
    client: TestClient, session: Session, monkeypatch: Any
) -> None:
    _forbid_network(monkeypatch)
    headers = _auth(client)
    security_id, _ = _security_with_price(client, headers, age_days=10)
    _buy_position(client, headers, _account(client, headers), security_id)

    fresh = client.get("/api/v1/portfolio/positions", headers=headers)
    assert fresh.status_code == 200  # without privacy mode the stale fetch just degrades

    # Valuation is cache-only regardless of freshness; privacy mode must not
    # turn an otherwise valid stale-cache read into a provider exception.
    row = session.get(Security, security_id)
    assert row is not None
    assert (row.quote_miss_count, row.dead_symbol) == (0, False)

    _enable_privacy(client, headers)
    locked = client.get("/api/v1/portfolio/positions", headers=headers)
    assert locked.status_code == 200
    position = locked.json()["items"][0]
    assert position["price_stale"] is True
    assert position["price_age_days"] >= 10


def test_valuation_fresh_cached_price_served_under_privacy(
    client: TestClient, monkeypatch: Any
) -> None:
    _forbid_network(monkeypatch)
    headers = _auth(client)
    security_id, _ = _security_with_price(client, headers, age_days=0)
    _buy_position(client, headers, _account(client, headers), security_id)

    _enable_privacy(client, headers)
    response = client.get("/api/v1/portfolio/positions", headers=headers)
    assert response.status_code == 200, response.text
    position = response.json()["items"][0]
    assert position["price_source"] == "manual"
    assert position["price_stale"] is False

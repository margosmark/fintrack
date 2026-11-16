"""Fundamentals backend contract (M16, docs/07): guarded yfinance snapshot,
7-day cache window, manual-wins upsert, silent degradation under privacy
mode, and tenancy on every route.

Network edges are faked offline: fetch_fundamentals is replaced by a
recording fake (call counter drives the cache-window assertions), and one
unit test pins the dividendYield normalization through a fake yfinance
module instead of a live call.
"""

import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import app.services.market_data as market_data
from app.models.fundamentals import Fundamentals

API = "/api/v1"
SEC_URL = f"{API}/securities"
USERS_URL = f"{API}/users/me"

FAKE_FETCH = {
    "trailing_eps": Decimal("11.22"),
    "pe_ratio": Decimal("35.40"),
    "market_cap": Decimal("2500000000000"),
    "dividend_yield_pct": Decimal("0.55"),
    "beta": Decimal("1.29"),
    "sector": "Technology",
    "industry": "Consumer Electronics",
    "country": "United States",
    "fifty_two_week_high": Decimal("199.62"),
    "fifty_two_week_low": Decimal("164.08"),
}


@pytest.fixture(autouse=True)
def offline_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default no-ops keep tests offline (docs/07 network discipline)."""
    monkeypatch.setattr(market_data, "backfill_range", lambda *a, **k: None)
    monkeypatch.setattr(market_data, "ensure_recent_price", lambda *a, **k: None)


@pytest.fixture()
def fetch_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Controllable fetch seam recording every attempted symbol fetch."""
    calls: list[str] = []

    def fake_fetch(symbol: str) -> dict[str, object] | None:
        calls.append(symbol)
        return dict(FAKE_FETCH)

    monkeypatch.setattr(market_data, "fetch_fundamentals", fake_fetch)
    return calls


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m16-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _security(client: TestClient, headers: dict[str, str], symbol: str = "AAPL") -> dict:
    body = client.post(
        SEC_URL,
        json={
            "symbol": symbol,
            "name": "Apple Inc.",
            "kind": "stock",
            "listing_currency": "USD",
            "exchange": "NASDAQ",
        },
        headers=headers,
    )
    assert body.status_code == 201, body.text
    return body.json()


def _overview(client: TestClient, headers: dict[str, str], security_id: int) -> dict:
    body = client.get(f"{SEC_URL}/{security_id}/overview", headers=headers)
    assert body.status_code == 200, body.text
    return body.json()


# --- fetch_fundamentals unit behaviour ------------------------------------------


def test_dividend_yield_normalization_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass-through, pinned by live probe (yfinance 1.6.0 sends percents
    unscaled): AAPL's genuine 0.35% must NOT be scaled to 35."""
    infos = iter(
        [
            {"dividendYield": 0.35},  # live AAPL shape — sub-1% percent value
            {"dividendYield": 2.49},  # live RY.TO shape
            {},  # no yield at all — key simply absent
            {"unrelated": True},  # maps to nothing -> None
        ]
    )

    def make_fake_yf(info: dict) -> SimpleNamespace:
        return SimpleNamespace(Ticker=lambda symbol: SimpleNamespace(info=info))

    for expected in (Decimal("0.35"), Decimal("2.49"), None):
        info = next(infos)
        monkeypatch.setitem(sys.modules, "yfinance", make_fake_yf(info))
        data = market_data.fetch_fundamentals("aapl")
        got = (data or {}).get("dividend_yield_pct")
        assert got == expected, (info, got)


def test_fetch_degrades_to_none_on_yfinance_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        @property
        def info(self) -> dict:
            raise RuntimeError("yahoo down")

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=lambda s: Boom()))
    assert market_data.fetch_fundamentals("AAPL") is None


# --- GET /securities/{id}/overview ------------------------------------------------


def test_overview_includes_fundamentals_block(client: TestClient, fetch_log: list[str]) -> None:
    headers = _auth(client)
    security = _security(client, headers)

    body = _overview(client, headers, security["id"])
    block = body["fundamentals"]
    assert block is not None
    assert block["source"] == "yfinance"
    assert block["fetched_at"] == date.today().isoformat()
    # Decimal round-trip survives Numeric columns + string serialization.
    assert Decimal(block["trailing_eps"]) == Decimal("11.22")
    assert Decimal(block["pe_ratio"]) == Decimal("35.40")
    assert Decimal(block["market_cap"]) == Decimal("2500000000000")
    assert block["sector"] == "Technology"
    assert block["country"] == "United States"
    assert Decimal(block["fifty_two_week_high"]) == Decimal("199.62")
    assert fetch_log == ["AAPL"]


def test_fresh_cache_window_does_not_refetch(client: TestClient, fetch_log: list[str]) -> None:
    headers = _auth(client)
    security = _security(client, headers)

    _overview(client, headers, security["id"])
    _overview(client, headers, security["id"])
    assert len(fetch_log) == 1


def test_manual_row_survives_stale_refresh(
    client: TestClient, session: Session, fetch_log: list[str]
) -> None:
    headers = _auth(client)
    security = _security(client, headers)

    put = client.put(
        f"{SEC_URL}/{security['id']}/fundamentals",
        json={"sector": "My Manual Sector", "pe_ratio": "12.5", "source": "manual"},
        headers=headers,
    )
    assert put.status_code == 200, put.text
    assert put.json()["source"] == "manual"

    # Age the manual row past the 7-day window: refresh must NOT touch it.
    row = session.exec(select(Fundamentals)).one()
    row.fetched_at = date.today() - timedelta(days=30)
    session.add(row)
    session.commit()

    before = len(fetch_log)
    block = _overview(client, headers, security["id"])["fundamentals"]
    assert len(fetch_log) == before  # manual wins: no fetch attempted
    assert block["sector"] == "My Manual Sector"
    assert Decimal(block["pe_ratio"]) == Decimal("12.5")
    assert block["source"] == "manual"


# --- PUT /securities/{id}/fundamentals --------------------------------------------


def test_put_upsert_roundtrip_and_patch_semantics(client: TestClient) -> None:
    headers = _auth(client)
    security = _security(client, headers)
    url = f"{SEC_URL}/{security['id']}/fundamentals"

    first = client.put(
        url,
        json={"sector": "Healthcare", "beta": "0.8", "source": "manual"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["sector"] == "Healthcare"
    assert Decimal(body["beta"]) == Decimal("0.8")
    assert body["fetched_at"] == date.today().isoformat()

    # A second PUT touching only country keeps earlier manual fields intact.
    second = client.put(url, json={"country": "Canada", "source": "manual"}, headers=headers)
    assert second.status_code == 200, second.text
    merged = second.json()
    assert merged["sector"] == "Healthcare"
    assert Decimal(merged["beta"]) == Decimal("0.8")
    assert merged["country"] == "Canada"


def test_put_rejects_non_manual_source(client: TestClient) -> None:
    headers = _auth(client)
    security = _security(client, headers)
    body = client.put(
        f"{SEC_URL}/{security['id']}/fundamentals",
        json={"sector": "Tech", "source": "yfinance"},
        headers=headers,
    )
    assert body.status_code == 422, body.text


def test_put_foreign_security_404(client: TestClient) -> None:
    owner_headers = _auth(client)
    intruder_headers = _auth(client)
    security = _security(client, owner_headers)

    body = client.put(
        f"{SEC_URL}/{security['id']}/fundamentals",
        json={"sector": "Tech", "source": "manual"},
        headers=intruder_headers,
    )
    assert body.status_code == 404, body.text
    assert body.json()["code"] == "not_found"


# --- silent degradation ------------------------------------------------------------


def test_privacy_mode_degrades_overview_to_null_without_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Privacy lock raises from the refresh seam; the overview still serves."""

    def blocked(symbol: str) -> None:
        raise PermissionError("privacy_mode_blocks_external_api")

    monkeypatch.setattr(market_data, "fetch_fundamentals", blocked)
    headers = _auth(client)
    security = _security(client, headers)
    enabled = client.patch(USERS_URL, json={"privacy_mode": True}, headers=headers)
    assert enabled.status_code == 200, enabled.text

    body = _overview(client, headers, security["id"])
    assert body["fundamentals"] is None
    assert body["stats"] is not None  # rest of the payload unaffected


# --- POST /portfolio/refresh fan-out -----------------------------------------------


def test_refresh_queues_even_when_fundamentals_fetch_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(session, security, today):
        raise RuntimeError("network down")

    monkeypatch.setattr(market_data, "ensure_fundamentals", explode)
    headers = _auth(client)
    security = _security(client, headers, "MSFT")
    # Refresh fans out over held OR watched securities — watch it.
    watch = client.post(f"{API}/watchlist", json={"security_id": security["id"]}, headers=headers)
    assert watch.status_code in (200, 201), watch.text

    body = client.post(f"{API}/portfolio/refresh", headers=headers)
    assert body.status_code == 202, body.text
    # M141: `skipped_dead` names the rows the sweep did NOT queue (docs/07).
    assert body.json() == {"queued": 1, "skipped_dead": 0}

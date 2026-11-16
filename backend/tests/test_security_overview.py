"""Security dashboard backend contract (M13, docs/05): securities group in
GET /search + GET /securities/{id}/overview aggregation.

Network edges (yfinance) are monkeypatched — no external HTTP. An autouse
fixture replaces backfill_range/ensure_recent_price with recording fakes so
accidental test data never triggers real fetches.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

import app.services.market_data as market_data

API = "/api/v1"
SEC_URL = f"{API}/securities"
PRICES_URL = f"{API}/prices"


@pytest.fixture(autouse=True)
def fake_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default no-ops keep tests offline (docs/07 network discipline)."""

    def fake_backfill(session, security, start_date, end_date):
        return None

    def fake_ensure(session, security, today, **_kwargs):
        return None

    monkeypatch.setattr(market_data, "backfill_range", fake_backfill)
    monkeypatch.setattr(market_data, "ensure_recent_price", fake_ensure)


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m13-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _security(client: TestClient, headers: dict[str, str], symbol: str, name: str) -> dict:
    body = client.post(
        SEC_URL,
        json={
            "symbol": symbol,
            "name": name,
            "kind": "stock",
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
        headers=headers,
    )
    assert body.status_code == 201, body.text
    return body.json()


def _seed_prices(
    client: TestClient, headers: dict[str, str], security_id: int, prices: list
) -> None:
    """POST /prices is a manual upsert — deterministic cached closes."""
    for day_offset, price in prices:
        body = client.post(
            PRICES_URL,
            json={
                "security_id": security_id,
                "date": (date.today() - timedelta(days=day_offset)).isoformat(),
                "price": price,
            },
            headers=headers,
        )
        assert body.status_code == 201, body.text


# --- GET /search: securities group ---------------------------------------------


def test_search_finds_security_by_symbol_and_name(client: TestClient) -> None:
    headers = _auth(client)
    security = _security(client, headers, "NVDA", "NVIDIA Corporation")

    by_symbol = client.get(f"{API}/search", params={"q": "nvda"}, headers=headers).json()
    assert [hit["id"] for hit in by_symbol["securities"]] == [security["id"]]
    hit = by_symbol["securities"][0]
    assert hit["symbol"] == "NVDA"
    assert hit["name"] == "NVIDIA Corporation"
    assert hit["kind"] == "stock"

    by_name = client.get(f"{API}/search", params={"q": "nvidia"}, headers=headers).json()
    assert [s["id"] for s in by_name["securities"]] == [security["id"]]


def test_search_securities_tenant_scoped_and_missing_q(
    client: TestClient, session: Session
) -> None:
    owner_headers = _auth(client)
    attacker_headers = _auth(client)
    security = _security(client, owner_headers, "RY", "Royal Bank of Canada")

    hits = client.get(f"{API}/search", params={"q": "royal"}, headers=attacker_headers).json()
    assert all(s["id"] != security["id"] for s in hits["securities"])

    missing_q = client.get(f"{API}/search", headers=attacker_headers)
    assert missing_q.status_code == 422


# --- GET /securities/{id}/overview ---------------------------------------------


def test_overview_latest_stats_history_from_manual_prices(client: TestClient) -> None:
    headers = _auth(client)
    security = _security(client, headers, "SHOP", "Shopify")
    # Oldest first; newest (offset 0) must win as `latest`.
    _seed_prices(
        client,
        headers,
        security["id"],
        [(4, "100"), (3, "102"), (2, "98"), (1, "105"), (0, "110")],
    )

    body = client.get(f"{SEC_URL}/{security['id']}/overview", headers=headers).json()

    assert Decimal(body["latest"]["price"]) == Decimal("110")
    assert body["latest"]["age_days"] == 0
    assert body["latest"]["stale"] is False
    assert Decimal(body["stats"]["high_52w"]) == Decimal("110")
    assert Decimal(body["stats"]["low_52w"]) == Decimal("98")
    assert body["stats"]["cached_days"] == 5
    # First close in window 100 -> latest 110.
    assert body["stats"]["change_pct"] == 10.0

    history = body["history"]
    dates = [point["date"] for point in history]
    assert dates == sorted(dates)
    assert len(history) == 5
    assert Decimal(history[-1]["price"]) == Decimal("110")
    assert body["watchlisted"] is False


def test_overview_positions_totals_across_accounts(client: TestClient) -> None:
    headers = _auth(client)
    acct_a = client.post(
        f"{API}/accounts",
        json={"name": "Cash A", "type": "brokerage_non_registered", "currency": "CAD"},
        headers=headers,
    ).json()
    acct_b = client.post(
        f"{API}/accounts",
        json={"name": "Cash B", "type": "brokerage_non_registered", "currency": "CAD"},
        headers=headers,
    ).json()
    security = _security(client, headers, "XEQ", "iShares Core Equity")

    def trade(account_id: str, action: str, quantity: str) -> None:
        body = client.post(
            f"{API}/trades",
            json={
                "account_id": account_id,
                "security_id": security["id"],
                "action": action,
                "date": date.today().isoformat(),
                "quantity": quantity,
                "price_native": "50",
            },
            headers=headers,
        )
        assert body.status_code == 201, body.text

    trade(acct_a["id"], "buy", "2")
    trade(acct_b["id"], "buy", "3")
    trade(acct_b["id"], "sell", "1")  # partially closes B; still open

    body = client.get(f"{SEC_URL}/{security['id']}/overview", headers=headers).json()

    assert {row["account_id"] for row in body["positions"]} == {acct_a["id"], acct_b["id"]}
    assert Decimal(body["totals"]["quantity"]) == Decimal("4")


def test_overview_drops_fully_closed_account(client: TestClient) -> None:
    headers = _auth(client)
    acct_open = client.post(
        f"{API}/accounts",
        json={"name": "Open", "type": "brokerage_non_registered", "currency": "CAD"},
        headers=headers,
    ).json()
    acct_closed = client.post(
        f"{API}/accounts",
        json={"name": "Closed", "type": "brokerage_non_registered", "currency": "CAD"},
        headers=headers,
    ).json()
    security = _security(client, headers, "BAM", "Brookfield Asset Management")

    def trade(account_id: str, action: str, quantity: str) -> None:
        body = client.post(
            f"{API}/trades",
            json={
                "account_id": account_id,
                "security_id": security["id"],
                "action": action,
                "date": date.today().isoformat(),
                "quantity": quantity,
                "price_native": "40",
            },
            headers=headers,
        )
        assert body.status_code == 201, body.text

    trade(acct_open["id"], "buy", "5")
    trade(acct_closed["id"], "buy", "7")
    trade(acct_closed["id"], "sell", "7")  # fully closed -> compute_positions drops it

    body = client.get(f"{SEC_URL}/{security['id']}/overview", headers=headers).json()

    assert [row["account_id"] for row in body["positions"]] == [acct_open["id"]]
    assert Decimal(body["totals"]["quantity"]) == Decimal("5")


def test_overview_watchlisted_flag_roundtrip(client: TestClient) -> None:
    headers = _auth(client)
    security = _security(client, headers, "VFV", "Vanguard S&P 500 ETF")

    before = client.get(f"{SEC_URL}/{security['id']}/overview", headers=headers).json()
    assert before["watchlisted"] is False

    watched = client.post(f"{API}/watchlist", json={"security_id": security["id"]}, headers=headers)
    assert watched.status_code == 201, watched.text
    after_add = client.get(f"{SEC_URL}/{security['id']}/overview", headers=headers).json()
    assert after_add["watchlisted"] is True

    removed = client.delete(f"{API}/watchlist/{security['id']}", headers=headers)
    assert removed.status_code == 204
    after_remove = client.get(f"{SEC_URL}/{security['id']}/overview", headers=headers).json()
    assert after_remove["watchlisted"] is False


def test_overview_recent_trades_newest_first_capped_at_ten(client: TestClient) -> None:
    headers = _auth(client)
    account = client.post(
        f"{API}/accounts",
        json={"name": "FHSA", "type": "fhsa", "currency": "CAD"},
        headers=headers,
    ).json()
    security = _security(client, headers, "TD", "Toronto-Dominion Bank")

    trade_ids: list[int] = []
    for offset in range(12):
        body = client.post(
            f"{API}/trades",
            json={
                "account_id": account["id"],
                "security_id": security["id"],
                "action": "dividend",
                "date": (date.today() - timedelta(days=offset)).isoformat(),
                "quantity": "0",
                "price_native": str(1 + offset),
            },
            headers=headers,
        )
        assert body.status_code == 201, body.text
        trade_ids.append(body.json()["id"])

    body = client.get(f"{SEC_URL}/{security['id']}/overview", headers=headers).json()
    trades = body["recent_trades"]
    assert len(trades) == 10
    # offset 0 (newest date) was created first, so ids ascend with recency.
    assert [trade["id"] for trade in trades] == trade_ids[:10]
    assert trades[0]["action"] == "dividend"


def test_overview_foreign_security_404(client: TestClient) -> None:
    owner_headers = _auth(client)
    attacker_headers = _auth(client)
    security = _security(client, owner_headers, "ENB", "Enbridge")

    foreign = client.get(f"{SEC_URL}/{security['id']}/overview", headers=attacker_headers)
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "not_found"

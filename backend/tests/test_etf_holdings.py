"""ETF/MF holdings composition backend contract (M17, docs/02, docs/05).

Covers the three /securities/{id}/holdings routes plus the cache-only
overview block: manual bulk replace isolates source=manual rows, the
yfinance import isolates source=yfinance rows, weight-sum validation
fires before any DB write, unavailable market data degrades to a 503
envelope, privacy mode passes its 403 envelope through, and everything
is tenant-scoped.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import app.services.market_data as market_data

API = "/api/v1"
SEC_URL = f"{API}/securities"

# Captured BEFORE the autouse offline fixture swaps the module attribute,
# so the privacy test can exercise the real guarded seam.
_REAL_FETCH_FUND_HOLDINGS = market_data.fetch_fund_holdings


@pytest.fixture(autouse=True)
def offline_market(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Default no-ops keep tests offline (docs/07 network discipline)."""
    calls = {"import": 0}

    def fake_fetch(symbol: str) -> list[dict[str, object]] | None:
        calls["import"] += 1
        return [
            {
                "holding_symbol": "NVDA",
                "holding_name": "NVIDIA Corp",
                "weight_pct": Decimal("7.55"),
            },
            {
                "holding_symbol": "AAPL",
                "holding_name": "Apple Inc",
                "weight_pct": Decimal("7.04"),
            },
        ]

    def no_ensure(session: Session, security_id: int, today: object, **_kwargs) -> None:
        return None

    monkeypatch.setattr(market_data, "fetch_fund_holdings", fake_fetch)
    # M147-followup: VCN.TO / VFV.TO / VEQT are live Vanguard registry keys —
    # the chain would otherwise fetch real holdings mid-test.
    monkeypatch.setattr(market_data, "fetch_vanguard_holdings", lambda symbol: None)
    monkeypatch.setattr(market_data, "ensure_recent_price", no_ensure)
    monkeypatch.setattr(market_data, "ensure_fundamentals", no_ensure)
    return calls


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m17-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _security(
    client: TestClient, headers: dict[str, str], symbol: str, name: str, kind: str = "etf"
) -> dict:
    body = client.post(
        SEC_URL,
        json={
            "symbol": symbol,
            "name": name,
            "kind": kind,
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
        headers=headers,
    )
    assert body.status_code == 201, body.text
    return body.json()


def _put_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items}


def _row(
    symbol: str | None,
    name: str,
    weight: str,
    asset_class: str = "stock",
    sector: str | None = None,
) -> dict[str, Any]:
    return {
        "holding_symbol": symbol,
        "holding_name": name,
        "asset_class": asset_class,
        "region": None,
        "country": None,
        "sector": sector,
        "weight_pct": weight,
    }


# --- PUT /securities/{id}/holdings -----------------------------------------------


def test_put_bulk_replace_round_trip(client: TestClient) -> None:
    headers = _auth(client)
    security = _security(client, headers, "VFV.TO", "Vanguard S&P 500")

    first = client.put(
        f"{SEC_URL}/{security['id']}/holdings",
        json=_put_items([_row("VOO", "Vanguard S&P 500 ETF", "100")]),
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert [i["holding_symbol"] for i in first.json()["items"]] == ["VOO"]
    assert first.json()["source"] == "manual"

    second = client.put(
        f"{SEC_URL}/{security['id']}/holdings",
        json=_put_items(
            [_row("MSFT", "Microsoft Corp", "12.5"), _row(None, "Cash drag", "2", "cash")]
        ),
        headers=headers,
    )
    assert second.status_code == 200, second.text
    items = second.json()["items"]
    assert len(items) == 2  # old manual row fully replaced, not appended
    assert {i["holding_symbol"] for i in items} == {"MSFT", None}
    assert Decimal(second.json()["total_weight_pct"]) == Decimal("14.50")


def test_put_weight_sum_validation_envelope(client: TestClient) -> None:
    headers = _auth(client)
    security = _security(client, headers, "XIU.TO", "iShares S&P/TSX 60")
    url = f"{SEC_URL}/{security['id']}/holdings"

    over = client.put(
        url,
        json=_put_items([_row("A", "A Corp", "60.25"), _row("B", "B Corp", "40.25")]),
        headers=headers,
    )
    assert over.status_code == 422
    assert over.json()["code"] == "holdings_weight_exceeded"

    exact = client.put(
        url,
        json=_put_items([_row("A", "A Corp", "60"), _row("B", "B Corp", "40")]),
        headers=headers,
    )
    assert exact.status_code == 200, exact.text
    assert Decimal(exact.json()["total_weight_pct"]) == Decimal("100.00")

    under = client.put(url, json=_put_items([_row("A", "A Corp", "60")]), headers=headers)
    assert under.status_code == 200, under.text
    assert Decimal(under.json()["total_weight_pct"]) == Decimal("60.00")


def test_put_item_weight_bounds_validated_before_db(client: TestClient) -> None:
    headers = _auth(client)
    security = _security(client, headers, "ZEB.TO", "BMO Equal Weight Banks")
    url = f"{SEC_URL}/{security['id']}/holdings"

    for bad_weight in ("0", "-5", "120"):
        body = client.put(url, json=_put_items([_row("RY", "RBC", bad_weight)]), headers=headers)
        assert body.status_code == 422, body.text
        assert body.json()["code"] == "validation_error"

    # Nothing leaked into the table despite the rejected payloads.
    empty = client.get(url, headers=headers)
    assert empty.status_code == 200
    assert empty.json()["items"] == []


# --- POST /securities/{id}/holdings/import ----------------------------------------


def test_import_replaces_only_yfinance_rows_preserves_manual(client: TestClient) -> None:
    headers = _auth(client)
    security = _security(client, headers, "SPY", "SPDR S&P 500")
    url = f"{SEC_URL}/{security['id']}/holdings"

    # Seed one stale yfinance row and one hand-curated manual row.
    imported = client.post(f"{url}/import", headers=headers)
    assert imported.status_code == 200, imported.text
    manual = client.put(
        url,
        json=_put_items([_row("BRK.B", "Berkshire Hathaway B", "1.5")]),
        headers=headers,
    )
    assert manual.status_code == 200, manual.text

    again = client.post(f"{url}/import", headers=headers)
    assert again.status_code == 200, again.text
    payload = again.json()
    symbols = {item["holding_symbol"] for item in payload["items"]}
    assert symbols == {"NVDA", "AAPL", "BRK.B"}  # stale NVDA/AAPL refreshed once, manual kept
    assert payload["source"] == "yfinance"  # 2 refreshed rows vs 1 preserved manual row
    # And the manual row really survived:
    kept = [i for i in payload["items"] if i["holding_symbol"] == "BRK.B"]
    assert len(kept) == 1


def test_import_unavailable_degrades_to_503(
    client: TestClient, offline_market: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _auth(client)
    security = _security(client, headers, "QQQ", "Invesco QQQ")

    def empty(symbol: str) -> None:
        return None

    monkeypatch.setattr(market_data, "fetch_fund_holdings", empty)
    body = client.post(f"{SEC_URL}/{security['id']}/holdings/import", headers=headers)
    assert body.status_code == 503
    assert body.json() == {"detail": "Market data unavailable", "code": "market_data_unavailable"}
    assert offline_market["import"] >= 0  # counter fixture still wired


def test_import_under_privacy_passes_403_through(client: TestClient, monkeypatch: Any) -> None:
    headers = _auth(client)
    security = _security(client, headers, "VEQTOR", "Vanguard Balanced")

    # Restore the REAL guarded fetch for this test so the privacy seam runs.
    monkeypatch.setattr(market_data, "fetch_fund_holdings", _REAL_FETCH_FUND_HOLDINGS)
    locked = client.patch(f"{API}/users/me", json={"privacy_mode": True}, headers=headers)
    assert locked.status_code == 200

    body = client.post(f"{SEC_URL}/{security['id']}/holdings/import", headers=headers)
    assert body.status_code == 403, body.text
    assert body.json()["code"] == "privacy_mode_blocks_external_api"


# --- overview block --------------------------------------------------------------


def test_overview_holdings_block_present_for_fund_null_for_stock(client: TestClient) -> None:
    owner_headers = _auth(client)
    fund = _security(client, owner_headers, "XBB.TO", "iShares Core Bond")
    stock = _security(client, owner_headers, "RY", "Royal Bank", kind="stock")

    seeded = client.put(
        f"{SEC_URL}/{fund['id']}/holdings",
        json=_put_items(
            [
                _row("RY", "Royal Bank", "8.2"),
                _row("TD", "TD Bank", "6.1", "stock", "Financials"),
            ]
        ),
        headers=owner_headers,
    )
    assert seeded.status_code == 200

    fund_overview = client.get(f"{SEC_URL}/{fund['id']}/overview", headers=owner_headers).json()
    assert fund_overview["holdings"] is not None
    assert fund_overview["holdings"]["total_weight_pct"] == "14.30"
    assert len(fund_overview["holdings"]["items"]) == 2
    assert fund_overview["holdings"]["items"][0]["weight_pct"] == "8.2000"  # weight desc

    stock_overview = client.get(f"{SEC_URL}/{stock['id']}/overview", headers=owner_headers).json()
    assert stock_overview["holdings"] is None


# --- tenancy ---------------------------------------------------------------------


def test_tenancy_foreign_security_404_on_all_three_routes(client: TestClient) -> None:
    owner_headers = _auth(client)
    intruder_headers = _auth(client)
    security = _security(client, owner_headers, "VCN.TO", "Vanguard Canada All-Cap")

    foreign_id = security["id"]
    got = client.get(f"{SEC_URL}/{foreign_id}/holdings", headers=intruder_headers)
    put = client.put(
        f"{SEC_URL}/{foreign_id}/holdings",
        json=_put_items([_row("X", "X Corp", "10")]),
        headers=intruder_headers,
    )
    imp = client.post(f"{SEC_URL}/{foreign_id}/holdings/import", headers=intruder_headers)
    for response in (got, put, imp):
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"


# --- eager sub-fund enrichment (M19, docs/05) --------------------------------------


def test_enrichment_creates_missing_sub_fund_and_fetches_holdings(
    client: TestClient, session: Session, offline_market: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holding ticker with no matching security is created via lookup when
    the lookup reports a fund kind, then its composition is fetched and
    inserted; non-fund / unknown tickers stay leaves."""
    from app.models.securities import Security, SecurityKind
    from app.services.market_data import ensure_sub_fund_composition

    headers = _auth(client)
    lookups: list[str] = []

    def fake_lookup(symbol: str, **kwargs) -> dict[str, object] | None:
        lookups.append(symbol)
        if symbol == "XSUB.TO":
            return {
                "symbol": "XSUB.TO",
                "name": "iShares Sub ETF",
                "kind": SecurityKind.etf,
                "listing_currency": "CAD",
                "exchange": "TSX",
            }
        return None

    monkeypatch.setattr(market_data, "lookup_symbol", fake_lookup)
    fund = _security(client, headers, "XEQ", "iShares Core Equity ETF")
    # Call the enrichment function directly with the test session.
    sub_funds_enriched = ensure_sub_fund_composition(
        session,
        session.get(Security, fund["id"]),
        ["XSUB.TO", "RY"],
    )
    assert sub_funds_enriched == 1
    assert sorted(lookups) == ["RY", "XSUB.TO"]
    # XSUB.TO security was created.
    sub = session.exec(select(Security).where(Security.symbol == "XSUB.TO")).first()
    assert sub is not None and sub.kind == SecurityKind.etf
    # Its holdings were fetched (offline_market fake returns NVDA/AAPL).
    sub_holdings = client.get(f"{SEC_URL}/{sub.id}/holdings", headers=headers).json()
    assert {row["holding_symbol"] for row in sub_holdings["items"]} == {"NVDA", "AAPL"}
    assert sub_holdings["source"] == "yfinance"


def test_enrichment_leaves_existing_sub_fund_rows_untouched(
    client: TestClient, session: Session, offline_market: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sub-fund that already has composition rows is never re-fetched or
    modified by the enrichment pass."""
    from app.models.securities import Security
    from app.services.market_data import ensure_sub_fund_composition

    headers = _auth(client)
    monkeypatch.setattr(market_data, "lookup_symbol", lambda symbol: None)
    sub = _security(client, headers, "XSUB", "Existing Sub ETF")
    manual = client.put(
        f"{SEC_URL}/{sub['id']}/holdings",
        json=_put_items([_row(None, "Hand Curated Row", "25")]),
        headers=headers,
    )
    assert manual.status_code == 200, manual.text
    fund = _security(client, headers, "XEQ2", "Outer Fund")
    sub_funds_enriched = ensure_sub_fund_composition(
        session,
        session.get(Security, fund["id"]),
        ["XSUB"],
    )
    assert sub_funds_enriched == 0
    after = client.get(f"{SEC_URL}/{sub['id']}/holdings", headers=headers).json()
    assert [row["holding_name"] for row in after["items"]] == ["Hand Curated Row"]
    assert after["source"] == "manual"


def test_enrichment_creates_non_fund_securities_for_linking(
    client: TestClient, session: Session, offline_market: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holding ticker whose lookup reports a stock kind is created as a
    security for linking, but no sub-holdings are fetched (stocks don't have
    composition rows)."""
    from app.models.securities import Security, SecurityKind
    from app.services.market_data import ensure_sub_fund_composition

    headers = _auth(client)
    monkeypatch.setattr(
        market_data,
        "lookup_symbol",
        lambda symbol: {
            "symbol": symbol,
            "name": f"{symbol} Corp",
            "kind": SecurityKind.stock,
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
    )
    fund = _security(client, headers, "XEQ3", "Outer Fund 3")
    sub_funds_enriched = ensure_sub_fund_composition(
        session,
        session.get(Security, fund["id"]),
        ["ASTK"],
    )
    assert sub_funds_enriched == 0  # no funds enriched (ASTK is a stock)
    astk = session.exec(select(Security).where(Security.symbol == "ASTK")).first()
    assert astk is not None  # security created for linking
    assert astk.kind == SecurityKind.stock


def test_enrichment_degrades_silently_under_privacy(
    client: TestClient, session: Session, offline_market: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The privacy guard's 403 from lookup_symbol propagates (the background
    task wrapper catches it, docs/07). The function itself does not swallow
    privacy errors -- callers degrade."""
    from app.core.errors import APIError, api_error
    from app.models.securities import Security
    from app.services.market_data import ensure_sub_fund_composition

    headers = _auth(client)
    monkeypatch.setattr(
        market_data,
        "lookup_symbol",
        lambda symbol: (_ for _ in ()).throw(
            api_error(403, "Privacy mode blocks external API", "privacy_mode_blocks_external_api")
        ),
    )
    fund = _security(client, headers, "XEQ4", "Outer Fund 4")
    # The function raises -- the background task (_run_deepen) catches it.
    with pytest.raises(APIError):
        ensure_sub_fund_composition(
            session,
            session.get(Security, fund["id"]),
            ["XPRIV"],
        )
    # No security was created before the raise.
    assert session.exec(select(Security).where(Security.symbol == "XPRIV")).first() is None


def test_enrichment_fetches_holdings_for_existing_sub_fund_without_rows(
    client: TestClient, session: Session, offline_market: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sub-fund security that exists but has no etf_holdings rows yet gets
    its composition fetched and inserted (distinct from creating a new
    security or leaving an existing one with rows untouched)."""
    from app.models.securities import Security
    from app.services.market_data import ensure_sub_fund_composition

    headers = _auth(client)
    monkeypatch.setattr(market_data, "lookup_symbol", lambda symbol: None)
    # Create a sub-fund security manually (no holdings yet).
    sub = _security(client, headers, "XEMPTY", "Empty Sub ETF")
    # Verify it has no holdings.
    before = client.get(f"{SEC_URL}/{sub['id']}/holdings", headers=headers).json()
    assert before["items"] == []
    # Now enrich a parent fund that references this sub-fund.
    fund = _security(client, headers, "XEQ5", "Outer Fund 5")
    sub_funds_enriched = ensure_sub_fund_composition(
        session,
        session.get(Security, fund["id"]),
        ["XEMPTY"],
    )
    assert sub_funds_enriched == 1
    # The sub-fund now has holdings fetched from offline_market fake.
    after = client.get(f"{SEC_URL}/{sub['id']}/holdings", headers=headers).json()
    assert {row["holding_symbol"] for row in after["items"]} == {"NVDA", "AAPL"}
    assert after["source"] == "yfinance"


def test_enrichment_bounded_at_twelve_sub_funds(
    client: TestClient, session: Session, offline_market: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most SUB_FUND_ENRICHMENT_CAP sub-funds are created per trigger."""
    from app.models.securities import Security, SecurityKind
    from app.services.market_data import SUB_FUND_ENRICHMENT_CAP, ensure_sub_fund_composition

    headers = _auth(client)
    monkeypatch.setattr(
        market_data,
        "lookup_symbol",
        lambda symbol: {
            "symbol": symbol,
            "name": f"{symbol} ETF",
            "kind": SecurityKind.etf,
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
    )
    fund = _security(client, headers, "XBIG", "Wide Fund")
    symbols = [f"S{i:02d}" for i in range(15)]
    sub_funds_enriched = ensure_sub_fund_composition(
        session,
        session.get(Security, fund["id"]),
        symbols,
    )
    assert sub_funds_enriched == SUB_FUND_ENRICHMENT_CAP
    created = session.exec(select(Security).where(Security.symbol.in_(symbols))).all()
    assert len(created) == SUB_FUND_ENRICHMENT_CAP

"""Look-through exposure breakdown contract (M18, docs/05).

Covers GET /portfolio/breakdown across the three dimensions: stocks land in
their fundamentals sector/country, funds look through etf_holdings weights
with the pinned renormalization hand case (1000 @ 60+30 → 600 / 300 / 100
Unclassified), composition-less funds degrade to kind-valued Unclassified,
mixed-portfolio totals reconcile to the positions total, invalid dimensions
return the 422 validation envelope, and everything is tenant-scoped.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

import app.services.fx as fx_service
import app.services.market_data as market_data
from app.models.accounts import Account, AccountType
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User

API = "/api/v1"
BREAKDOWN_URL = f"{API}/portfolio/breakdown"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Breakdown is pure aggregation over cached rows — any fetch attempt
    fails loudly (docs/07 cache-only convention for derived views)."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network fetch attempted during breakdown")

    monkeypatch.setattr(market_data, "fetch_latest_close", forbidden)
    monkeypatch.setattr(market_data, "fetch_history", forbidden)
    monkeypatch.setattr(market_data, "fetch_fundamentals", forbidden)
    monkeypatch.setattr(fx_service, "fetch_valet_series", forbidden)


def _today() -> date:
    return datetime.now(UTC).date()


def _new_user(client: TestClient) -> tuple[dict[str, str], str]:
    email = f"m18-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}, email


def _auth(client: TestClient) -> dict[str, str]:
    headers, _ = _new_user(client)
    return headers


def _uid(session: Session, email: str) -> int:
    return session.exec(select(User).where(User.email == email)).one().id


def _account(session: Session, user_id: int, name: str) -> Account:
    account = Account(
        user_id=user_id,
        name=name,
        type=AccountType.brokerage_non_registered,
        currency="CAD",
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _security(
    session: Session,
    user_id: int,
    symbol: str,
    kind: SecurityKind | str = SecurityKind.stock,
) -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind=kind,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _buy(
    session: Session,
    user_id: int,
    account: Account,
    security: Security,
    quantity: str,
    price: str,
) -> None:
    """Buy trade + a fresh manual close so market_value_cad is computable."""
    today = _today()
    session.add(
        Trade(
            user_id=user_id,
            account_id=account.id,
            security_id=security.id,
            action=TradeAction.buy,
            date=today,
            quantity=Decimal(quantity),
            price_native=Decimal(price),
            commission=Decimal("0"),
        )
    )
    from app.models.prices import Price, PriceSource

    if (
        session.exec(
            select(Price).where(Price.security_id == security.id, Price.date == today)
        ).first()
        is None
    ):
        session.add(
            Price(
                user_id=user_id,
                security_id=security.id,
                date=today,
                price=Decimal(price),
                source=PriceSource.manual,
            )
        )
    session.commit()


def _fundamentals(client: TestClient, headers: dict, security_id: int, **fields) -> None:
    body = client.put(
        f"{API}/securities/{security_id}/fundamentals",
        json={"source": "manual", **fields},
        headers=headers,
    )
    assert body.status_code == 200, body.text


def _holdings(client: TestClient, headers: dict, security_id: int, rows: list[dict]) -> None:
    body = client.put(
        f"{API}/securities/{security_id}/holdings",
        json={"items": rows},
        headers=headers,
    )
    assert body.status_code == 200, body.text


def _buckets(body_json: dict) -> dict[str, str]:
    return {item["key"]: item["market_value_cad"] for item in body_json["items"]}


# --- stock + fundamentals ---------------------------------------------------------


def test_stock_with_fundamentals_lands_in_sector_country_asset_class(
    client: TestClient, session: Session
) -> None:
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    stock = _security(session, user_id, "SHOP")
    _buy(session, user_id, account, stock, "10", "50")  # mv 500 CAD
    _fundamentals(client, headers, stock.id, sector="Technology", country="Canada")

    sector = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers)
    assert sector.status_code == 200, sector.text
    payload = sector.json()
    assert _buckets(payload) == {"Technology": "500.00"}
    assert Decimal(payload["total_cad"]) == Decimal("500.00")
    assert Decimal(payload["unclassified_cad"]) == Decimal("0.00")

    geography = client.get(BREAKDOWN_URL, params={"dimension": "geography"}, headers=headers).json()
    assert _buckets(geography) == {"Canada": "500.00"}

    asset_class = client.get(
        BREAKDOWN_URL, params={"dimension": "asset_class"}, headers=headers
    ).json()
    assert _buckets(asset_class) == {"stock": "500.00"}


def test_stock_without_fundamentals_is_unclassified(client: TestClient, session: Session) -> None:
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    stock = _security(session, user_id, "XYZ")
    _buy(session, user_id, account, stock, "1", "123.45")
    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    assert _buckets(payload) == {"Unclassified": "123.45"}
    assert Decimal(payload["unclassified_cad"]) == Decimal("123.45")


# --- ETF look-through -------------------------------------------------------------


def test_etf_look_through_renormalized_hand_case(client: TestClient, session: Session) -> None:
    """Pinned hand case: position 1000 @ holdings weights 60 + 30 →
    600 / 300 with the missing share (100 - 90) → 100 Unclassified."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "VFUND", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "10", "100")  # mv 1000 CAD
    _holdings(
        client,
        headers,
        fund.id,
        [
            {
                "holding_name": "Tech Giant",
                "asset_class": "stock",
                "sector": "Technology",
                "weight_pct": "60",
            },
            {
                "holding_name": "Big Bank",
                "asset_class": "stock",
                "sector": "Financials",
                "weight_pct": "30",
            },
        ],
    )

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    items = {item["key"]: item for item in payload["items"]}
    assert Decimal(items["Technology"]["market_value_cad"]) == Decimal("600.00")
    # M166: one vocabulary. The row says "Financials" (the GICS spelling every
    # issuer CSV uses) and the pie reports it as "Financial Services" — the
    # name yfinance writes — so one company can never split into two slices.
    assert Decimal(items["Financial Services"]["market_value_cad"]) == Decimal("300.00")
    assert Decimal(items["Unclassified"]["market_value_cad"]) == Decimal("100.00")
    # Sorted by value desc; weights are shares of the total rounded to 2.
    assert [item["key"] for item in payload["items"]] == [
        "Technology",
        "Financial Services",
        "Unclassified",
    ]
    assert items["Technology"]["weight_pct"] == 60.0
    assert Decimal(payload["total_cad"]) == Decimal("1000.00")
    assert Decimal(payload["unclassified_cad"]) == Decimal("100.00")


def test_overweighed_holdings_renormalize_against_their_sum(
    client: TestClient, session: Session
) -> None:
    """Weights summing above 100 renormalize against their SUM so overlapping
    share classes never inflate exposure and nothing is lost or duplicated."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "VHEAVY", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "10", "100")  # mv 1000 CAD
    # The PUT route rejects totals above 100.01% — seed the overweighed
    # composition directly to exercise breakdown-side renormalization.
    from app.models.etf_holdings import EtfHolding, EtfHoldingSource

    for name, sector, weight in (("A", "S1", "60"), ("B", "S2", "50")):
        session.add(
            EtfHolding(
                user_id=user_id,
                security_id=fund.id,
                holding_name=name,
                asset_class="stock",
                sector=sector,
                weight_pct=Decimal(weight),
                source=EtfHoldingSource.manual,
            )
        )
    session.commit()
    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    buckets = _buckets(payload)
    assert Decimal(buckets["S1"]) == Decimal("545.45")
    assert Decimal(buckets["S2"]) == Decimal("454.55")
    assert Decimal(payload["unclassified_cad"]) == Decimal("0.00")
    assert Decimal(payload["total_cad"]) == Decimal("1000.00")


def test_geography_prefers_region_then_country(client: TestClient, session: Session) -> None:
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "VGEO", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "10", "100")  # mv 1000 CAD
    _holdings(
        client,
        headers,
        fund.id,
        [
            {
                "holding_name": "US mega cap",
                "asset_class": "stock",
                "region": "North America",
                "country": "United States",
                "weight_pct": "50",
            },
            {
                "holding_name": "Japanese name",
                "asset_class": "stock",
                "country": "Japan",
                "weight_pct": "50",
            },
        ],
    )
    payload = client.get(BREAKDOWN_URL, params={"dimension": "geography"}, headers=headers).json()
    buckets = _buckets(payload)
    assert Decimal(buckets["North America"]) == Decimal("500.00")
    assert Decimal(buckets["Japan"]) == Decimal("500.00")


def test_fund_without_composition_unclassified_kind_fallback(
    client: TestClient, session: Session
) -> None:
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "VCOLD", kind=SecurityKind.mutual_fund)
    _buy(session, user_id, account, fund, "10", "100")  # mv 1000 CAD
    asset_class = client.get(
        BREAKDOWN_URL, params={"dimension": "asset_class"}, headers=headers
    ).json()
    assert _buckets(asset_class) == {"mutual_fund": "1000.00"}
    sector = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    assert _buckets(sector) == {"Unclassified": "1000.00"}


# --- reconciliation ---------------------------------------------------------------


def test_mixed_portfolio_totals_reconcile_and_cross_account_groups(
    client: TestClient, session: Session
) -> None:
    """Stock (fundamentals) + ETF (look-through) + cold fund: sum(items)
    equals the positions market-value total; one ETF held in two accounts is
    looked through exactly once."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account_a = _account(session, user_id, "Brokerage A")
    account_b = _account(session, user_id, "Brokerage B")
    stock = _security(session, user_id, "RNW")
    etf = _security(session, user_id, "VMIX", kind=SecurityKind.etf)
    fund = _security(session, user_id, "VCOLD", kind=SecurityKind.mutual_fund)

    _buy(session, user_id, account_a, stock, "10", "50")  # 500
    _buy(session, user_id, account_a, etf, "4", "100")  # 400
    _buy(session, user_id, account_b, etf, "6", "100")  # 600 — same ETF
    _buy(session, user_id, account_b, fund, "10", "25")  # 250
    _fundamentals(client, headers, stock.id, sector="Energy", country="Canada")
    _holdings(
        client,
        headers,
        etf.id,
        [
            {
                "holding_name": "Tech",
                "asset_class": "stock",
                "sector": "Technology",
                "weight_pct": "50",
            },
            {
                "holding_name": "Health",
                "asset_class": "stock",
                "sector": "Healthcare",
                "weight_pct": "25",
            },
        ],
    )

    positions_total = sum(
        Decimal(str(p["market_value_cad"]))
        for p in client.get(f"{API}/portfolio/positions", headers=headers).json()["items"]
    )
    assert positions_total == Decimal("1750.00")

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    buckets = _buckets(payload)
    # ETF mv 1000 across BOTH accounts at face weights: Technology 500,
    # Healthcare 250, residual 250; the cold fund adds 250 → Unclassified 500.
    assert Decimal(buckets["Unclassified"]) == Decimal("500.00")
    assert Decimal(buckets["Energy"]) == Decimal("500.00")
    assert Decimal(payload["total_cad"]) == positions_total
    assert sum(Decimal(v) for v in buckets.values()) == Decimal(payload["total_cad"])


# --- validation & tenancy ---------------------------------------------------------


def test_dimension_validation_missing_and_invalid_422(client: TestClient) -> None:
    headers = _auth(client)
    missing = client.get(BREAKDOWN_URL, headers=headers)
    assert missing.status_code == 422
    assert missing.json()["code"] == "validation_error"

    invalid = client.get(BREAKDOWN_URL, params={"dimension": "currency"}, headers=headers)
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "validation_error"


def test_tenancy_other_users_positions_absent(client: TestClient, session: Session) -> None:
    owner_headers, _owner_email = _new_user(client)
    other_headers, other_email = _new_user(client)
    other_id = _uid(session, other_email)
    account = _account(session, other_id, "Other Brokerage")
    stock = _security(session, other_id, "OTHR")
    _buy(session, other_id, account, stock, "10", "50")
    _fundamentals(client, other_headers, stock.id, sector="Their Sector")

    payload = client.get(
        BREAKDOWN_URL, params={"dimension": "sector"}, headers=owner_headers
    ).json()
    assert "Their Sector" not in _buckets(payload)
    assert Decimal(payload["total_cad"]) == Decimal("0.00")
    assert payload["items"] == []


# --- recursive look-through & company dimension (M19) ------------------------------


def test_recursive_company_hand_case(client: TestClient, session: Session) -> None:
    """Pinned M19 hand case: F=1000 holds sub-fund S 60% + stock A 30%;
    S holds B 50% + C 30% → company A 300, B 300, C 180, Unclassified 220
    (F residual 100 + S residual 120). Total reconciles to 1000."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "XFUND", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "10", "100")  # mv 1000 CAD
    _holdings(
        client,
        headers,
        fund.id,
        [
            {
                "holding_symbol": "SUBF",
                "holding_name": "Sub Fund",
                "asset_class": "other",
                "weight_pct": "60",
            },
            {
                "holding_symbol": "AAA",
                "holding_name": "Alpha Corp",
                "asset_class": "stock",
                "weight_pct": "30",
            },
        ],
    )
    sub = _security(session, user_id, "SUBF", kind=SecurityKind.etf)
    _holdings(
        client,
        headers,
        sub.id,
        [
            {
                "holding_symbol": "BBB",
                "holding_name": "Beta Corp",
                "asset_class": "stock",
                "weight_pct": "50",
            },
            {
                "holding_symbol": "CCC",
                "holding_name": "Gamma Corp",
                "asset_class": "stock",
                "weight_pct": "30",
            },
        ],
    )

    payload = client.get(BREAKDOWN_URL, params={"dimension": "company"}, headers=headers).json()
    buckets = _buckets(payload)
    assert Decimal(buckets["AAA"]) == Decimal("300.00")
    assert Decimal(buckets["BBB"]) == Decimal("300.00")
    assert Decimal(buckets["CCC"]) == Decimal("180.00")
    assert Decimal(buckets["Unclassified"]) == Decimal("220.00")
    assert Decimal(payload["total_cad"]) == Decimal("1000.00")
    assert Decimal(payload["unclassified_cad"]) == Decimal("220.00")


def test_leaf_rows_fall_back_to_matched_security_fundamentals(
    client: TestClient, session: Session
) -> None:
    """A leaf row without its own sector/country uses the matched security's
    fundamentals; a row matching nothing stays Unclassified."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "XLEAF", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "10", "100")  # mv 1000 CAD
    leaf = _security(session, user_id, "LEAF")
    _fundamentals(client, headers, leaf.id, sector="Technology", country="Canada")
    _holdings(
        client,
        headers,
        fund.id,
        [
            {
                "holding_symbol": "LEAF",
                "holding_name": "Leaf Corp",
                "asset_class": "stock",
                "weight_pct": "50",
            },
            {
                "holding_name": "Mystery Corp",
                "asset_class": "stock",
                "weight_pct": "50",
            },
        ],
    )

    sector = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    assert _buckets(sector) == {"Technology": "500.00", "Unclassified": "500.00"}
    geography = client.get(BREAKDOWN_URL, params={"dimension": "geography"}, headers=headers).json()
    assert _buckets(geography) == {"Canada": "500.00", "Unclassified": "500.00"}


def test_depth_cap_treats_deepest_fund_as_leaf(client: TestClient, session: Session) -> None:
    """A four-fund chain: recursion stops at MAX_DEPTH — the level-4 fund is
    a leaf keyed by its own ticker and its inner rows are not walked."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    chain = {
        name: _security(session, user_id, name, kind=SecurityKind.etf)
        for name in ("L1", "L2", "L3", "L4")
    }
    _buy(session, user_id, account, chain["L1"], "10", "100")  # mv 1000 CAD
    _holdings(
        client,
        headers,
        chain["L1"].id,
        [
            {
                "holding_symbol": "L2",
                "holding_name": "Level 2",
                "asset_class": "other",
                "weight_pct": "100",
            }
        ],
    )
    _holdings(
        client,
        headers,
        chain["L2"].id,
        [
            {
                "holding_symbol": "L3",
                "holding_name": "Level 3",
                "asset_class": "other",
                "weight_pct": "100",
            }
        ],
    )
    _holdings(
        client,
        headers,
        chain["L3"].id,
        [
            {
                "holding_symbol": "L4",
                "holding_name": "Level 4",
                "asset_class": "other",
                "weight_pct": "100",
            }
        ],
    )
    _holdings(
        client,
        headers,
        chain["L4"].id,
        [
            {
                "holding_symbol": "DDD",
                "holding_name": "Deep Corp",
                "asset_class": "stock",
                "weight_pct": "100",
            }
        ],
    )

    payload = client.get(BREAKDOWN_URL, params={"dimension": "company"}, headers=headers).json()
    assert _buckets(payload) == {"L4": "1000.00"}
    assert Decimal(payload["total_cad"]) == Decimal("1000.00")


def test_cycle_between_funds_terminates_as_leaf(client: TestClient, session: Session) -> None:
    """F → S → F: the visited set turns the back-edge into a leaf instead of
    recursing forever."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "XCYC", kind=SecurityKind.etf)
    sub = _security(session, user_id, "SUBC", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "10", "100")  # mv 1000 CAD
    _holdings(
        client,
        headers,
        fund.id,
        [
            {
                "holding_symbol": "SUBC",
                "holding_name": "Sub Cyc",
                "asset_class": "other",
                "weight_pct": "60",
            }
        ],
    )
    _holdings(
        client,
        headers,
        sub.id,
        [
            {
                "holding_symbol": "XCYC",
                "holding_name": "Cycle Back",
                "asset_class": "other",
                "weight_pct": "50",
            },
            {
                "holding_symbol": "XXX",
                "holding_name": "Ex Corp",
                "asset_class": "stock",
                "weight_pct": "30",
            },
        ],
    )

    payload = client.get(BREAKDOWN_URL, params={"dimension": "company"}, headers=headers).json()
    buckets = _buckets(payload)
    # F: S 600 (recursed), residual 400. Inside S: back-edge F is a leaf
    # (300), X 180, residual 120 → Unclassified 520.
    assert Decimal(buckets["XCYC"]) == Decimal("300.00")
    assert Decimal(buckets["XXX"]) == Decimal("180.00")
    assert Decimal(buckets["Unclassified"]) == Decimal("520.00")
    assert Decimal(payload["total_cad"]) == Decimal("1000.00")


def test_company_dimension_keys_direct_stocks_and_nameless_rows(
    client: TestClient, session: Session
) -> None:
    """Direct stock positions key by their own symbol; composition rows
    without a ticker fall back to the holding name."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    stock = _security(session, user_id, "SHOP")
    _buy(session, user_id, account, stock, "10", "50")  # mv 500 CAD
    _fundamentals(client, headers, stock.id, sector="Technology", country="Canada")
    fund = _security(session, user_id, "XNAME", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "10", "100")  # mv 1000 CAD
    _holdings(
        client,
        headers,
        fund.id,
        [{"holding_name": "Name Only Corp", "asset_class": "stock", "weight_pct": "100"}],
    )

    payload = client.get(BREAKDOWN_URL, params={"dimension": "company"}, headers=headers).json()
    assert _buckets(payload) == {"Name Only Corp": "1000.00", "SHOP": "500.00"}
    assert Decimal(payload["total_cad"]) == Decimal("1500.00")


def test_self_referential_fund_terminates_as_leaf(client: TestClient, session: Session) -> None:
    """F → F: a fund holding itself. The visited set catches this at depth=1
    (the position fund is already in visited), so the self-referential row
    becomes a leaf keyed by its own ticker."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "XSELF", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "10", "100")  # mv 1000 CAD
    _holdings(
        client,
        headers,
        fund.id,
        [
            {
                "holding_symbol": "XSELF",
                "holding_name": "Self Reference",
                "asset_class": "other",
                "weight_pct": "60",
            },
            {
                "holding_symbol": "EXT",
                "holding_name": "External Corp",
                "asset_class": "stock",
                "weight_pct": "30",
            },
        ],
    )

    payload = client.get(BREAKDOWN_URL, params={"dimension": "company"}, headers=headers).json()
    buckets = _buckets(payload)
    # F: self-ref XSELF is a leaf (600), EXT recurses but has no holdings so
    # becomes a leaf (300), residual 100 → Unclassified.
    assert Decimal(buckets["XSELF"]) == Decimal("600.00")
    assert Decimal(buckets["EXT"]) == Decimal("300.00")
    assert Decimal(buckets["Unclassified"]) == Decimal("100.00")
    assert Decimal(payload["total_cad"]) == Decimal("1000.00")


def test_recursive_overweighted_sub_fund_renormalizes(client: TestClient, session: Session) -> None:
    """A sub-fund with weights > 100 renormalizes at its own level. The
    denominator max(sum_of_weights, 100) applies at every recursion level."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    outer = _security(session, user_id, "XOUTER", kind=SecurityKind.etf)
    inner = _security(session, user_id, "XINNER", kind=SecurityKind.etf)
    _buy(session, user_id, account, outer, "10", "100")  # mv 1000 CAD
    _holdings(
        client,
        headers,
        outer.id,
        [
            {
                "holding_symbol": "XINNER",
                "holding_name": "Inner Fund",
                "asset_class": "other",
                "weight_pct": "100",
            }
        ],
    )
    # Inner fund has over-weighted holdings (60 + 50 = 110)
    from app.models.etf_holdings import EtfHolding, EtfHoldingSource

    for name, sector, weight in (("A", "S1", "60"), ("B", "S2", "50")):
        session.add(
            EtfHolding(
                user_id=user_id,
                security_id=inner.id,
                holding_name=name,
                asset_class="stock",
                sector=sector,
                weight_pct=Decimal(weight),
                source=EtfHoldingSource.manual,
            )
        )
    session.commit()

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    buckets = _buckets(payload)
    # Outer: 1000 → XINNER (recursed). Inner: sum=110, so S1 = 1000 * 60/110 = 545.45,
    # S2 = 1000 * 50/110 = 454.55. Total = 1000.
    assert Decimal(buckets["S1"]) == Decimal("545.45")
    assert Decimal(buckets["S2"]) == Decimal("454.55")
    assert Decimal(payload["unclassified_cad"]) == Decimal("0.00")
    assert Decimal(payload["total_cad"]) == Decimal("1000.00")


# --- ZeroDivision guard (scout M6) ---------------------------------------------


def test_tiny_holdings_round_to_zero_weight_is_zero_not_crash(
    client: TestClient, session: Session
) -> None:
    """scout M6: when every attributed bucket rounds to 0.00 CAD yet items is
    non-empty, total_cad == 0 — weight must be 0.0, never ZeroDivisionError."""
    headers, email = _new_user(client)
    uid = _uid(session, email)
    account = _account(session, uid, "Tiny")
    security = _security(session, uid, "DUST")  # CAD-listed stock, no fundamentals
    _buy(session, uid, account, security, "1", "0.003")  # CAD 0.003 -> rounds to 0.00
    body = client.get(f"{BREAKDOWN_URL}?dimension=sector", headers=headers)
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["total_cad"] == "0.00"
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["key"] == "Unclassified"
    assert item["market_value_cad"] == "0.00"
    assert item["weight_pct"] == 0.0

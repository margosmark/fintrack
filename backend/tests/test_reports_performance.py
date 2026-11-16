"""Investment-performance contract tests (docs/12 §5, w6-contract pins 5 & 8).

Realized figures MUST equal /reports/capital-gains on overlapping sells
(single source of gain math); unrealized/pool apply to non-registered only;
registered holdings carry market value with null cost fields.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_tax_acb import _user, add_trade, make_account, make_security

from app.core.security import create_access_token
from app.models.accounts import AccountType
from app.models.fx_rates import FxRate, FxSource
from app.models.prices import Price, PriceSource
from app.models.trades import TradeAction as TA
from app.models.users import User

D = Decimal

RANGE_2025 = {"from": "2025-01-01", "to": "2025-12-31"}


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _seed(session: Session) -> User:
    """Hand-calculated pools (all-CAD conversions except VTI):

    brokerage non-reg RY: buy 100@120+9.95 (2024-01-15) → 12009.95
                          buy 50@132+9.95 (2024-03-10) → +6609.95 = 18619.90
        sell 2025-05-20 -60@140: alloc 18619.90x60/150 = 7447.96 → gain 8400-7447.96
                                 = 952.04; pool → 90 sh / 11171.94
        sell 2026-01-05 -10@145 (out of 2025 range): alloc 11171.94x10/90 =
                                 1241.33 → gain 208.67; pool → 80 sh / 9930.61
    rrsp RY: buy 40@100 (2024-02-01) — registered, no pool, no fx needed.
    rrsp VTI (USD): buy 20@210 (2024-02-01) — registered.
    Prices: RY manual 2025-06-01 @141; VTI manual 2025-06-01 @220 USD.
    fx USD/CAD manual 2025-06-01 @1.30 (newest ≤ today).
    """
    user = _user(session)
    brokerage = make_account(
        session, user, "Brokerage", AccountType.brokerage_non_registered, taxable=True
    )
    rrsp = make_account(session, user, "RRSP", AccountType.rrsp, taxable=False)
    ry = make_security(session, user, "RY", "CAD")
    vti = make_security(session, user, "VTI", "USD")

    add_trade(session, user, brokerage, ry, TA.buy, "2024-01-15", "100", "120.00", "9.95")
    add_trade(session, user, brokerage, ry, TA.buy, "2024-03-10", "50", "132.00", "9.95")
    add_trade(session, user, brokerage, ry, TA.sell, "2025-05-20", "-60", "140.00")
    add_trade(session, user, brokerage, ry, TA.sell, "2026-01-05", "-10", "145.00")
    add_trade(session, user, rrsp, ry, TA.buy, "2024-02-01", "40", "100.00")
    add_trade(session, user, rrsp, vti, TA.buy, "2024-02-01", "20", "210.00")

    session.add(
        Price(
            user_id=user.id,
            security_id=ry.id,
            date=date(2025, 6, 1),
            price=D("141"),
            source=PriceSource.manual,
        )
    )
    session.add(
        Price(
            user_id=user.id,
            security_id=vti.id,
            date=date(2025, 6, 1),
            price=D("220"),
            source=PriceSource.manual,
        )
    )
    # The tax engine converts every acquisition (registered included) at the
    # cached rate on the trade date.
    session.add(
        FxRate(
            base="USD", quote="CAD", date=date(2024, 2, 1), rate=D("1.36"), source=FxSource.manual
        )
    )
    session.add(
        FxRate(
            base="USD", quote="CAD", date=date(2025, 6, 1), rate=D("1.30"), source=FxSource.manual
        )
    )
    session.commit()
    return user


def _items_by_symbol(body: dict) -> dict[str, dict]:
    return {i["symbol"]: i for i in body["items"]}


def test_performance_splits_realized_and_unrealized(client: TestClient, session: Session) -> None:
    user = _seed(session)
    body = client.get(
        "/api/v1/reports/investment-performance", params=RANGE_2025, headers=_headers(user)
    ).json()
    items = _items_by_symbol(body)

    ry = items["RY"]
    assert D(str(ry["quantity"])) == D("120")  # 80 non-reg + 40 registered
    assert D(str(ry["realized_cad"])) == D("952.04")  # in-range sell ONLY
    assert D(str(ry["acb_pool_cad"])) == D("9930.61")
    assert D(str(ry["unrealized_cad"])) == D("1349.39")  # 80x141 - 9930.61
    assert D(str(ry["market_value_cad"])) == D("16920.00")  # 120x141

    vti = items["VTI"]  # registered: market value only, cost fields null
    assert D(str(vti["quantity"])) == D("20")
    assert D(str(vti["realized_cad"])) == D("0")
    assert vti["unrealized_cad"] is None and vti["acb_pool_cad"] is None
    assert D(str(vti["market_value_cad"])) == D("5720.00")  # 20 x 220 x 1.30

    totals = body["totals"]
    assert D(str(totals["realized_cad"])) == D("952.04")
    assert D(str(totals["unrealized_cad"])) == D("1349.39")
    assert D(str(totals["market_value_cad"])) == D("22640.00")


def test_realized_matches_capital_gains_endpoint(client: TestClient, session: Session) -> None:
    """Single source of gain math: overlapping sells must agree exactly."""
    user = _seed(session)
    perf_2025 = client.get(
        "/api/v1/reports/investment-performance", params=RANGE_2025, headers=_headers(user)
    ).json()
    cg_2025 = client.get(
        "/api/v1/reports/capital-gains", params={"year": 2025}, headers=_headers(user)
    ).json()
    by_symbol_perf = {i["symbol"]: D(str(i["realized_cad"])) for i in perf_2025["items"]}
    by_symbol_cg = {i["symbol"]: D(str(i["gain_loss_cad"])) for i in cg_2025["items"]}
    assert by_symbol_perf.get("RY") == by_symbol_cg["RY"] == D("952.04")

    perf_2026 = client.get(
        "/api/v1/reports/investment-performance",
        params={"from": "2026-01-01", "to": "2026-12-31"},
        headers=_headers(user),
    ).json()
    cg_2026 = client.get(
        "/api/v1/reports/capital-gains", params={"year": 2026}, headers=_headers(user)
    ).json()
    realized_2026 = next(i for i in perf_2026["items"] if i["symbol"] == "RY")
    assert D(str(realized_2026["realized_cad"])) == D(str(cg_2026["items"][0]["gain_loss_cad"]))
    assert D(str(realized_2026["realized_cad"])) == D("208.67")


def test_performance_csv_header_pinned_with_registered_nulls(
    client: TestClient, session: Session
) -> None:
    import csv
    import io

    user = _seed(session)
    resp = client.get(
        "/api/v1/reports/investment-performance",
        params={**RANGE_2025, "format": "csv"},
        headers=_headers(user),
    )
    lines = resp.text.strip().splitlines()
    assert lines[0] == (
        "security_id,symbol,quantity,realized_cad,unrealized_cad,acb_pool_cad,market_value_cad"
    )
    rows = {r["symbol"]: r for r in csv.DictReader(io.StringIO(resp.text))}
    assert rows["VTI"]["unrealized_cad"] == "" and rows["VTI"]["acb_pool_cad"] == ""
    assert rows["RY"]["unrealized_cad"] == "1349.39"

"""Tax-report API contract tests (w4-contract, docs/08) — JSON/CSV/HTML capital gains.

Reuses the hand-calculated fixture scenario from test_tax_acb; asserts the
exact wire shapes consumed by the Taxes page (see FrontendWealth contract).
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_tax_acb import Scenario

from app.core.security import create_access_token
from app.models.trades import TradeAction
from app.models.users import User
from app.services import fx as fx_service
from app.tax.reports import DISCLAIMER

D = Decimal


def _valet_down(*_args: object, **_kwargs: object) -> dict:
    """Stand in for an unreachable Bank of Canada (docs/07 retry exhaustion)."""
    raise fx_service.ValetUnavailable("simulated: no network in this suite")


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _other_user(session: Session) -> User:
    user = User(email=f"tax-other-{id(session)}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _full_scenario(session: Session) -> tuple[Scenario, dict[str, str]]:
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-15", "100", "120.00", "9.95")
    sc.ry_trade(TradeAction.buy, "2024-03-10", "50", "132.00", "9.95")
    sc.ry_trade(TradeAction.return_of_capital, "2024-05-24", "0", "52.50")
    sc.ry_trade(TradeAction.sell, "2024-06-20", "-60", "140.00", "9.95")
    sc.ry_trade(TradeAction.sell, "2024-11-25", "-10", "118.00")
    sc.ry_trade(TradeAction.buy, "2024-12-05", "10", "119.50")
    sc.vti_trade(TradeAction.buy, "2024-02-01", "20", "210.00", "1.00", fx_used="1.35")
    sc.vti_trade(TradeAction.corporate_action_adjustment, "2024-09-16", "40", "0")
    return sc, _headers(sc.user)


def test_registered_trade_paths_build_without_persisting_acb_pool(
    client: TestClient, session: Session
) -> None:
    """Registered holdings are validated without creating an ACB pool."""
    sc = Scenario(session)
    headers = _headers(sc.user)

    for action, quantity, price in (
        ("buy", "10", "100.00"),
        ("drip", "2", "25.00"),
        ("sell", "-4", "110.00"),
        ("transfer_out", "-2", "0.00"),
    ):
        response = client.post(
            "/api/v1/trades",
            json={
                "account_id": sc.registered.id,
                "security_id": sc.ry.id,
                "action": action,
                "date": "2024-01-15",
                "quantity": quantity,
                "price_native": price,
                "commission": "0",
            },
            headers=headers,
        )
        assert response.status_code == 201, (action, response.text)

    listing = client.get("/api/v1/acb", headers=headers).json()
    assert listing["total"] == 0


def test_post_acb_rebuild_counts_and_persists(client: TestClient, session: Session) -> None:
    _sc, headers = _full_scenario(session)
    response = client.post("/api/v1/acb/rebuild", json={}, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"pools": 2, "trades": 8}

    listing = client.get("/api/v1/acb", headers=headers).json()
    assert listing["total"] == 2
    by_symbol = {i["symbol"]: i for i in listing["items"]}
    assert D(str(by_symbol["RY"]["quantity"])) == D("90")
    assert D(str(by_symbol["RY"]["total_acb_cad"])) == D("11155.44")
    assert D(str(by_symbol["VTI"]["quantity"])) == D("40")
    assert D(str(by_symbol["VTI"]["total_acb_cad"])) == D("5713.36")


def test_get_acb_filters(client: TestClient, session: Session) -> None:
    sc, headers = _full_scenario(session)
    client.post("/api/v1/acb/rebuild", json={}, headers=headers)

    only_ry = client.get("/api/v1/acb", params={"security_id": sc.ry.id}, headers=headers).json()
    assert [i["symbol"] for i in only_ry["items"]] == ["RY"]

    empty = client.get("/api/v1/acb", headers=_headers(_other_user(session))).json()
    assert empty["items"] == [] and empty["total"] == 0


def test_capital_gains_json_exact_values(client: TestClient, session: Session) -> None:
    sc, headers = _full_scenario(session)
    response = client.get("/api/v1/reports/capital-gains", params={"year": 2024}, headers=headers)
    assert response.status_code == 200
    body = response.json()

    assert body["year"] == 2024
    assert body["disclaimer"] == DISCLAIMER
    # RY is the only security with disposals in 2024.
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["security"] == "RY"
    assert item["symbol"] == "RY"
    assert item["security_id"] == sc.ry.id
    assert D(str(item["proceeds_cad"])) == D("9580.00")  # 8400.00 + 1180.00
    assert D(str(item["acb_cad"])) == D("8664.79")  # 7426.96 + 1237.83
    # Raw gains 973.04 + (-57.83); the 57.83 loss is denied → 973.04 net.
    assert D(str(item["gain_loss_cad"])) == D("973.04")
    assert D(str(item["outlays_commissions_cad"])) == D("9.95")

    totals = body["totals"]
    assert D(str(totals["gain_loss_cad"])) == D("973.04")

    assert len(body["denials"]) == 1
    denial = body["denials"][0]
    assert denial["symbol"] == "RY"
    assert denial["sale_date"] == "2024-11-25"
    assert D(str(denial["denied_loss_cad"])) == D("57.83")
    assert denial["outcome"] == "deferral"
    assert denial["acquiring_account_id"] == sc.brokerage.id


def test_capital_gains_csv_format(client: TestClient, session: Session) -> None:
    _, headers = _full_scenario(session)
    response = client.get(
        "/api/v1/reports/capital-gains",
        params={"year": 2024, "format": "csv"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert (
        response.headers["content-disposition"] == 'attachment; filename="capital-gains-2024.csv"'
    )
    lines = response.text.strip().splitlines()
    assert lines[0] == "security,proceeds_cad,acb_cad,gain_loss_cad,outlays_commissions_cad"
    assert lines[1] == "RY,9580.00,8664.79,973.04,9.95"
    assert "superficial_loss_denials" in lines
    denial_line = next(line for line in lines if line.startswith("RY,2024-11-25"))
    assert "deferral" in denial_line and "57.83" in denial_line
    assert lines[-1] == f"disclaimer,{DISCLAIMER}"


def test_capital_gains_html_format(client: TestClient, session: Session) -> None:
    _, headers = _full_scenario(session)
    response = client.get(
        "/api/v1/reports/capital-gains",
        params={"year": 2024, "format": "html"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    text = response.text
    assert "Capital Gains Summary 2024" in text
    assert "Schedule" in text
    assert "9580.00" in text and "8664.79" in text and "973.04" in text
    assert "Superficial-loss denials" in text and "57.83" in text
    assert DISCLAIMER in text


def test_capital_gains_empty_year_zeroes(client: TestClient, session: Session) -> None:
    _, headers = _full_scenario(session)
    body = client.get(
        "/api/v1/reports/capital-gains", params={"year": 2023}, headers=headers
    ).json()
    assert body["items"] == []
    assert body["denials"] == []
    assert D(str(body["totals"]["gain_loss_cad"])) == D("0")


def test_capital_gains_missing_fx_rate_envelope(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M115: the sweep backfills on a miss, so an empty cache alone is no
    # longer enough to 422 — pin the fetch failing too (docs/07).
    monkeypatch.setattr(fx_service, "fetch_valet_series", _valet_down)
    sc = Scenario(session, with_usd_fx=False)
    sc.vti_trade(TradeAction.buy, "2024-02-01", "20", "210.00", "1.00")
    headers = _headers(sc.user)

    response = client.get("/api/v1/reports/capital-gains", params={"year": 2024}, headers=headers)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "fx_rate_missing"
    assert "VTI" in body["detail"]


def test_trades_write_path_auto_rebuilds_pools(client: TestClient, session: Session) -> None:
    """Integration hook (docs/02 invariant): POST /trades must rebuild pools in the
    SAME transaction — no explicit /acb/rebuild needed afterwards."""
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-15", "100", "120.00", "9.95")
    headers = _headers(sc.user)
    response = client.post(
        "/api/v1/trades",
        json={
            "account_id": sc.brokerage.id,
            "security_id": sc.ry.id,
            "action": "buy",
            "date": "2024-02-15",
            "quantity": "10",
            "price_native": "130.00",
            "commission": "0",
        },
        headers=headers,
    )
    assert response.status_code == 201

    pools = client.get("/api/v1/acb", params={"security_id": sc.ry.id}, headers=headers).json()
    # Auto-rebuilt inside the trade write: (100x120+9.95) + 10x130 = 13309.95.
    assert D(str(pools["items"][0]["total_acb_cad"])) == D("13309.95")

"""Total-return contract tests (M77, docs/05 §Total return).

The two fixtures below are hand-derived from the seeded ledger: every expected
return is computed with plain ``Decimal`` arithmetic on numbers written out in
the test, then compared with the endpoint's answer inside 1e-8 — the numbers
were never produced by calling the implementation.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_tax_acb import _user, add_trade, make_account, make_security

from app.core.security import create_access_token
from app.models.accounts import AccountType
from app.models.prices import Price, PriceSource
from app.models.trades import TradeAction as TA
from app.models.users import User

D = Decimal
TOL = D("1e-8")

# Fixture 1 window: Jan 1 .. Mar 31 2025 = 90 days (31 + 28 + 31).
RANGE = {"from": "2025-01-01", "to": "2025-03-31"}


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _price(session: Session, user: User, security_id: int, day: str, price: str) -> None:
    session.add(
        Price(
            user_id=user.id,
            security_id=security_id,
            date=date.fromisoformat(day),
            price=D(price),
            source=PriceSource.manual,
        )
    )
    session.commit()


def _brokerage(session: Session, user: User):
    account = make_account(
        session, user, "Brokerage", AccountType.brokerage_non_registered, taxable=True
    )
    return account


def test_fixture_one_hand_computed_twr_and_mwr(client: TestClient, session: Session) -> None:
    """Contribution, buy, price moves, a partial sale and a withdrawal.

    Ledger (all CAD, no fx involved):

    ==========  ==========  ===================================================
    date        event       effect
    ==========  ==========  ===================================================
    2025-01-10  +1000 cash  external flow +1000
    2025-01-10  buy 10@50   cash -500, 10 shares
    2025-01-10  price 50    V = 1000
    2025-02-10  price 55    V = 1050
    2025-03-10  price 60    V = 1100
    2025-03-15  sell -5@62  cash +309 (310 - 1 commission), 5 shares left
    2025-03-20  -200 cash   external flow -200, V = 909
    ==========  ==========  ===================================================

    TWR chain-links only the days the VALUE moved without an explaining flow:
    1050/1000 x 1100/1050 x 1109/1100 = 1109/1000 -> +10.9% exactly.
    (Mar 20's drop to 909 is cancelled by the +200 flow in the numerator.)

    MWR (Modified Dietz), window = 89 days:
      delta        = 909 - 0 - 800            = 109
      denominator  = (1000 x 80) + (-200 x 11) = 77800 dollar-days
      r            = 109 x 89 / 77800
    """
    user = _user(session)
    brokerage = _brokerage(session, user)
    ry = make_security(session, user, "RY", "CAD")

    add_trade(session, user, brokerage, ry, TA.contribution, "2025-01-10", "0", "1000.00")
    add_trade(session, user, brokerage, ry, TA.buy, "2025-01-10", "10", "50.00", "0")
    _price(session, user, ry.id, "2025-01-10", "50.00")
    _price(session, user, ry.id, "2025-02-10", "55.00")
    _price(session, user, ry.id, "2025-03-10", "60.00")
    add_trade(session, user, brokerage, ry, TA.sell, "2025-03-15", "-5", "62.00", "1.00")
    add_trade(session, user, brokerage, ry, TA.withdrawal, "2025-03-20", "0", "200.00")

    body = client.get("/api/v1/reports/total-return", params=RANGE, headers=_headers(user)).json()

    assert set(body) == {
        "from",
        "to",
        "status",
        "start_value_cad",
        "end_value_cad",
        "net_flow_cad",
        "income_cad",
        "twr_pct",
        "mwr_pct",
        "valuation_days",
        "active_days",
        "flow_days",
        "account_count",
    }
    assert body["from"] == "2025-01-01" and body["to"] == "2025-03-31"
    assert body["status"] == "ok"
    assert body["valuation_days"] == 90
    assert body["flow_days"] == 2
    assert body["account_count"] == 1
    assert D(body["start_value_cad"]) == D("0.00")
    assert D(body["end_value_cad"]) == D("909.00")
    assert D(body["net_flow_cad"]) == D("800.00")

    expected_twr = D("10.9")
    expected_mwr = (D(109) * D(89) / D(77800) * 100).quantize(
        D("0.00000001"), rounding=ROUND_HALF_UP
    )
    assert abs(D(body["twr_pct"]) - expected_twr) <= TOL, body["twr_pct"]
    assert abs(D(body["mwr_pct"]) - expected_mwr) <= TOL, body["mwr_pct"]
    # the endpoint reports to 8 decimal places
    assert len(body["twr_pct"].split(".")[1]) == 8
    assert len(body["mwr_pct"].split(".")[1]) == 8


def test_fixture_two_reinvested_dividend_is_neutral(client: TestClient, session: Session) -> None:
    """A cash dividend plus its DRIP must not move value, only the price does.

    Opening (2025-03-25): +1000 contribution, buy 20 @ 50 -> 0 cash, 20 shares.
    2025-04-10: price drops to 48 (ex-dividend), +40.00 dividend cash, and a
    DRIP of 40/48 = 0.833333 shares puts the cash straight back:
        cash  = +40.00 - 39.999984 -> 0.00 (each row quantized once)
        value = 20.833333 x 48     -> 1000.00  (unchanged)
    2025-05-15: price 55 -> 20.833333 x 55 = 1145.833315 -> 1145.83.

    No external flow in the window, so TWR and MWR must agree:
        1145.83 / 1000 - 1 = 14.583%
    """
    user = _user(session)
    brokerage = _brokerage(session, user)
    ry = make_security(session, user, "RY", "CAD")

    add_trade(session, user, brokerage, ry, TA.contribution, "2025-03-25", "0", "1000.00")
    add_trade(session, user, brokerage, ry, TA.buy, "2025-03-25", "20", "50.00", "0")
    _price(session, user, ry.id, "2025-03-25", "50.00")
    _price(session, user, ry.id, "2025-04-10", "48.00")
    _price(session, user, ry.id, "2025-05-15", "55.00")

    add_trade(session, user, brokerage, ry, TA.dividend, "2025-04-10", "0", "40.00")
    add_trade(session, user, brokerage, ry, TA.drip, "2025-04-10", "0.833333", "48.00", "0")

    window = {"from": "2025-04-01", "to": "2025-06-30"}  # 91 days
    body = client.get("/api/v1/reports/total-return", params=window, headers=_headers(user)).json()

    assert body["status"] == "ok"
    assert body["valuation_days"] == 91
    assert D(body["start_value_cad"]) == D("1000.00")
    assert D(body["end_value_cad"]) == D("1145.83")
    assert D(body["net_flow_cad"]) == D("0.00")
    assert D(body["income_cad"]) == D("40.00")  # the dividend is still reported
    assert body["flow_days"] == 0

    expected = (D("145.83") / D("1000") * 100).quantize(D("0.00000001"), rounding=ROUND_HALF_UP)
    assert expected == D("14.583")
    assert abs(D(body["twr_pct"]) - expected) <= TOL, body["twr_pct"]
    assert abs(D(body["mwr_pct"]) - expected) <= TOL, body["mwr_pct"]


def test_transfers_are_flows_not_returns(client: TestClient, session: Session) -> None:
    """Money crossing the boundary cancels exactly, so a pure transfer shows
    a 0.00000000% return — including an IN-KIND transfer, whose shares appear
    on the very day its value arrives.

    ==========  ================================  ==========================
    date        event                             value / flow
    ==========  ================================  ==========================
    2025-01-10  +1000 cash, buy 10 @ 50, price 50 V = 1000, flow +1000
    2025-02-10  transfer_out cash 300             V =  700, flow  -300
    2025-03-10  transfer_in  cash 100             V =  800, flow  +100
    2025-03-15  transfer_in  5 shares @ 50        V = 1050, flow  +250
    ==========  ================================  ==========================

    Every numerator is `current - flow == previous`, so the TWR product is
    1 and both returns are exactly zero; net flow is 1050.00 over 4 days.
    """
    user = _user(session)
    brokerage = _brokerage(session, user)
    ry = make_security(session, user, "RY", "CAD")

    add_trade(session, user, brokerage, ry, TA.contribution, "2025-01-10", "0", "1000.00")
    add_trade(session, user, brokerage, ry, TA.buy, "2025-01-10", "10", "50.00", "0")
    _price(session, user, ry.id, "2025-01-10", "50.00")
    add_trade(session, user, brokerage, ry, TA.transfer_out, "2025-02-10", "0", "300.00")
    add_trade(session, user, brokerage, ry, TA.transfer_in, "2025-03-10", "0", "100.00")
    add_trade(session, user, brokerage, ry, TA.transfer_in, "2025-03-15", "5", "50.00")

    body = client.get("/api/v1/reports/total-return", params=RANGE, headers=_headers(user)).json()

    assert body["status"] == "ok"
    assert D(body["start_value_cad"]) == D("0.00")
    assert D(body["end_value_cad"]) == D("1050.00")
    assert D(body["net_flow_cad"]) == D("1050.00")
    assert body["flow_days"] == 4
    assert body["twr_pct"] == "0.00000000"
    assert body["mwr_pct"] == "0.00000000"


def test_empty_portfolio_is_no_data_not_zero(client: TestClient, session: Session) -> None:
    """An empty window must not print a confident 0.00000000% (M65 honesty)."""
    user = _user(session)
    _brokerage(session, user)
    body = client.get("/api/v1/reports/total-return", params=RANGE, headers=_headers(user)).json()

    assert body["status"] == "no_data"
    assert body["twr_pct"] is None
    assert body["mwr_pct"] is None
    assert body["active_days"] == 0
    assert body["valuation_days"] == 90


def test_non_investment_accounts_are_out_of_scope(client: TestClient, session: Session) -> None:
    """Chequing cash is not the investment portfolio (docs/05 scope)."""
    user = _user(session)
    make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    body = client.get("/api/v1/reports/total-return", params=RANGE, headers=_headers(user)).json()
    assert body["account_count"] == 0
    assert body["status"] == "no_data"


def test_total_return_rejects_inverted_range(client: TestClient, session: Session) -> None:
    user = _user(session)
    res = client.get(
        "/api/v1/reports/total-return",
        params={"from": "2025-03-31", "to": "2025-01-01"},
        headers=_headers(user),
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_date_range"


def test_ratio_pct_rounds_half_up_once() -> None:
    """The shared integer division is the single rounding both sides make."""
    from app.services.total_return import ratio_pct

    # 1/3 -> 33.33333333 (8 dp, the 9th digit 3 rounds down)
    assert ratio_pct(1, 3) == "33.33333333"
    # exact values keep their trailing zeros (no scientific notation)
    assert ratio_pct(1, 16) == "6.25000000"
    assert ratio_pct(-1, 16) == "-6.25000000"
    assert ratio_pct(3, 2) == "150.00000000"
    assert ratio_pct(0, 1) == "0.00000000"
    # exact tie at the 9th digit rounds AWAY from zero (half-up, not half-even)
    assert ratio_pct(1, 20_000_000_000) == "0.00000001"
    assert ratio_pct(-1, 20_000_000_000) == "-0.00000001"
    assert ratio_pct(1, 0) is None

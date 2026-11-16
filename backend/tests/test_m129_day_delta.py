"""M129 — day delta + own-position impact, server-computed (docs/08, docs/09).

Every number the delta story shows is computed HERE, from cached closes and
fx_rates rows, and hand-recomputed in the assertions (M91 anti-fabrication):

- `previous_close_native` / `day_change_native` / `day_change_pct` — the move
  vs the newest cached close STRICTLY before the row a position is valued at;
- `day_change_cad` — cents(qty x Δ x rate), "the delta for your own shares";
- `unrealized_gain_cad` / `unrealized_gain_native` / `unrealized_gain_pct` —
  market value vs book cost;
- `fx_rate_to_cad` — the exact rate the server used (the live tick may only
  multiply by this, never by a rate of its own);
- `total_unrealized_cad` on GET /portfolio/positions, and the impact totals
  on GET /securities/{id}/overview.

None always means "no data" — a first-day listing has no delta row, a
missing fx rate degrades the CAD fields to None, never a fabricated zero.
Network edges are monkeypatched — no external HTTP in tests.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.fx as fx_service
import app.services.market_data as market_data
from app.models.acb_pools import AcbPool
from app.models.accounts import Account, AccountType
from app.models.fx_rates import FxRate, FxSource
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import Trade, TradeAction

PORTFOLIO_URL = "/api/v1/portfolio"
SECURITIES_URL = "/api/v1/securities"


def _today() -> date:
    return datetime.now(UTC).date()


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m129-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _tfsa(session: Session, user_id: int) -> Account:
    """Registered account: book cost is the pure average-cost replay (no
    acb_pools row needed), which keeps these fixtures about the DELTA math."""
    account = Account(
        user_id=user_id,
        name="TFSA",
        type=AccountType.tfsa,
        currency="CAD",
        taxable=False,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _brokerage(session: Session, user_id: int) -> Account:
    """Non-registered account WITHOUT an acb_pools rebuild: its book cost is
    genuinely unknown (None), which is the honest state under test."""
    account = Account(
        user_id=user_id,
        name="Margin",
        type=AccountType.brokerage_non_registered,
        currency="CAD",
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _security(session: Session, user_id: int, symbol: str, listing: str = "CAD") -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind="stock",
        listing_currency=listing,
        exchange="TSX" if listing == "CAD" else "NMS",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _trade(
    session: Session,
    user_id: int,
    account: Account,
    security: Security,
    quantity: str,
    price: str,
    day: date | None = None,
) -> None:
    session.add(
        Trade(
            user_id=user_id,
            account_id=account.id,
            security_id=security.id,
            action=TradeAction.buy,
            date=day or (_today() - timedelta(days=30)),
            quantity=Decimal(quantity),
            price_native=Decimal(price),
            commission=Decimal("0"),
        )
    )
    session.commit()


def _price(session: Session, user_id: int, security: Security, day: date, value: str) -> None:
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=day,
            price=Decimal(value),
            source=PriceSource.manual,
        )
    )


def _fx(session: Session, base: str, quote: str, rate: str, day: date) -> None:
    session.add(
        FxRate(base=base, quote=quote, date=day, rate=Decimal(rate), source=FxSource.manual)
    )
    session.commit()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("network fetch attempted although data is cached")

    monkeypatch.setattr(market_data, "fetch_latest_close", forbidden)
    monkeypatch.setattr(market_data, "fetch_history", forbidden)
    monkeypatch.setattr(fx_service, "fetch_valet_series", forbidden)


def test_positions_day_delta_and_impact_fields_cad(client: TestClient, session: Session) -> None:
    """Two cached closes + one registered buy: every M129 field is the
    hand-computed number, in Decimal-exact agreement."""
    headers = _auth(client)
    user_id = _user_id(client, headers)
    account = _tfsa(session, user_id)
    security = _security(session, user_id, "CADBANK")
    _trade(session, user_id, account, security, "10", "90")
    today = _today()
    _price(session, user_id, security, today - timedelta(days=2), "100")
    _price(session, user_id, security, today - timedelta(days=1), "102")

    position = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()["items"][0]

    # Day delta vs the close strictly before the valued row (102 ← 100).
    assert Decimal(position["previous_close_native"]) == Decimal("100")
    assert Decimal(position["day_change_native"]) == Decimal("2")
    assert position["day_change_pct"] == 2.0  # (102 - 100) / 100
    # The delta on YOUR shares: cents(qty x Δ x rate) = 10 x 2 x 1.
    assert Decimal(position["day_change_cad"]) == Decimal("20.00")
    assert position["fx_rate_to_cad"] == "1"
    # Unrealized vs the replayed book cost (10 x 90 = 900; mv 10 x 102 = 1020).
    assert Decimal(position["unrealized_gain_cad"]) == Decimal("120.00")
    assert Decimal(position["unrealized_gain_native"]) == Decimal("120.00")
    assert position["unrealized_gain_pct"] == 13.33  # 120 / 900
    # The sparkline and the delta share ONE scan: both end at the same close.
    assert Decimal(position["spark"][-1]) == Decimal("102")


def test_first_day_listing_has_no_delta_but_still_gain(
    client: TestClient, session: Session
) -> None:
    """ONE cached row: no previous close exists, so every day-Δ field is None
    (the UI withholds the delta line) while the unrealized gain still computes."""
    headers = _auth(client)
    user_id = _user_id(client, headers)
    account = _tfsa(session, user_id)
    security = _security(session, user_id, "NEWLIST")
    _trade(session, user_id, account, security, "4", "10")
    _price(session, user_id, security, _today() - timedelta(days=1), "12")

    position = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()["items"][0]

    assert position["previous_close_native"] is None
    assert position["day_change_native"] is None
    assert position["day_change_pct"] is None
    assert position["day_change_cad"] is None
    # mv 4 x 12 = 48 vs book 4 x 10 = 40.
    assert Decimal(position["unrealized_gain_cad"]) == Decimal("8.00")
    assert position["unrealized_gain_pct"] == 20.0


def test_positions_total_unrealized_cad_is_sum_of_rows(
    client: TestClient, session: Session
) -> None:
    """The Σ row's total is the arithmetic sum of the per-row server gains —
    a gain and a loss net honestly, and a row with no book cost contributes
    nothing instead of a lie."""
    headers = _auth(client)
    user_id = _user_id(client, headers)
    account = _tfsa(session, user_id)
    today = _today()

    winner = _security(session, user_id, "WINNER")
    _trade(session, user_id, account, winner, "10", "100")
    _price(session, user_id, winner, today - timedelta(days=2), "100")
    _price(session, user_id, winner, today - timedelta(days=1), "110")

    loser = _security(session, user_id, "LOSER")
    _trade(session, user_id, account, loser, "5", "50")
    _price(session, user_id, loser, today - timedelta(days=2), "50")
    _price(session, user_id, loser, today - timedelta(days=1), "40")

    # Taxable account, priced, but its acb_pools row was never built: the
    # book cost is unknown, so the gain is None — the row must be EXCLUDED
    # from the total, never counted as a zero gain.
    taxable = _brokerage(session, user_id)
    poolless = _security(session, user_id, "POOLLESS")
    _trade(session, user_id, taxable, poolless, "1", "10")
    _price(session, user_id, poolless, today - timedelta(days=1), "12")

    body = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()
    gains = {row["symbol"]: row["unrealized_gain_cad"] for row in body["items"]}
    assert Decimal(gains["WINNER"]) == Decimal("100.00")  # 10 x (110 - 100)
    assert Decimal(gains["LOSER"]) == Decimal("-50.00")  # 5 x (40 - 50)
    assert gains["POOLLESS"] is None
    # 100.00 + (-50.00) = 50.00; the poolless row is excluded, not zeroed.
    assert Decimal(body["total_unrealized_cad"]) == Decimal("50.00")
    hand_sum = sum(Decimal(g) for g in gains.values() if g is not None)
    assert Decimal(body["total_unrealized_cad"]) == hand_sum


def test_positions_no_holdings_total_is_none(client: TestClient, session: Session) -> None:
    """A user with no open positions gets total_unrealized_cad=None — no
    fabricated $0.00 claim about an empty portfolio."""
    headers = _auth(client)
    body = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()
    assert body["items"] == []
    assert body["total_unrealized_cad"] is None


def test_positions_usd_fields_convert_with_server_rate(
    client: TestClient, session: Session
) -> None:
    """A USD listing in a CAD account: native figures stay native, CAD figures
    use the exact fx row, and `fx_rate_to_cad` publishes that rate so the live
    tick can only ever multiply by the server's number."""
    headers = _auth(client)
    user_id = _user_id(client, headers)
    account = _tfsa(session, user_id)
    security = _security(session, user_id, "MSFT", listing="USD")
    today = _today()
    _trade(session, user_id, account, security, "10", "90", day=today - timedelta(days=30))
    _fx(session, "USD", "CAD", "1.3", today - timedelta(days=32))  # trade-date carry window
    _price(session, user_id, security, today - timedelta(days=2), "99")
    _price(session, user_id, security, today - timedelta(days=1), "100")

    response = client.get(f"{PORTFOLIO_URL}/positions", headers=headers)
    assert response.status_code == 200, response.text
    position = response.json()["items"][0]

    assert Decimal(position["fx_rate_to_cad"]) == Decimal("1.3")
    assert Decimal(position["previous_close_native"]) == Decimal("99")
    assert Decimal(position["day_change_native"]) == Decimal("1")
    assert position["day_change_pct"] == 1.01  # 1 / 99
    # cents(qty x Δ x rate) = 10 x 1 x 1.3.
    assert Decimal(position["day_change_cad"]) == Decimal("13.00")
    # Book (replayed at the trade-date rate 1.3): 10 x 90 x 1.3 = 1170.00.
    assert Decimal(position["book_cost_cad"]) == Decimal("1170.00")
    assert Decimal(position["market_value_native"]) == Decimal("1000.00")
    assert Decimal(position["market_value_cad"]) == Decimal("1300.00")
    assert Decimal(position["unrealized_gain_cad"]) == Decimal("130.00")
    # Native gain = mv_native - book ÷ rate = 1000 - 1170/1.3 = 100.00.
    assert Decimal(position["unrealized_gain_native"]) == Decimal("100.00")
    assert position["unrealized_gain_pct"] == 11.11  # 130 / 1170


def test_positions_missing_fx_degrades_cad_fields_to_none(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No usable fx row: the native delta still computes, every CAD figure
    (including the day impact and the gain) degrades to None — never a 5xx.

    The pool row is seeded directly (the docs/06 cache the ACB engine would
    have written at trade time), so the book cost is KNOWN and the only
    missing input is the valuation-date fx rate.
    """
    headers = _auth(client)
    user_id = _user_id(client, headers)
    account = _brokerage(session, user_id)
    security = _security(session, user_id, "NOFX", listing="USD")
    today = _today()
    _trade(session, user_id, account, security, "2", "10")
    session.add(
        AcbPool(
            user_id=user_id,
            account_id=account.id,
            security_id=security.id,
            quantity=Decimal("2"),
            total_acb_cad=Decimal("20.00"),
        )
    )
    session.commit()
    _price(session, user_id, security, today - timedelta(days=2), "10")
    _price(session, user_id, security, today - timedelta(days=1), "11")

    def exhausted(*args, **kwargs):
        raise fx_service.ValetUnavailable("offline")

    monkeypatch.setattr(fx_service, "fetch_valet_series", exhausted)

    response = client.get(f"{PORTFOLIO_URL}/positions", headers=headers)
    assert response.status_code == 200  # never a 5xx for a missing rate
    position = response.json()["items"][0]

    assert Decimal(position["day_change_native"]) == Decimal("1")
    assert position["day_change_pct"] == 10.0
    assert Decimal(position["book_cost_cad"]) == Decimal("20.00")
    assert position["fx_rate_to_cad"] is None
    assert position["day_change_cad"] is None
    assert position["market_value_cad"] is None
    assert position["unrealized_gain_cad"] is None
    assert position["unrealized_gain_native"] is None
    assert position["unrealized_gain_pct"] is None


def test_overview_latest_delta_and_impact_totals(client: TestClient, session: Session) -> None:
    """The security page payload: `latest` carries the day delta, `totals`
    carries the impact block as the exact sum of the per-position rows (the
    headline and the table can never disagree)."""
    headers = _auth(client)
    user_id = _user_id(client, headers)
    account = _tfsa(session, user_id)
    security = _security(session, user_id, "MSFT", listing="USD")
    today = _today()
    _trade(session, user_id, account, security, "35.2491", "480", day=today - timedelta(days=60))
    _fx(session, "USD", "CAD", "1.4", today - timedelta(days=62))  # trade-date carry window
    _price(session, user_id, security, today - timedelta(days=2), "510")
    _price(session, user_id, security, today - timedelta(days=1), "517.42")

    body = client.get(f"{SECURITIES_URL}/{security.id}/overview", headers=headers).json()

    latest = body["latest"]
    assert Decimal(latest["previous_close"]) == Decimal("510")
    assert Decimal(latest["day_change_native"]) == Decimal("7.42")
    assert latest["day_change_pct"] == 1.45  # 7.42 / 510 = 1.4549…

    (row,) = body["positions"]
    assert Decimal(row["unrealized_gain_cad"]) > 0
    totals = body["totals"]
    # The totals quantity rides the pre-M129 cents quantizer (35.2491 → 35.25).
    assert Decimal(totals["quantity"]) == Decimal("35.25")
    assert totals["unrealized_gain_cad"] == row["unrealized_gain_cad"]
    assert totals["unrealized_gain_native"] == row["unrealized_gain_native"]
    assert totals["day_change_cad"] == row["day_change_cad"]
    assert totals["unrealized_gain_pct"] == row["unrealized_gain_pct"]
    # mv = cents(35.2491 x 517.42) = 18238.60... → hand-recompute the CAD.
    hand_mv = (Decimal("35.2491") * Decimal("517.42")).quantize(Decimal("0.01"))
    hand_mv_cad = (hand_mv * Decimal("1.4")).quantize(Decimal("0.01"))
    hand_book = (Decimal("35.2491") * Decimal("480") * Decimal("1.4")).quantize(Decimal("0.01"))
    assert Decimal(totals["market_value_cad"]) == hand_mv_cad
    assert Decimal(totals["unrealized_gain_cad"]) == hand_mv_cad - hand_book


def test_overview_unheld_security_has_delta_but_no_impact(
    client: TestClient, session: Session
) -> None:
    """Watchlist-only: the daily delta still renders (it is a fact about the
    SECURITY), while the impact block stays empty — no zero-position row, no
    fake "You hold" line (docs/09 suppress-when-unheld)."""
    headers = _auth(client)
    user_id = _user_id(client, headers)
    security = _security(session, user_id, "WATCHED")
    today = _today()
    _price(session, user_id, security, today - timedelta(days=2), "20")
    _price(session, user_id, security, today - timedelta(days=1), "21")

    body = client.get(f"{SECURITIES_URL}/{security.id}/overview", headers=headers).json()

    assert Decimal(body["latest"]["previous_close"]) == Decimal("20")
    assert Decimal(body["latest"]["day_change_native"]) == Decimal("1")
    assert body["positions"] == []
    assert body["totals"]["unrealized_gain_cad"] is None
    assert body["totals"]["day_change_cad"] is None


def test_overview_zero_price_previous_close_yields_no_pct(
    client: TestClient, session: Session
) -> None:
    """A degenerate zero previous close: the native delta still states the
    move, the percentage honestly refuses (division by zero)."""
    headers = _auth(client)
    user_id = _user_id(client, headers)
    security = _security(session, user_id, "ZERO")
    today = _today()
    _price(session, user_id, security, today - timedelta(days=2), "0")
    _price(session, user_id, security, today - timedelta(days=1), "5")

    body = client.get(f"{SECURITIES_URL}/{security.id}/overview", headers=headers).json()

    assert Decimal(body["latest"]["day_change_native"]) == Decimal("5")
    assert body["latest"]["day_change_pct"] is None


def _user_id(client: TestClient, headers: dict[str, str]) -> int:
    return client.get("/api/v1/users/me", headers=headers).json()["id"]

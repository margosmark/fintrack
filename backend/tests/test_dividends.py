"""Dividend income analytics contract tests (M14, docs/12 §Dividends).

Aggregation splits per action type, trade-date fx conversion from the cached
fx_rates table, year selection, zero-filled months, tenancy, and the CSV
variant. Seeding mirrors test_reports_performance.py (session helpers).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_tax_acb import _user, add_trade, make_account, make_security

from app.core.security import create_access_token
from app.models.accounts import AccountType
from app.models.fx_rates import FxRate, FxSource
from app.models.securities import Security
from app.models.trades import TradeAction as TA
from app.models.users import User

D = Decimal
API = "/api/v1"
DIVIDENDS_URL = f"{API}/reports/dividends"


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _extra_user(session: Session) -> User:
    """Second tenant — `_user` keys off id(session), which would collide when
    called twice inside one test."""
    user = User(email=f"m14-{uuid4().hex}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _seed(session: Session) -> tuple[User, dict[str, Security]]:
    """RY (CAD): one of each income action in 2023. VTI (USD): two 2024
    dividends straddling a rate change (1.36 → 1.30). EURX (EUR): 2024 income
    with NO seeded EUR rate → unresolved."""
    user = _user(session)
    account = make_account(session, user, "Brokerage", AccountType.brokerage_non_registered, True)
    ry = make_security(session, user, "RY", "CAD")
    vti = make_security(session, user, "VTI", "USD")
    eurx = make_security(session, user, "EURX", "EUR")

    add_trade(session, user, account, ry, TA.dividend, "2023-03-15", "0", "100.00")
    add_trade(session, user, account, ry, TA.distribution, "2023-06-20", "0", "50.00")
    add_trade(session, user, account, ry, TA.return_of_capital, "2023-09-01", "0", "25.00")
    # Trailing-12-months income (all securities, independent of selected year).
    add_trade(
        session,
        user,
        account,
        ry,
        TA.dividend,
        (date.today() - timedelta(days=10)).isoformat(),
        "0",
        "40.00",
    )

    add_trade(session, user, account, vti, TA.dividend, "2024-01-10", "0", "100.00")
    add_trade(session, user, account, vti, TA.dividend, "2024-02-10", "0", "200.00")
    add_trade(session, user, account, eurx, TA.dividend, "2024-05-01", "0", "70.00")
    # One non-income action must never leak into the report.
    add_trade(session, user, account, ry, TA.buy, "2023-01-05", "10", "120.00")

    session.add(
        FxRate(
            base="USD", quote="CAD", date=date(2024, 1, 5), rate=D("1.36"), source=FxSource.manual
        )
    )
    session.add(
        FxRate(
            base="USD", quote="CAD", date=date(2024, 2, 8), rate=D("1.30"), source=FxSource.manual
        )
    )
    session.commit()
    return user, {"RY": ry, "VTI": vti, "EURX": eurx}


def _get(client: TestClient, user: User, **params: object) -> dict:
    body = client.get(DIVIDENDS_URL, params=params, headers=_headers(user))
    assert body.status_code == 200, body.text
    return body.json()


# --- Aggregation ---------------------------------------------------------------


def test_aggregation_splits_by_action_type(client: TestClient, session: Session) -> None:
    user, _ = _seed(session)
    report = _get(client, user, year=2023)

    assert report["year"] == 2023
    assert len(report["items"]) == 1
    ry = report["items"][0]
    assert ry["symbol"] == "RY"
    assert D(str(ry["dividend_cad"])) == D("100.00")  # TTM trade lands in the current year
    assert D(str(ry["distribution_cad"])) == D("50.00")
    assert D(str(ry["roc_cad"])) == D("25.00")
    assert D(str(ry["total_cad"])) == D("175.00")
    assert ry["payment_count"] == 3
    assert ry["last_payment_date"] == "2023-09-01"

    totals = report["totals"]
    assert D(str(totals["dividend_cad"])) == D("100.00")
    assert D(str(totals["distribution_cad"])) == D("50.00")
    assert D(str(totals["roc_cad"])) == D("25.00")
    assert D(str(totals["total_cad"])) == D("175.00")

    months = report["by_month"]

    assert [m["month"] for m in months] == [f"2023-{m:02d}" for m in range(1, 13)]
    amounts = {m["month"]: D(str(m["total_cad"])) for m in months}
    assert amounts["2023-03"] == D("100.00")
    assert amounts["2023-06"] == D("50.00")
    assert amounts["2023-09"] == D("25.00")
    assert amounts["2023-01"] == D("0.00")

    empty = _get(client, user, year=2022)
    assert empty["items"] == []
    assert all(D(str(m["total_cad"])) == D("0.00") for m in empty["by_month"])
    assert len(empty["by_month"]) == 12


def test_ttm_trailing_365d_independent_of_selected_year(
    client: TestClient, session: Session
) -> None:
    user, _ = _seed(session)
    for params in ({}, {"year": 2023}, {"year": 2024}):
        report = _get(client, user, **params)
        assert D(str(report["ttm_total_cad"])) == D("40.00")


# --- Trade-date fx conversion ---------------------------------------------------


def test_conversion_uses_trade_date_rate(client: TestClient, session: Session) -> None:
    user, _ = _seed(session)
    report = _get(client, user, year=2024)

    vti = next(item for item in report["items"] if item["symbol"] == "VTI")
    # 100 USD @ newest rate ≤ 2024-01-10 (=1.36) + 200 USD @ newest ≤ 2024-02-10 (=1.30).
    assert D(str(vti["dividend_cad"])) == D("136.00") + D("260.00")
    assert D(str(vti["total_cad"])) == D("396.00")
    assert D(str(report["totals"]["total_cad"])) == D("396.00")
    assert D(str(report["by_month"][0]["total_cad"])) == D("136.00")
    assert D(str(report["by_month"][1]["total_cad"])) == D("260.00")


def test_unresolved_fx_counted_and_excluded(client: TestClient, session: Session) -> None:
    user, _ = _seed(session)
    report = _get(client, user, year=2024)

    assert report["unresolved_fx"] == 1  # the EUR dividend has no cached rate
    assert all(item["symbol"] != "EURX" for item in report["items"])
    assert D(str(report["totals"]["total_cad"])) == D("396.00")

    other_year = _get(client, user, year=2023)
    assert other_year["unresolved_fx"] == 0  # counter is scoped to the selected year


# --- Year selection ----------------------------------------------------------------


def test_available_years_desc_and_default_latest(client: TestClient, session: Session) -> None:
    user, _ = _seed(session)
    report = _get(client, user)  # no year → latest with rows
    # The trailing dividend is seeded 10 days back, so in early January it
    # lands in the PRIOR year — the year set (and the default "latest")
    # must follow the data, not the wall clock's year.
    recent = date.today() - timedelta(days=10)
    years = sorted({2023, 2024, recent.year}, reverse=True)
    assert report["available_years"] == years
    assert report["year"] == years[0]

    picked = _get(client, user, year=2023)
    assert picked["year"] == 2023
    assert picked["available_years"] == report["available_years"]


# --- Tenancy -------------------------------------------------------------------------


def test_tenancy_other_users_dividends_absent(client: TestClient, session: Session) -> None:
    user, _ = _seed(session)
    other = _extra_user(session)
    other_account = make_account(
        session, other, "Other Brokerage", AccountType.brokerage_non_registered, True
    )
    other_sec = make_security(session, other, "TD", "CAD")
    add_trade(session, other, other_account, other_sec, TA.dividend, "2023-04-01", "0", "999.00")
    session.commit()

    mine = _get(client, user, year=2023)
    assert [item["symbol"] for item in mine["items"]] == ["RY"]
    assert D(str(mine["totals"]["total_cad"])) == D("175.00")

    theirs = _get(client, other, year=2023)
    assert [item["symbol"] for item in theirs["items"]] == ["TD"]
    assert D(str(theirs["totals"]["total_cad"])) == D("999.00")
    assert D(str(theirs["totals"]["dividend_cad"])) == D("999.00")
    assert theirs["unresolved_fx"] == 0


# --- CSV variant -----------------------------------------------------------------------


def test_csv_variant_flat_items_attachment(client: TestClient, session: Session) -> None:
    user, _ = _seed(session)
    body = client.get(DIVIDENDS_URL, params={"year": 2023, "format": "csv"}, headers=_headers(user))
    assert body.status_code == 200
    assert body.headers["content-type"].startswith("text/csv")
    assert (
        'attachment; filename="dividends-2023-01-01-2023-12-31.csv"'
        in (body.headers["content-disposition"])
    )
    lines = body.text.strip().splitlines()
    assert lines[0] == (
        "security_id,symbol,name,dividend_cad,distribution_cad,"
        "roc_cad,total_cad,payment_count,last_payment_date"
    )
    assert len(lines) == 2  # flat items rows only — one security
    assert "RY," in lines[1]


# --- Year selection edge contracts ----------------------------------------------


def test_selected_year_excludes_other_years_and_by_month_sums(
    client: TestClient, session: Session
) -> None:
    """Yearly aggregation must isolate the selected year; the 12 by_month
    buckets must sum exactly to the year's totals (docs/12 §Dividends)."""
    user = _user(session)
    account = make_account(session, user, "Brokerage", AccountType.brokerage_non_registered, True)
    ry = make_security(session, user, "RY", "CAD")
    add_trade(session, user, account, ry, TA.dividend, "2023-03-15", "0", "100.00")
    add_trade(session, user, account, ry, TA.dividend, "2024-03-15", "0", "200.00")
    report = _get(client, user, year=2023)
    assert report["year"] == 2023
    assert D(str(report["totals"]["dividend_cad"])) == D("100.00")
    assert D(str(report["totals"]["total_cad"])) == D("100.00")
    by_month_total = sum(D(str(m["total_cad"])) for m in report["by_month"])
    assert by_month_total == D("100.00")
    assert report["unresolved_fx"] == 0

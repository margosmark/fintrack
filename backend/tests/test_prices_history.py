"""M126 — `GET /prices/history` (docs/08 §Price history, docs/12 §Range → granularity).

Cache-only chart series: window coverage, the documented auto-granularity
formula, the point roofline on an EXPLICIT ask, bucket-end valuation and the
tenancy/empty-state contracts the UI's "no data" honesty depends on.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.services.price_series import (
    MAX_SERIES_POINTS,
    pick_granularity,
    span_days,
)

HISTORY_URL = "/api/v1/prices/history"


def _auth(client: TestClient) -> dict[str, str]:
    email = f"{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _me_id(client: TestClient, headers: dict[str, str]) -> int:
    return client.get("/api/v1/users/me", headers=headers).json()["id"]


def _security(session: Session, user_id: int, symbol: str = "MSFT") -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} Inc.",
        kind="stock",
        listing_currency="USD",
        exchange="NMS",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    assert security.id is not None
    return security


def _seed_daily(session: Session, user_id: int, security_id: int, days: int) -> None:
    """`days` daily closes ending today (100.00, 100.01, ... ascending)."""
    today = date.today()
    rows = [
        Price(
            user_id=user_id,
            security_id=security_id,
            date=today - timedelta(days=offset),
            price=Decimal(f"{100 + (days - offset):.2f}"),
            source=PriceSource.yfinance,
        )
        for offset in range(days - 1, -1, -1)
    ]
    session.add_all(rows)
    session.commit()


def test_auto_granularity_follows_the_documented_formula(
    client: TestClient, session: Session
) -> None:
    """1M → day, 6M → week, 5Y → month (docs/12 §Range → granularity)."""
    headers = _auth(client)
    user_id = _me_id(client, headers)
    security = _security(session, user_id)
    _seed_daily(session, user_id, security.id, days=1900)

    today = date.today()
    cases = [
        (31, "day"),
        (92, "day"),
        (182, "week"),
        (365, "week"),
        (1826, "month"),
    ]
    for span, expected in cases:
        body = client.get(
            HISTORY_URL,
            params={"security_id": security.id, "from": (today - timedelta(days=span)).isoformat()},
            headers=headers,
        )
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["requested_granularity"] == "auto"
        assert payload["granularity"] == expected, f"span {span}d"
        # Every point sits inside the asked window and the series is ascending.
        dates = [point["date"] for point in payload["items"]]
        assert dates == sorted(dates)
        assert len(dates) == payload["count"]
        assert payload["count"] <= MAX_SERIES_POINTS


def test_pick_granularity_is_the_finest_fit(client: TestClient) -> None:
    assert pick_granularity(0) == "day"
    assert pick_granularity(31) == "day"
    assert pick_granularity(150) == "day"  # budget boundary
    assert pick_granularity(151) == "week"
    assert pick_granularity(1826) == "month"
    assert pick_granularity(3650) == "month"
    assert pick_granularity(365 * 40) == "year"
    assert span_days(None, date(2026, 1, 1)) == 0


def test_from_bounds_the_window_and_to_defaults_to_today(
    client: TestClient, session: Session
) -> None:
    headers = _auth(client)
    user_id = _me_id(client, headers)
    security = _security(session, user_id)
    _seed_daily(session, user_id, security.id, days=400)

    today = date.today()
    year_start = date(today.year, 1, 1)
    body = client.get(
        HISTORY_URL,
        params={"security_id": security.id, "from": year_start.isoformat()},
        headers=headers,
    )
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["to"] == today.isoformat()
    # Coverage: nothing before the floor, nothing after `to`, and the floor is
    # echoed so the client can assert the window it asked for.
    dates = [date.fromisoformat(point["date"]) for point in payload["items"]]
    assert dates, "YTD must return points"
    assert min(dates) >= year_start
    assert max(dates) <= today
    assert payload["from"] >= year_start.isoformat()
    assert dates == sorted(dates)


def test_no_floor_serves_the_whole_cached_history(client: TestClient, session: Session) -> None:
    headers = _auth(client)
    user_id = _me_id(client, headers)
    security = _security(session, user_id)
    _seed_daily(session, user_id, security.id, days=900)

    body = client.get(HISTORY_URL, params={"security_id": security.id}, headers=headers)
    payload = body.json()
    assert payload["requested_granularity"] == "auto"
    # 899d fits the week budget (129 points), month is the next step up.
    assert payload["granularity"] == "week"
    assert payload["from"] == (date.today() - timedelta(days=899)).isoformat()


def test_explicit_granularity_is_coarsened_past_the_roofline(
    client: TestClient, session: Session
) -> None:
    headers = _auth(client)
    user_id = _me_id(client, headers)
    security = _security(session, user_id)
    _seed_daily(session, user_id, security.id, days=600)

    body = client.get(
        HISTORY_URL,
        params={"security_id": security.id, "granularity": "day"},
        headers=headers,
    )
    payload = body.json()
    assert payload["requested_granularity"] == "day"
    assert payload["granularity"] == "week"  # 600 daily points > 400 roofline
    assert payload["count"] <= 400


def test_week_bucket_carries_the_last_close_inside_it(client: TestClient, session: Session) -> None:
    headers = _auth(client)
    user_id = _me_id(client, headers)
    security = _security(session, user_id)

    monday = date(2026, 3, 2)
    session.add_all(
        [
            Price(
                user_id=user_id,
                security_id=security.id,
                date=monday + timedelta(days=offset),
                price=Decimal(f"{100 + offset}.00"),
                source=PriceSource.yfinance,
            )
            for offset in range(5)  # Mon..Fri
        ]
    )
    session.commit()

    body = client.get(
        HISTORY_URL,
        params={
            "security_id": security.id,
            "from": monday.isoformat(),
            "to": (monday + timedelta(days=6)).isoformat(),
            "granularity": "week",
        },
        headers=headers,
    )
    payload = body.json()
    assert payload["granularity"] == "week"
    assert payload["count"] == 1
    point = payload["items"][0]
    # Valued at the bucket END, price = the last close inside the week.
    assert point["date"] == "2026-03-08"
    assert Decimal(point["price"]) == Decimal("104")


def test_from_after_to_is_invalid_date_range(client: TestClient, session: Session) -> None:
    headers = _auth(client)
    user_id = _me_id(client, headers)
    security = _security(session, user_id)

    body = client.get(
        HISTORY_URL,
        params={"security_id": security.id, "from": "2026-12-31", "to": "2026-01-01"},
        headers=headers,
    )
    assert body.status_code == 422
    assert body.json()["code"] == "invalid_date_range"


def test_a_security_with_no_history_answers_an_empty_series(
    client: TestClient, session: Session
) -> None:
    """Honest empty, never a fabricated line (docs/09 §Empty states)."""
    headers = _auth(client)
    user_id = _me_id(client, headers)
    security = _security(session, user_id, symbol="NEWCO")

    body = client.get(HISTORY_URL, params={"security_id": security.id}, headers=headers)
    assert body.status_code == 200
    payload = body.json()
    assert payload["items"] == []
    assert payload["count"] == 0
    assert payload["from"] == payload["to"] == date.today().isoformat()


def test_another_tenants_security_is_not_readable(client: TestClient, session: Session) -> None:
    headers_a = _auth(client)
    security = _security(session, _me_id(client, headers_a), symbol="PRIVATE")
    headers_b = _auth(client)

    body = client.get(HISTORY_URL, params={"security_id": security.id}, headers=headers_b)
    assert body.status_code == 404

    missing = client.get(HISTORY_URL, params={"security_id": 999999}, headers=headers_b)
    assert missing.status_code == 404


def test_the_series_never_fetches(client: TestClient, session: Session, monkeypatch) -> None:
    """Cache-only read: no yfinance call, so it works under privacy mode."""
    import app.services.market_data as market_data

    headers = _auth(client)
    user_id = _me_id(client, headers)
    security = _security(session, user_id)
    _seed_daily(session, user_id, security.id, days=40)

    def _explode(*args, **kwargs):
        raise AssertionError("price history must never fetch")

    monkeypatch.setattr(market_data, "fetch_history", _explode)
    monkeypatch.setattr(market_data, "fetch_latest_close", _explode)

    body = client.get(HISTORY_URL, params={"security_id": security.id}, headers=headers)
    assert body.status_code == 200
    assert body.json()["count"] > 0

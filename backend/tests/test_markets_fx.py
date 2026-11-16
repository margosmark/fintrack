"""fx service + router contract (w2-contract §BackendMarkets 4, docs/07).

All network edges are monkeypatched — no external HTTP in CI.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

import app.services.fx as fx_service
from app.models.fx_rates import FxRate, FxSource

FX_URL = "/api/v1/fx"


def _auth(client: TestClient) -> dict[str, str]:
    email = f"{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _today() -> date:
    return datetime.now(UTC).date()


def _usd_cad_rows(session: Session, day: date) -> list[FxRate]:
    return list(
        session.exec(
            select(FxRate).where(FxRate.base == "USD", FxRate.quote == "CAD", FxRate.date == day)
        )
    )


def _refuse_fetch(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("Valet fetch attempted but the cache should have served this request")


def _weekday_rates(start_date: date, end_date: date, rate: str) -> dict[date, Decimal]:
    rates: dict[date, Decimal] = {}
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() < 5:  # BoC publishes on business days only
            rates[cursor] = Decimal(rate)
        cursor += timedelta(days=1)
    return rates


def test_get_rate_cache_hit_skips_network(
    client: TestClient, session: Session, monkeypatch
) -> None:
    today = _today()
    session.add(
        FxRate(base="USD", quote="CAD", rate=Decimal("1.25"), date=today, source=FxSource.boc_valet)
    )
    session.commit()
    monkeypatch.setattr(fx_service, "fetch_valet_series", _refuse_fetch)

    response = client.get(FX_URL, params={"base": "USD", "quote": "CAD"}, headers=_auth(client))

    assert response.status_code == 200
    body = response.json()
    assert Decimal(str(body["rate"])) == Decimal("1.25")
    assert body["source"] == "boc_valet"
    assert body["date"] == today.isoformat()


def test_get_rate_miss_fetches_once_then_serves_from_cache_forever(
    client: TestClient, session: Session, monkeypatch
) -> None:
    # A Saturday: BoC publishes nothing, so the fetch must reach back to Friday.
    today = _today()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    calls: list[tuple[date, date]] = []

    def fake_fetch(base: str, quote: str, start_date: date, end_date: date) -> dict[date, Decimal]:
        calls.append((start_date, end_date))
        return _weekday_rates(start_date, end_date, "1.35")

    monkeypatch.setattr(fx_service, "fetch_valet_series", fake_fetch)
    headers = _auth(client)

    first = client.get(
        FX_URL,
        params={"base": "usd", "quote": "cad", "date": saturday.isoformat()},
        headers=headers,
    )

    assert first.status_code == 200
    assert len(calls) == 1  # exactly ONE Valet call on the miss
    body = first.json()
    assert body["source"] == "boc_valet"
    assert body["date"] == (saturday - timedelta(days=1)).isoformat()  # Friday rate
    friday_rows = _usd_cad_rows(session, saturday - timedelta(days=1))
    assert len(friday_rows) == 1 and friday_rows[0].source is FxSource.boc_valet

    calls.clear()
    second = client.get(
        FX_URL,
        params={"base": "USD", "quote": "CAD", "date": saturday.isoformat()},
        headers=headers,
    )
    assert second.status_code == 200
    assert Decimal(str(second.json()["rate"])) == Decimal("1.35")
    assert calls == []  # cached rows serve every later lookup


def test_get_rate_unavailable_returns_404_envelope_never_5xx(
    client: TestClient, session: Session, monkeypatch
) -> None:
    def exhausted(*args: Any, **kwargs: Any) -> None:
        raise fx_service.ValetUnavailable("all retries exhausted")

    monkeypatch.setattr(fx_service, "fetch_valet_series", exhausted)

    response = client.get(
        FX_URL,
        params={"base": "EUR", "quote": "CAD", "date": _today().isoformat()},
        headers=_auth(client),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "fx_unavailable"


def test_get_rate_manual_gap_fill_served_without_network(
    client: TestClient, session: Session, monkeypatch
) -> None:
    today = _today()
    session.add(
        FxRate(base="EUR", quote="CAD", rate=Decimal("1.47"), date=today, source=FxSource.manual)
    )
    session.commit()
    monkeypatch.setattr(fx_service, "fetch_valet_series", _refuse_fetch)

    response = client.get(
        FX_URL,
        params={"base": "EUR", "quote": "CAD", "date": today.isoformat()},
        headers=_auth(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert Decimal(str(body["rate"])) == Decimal("1.47")
    assert body["source"] == "manual"


def test_post_fx_rejects_boc_row_with_409_fx_date_not_gap(
    client: TestClient, session: Session
) -> None:
    today = _today()
    session.add(
        FxRate(base="USD", quote="CAD", rate=Decimal("1.25"), date=today, source=FxSource.boc_valet)
    )
    session.commit()

    response = client.post(
        FX_URL,
        json={"base": "USD", "quote": "CAD", "date": today.isoformat(), "rate": "9.99"},
        headers=_auth(client),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "fx_date_not_gap"
    stored = _usd_cad_rows(session, today)[0]
    assert Decimal(str(stored.rate)) == Decimal("1.25")  # BoC row untouched


def test_post_fx_fills_gap_then_first_writer_wins(client: TestClient, session: Session) -> None:
    today = _today()
    headers = _auth(client)

    created = client.post(
        FX_URL,
        json={"base": "gbp", "quote": "cad", "date": today.isoformat(), "rate": "1.72"},
        headers=headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["source"] == "manual"
    assert body["base"] == "GBP"  # normalized

    # M7: an existing row (manual OR fetched) is immutable through POST /fx —
    # first writer wins, so a re-POST cannot silently rewrite shared rates.
    rejected = client.post(
        FX_URL,
        json={"base": "GBP", "quote": "CAD", "date": today.isoformat(), "rate": "1.73"},
        headers=headers,
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "fx_date_not_gap"

    rows = list(session.exec(select(FxRate).where(FxRate.base == "GBP", FxRate.quote == "CAD")))
    assert len(rows) == 1
    assert rows[0].source is FxSource.manual
    assert Decimal(str(rows[0].rate)) == Decimal("1.72")  # original value intact


def test_range_query_backfills_whole_span_in_one_bulk_call(
    client: TestClient, session: Session, monkeypatch
) -> None:
    start = _today() - timedelta(days=10)
    end = _today()
    calls: list[tuple[date, date]] = []

    def fake_fetch(base: str, quote: str, start_date: date, end_date: date) -> dict[date, Decimal]:
        calls.append((start_date, end_date))
        return _weekday_rates(start_date, end_date, "1.30")

    monkeypatch.setattr(fx_service, "fetch_valet_series", fake_fetch)
    params = {"base": "USD", "quote": "CAD", "start_date": start, "end_date": end}
    headers = _auth(client)

    first = client.get(FX_URL, params=params, headers=headers)

    assert first.status_code == 200
    items = first.json()["items"]
    weekdays = sum(
        1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5
    )
    assert len(items) == weekdays
    assert len(calls) == 1  # ONE bulk backfill for the whole span

    calls.clear()
    second = client.get(FX_URL, params=params, headers=headers)
    assert second.status_code == 200
    assert calls == []  # fully covered span never re-fetches


def test_range_query_half_open_range_is_422_invalid_date_range(client: TestClient) -> None:
    response = client.get(
        FX_URL,
        params={"base": "USD", "quote": "CAD", "start_date": _today()},
        headers=_auth(client),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_date_range"

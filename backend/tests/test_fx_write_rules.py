"""POST /fx write rules + `fx_rates` source split (docs/07; findings H7/M7).

Covers: future dates rejected with 422 `fx_future_date`, no existing row
(fetched OR manual) can be overwritten through POST /fx (409
`fx_date_not_gap`), manual gap-fill still creates rows on empty past dates,
and `_store_fetched` inserts only empty dates (manual + boc_valet rows are
permanent). All network edges are monkeypatched — no external HTTP.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlmodel import Session, select

import app.services.fx as fx_service
from app.models.fx_rates import FxRate, FxSource

FX_URL = "/api/v1/fx"
D = Decimal


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


def _rows(session: Session, day: date) -> list[FxRate]:
    return list(
        session.exec(
            select(FxRate).where(FxRate.base == "USD", FxRate.quote == "CAD", FxRate.date == day)
        )
    )


# --- M7: future dates can never be pre-poisoned ------------------------------


def test_post_fx_future_date_is_422_fx_future_date(client: TestClient, session: Session) -> None:
    tomorrow = _today() + timedelta(days=1)
    response = client.post(
        FX_URL,
        json={"base": "USD", "quote": "CAD", "date": tomorrow.isoformat(), "rate": "9.99"},
        headers=_auth(client),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "fx_future_date"
    assert _rows(session, tomorrow) == []  # nothing pre-poisoned ahead of BoC


def test_post_fx_far_future_date_is_422_too(client: TestClient) -> None:
    far = _today() + timedelta(days=400)
    response = client.post(
        FX_URL,
        json={"base": "EUR", "quote": "CAD", "date": far.isoformat(), "rate": "1.11"},
        headers=_auth(client),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "fx_future_date"


def test_post_fx_today_is_still_allowed(client: TestClient) -> None:
    today = _today()
    response = client.post(
        FX_URL,
        json={"base": "USD", "quote": "CAD", "date": today.isoformat(), "rate": "1.31"},
        headers=_auth(client),
    )
    assert response.status_code == 201, response.text
    assert response.json()["source"] == "manual"


# --- manual gap-fill keeps working (spec: empty dates only) ------------------


def test_post_fx_manual_gap_fill_on_empty_past_date(client: TestClient, session: Session) -> None:
    past = _today() - timedelta(days=30)
    response = client.post(
        FX_URL,
        json={"base": "USD", "quote": "CAD", "date": past.isoformat(), "rate": "1.33"},
        headers=_auth(client),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source"] == "manual"
    assert Decimal(str(body["rate"])) == D("1.33")
    stored = _rows(session, past)
    assert len(stored) == 1 and stored[0].source is FxSource.manual


# --- M7: fetched rows can never be overwritten via POST /fx ------------------


def test_post_fx_over_fetched_row_is_409(client: TestClient, session: Session) -> None:
    today = _today()
    session.add(
        FxRate(base="USD", quote="CAD", rate=D("1.25"), date=today, source=FxSource.boc_valet)
    )
    session.commit()

    response = client.post(
        FX_URL,
        json={"base": "USD", "quote": "CAD", "date": today.isoformat(), "rate": "9.99"},
        headers=_auth(client),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "fx_date_not_gap"
    stored = _rows(session, today)
    assert len(stored) == 1
    assert Decimal(str(stored[0].rate)) == D("1.25")  # fetched row untouched
    assert stored[0].source is FxSource.boc_valet


def test_post_fx_over_manual_row_is_409_first_writer_wins(
    client: TestClient, session: Session
) -> None:
    """docs/07/docs/08: an existing (BoC OR manual) row answers 409 — the
    shared table has no user_id, so cross-tenant overwrite must be impossible."""
    day = _today() - timedelta(days=7)
    session.add(FxRate(base="GBP", quote="CAD", rate=D("1.70"), date=day, source=FxSource.manual))
    session.commit()

    headers = _auth(client)
    response = client.post(
        FX_URL,
        json={"base": "GBP", "quote": "CAD", "date": day.isoformat(), "rate": "1.99"},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "fx_date_not_gap"
    stored = list(
        session.exec(
            select(FxRate).where(FxRate.base == "GBP", FxRate.quote == "CAD", FxRate.date == day)
        )
    )
    assert len(stored) == 1
    assert Decimal(str(stored[0].rate)) == D("1.70")  # other tenant's row untouched


# --- source split: _store_fetched writes empty dates only ---------------------


def test_store_fetched_fills_gaps_only_never_overwrites(session: Session, monkeypatch) -> None:
    monday = _today() - timedelta(days=_today().weekday() + 14)  # a Monday two weeks back
    manual_day, fetched_day, gap_day = (
        monday,
        monday + timedelta(days=1),
        monday + timedelta(days=2),
    )
    session.add(
        FxRate(base="USD", quote="CAD", rate=D("1.11"), date=manual_day, source=FxSource.manual)
    )
    session.add(
        FxRate(base="USD", quote="CAD", rate=D("1.22"), date=fetched_day, source=FxSource.boc_valet)
    )
    session.commit()

    calls: list[tuple[date, date]] = []

    def fake_fetch(base: str, quote: str, start_date: date, end_date: date) -> dict[date, Decimal]:
        calls.append((start_date, end_date))
        return {
            manual_day: D("9.99"),  # both existing rows are offered — neither may change
            fetched_day: D("9.99"),
            gap_day: D("1.33"),
        }

    monkeypatch.setattr(fx_service, "fetch_valet_series", fake_fetch)
    rows = fx_service.get_rates_between(session, "USD", "CAD", manual_day, gap_day)

    assert len(calls) == 1  # ONE bulk fetch for the span
    by_day = {row.date: row for row in rows}
    # Manual gap-fill survives the fetch (user data, permanent).
    assert by_day[manual_day].source is FxSource.manual
    assert Decimal(str(by_day[manual_day].rate)) == D("1.11")
    # Existing fetched row is not re-written either.
    assert by_day[fetched_day].source is FxSource.boc_valet
    assert Decimal(str(by_day[fetched_day].rate)) == D("1.22")
    # Only the empty date received the fetched rate.
    assert by_day[gap_day].source is FxSource.boc_valet
    assert Decimal(str(by_day[gap_day].rate)) == D("1.33")
    assert len(_rows(session, manual_day)) == 1 and len(_rows(session, gap_day)) == 1

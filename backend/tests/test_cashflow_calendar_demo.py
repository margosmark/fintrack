"""M52 acceptance against the REAL demo tenant (docs/36 Verification §12).

The plan's acceptance is about what the *demo month* shows: bill events on the
seeded due dates (docs/30), dividend events on the M29 next-payment dates and —
since M170 (docs/36 §M170) — the subscription series equal to the tenant's
CONFIRMED set, which for a demo tenant that never answered a recurring
candidate is exactly its seeded bills and nothing else. This file seeds the demo
through `POST /auth/demo` in its own hermetic `<suite-db>_demo` database (the
pattern `tests/test_demo_seed.py` established) so committed demo rows never
leak into the suite database the other modules roll back against.

Network is patched to raise for the calendar read itself — every input is a
cached row (docs/07, docs/12); the seed runs before the patch is installed.
"""

from __future__ import annotations

import calendar as calendar_mod
import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config
from conftest import drop_database_force
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlmodel import Session

from alembic import command
from app.core.config import get_settings
from app.services import fx as fx_service
from app.services import market_data
from app.services.confirmed_subscriptions import confirmed_subscriptions
from app.services.dividend_calendar import compute_dividend_calendar
from app.services.subscriptions import detect_subscriptions
from app.services.txn_filters import today_et

API = "/api/v1"
CAL = f"{API}/reports/cashflow-calendar"

_BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _demo_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest disables lifespan seeding globally; this suite opts back in."""
    monkeypatch.setattr(get_settings(), "seed_demo_account", True)


@pytest.fixture(scope="module", autouse=True)
def _demo_database() -> Iterator[None]:
    """Hermetic `<suite-db>_demo` DB (named from the SUITE database so two
    agents can never drop each other's database mid-run)."""
    base = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://fintrack:fintrack@localhost:5432/fintrack_test",
    )
    suite_db = base.rsplit("/", 1)[-1]
    demo_db = f"{suite_db}_demo"
    demo_url = base.rsplit("/", 1)[0] + f"/{demo_db}"
    admin = create_engine(base.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        drop_database_force(conn, demo_db)
        conn.execute(text(f'CREATE DATABASE "{demo_db}"'))
    admin.dispose()

    import app.api.deps as deps

    # From the swap onward everything runs under try/finally: a generator
    # fixture that raises BEFORE its `yield` never executes the code after
    # it, so the restore used to be skipped and every later test in the
    # session kept running against `<suite>_demo` (t_435c2d14).
    os.environ["DATABASE_URL"] = demo_url
    try:
        get_settings().seed_demo_account = True
        get_settings.cache_clear()
        deps._engine = None
        cfg = Config(str(_BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
        cfg.set_main_option("sqlalchemy.url", demo_url)
        command.upgrade(cfg, "head")
        yield
    finally:
        os.environ["DATABASE_URL"] = base
        get_settings.cache_clear()
        deps._engine = None


def _forbid_network(*_args, **_kwargs):
    raise AssertionError("network fetch attempted while rendering the calendar")


def _month_of(day: date) -> str:
    return day.strftime("%Y-%m")


def _events_for(client: TestClient, headers: dict[str, str], month: str) -> dict[str, list[dict]]:
    response = client.get(CAL, headers=headers, params={"month": month})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["month"] == month
    expected_days = calendar_mod.monthrange(*map(int, month.split("-")))[1]
    assert len(body["days"]) == expected_days  # zero-filled, never short
    assert [day["date"] for day in body["days"]] == [
        f"{month}-{index:02d}" for index in range(1, expected_days + 1)
    ]
    return {day["date"]: day["events"] for day in body["days"]}


def test_demo_month_shows_bills_dividends_and_no_unconfirmed_subscriptions(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = client.post(f"{API}/auth/demo")
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    user_id = login.json()["user"]["id"]

    # Everything below is served from cached rows: any fetch is a failure.
    monkeypatch.setattr(market_data, "fetch_latest_close", _forbid_network)
    monkeypatch.setattr(market_data, "fetch_history", _forbid_network)
    monkeypatch.setattr(fx_service, "fetch_valet_series", _forbid_network)

    # --- bills: an event on every seeded due date (docs/30) -----------------
    bills = client.get(f"{API}/bills", headers=headers).json()
    assert len(bills) >= 5, f"expected the seeded demo bills, got {len(bills)}"
    bill_months = sorted({_month_of(date.fromisoformat(bill["next_due_date"])) for bill in bills})
    kinds_seen: set[str] = set()
    seen_bill_events = 0
    for month in bill_months:
        days = _events_for(client, headers, month)
        for bill in bills:
            if _month_of(date.fromisoformat(bill["next_due_date"])) != month:
                continue
            matching = [
                event
                for event in days[bill["next_due_date"]]
                if event["kind"] == "bill" and event["label"] == bill["name"]
            ]
            assert matching, f"no bill event for {bill['name']} on {bill['next_due_date']}"
            assert matching[0]["source_id"] == str(bill["id"])
            if bill["amount"] is not None:
                assert matching[0]["amount"] == f"-{bill['amount']}"
                assert matching[0]["currency"] == "CAD"
            else:
                assert matching[0]["amount"] is None
                assert matching[0]["currency"] is None
            seen_bill_events += 1
            kinds_seen.add("bill")
    assert seen_bill_events >= 1

    # --- dividends: an event on every M29 next-payment date -----------------
    dividend_view = compute_dividend_calendar(session, user_id, today=today_et())
    assert dividend_view["upcoming"], "the demo tenant must project dividend income"
    seen_dividend_events = 0
    for entry in dividend_view["upcoming"]:
        day = entry["next_expected_date"]
        days = _events_for(client, headers, _month_of(day))
        matching = [
            event
            for event in days[day.isoformat()]
            if event["kind"] == "dividend" and event["source_id"] == str(entry["security_id"])
        ]
        assert matching, f"no dividend event on {day} for {entry['symbol']}"
        assert matching[0]["label"] == f"{entry['symbol']} dividend"
        assert matching[0]["amount"] == (
            str(entry["avg_amount"]) if entry["avg_amount"] is not None else None
        )
        assert matching[0]["currency"] == entry["currency"]
        assert (matching[0]["amount"] is None) == (entry["currency"] is None)
        seen_dividend_events += 1
        kinds_seen.add("dividend")
    assert seen_dividend_events >= 1

    # --- subscriptions: CONFIRMED state only (M170) --------------------------
    # The demo tenant's detector DOES find recurring payees — the seeded
    # ledger has monthly Spotify/Netflix charges, a variable utility and a
    # grocery/dining tail — and every one of them is either already a bill
    # (docs/30 seeds five) or a group nobody answered. So the subscription
    # series is honestly EMPTY here: a demo tenant that never accepted a
    # recurring candidate has nothing to draw under that colour, and the
    # count says why (docs/36 §M170).
    detected = detect_subscriptions(session, user_id)
    assert detected, "the demo tenant must still have DETECTED subscriptions"
    months = set(bill_months) | {today_et().strftime("%Y-%m")}
    months |= {
        _month_of(subscription["next_expected"])
        for subscription in detected
        if subscription["next_expected"] is not None
    }
    days_with_subscription = [
        (month, day, event["label"])
        for month in sorted(months)
        for day, events in _events_for(client, headers, month).items()
        for event in events
        if event["kind"] == "subscription"
    ]
    assert days_with_subscription == [], (
        f"no detector guess may reach the calendar: {days_with_subscription}"
    )
    # The whole confirmed set is the seeded bills — nothing else.
    confirmed = confirmed_subscriptions(session, user_id)
    assert {entry.source for entry in confirmed} == {"bill"}
    assert len(confirmed) == len([bill for bill in bills if bill.get("enabled", True)])
    body = client.get(CAL, headers=headers).json()
    assert body["confirmed_subscription_count"] == len(confirmed)
    assert kinds_seen == {"bill", "dividend"}


def test_demo_read_is_allowed_and_privacy_neutral(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo write boundary blocks mutations only (docs/20); the calendar is
    a read of the caller's own rows with no LLM/external call, so privacy mode
    has nothing to gate here."""
    login = client.post(f"{API}/auth/demo")
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    monkeypatch.setattr(market_data, "fetch_latest_close", _forbid_network)
    monkeypatch.setattr(market_data, "fetch_history", _forbid_network)
    monkeypatch.setattr(fx_service, "fetch_valet_series", _forbid_network)

    current = today_et().strftime("%Y-%m")
    assert client.get(CAL, headers=headers, params={"month": current}).status_code == 200
    # default month (no param) also answers 200 for the demo tenant
    assert client.get(CAL, headers=headers).status_code == 200
    # the demo boundary is untouched: a mutation is still refused
    patch = client.patch(f"{API}/users/me", headers=headers, json={"display_name": "nope"})
    assert patch.status_code == 403
    assert patch.json()["code"] == "demo_account_restricted"

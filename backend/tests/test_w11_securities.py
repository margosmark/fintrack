"""W11 backend contract (w11-contract BackendSecurities pins 1-5):
investment-account gating on trades, historical backfill on security
creation / watchlist add, POST /portfolio/refresh fan-out, watchlist CRUD,
and the 0003 migration up/down.

Network edges (yfinance) are monkeypatched — no external HTTP. An autouse
fixture replaces backfill_range/ensure_recent_price with recording fakes so
accidental test data never triggers real fetches.
"""

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import app.services.market_data as market_data
from alembic import command
from app.models.users import User

API = "/api/v1"
SEC_URL = f"{API}/securities"
TRADES_URL = f"{API}/trades"
PRICES_URL = f"{API}/prices"


@pytest.fixture(autouse=True)
def fake_market(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Record every market-data seam call; default no-ops keep tests offline."""
    calls: dict[str, list] = {"backfill": [], "ensure": []}

    def fake_backfill(session, security, start_date, end_date, **_kwargs):
        calls["backfill"].append((security.id, (end_date - start_date).days))

    def fake_ensure(session, security, today, **_kwargs):
        calls["ensure"].append(security.id)

    monkeypatch.setattr(market_data, "backfill_range", fake_backfill)
    monkeypatch.setattr(market_data, "ensure_recent_price", fake_ensure)
    return calls


def _auth(client: TestClient) -> dict[str, str]:

    email = f"w11-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _account(client: TestClient, headers: dict[str, str], type_: str) -> dict:
    body = client.post(
        f"{API}/accounts",
        json={"name": f"Acct {type_}", "type": type_, "currency": "CAD"},
        headers=headers,
    )
    assert body.status_code == 201, body.text
    return body.json()


def _security(client: TestClient, headers: dict[str, str], symbol: str) -> dict:
    body = client.post(
        SEC_URL,
        json={
            "symbol": symbol,
            "name": f"{symbol} Inc.",
            "kind": "stock",
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
        headers=headers,
    )
    assert body.status_code == 201, body.text
    return body.json()


def _trade(client: TestClient, headers: dict[str, str], account_id: int, security_id: int):
    return client.post(
        TRADES_URL,
        json={
            "account_id": account_id,
            "security_id": security_id,
            "action": "buy",
            "date": date.today().isoformat(),
            "quantity": "1",
            "price_native": "50",
        },
        headers=headers,
    )


# --- pin 1: investment-account gating ----------------------------------------


def test_trade_on_chequing_account_rejected(client: TestClient) -> None:
    headers = _auth(client)
    chequing = _account(client, headers, "chequing")
    tfsa = _account(client, headers, "tfsa")
    security = _security(client, headers, "RY")

    rejected = _trade(client, headers, chequing["id"], security["id"])
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "account_not_investment"

    accepted = _trade(client, headers, tfsa["id"], security["id"])
    assert accepted.status_code == 201, accepted.text


def test_registered_plan_types_accept_trades(client: TestClient) -> None:
    headers = _auth(client)
    for type_ in ("rrsp", "fhsa", "lira"):
        account = _account(client, headers, type_)
        security = _security(client, headers, f"X{type_[:3].upper()}")
        response = _trade(client, headers, account["id"], security["id"])
        assert response.status_code == 201, response.text


def test_patch_moving_trade_to_non_investment_account_rejected(client: TestClient) -> None:
    headers = _auth(client)
    tfsa = _account(client, headers, "tfsa")
    chequing = _account(client, headers, "chequing")
    security = _security(client, headers, "TD")
    trade = _trade(client, headers, tfsa["id"], security["id"])
    assert trade.status_code == 201

    moved = client.patch(
        f"{TRADES_URL}/{trade.json()['id']}",
        json={"account_id": chequing["id"]},
        headers=headers,
    )
    assert moved.status_code == 422
    assert moved.json()["code"] == "account_not_investment"

    # The rejected PATCH must not have mutated the trade.
    unchanged = client.get(f"{TRADES_URL}/{trade.json()['id']}", headers=headers).json()
    assert unchanged["account_id"] == tfsa["id"]


# --- pin 2: historical backfill dispatched on creation -----------------------


def test_backfill_dispatched_on_security_create(client: TestClient, fake_market) -> None:
    headers = _auth(client)
    created = _security(client, headers, "BMO")

    assert len(fake_market["backfill"]) == 1
    security_id, span_days = fake_market["backfill"][0]
    assert security_id == created["id"]
    assert span_days == 365


def test_watchlist_add_dispatches_backfill(client: TestClient, fake_market) -> None:
    headers = _auth(client)
    security = _security(client, headers, "ENB")  # one backfill from the create itself
    assert len(fake_market["backfill"]) == 1

    watched = client.post(f"{API}/watchlist", json={"security_id": security["id"]}, headers=headers)
    assert watched.status_code == 201
    assert len(fake_market["backfill"]) == 2
    assert fake_market["backfill"][1][0] == security["id"]


# --- pin 3: price refresh fan-out --------------------------------------------


def test_refresh_queues_held_union_watched(client: TestClient, session, fake_market) -> None:
    headers = _auth(client)
    user_id = client.get(f"{API}/users/me", headers=headers).json()["id"]
    user = session.get(User, user_id)

    from app.models.securities import Security

    def direct_security(symbol: str) -> Security:
        security = Security(
            user_id=user.id,
            symbol=symbol,
            name=f"{symbol} Inc.",
            kind="stock",
            listing_currency="CAD",
            exchange="TSX",
        )
        session.add(security)
        session.commit()
        session.refresh(security)
        return security

    held_a = direct_security("AAA")
    held_b = direct_security("BBB")
    watched_c = direct_security("CCC")

    account = _account(client, headers, "tfsa")
    assert _trade(client, headers, account["id"], held_a.id).status_code == 201
    assert _trade(client, headers, account["id"], held_b.id).status_code == 201
    for watched in (held_b, watched_c):
        added = client.post(f"{API}/watchlist", json={"security_id": watched.id}, headers=headers)
        assert added.status_code == 201

    # M117: the button runs the same 365-day backfill as
    # POST /securities/{id}/refresh — not `ensure_recent_price`, whose 7-day
    # window made the whole fan-out a no-op for a freshly-fetched quote.
    backfills_before = len(fake_market["backfill"])
    response = client.post(f"{API}/portfolio/refresh", headers=headers)
    assert response.status_code == 202
    # M141: `skipped_dead` names the rows the sweep did NOT queue (docs/07).
    assert response.json() == {"queued": 3, "skipped_dead": 0}
    queued = fake_market["backfill"][backfills_before:]
    assert {security_id for security_id, _span in queued} == {
        held_a.id,
        held_b.id,
        watched_c.id,
    }
    assert all(span_days == 365 for _security_id, span_days in queued)


def test_refresh_with_no_holdings_or_watchlist_queues_zero(client: TestClient) -> None:
    headers = _auth(client)
    response = client.post(f"{API}/portfolio/refresh", headers=headers)
    assert response.status_code == 202
    assert response.json() == {"queued": 0, "skipped_dead": 0}


# --- pin 4: watchlist routes --------------------------------------------------


def test_watchlist_add_duplicate_get_delete_shapes(client: TestClient) -> None:
    headers = _auth(client)

    empty = client.get(f"{API}/watchlist", headers=headers)
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "total": 0}

    # Foreign / unknown security ids are 404.
    foreign = client.post(f"{API}/watchlist", json={"security_id": 999999}, headers=headers)
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "not_found"

    security = _security(client, headers, "XIU")
    added = client.post(f"{API}/watchlist", json={"security_id": security["id"]}, headers=headers)
    assert added.status_code == 201
    assert added.json()["security_id"] == security["id"]

    duplicate = client.post(
        f"{API}/watchlist", json={"security_id": security["id"]}, headers=headers
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "already_watched"

    listed = client.get(f"{API}/watchlist", headers=headers)
    assert listed.status_code == 200
    page = listed.json()
    assert page["total"] == 1
    item = page["items"][0]
    assert set(item) == {
        "security_id",
        "symbol",
        "name",
        "listing_currency",
        "latest_price",
        "price_source",
        "price_date",
        "price_age_days",
        # Option A (t_bd5ec95f): REAL provider-fetch evidence rides the shape —
        # null when no fetch ever stamped the row (this row: no price at all).
        "price_fetched_at",
        "price_stale",
        # M141 (docs/08 §Watchlist): the dead-symbol state rides every row so
        # the UI can say "no public quote" instead of the forever-"yet" "no price".
        "dead_symbol",
        "dead_symbol_reason",
    }
    # No fetch evidence = null, never a fabricated instant (t_bd5ec95f).
    assert item["price_fetched_at"] is None
    assert item["symbol"] == "XIU"
    assert item["latest_price"] is None and item["price_source"] is None
    assert item["price_age_days"] is None and item["price_stale"] is None
    assert item["dead_symbol"] is False and item["dead_symbol_reason"] is None

    removed = client.delete(f"{API}/watchlist/{security['id']}", headers=headers)
    assert removed.status_code == 204
    assert client.get(f"{API}/watchlist", headers=headers).json()["total"] == 0

    gone = client.delete(f"{API}/watchlist/{security['id']}", headers=headers)
    assert gone.status_code == 404


def test_watchlist_reports_latest_price_and_staleness(client: TestClient) -> None:
    headers = _auth(client)
    fresh = _security(client, headers, "FRES")
    stale_sec = _security(client, headers, "STAL")
    today = date.today()

    for security, age in ((fresh, 2), (stale_sec, 10)):
        priced = client.post(
            PRICES_URL,
            json={
                "security_id": security["id"],
                "date": (today - timedelta(days=age)).isoformat(),
                "price": "30.25",
            },
            headers=headers,
        )
        assert priced.status_code in (200, 201), priced.text
        watched = client.post(
            f"{API}/watchlist", json={"security_id": security["id"]}, headers=headers
        )
        assert watched.status_code == 201
    items = {
        row["symbol"]: row
        for row in client.get(f"{API}/watchlist", headers=headers).json()["items"]
    }
    assert Decimal(str(items["FRES"]["latest_price"])) == Decimal("30.25")
    assert items["FRES"]["price_source"] == "manual"
    assert items["FRES"]["price_age_days"] == 2
    assert items["FRES"]["price_date"] == (today - timedelta(days=2)).isoformat()
    # A MANUAL price has no provider fetch (Option A, t_bd5ec95f): null, never
    # a fabricated instant.
    assert items["FRES"]["price_fetched_at"] is None
    assert items["FRES"]["price_stale"] is False

    assert items["STAL"]["price_age_days"] == 10
    assert items["STAL"]["price_date"] == (today - timedelta(days=10)).isoformat()
    assert items["STAL"]["price_fetched_at"] is None
    assert items["STAL"]["price_stale"] is True


# --- pin 5: migration 0003 up/down on the isolated DB -------------------------


def test_migration_watchlist_up_down() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    engine = create_engine(url.render_as_string(hide_password=False))

    def table_exists() -> bool:
        with engine.connect() as conn:
            return bool(
                conn.execute(text("SELECT 1 FROM pg_tables WHERE tablename = 'watchlist'")).scalar()
            )

    try:
        command.downgrade(cfg, "0002_widen_account_types")
        assert not table_exists()
        command.upgrade(cfg, "head")
        assert table_exists()
        with engine.connect() as conn:
            unique = (
                conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'watchlist'::regclass AND contype = 'u'"
                    )
                )
                .scalars()
                .all()
            )
            assert unique == ["uq_watchlist_user_security"]
    finally:
        command.upgrade(cfg, "head")  # restore head for any later tests
        engine.dispose()

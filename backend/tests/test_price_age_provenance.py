"""Price-fetch provenance — Option A (t_bd5ec95f, reversing t_29881f89).

The prices table now records a REAL provider-fetch event: `prices.fetched_at`
(alembic `0057_price_fetched_at`, nullable, NO backfill) is stamped ONLY by
`market_data._cache_closes` from the provider-response ARRIVAL instant —
captured inside `_fetch_one_history` the moment `Ticker.history()` answers and
published as `market_data.last_fetch_at()`. `created_at` is row insertion and
`updated_at` is any mutation (a manual edit included); neither is provenance
and neither may ever leak into the field.

What this suite pins:

* the stamp lands on provider fetches — inserts, the intraday advance, and a
  NO-OP re-fetch of the displayed row (the provider was really contacted even
  though the value held) — and nowhere else;
* a manual price (POST /prices upsert, PATCH /prices/{id}) always clears it;
  human data has no provider fetch time;
* rows with no evidence surface `price_fetched_at: null` on BOTH payloads and
  the client-side rule is "null = no segment" (frontend pins live in
  `frontend/tests/priceAgeHonesty.test.tsx`);
* the stamp write is BOUNDED: a re-fetch refreshes only the newest
  (displayed) existing row, never per-page-load churn across history;
* market-date facts (`price_age_days`, `price_date`, `price_stale` with its
  >7 boundary) are computed ONLY from `price_date` and are unaffected;
* the migration backfills nothing; demo seed rows stay NULL; tenant scoping
  shows each tenant only its own row's stamp.

Both app clocks are FROZEN to one fixed date per test (the "controlled times"
the t_29881f89 ruling asked for): `valuation._today()` (UTC-based) drives the
positions age, while `api/watchlist._to_read` calls local `date.today()` — on
an evening box the two calendars differ by a day, so un-frozen "today" seeds
make the two surfaces disagree about the same row. One frozen date makes every
age exact.

Network edges are monkeypatched — no external HTTP in CI. The refresh
scenarios drive the REAL `_cache_closes` with controlled closes, because that
is the write path production actually runs.
"""

import os
import random
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlmodel import Session as SqlModelSession
from sqlmodel import select

import app.services.market_data as market_data
from alembic import command
from app.api import watchlist as watchlist_api
from app.models.accounts import Account, AccountType
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.services import demo_seed, valuation

# Bound at import time — BEFORE the autouse fixture replaces the module
# attribute with a no-op — so a test can drive the REAL seam.
real_ensure_recent_price = market_data.ensure_recent_price

PORTFOLIO_URL = "/api/v1/portfolio"
WATCHLIST_URL = "/api/v1/watchlist"
PRICES_URL = "/api/v1/prices"

# The one date every app clock in these tests answers with (a Sunday — no
# market-session semantics are in play anywhere in these scenarios).
FROZEN_TODAY = date(2026, 3, 15)

# Two provably-distinct provider-response instants for the scenarios below.
STAMP_1 = datetime(2026, 3, 15, 13, 0, 45, tzinfo=UTC)
STAMP_2 = datetime(2026, 3, 15, 15, 42, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze BOTH app calendars: positions read `valuation._today()`, the
    watchlist reads local `date.today()`."""

    class _FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return FROZEN_TODAY

    monkeypatch.setattr(valuation, "_today", lambda: FROZEN_TODAY)
    monkeypatch.setattr(watchlist_api, "date", _FixedDate)


@pytest.fixture(autouse=True)
def fake_market(monkeypatch: pytest.MonkeyPatch):
    """Keep tests offline: the watchlist add queues a backfill (W11) and the
    positions read may fan out `ensure_recent_price`; both become no-ops."""

    def forbidden(*args, **kwargs):
        raise AssertionError("network fetch attempted in a provenance test")

    monkeypatch.setattr(market_data, "backfill_range", lambda *a, **k: None)
    monkeypatch.setattr(market_data, "ensure_recent_price", lambda *a, **k: None)
    monkeypatch.setattr(market_data, "fetch_latest_close", forbidden)
    monkeypatch.setattr(market_data, "fetch_history", forbidden)


def _register(client: TestClient, tag: str) -> dict[str, str]:
    email = f"provenance-{tag}-{datetime.now(UTC).timestamp()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _user_id(client: TestClient, headers: dict[str, str]) -> int:
    return client.get("/api/v1/users/me", headers=headers).json()["id"]


def _account(session: Session, user_id: int, currency: str = "CAD") -> Account:
    account = Account(
        user_id=user_id,
        name=f"Brokerage {currency}",
        type=AccountType.brokerage_non_registered,
        currency=currency,
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _security(session: Session, user_id: int, symbol: str, currency: str = "CAD") -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind="stock",
        listing_currency=currency,
        exchange="TSX" if currency == "CAD" else "NMS",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _trade(session: Session, user_id: int, account: Account, security: Security) -> None:
    session.add(
        Trade(
            user_id=user_id,
            account_id=account.id,
            security_id=security.id,
            action=TradeAction.buy,
            date=FROZEN_TODAY,
            quantity=Decimal("10"),
            price_native=Decimal("50"),
        )
    )
    session.commit()


def _watch(client: TestClient, headers: dict[str, str], security_id: int) -> None:
    resp = client.post(WATCHLIST_URL, json={"security_id": security_id}, headers=headers)
    assert resp.status_code == 201, resp.text


def _position(client: TestClient, headers: dict[str, str]) -> dict:
    items = client.get(f"{PORTFOLIO_URL}/positions", headers=headers).json()["items"]
    assert len(items) == 1
    return items[0]


def _watched(client: TestClient, headers: dict[str, str]) -> dict:
    items = client.get(WATCHLIST_URL, headers=headers).json()["items"]
    assert len(items) == 1
    return items[0]


def _assert_no_fetch_evidence(payload: dict) -> None:
    """No fetch ever stamped this row → the key is NULL, never an instant
    derived from created_at/updated_at/date (Option A, t_bd5ec95f)."""
    assert payload.get("price_fetched_at") is None


def _assert_fetch_stamp(payload: dict, stamp: datetime) -> None:
    """The payload claims EXACTLY the provider-response instant — no more."""
    raw = payload.get("price_fetched_at")
    assert raw is not None, "a real provider fetch must publish its stamp"
    assert datetime.fromisoformat(raw) == stamp


# --- a real fetch stamps both surfaces ---------------------------------------


def test_real_fetch_stamps_both_surfaces_with_the_provider_instant(
    client: TestClient, session: Session
) -> None:
    headers = _register(client, "same-day")
    user_id = _user_id(client, headers)
    account = _account(session, user_id)
    security = _security(session, user_id, "SHOP")
    _trade(session, user_id, account, security)
    _watch(client, headers, security.id)

    # First scheduled refresh of the day writes the close, stamped with the
    # provider-response arrival instant…
    assert (
        market_data._cache_closes(
            session,
            security,
            {FROZEN_TODAY: Decimal("100.00")},
            today=FROZEN_TODAY,
            fetched_at=STAMP_1,
        )
        == 1
    )
    # …and a LATER refresh the same day advances it (the intraday walk) with
    # its OWN instant: the stamp is the newest provider contact, not the
    # row's insertion.
    assert (
        market_data._cache_closes(
            session,
            security,
            {FROZEN_TODAY: Decimal("101.50")},
            intraday_update=True,
            today=FROZEN_TODAY,
            fetched_at=STAMP_2,
        )
        == 1
    )

    position = _position(client, headers)
    _assert_fetch_stamp(position, STAMP_2)
    assert Decimal(str(position["price"])) == Decimal("101.50")
    assert position["price_date"] == FROZEN_TODAY.isoformat()
    assert position["price_age_days"] == 0
    assert position["price_source"] == "yfinance"
    assert position["price_stale"] is False

    watched = _watched(client, headers)
    _assert_fetch_stamp(watched, STAMP_2)
    assert Decimal(str(watched["latest_price"])) == Decimal("101.50")
    assert watched["price_age_days"] == 0


# --- no-op refresh (the critical provenance case) ----------------------------


def test_no_op_fetch_refreshes_the_stamp_without_moving_the_price(
    client: TestClient, session: Session
) -> None:
    headers = _register(client, "no-op")
    user_id = _user_id(client, headers)
    account = _account(session, user_id)
    security = _security(session, user_id, "MSFT")
    _trade(session, user_id, account, security)
    _watch(client, headers, security.id)

    assert (
        market_data._cache_closes(
            session,
            security,
            {FROZEN_TODAY: Decimal("300.00")},
            today=FROZEN_TODAY,
            fetched_at=STAMP_1,
        )
        == 1
    )
    row = session.exec(select(Price).where(Price.security_id == security.id)).one()
    created_at = row.created_at

    # Second refresh, same value: the provider WAS contacted — the value just
    # held. Price count stays 0 (nothing MOVED) but the displayed row's fetch
    # evidence MUST advance, or a no-op fetch would render as "never fetched".
    assert (
        market_data._cache_closes(
            session,
            security,
            {FROZEN_TODAY: Decimal("300.00")},
            intraday_update=True,
            today=FROZEN_TODAY,
            fetched_at=STAMP_2,
        )
        == 0
    )
    session.refresh(row)
    assert row.price == Decimal("300.00")
    assert row.created_at == created_at
    assert row.fetched_at == STAMP_2

    position = _position(client, headers)
    _assert_fetch_stamp(position, STAMP_2)
    assert Decimal(str(position["price"])) == Decimal("300.00")
    assert position["price_age_days"] == 0

    watched = _watched(client, headers)
    _assert_fetch_stamp(watched, STAMP_2)
    assert Decimal(str(watched["latest_price"])) == Decimal("300.00")


# --- the stamp is bounded to the newest (displayed) existing row -------------


def test_stamp_refresh_is_bounded_to_the_newest_existing_row(
    client: TestClient, session: Session
) -> None:
    """A re-run over an already-cached history (the request-path fan-out, a
    symbol repair) must not churn every historical row: only `max(closes)` —
    the row every surface displays — refreshes its stamp."""
    headers = _register(client, "bounded")
    user_id = _user_id(client, headers)
    security = _security(session, user_id, "BOUN")
    old_stamp = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
    history = {
        FROZEN_TODAY - timedelta(days=2): Decimal("10.00"),
        FROZEN_TODAY - timedelta(days=1): Decimal("11.00"),
        FROZEN_TODAY: Decimal("12.00"),
    }
    assert market_data._cache_closes(session, security, history, fetched_at=old_stamp) == 3

    # Same closes re-fetched (no value moved anywhere): zero price writes, and
    # the stamp refresh touches ONLY the newest row.
    assert (
        market_data._cache_closes(
            session,
            security,
            dict(history),
            intraday_update=True,
            today=FROZEN_TODAY,
            fetched_at=STAMP_2,
        )
        == 0
    )
    rows = {
        row.date: row
        for row in session.exec(select(Price).where(Price.security_id == security.id)).all()
    }
    assert rows[FROZEN_TODAY - timedelta(days=2)].fetched_at == old_stamp
    assert rows[FROZEN_TODAY - timedelta(days=1)].fetched_at == old_stamp
    assert rows[FROZEN_TODAY].fetched_at == STAMP_2


# --- ensure_recent_price passes the context stamp through --------------------


def test_ensure_recent_price_plumbs_the_provider_arrival_instant(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public valuation seam: the fake fetch sets the SAME context stamp
    the real fetch site sets, so what lands on the row is the arrival
    instant — proof the plumbing runs through `ensure_recent_price`, not just
    the direct `_cache_closes` tests above."""
    headers = _register(client, "plumbing")
    user_id = _user_id(client, headers)
    security = _security(session, user_id, "PLMB")

    def fake_fetch(symbol: str, today: date | None = None):
        market_data._last_fetch_at.set(STAMP_2)
        return FROZEN_TODAY, Decimal("55.00")

    monkeypatch.setattr(market_data, "fetch_latest_close", fake_fetch)
    price = real_ensure_recent_price(session, security, FROZEN_TODAY)
    assert price is not None
    session.refresh(price)
    assert price.fetched_at == STAMP_2


# --- manual precedence -------------------------------------------------------


def test_manual_price_precedence_never_stamps_and_never_overwrites(
    client: TestClient, session: Session
) -> None:
    headers = _register(client, "manual")
    user_id = _user_id(client, headers)
    account = _account(session, user_id)
    security = _security(session, user_id, "BMO")
    _trade(session, user_id, account, security)
    _watch(client, headers, security.id)

    # An older provider close (stamped), then a NEWER manual row — manual
    # wins (docs/07) and the manual row carries NO fetch evidence.
    assert (
        market_data._cache_closes(
            session,
            security,
            {FROZEN_TODAY - timedelta(days=3): Decimal("45.00")},
            today=FROZEN_TODAY,
            fetched_at=STAMP_1,
        )
        == 1
    )
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=FROZEN_TODAY,
            price=Decimal("47.25"),
            source=PriceSource.manual,
            fetched_at=None,
        )
    )
    session.commit()
    # A later provider refresh must NOT touch the manual row (manual wins,
    # permanently) — including its (absent) fetch stamp.
    assert (
        market_data._cache_closes(
            session,
            security,
            {FROZEN_TODAY: Decimal("48.00")},
            intraday_update=True,
            today=FROZEN_TODAY,
            fetched_at=STAMP_2,
        )
        == 0
    )

    position = _position(client, headers)
    _assert_no_fetch_evidence(position)
    assert Decimal(str(position["price"])) == Decimal("47.25")
    assert position["price_source"] == "manual"
    assert position["price_age_days"] == 0

    watched = _watched(client, headers)
    _assert_no_fetch_evidence(watched)
    assert Decimal(str(watched["latest_price"])) == Decimal("47.25")
    assert watched["price_source"] == "manual"


def test_manual_override_clears_the_fetch_stamp(client: TestClient, session: Session) -> None:
    """After a human edit the value is HUMAN data: POST /prices (upsert) and
    PATCH /prices/{id} both CLEAR the stamp — a provider fetch time for a
    hand-entered number would be a lie."""
    headers = _register(client, "override")
    user_id = _user_id(client, headers)
    security = _security(session, user_id, "OVRA")
    assert (
        market_data._cache_closes(
            session,
            security,
            {FROZEN_TODAY: Decimal("60.00")},
            today=FROZEN_TODAY,
            fetched_at=STAMP_1,
        )
        == 1
    )
    row = session.exec(select(Price).where(Price.security_id == security.id)).one()
    assert row.fetched_at == STAMP_1

    # The upsert path: same (security, date) flips the row to manual and
    # clears the evidence.
    upserted = client.post(
        PRICES_URL,
        json={
            "security_id": security.id,
            "date": FROZEN_TODAY.isoformat(),
            "price": "61.00",
        },
        headers=headers,
    )
    assert upserted.status_code == 200, upserted.text
    assert upserted.json()["price_fetched_at"] is None
    session.refresh(row)
    assert row.source == PriceSource.manual
    assert row.fetched_at is None

    # The patch path: same verdict, whatever the row's current source.
    patched = client.patch(
        f"{PRICES_URL}/{row.id}",
        json={"price": "62.00"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["price_fetched_at"] is None
    session.refresh(row)
    assert row.fetched_at is None

    # And a brand-new manual row never carries one either.
    created = client.post(
        PRICES_URL,
        json={
            "security_id": security.id,
            "date": (FROZEN_TODAY - timedelta(days=1)).isoformat(),
            "price": "59.00",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["price_fetched_at"] is None


# --- null (no price at all) --------------------------------------------------


def test_no_price_row_still_reports_no_evidence(client: TestClient, session: Session) -> None:
    headers = _register(client, "null")
    user_id = _user_id(client, headers)
    account = _account(session, user_id)
    security = _security(session, user_id, "NEWC")
    _trade(session, user_id, account, security)
    _watch(client, headers, security.id)

    position = _position(client, headers)
    _assert_no_fetch_evidence(position)
    assert position["price"] is None
    assert position["price_date"] is None
    assert position["price_age_days"] is None
    assert position["price_stale"] is None
    assert position["price_source"] is None

    watched = _watched(client, headers)
    _assert_no_fetch_evidence(watched)
    assert watched["latest_price"] is None
    assert watched["price_age_days"] is None
    assert watched["price_stale"] is None


# --- created_at / updated_at can NEVER become fetch evidence -----------------


def test_row_timestamps_in_any_timezone_never_become_fetch_evidence(
    client: TestClient, session: Session
) -> None:
    """Two rows with the SAME market date but wildly different `created_at`
    stamps (one a +05:30 offset years in the past, one a UTC offset skewed
    into the future — clock skew) must produce identical age facts: age is
    calendar-day math on `price_date` only, and `created_at`/`updated_at`
    NEVER surface as `price_fetched_at` — that leak is the backfill this
    migration deliberately does not perform."""
    headers = _register(client, "tz")
    user_id = _user_id(client, headers)
    account = _account(session, user_id)
    held = _security(session, user_id, "HELD")
    watched_sec = _security(session, user_id, "WATCHED")
    _trade(session, user_id, account, held)
    _watch(client, headers, watched_sec.id)
    market_day = FROZEN_TODAY - timedelta(days=2)

    past_stamp = datetime(2020, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    future_stamp = datetime.now(UTC) + timedelta(days=3)
    session.add(
        Price(
            user_id=user_id,
            security_id=held.id,
            date=market_day,
            price=Decimal("60.00"),
            source=PriceSource.yfinance,
            created_at=past_stamp,
            updated_at=past_stamp,
        )
    )
    session.add(
        Price(
            user_id=user_id,
            security_id=watched_sec.id,
            date=market_day,
            price=Decimal("70.00"),
            source=PriceSource.yfinance,
            created_at=future_stamp,
            updated_at=future_stamp,
        )
    )
    session.commit()

    position = _position(client, headers)
    _assert_no_fetch_evidence(position)
    assert position["price_date"] == market_day.isoformat()
    assert position["price_age_days"] == 2
    assert Decimal(str(position["price"])) == Decimal("60.00")

    watched = _watched(client, headers)
    _assert_no_fetch_evidence(watched)
    assert watched["price_date"] == market_day.isoformat()
    assert watched["price_age_days"] == 2
    assert Decimal(str(watched["latest_price"])) == Decimal("70.00")


# --- stale boundary (>7 days; exactly 7 is fresh) ----------------------------


@pytest.mark.parametrize("age, stale", [(7, False), (8, True)])
def test_stale_boundary_is_unchanged_by_fetch_provenance(
    client: TestClient, session: Session, age: int, stale: bool
) -> None:
    headers = _register(client, f"boundary-{age}")
    user_id = _user_id(client, headers)
    account = _account(session, user_id)
    security = _security(session, user_id, "TD")
    _trade(session, user_id, account, security)
    _watch(client, headers, security.id)
    market_day = FROZEN_TODAY - timedelta(days=age)
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=market_day,
            price=Decimal("80.00"),
            source=PriceSource.manual,
        )
    )
    session.commit()

    position = _position(client, headers)
    _assert_no_fetch_evidence(position)
    assert position["price_age_days"] == age
    assert position["price_stale"] is stale

    watched = _watched(client, headers)
    _assert_no_fetch_evidence(watched)
    assert watched["price_age_days"] == age
    assert watched["price_stale"] is stale


# --- tenancy -----------------------------------------------------------------


def test_tenant_isolation_scopes_the_stamp_to_its_own_row(
    client: TestClient, session: Session
) -> None:
    headers_a = _register(client, "tenant-a")
    headers_b = _register(client, "tenant-b")
    user_a = _user_id(client, headers_a)
    user_b = _user_id(client, headers_b)

    sec_a = _security(session, user_a, "AAAA")
    sec_b = _security(session, user_b, "BBBB")
    _trade(session, user_a, _account(session, user_a), sec_a)
    _trade(session, user_b, _account(session, user_b), sec_b)
    _watch(client, headers_a, sec_a.id)
    _watch(client, headers_b, sec_b.id)

    stamp_a = datetime(2019, 6, 1, 12, 0, tzinfo=UTC)
    session.add(
        Price(
            user_id=user_a,
            security_id=sec_a.id,
            date=FROZEN_TODAY,
            price=Decimal("11.00"),
            source=PriceSource.yfinance,
            fetched_at=stamp_a,
        )
    )
    session.add(
        Price(
            user_id=user_b,
            security_id=sec_b.id,
            date=FROZEN_TODAY,
            price=Decimal("22.00"),
            source=PriceSource.yfinance,
            fetched_at=None,
        )
    )
    session.commit()

    position_a = _position(client, headers_a)
    position_b = _position(client, headers_b)
    assert position_a["symbol"] == "AAAA"
    assert position_b["symbol"] == "BBBB"
    # Each tenant sees EXACTLY its own row's evidence — tenant A's stamp
    # never reaches tenant B, and B's absent evidence stays null.
    _assert_fetch_stamp(position_a, stamp_a)
    _assert_no_fetch_evidence(position_b)

    watched_a = _watched(client, headers_a)
    watched_b = _watched(client, headers_b)
    _assert_fetch_stamp(watched_a, stamp_a)
    _assert_no_fetch_evidence(watched_b)


# --- demo seed ---------------------------------------------------------------


def test_demo_seed_rows_carry_no_fetch_stamp(client: TestClient, session: Session) -> None:
    """The demo tenant is synthetic MANUAL data (docs/30): no provider was
    ever contacted for it, so `_seed_prices` rows must stay NULL."""
    headers = _register(client, "demo-seed")
    user_id = _user_id(client, headers)
    user = session.get(User, user_id)
    assert user is not None
    securities = {
        symbol: _security(session, user_id, symbol) for symbol, _s, _e in demo_seed._PRICE_ARCS
    }

    demo_seed._seed_prices(session, user, random.Random(0), FROZEN_TODAY, securities)  # noqa: S311 -- seeded

    rows = session.exec(select(Price).where(Price.user_id == user_id)).all()
    assert len(rows) > 0
    assert all(row.source == PriceSource.manual for row in rows)
    assert all(row.fetched_at is None for row in rows)


# --- migration: NO backfill --------------------------------------------------


def test_migration_0057_backfills_nothing(client: TestClient, session: Session) -> None:
    """A row that existed BEFORE 0057 must come out of the upgrade with
    `fetched_at IS NULL` — copying created_at/updated_at in would fabricate
    the exact fetch evidence this column exists to make honest."""
    url = make_url(os.environ["DATABASE_URL"])
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    engine = create_engine(url.render_as_string(hide_password=False))
    # A COMMITTED tenant of my own: the alembic DDL and the raw INSERT below
    # run on connections that cannot see the test session's uncommitted
    # (savepoint-mode) writes. Every row this creates is deleted in finally —
    # a test that COMMITS must delete every one (AGENTS §5).
    # `expire_on_commit=False` is load-bearing here, not convenience: with the
    # default expiry every `own.commit()` (there is one per row, plus the one
    # that closes the refresh's read) would re-arm a lazy reload, and the NEXT
    # attribute read — `security.id` feeding the INSERT below — would reopen an
    # idle-in-transaction READ on `securities`. Alembic's 0058 step then takes
    # `ALTER TABLE … ENABLE ROW LEVEL SECURITY` (ACCESS EXCLUSIVE) against it
    # and the suite-wide `lock_timeout = '500ms'` cancels the migration
    # (LockNotAvailable — QA t_592a6e1a red 1). Nothing in this test asserts on
    # ORM state after DDL: the checks are raw queries on `engine` below.
    own = SqlModelSession(engine, expire_on_commit=False)
    user = User(
        email=f"mig0057-{datetime.now(UTC).timestamp()}@example.com",
        hashed_password="not-a-real-hash",
    )
    own.add(user)
    own.commit()
    own.refresh(user)
    security = Security(
        user_id=user.id,  # type: ignore[arg-type]
        symbol="MIGR",
        name="MIGR long name",
        kind="stock",
        listing_currency="CAD",
        exchange="TSX",
    )
    own.add(security)
    own.commit()
    own.refresh(security)
    # End the refresh's read transaction BEFORE the range downgrade. The
    # downgrade now runs through 0058's `revoke_rls`, and its DROP POLICY on
    # `securities` takes ACCESS EXCLUSIVE: an idle-in-transaction session
    # holding even a plain SELECT blocks it until the suite-wide
    # `lock_timeout = '500ms'` cancels the statement (LockNotAvailable —
    # QA t_592a6e1a red 1). `own` is needed again in `finally`, so end the
    # transaction rather than closing the session.
    own.commit()
    try:
        command.downgrade(cfg, "0056_dividend_retry_backoff")
        with engine.begin() as conn:
            # The column does not exist at 0056 — this row is "pre-existing".
            conn.execute(
                text(
                    "INSERT INTO prices (user_id, security_id, date, price, source)"
                    " VALUES (:u, :s, :d, 42.5, 'yfinance')"
                ),
                {"u": user.id, "s": security.id, "d": FROZEN_TODAY.isoformat()},
            )
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            column = conn.execute(
                text(
                    "SELECT data_type, is_nullable FROM information_schema.columns"
                    " WHERE table_name = 'prices' AND column_name = 'fetched_at'"
                )
            ).one()
            assert column == ("timestamp with time zone", "YES")
            stamped = conn.execute(
                text("SELECT fetched_at FROM prices WHERE user_id = :u AND security_id = :s"),
                {"u": user.id, "s": security.id},
            ).scalar_one()
            assert stamped is None, "0057 must NEVER backfill fetch evidence"
    finally:
        command.upgrade(cfg, "head")  # restore head for any later tests
        for price_row in own.exec(select(Price).where(Price.user_id == user.id)).all():  # type: ignore[arg-type]
            own.delete(price_row)
        own.delete(security)
        own.delete(user)
        own.commit()
        own.close()
        engine.dispose()

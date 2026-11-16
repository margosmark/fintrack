"""Pending-import scope leak for TRADES (docs/03 staging model).

Transactions have a shared committed-only predicate
(`app.services.txn_filters.committed_tx_conditions`, docs/29 F1). The
investment-CSV path stages `trades` with the SAME `import_batch_id` but had no
equivalent predicate, so a batch sitting in `pending_review` (or `discarded`)
moved positions, ACB pools, dividends, tax alerts, benchmarks, the dividend
calendar and the security-detail page BEFORE the user ever committed it.

These tests pin the contract: staged/discarded trades are invisible to every
trade-derived LIVE calculation, while the import review endpoints (preview,
commit, discard) keep seeing their staged rows — that is the only window into
them (docs/03, mirroring `GET /imports/{id}/rows`).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.acb_pools import AcbPool
from app.models.accounts import Account, AccountType
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.services import benchmarks, dividend_calendar, dividends, tax_alerts, valuation
from app.services import reports as reports_service
from app.tax.acb import compute_user_tax_state, rebuild_user_pools

API = "/api/v1"


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _auth(client: TestClient) -> tuple[dict[str, str], int]:
    from datetime import UTC, datetime

    email = f"stagescope-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    user_id = client.get(f"{API}/users/me", headers=headers).json()["id"]
    return headers, user_id


def _account(session: Session, user_id: int, type_: str = "brokerage_non_registered") -> Account:
    account = Account(
        user_id=user_id,
        name=f"Acct {type_} {user_id}",
        type=AccountType(type_),
        currency="CAD",
        taxable=type_ == "brokerage_non_registered",
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _security(session: Session, user_id: int, symbol: str) -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} Inc",
        kind="stock",
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _batch(session: Session, user_id: int, account_id: int, status: str) -> ImportBatch:
    batch = ImportBatch(
        user_id=user_id,
        account_id=account_id,
        institution="investment_csv",
        file_name="staged.csv",
        status=ImportBatchStatus(status),
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def _trade(
    session: Session,
    user_id: int,
    account: Account,
    security: Security,
    action: str,
    *,
    batch_id: int | None,
    quantity: str = "0",
    price_native: str = "0",
    day: dt.date | None = None,
    currency: str | None = None,
) -> Trade:
    trade = Trade(
        user_id=user_id,
        account_id=account.id,
        security_id=security.id,
        action=TradeAction(action),
        date=day or _today(),
        quantity=Decimal(quantity),
        price_native=Decimal(price_native),
        commission=Decimal("0"),
        currency=currency,
        import_batch_id=batch_id,
    )
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def _price(session: Session, user_id: int, security: Security, day: dt.date, price: str) -> None:
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=day,
            price=Decimal(price),
            source=PriceSource.manual,
        )
    )
    session.commit()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every input is a cached row; any fetch attempt is a test failure."""

    def forbidden(*args, **kwargs):
        raise AssertionError("network fetch attempted although all data is cached")

    from app.services import fx as fx_service
    from app.services import market_data

    monkeypatch.setattr(market_data, "fetch_latest_close", forbidden)
    monkeypatch.setattr(market_data, "fetch_history", forbidden)
    monkeypatch.setattr(fx_service, "fetch_valet_series", forbidden)


# ---------------------------------------------------------------------------
# ACB replay (the pool state every tax figure is derived from)
# ---------------------------------------------------------------------------


def test_acb_replay_ignores_pending_and_discarded_buys(
    client: TestClient, session: Session
) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _price(session, user_id, sec, _today(), "20.00")

    _trade(
        session, user_id, account, sec, "buy", batch_id=None, quantity="10", price_native="20.00"
    )
    pending = _batch(session, user_id, account.id, "pending_review")
    discarded = _batch(session, user_id, account.id, "discarded")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="500",
        price_native="20.00",
    )
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=discarded.id,
        quantity="500",
        price_native="20.00",
    )

    state = compute_user_tax_state(session, user_id)
    pool = state.pools[(account.id, sec.id)]
    assert pool.quantity == Decimal("10")
    assert pool.total_acb_cad == Decimal("200.00")
    # Staged rows are not even acquisitions: they cannot trip the
    # superficial-loss rule or the oversell guard.
    assert [a.trade_id for a in state.acquisitions] == [
        t.id
        for t in session.exec(
            select(Trade).where(Trade.user_id == user_id, Trade.import_batch_id.is_(None))
        ).all()
    ]


def test_rebuild_user_pools_never_persists_staged_quantity(
    client: TestClient, session: Session
) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _trade(
        session, user_id, account, sec, "buy", batch_id=None, quantity="10", price_native="20.00"
    )
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="500",
        price_native="20.00",
    )

    counts = rebuild_user_pools(session, user_id)
    session.commit()

    # `trades` in the return payload is the replayed row count, not a raw
    # COUNT(*) over the table — staged rows must not inflate it.
    assert counts["trades"] == 1
    pools = session.exec(select(AcbPool).where(AcbPool.user_id == user_id)).all()
    assert len(pools) == 1
    assert pools[0].quantity == Decimal("10")
    assert pools[0].total_acb_cad == Decimal("200.00")


def test_staged_sell_cannot_reduce_or_oversell_committed_pools(
    client: TestClient, session: Session
) -> None:
    """A staged sell is invisible, so it neither realizes a gain nor trips
    the oversell guard on a committed position."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _trade(
        session, user_id, account, sec, "buy", batch_id=None, quantity="10", price_native="10.00"
    )
    pending = _batch(session, user_id, account.id, "pending_review")
    # Sell far more than is held; while staged this must not raise `oversell`.
    _trade(
        session,
        user_id,
        account,
        sec,
        "sell",
        batch_id=pending.id,
        quantity="-99",
        price_native="50.00",
    )

    state = compute_user_tax_state(session, user_id)
    pool = state.pools[(account.id, sec.id)]
    assert pool.quantity == Decimal("10")
    assert state.disposals == []


def test_superficial_loss_rule_ignores_staged_repurchase(
    client: TestClient, session: Session
) -> None:
    """A committed loss + a STAGED buyback must NOT be denied: the staged
    row is not a repurchase until the batch is committed."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    # Bought at 100, sold at 40 → a real, committed loss.
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=None,
        quantity="10",
        price_native="100.00",
        day=_today() - dt.timedelta(days=45),
    )
    _trade(
        session,
        user_id,
        account,
        sec,
        "sell",
        batch_id=None,
        quantity="-10",
        price_native="40.00",
        day=_today() - dt.timedelta(days=40),
    )
    # A partial repurchase, staged only.
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="5",
        price_native="40.00",
        day=_today() - dt.timedelta(days=38),
    )

    state = compute_user_tax_state(session, user_id)
    assert [d.gain_cad for d in state.disposals] == [Decimal("-600.00")]
    assert state.denials == []


# ---------------------------------------------------------------------------
# Positions / valuation (the source every other live view reads)
# ---------------------------------------------------------------------------


def test_compute_positions_excludes_staged_buys_and_sells(
    client: TestClient, session: Session
) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _price(session, user_id, sec, _today(), "20.00")
    _trade(
        session, user_id, account, sec, "buy", batch_id=None, quantity="10", price_native="20.00"
    )

    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="100",
        price_native="20.00",
    )

    positions = valuation.compute_positions(session, user_id, today=_today())
    assert len(positions) == 1
    assert positions[0]["quantity"] == Decimal("10.000000")
    assert positions[0]["market_value_cad"] == Decimal("200.00")


def test_position_created_only_by_a_staged_buy_is_invisible(
    client: TestClient, session: Session
) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _price(session, user_id, sec, _today(), "20.00")
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="100",
        price_native="20.00",
    )

    assert valuation.compute_positions(session, user_id, today=_today()) == []


def test_positions_endpoint_hides_staged_quantity(client: TestClient, session: Session) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _price(session, user_id, sec, _today(), "20.00")
    _trade(
        session, user_id, account, sec, "buy", batch_id=None, quantity="10", price_native="20.00"
    )
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="100",
        price_native="20.00",
    )

    r = client.get(f"{API}/portfolio/positions", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert Decimal(body["items"][0]["quantity"]) == Decimal("10.000000")


def test_corporate_action_split_staged_does_not_rewrite_quantity(
    client: TestClient, session: Session
) -> None:
    """quantity>0 is the RESULTING absolute share count — a staged split must
    not silently replace the committed position."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _price(session, user_id, sec, _today(), "10.00")
    _trade(
        session, user_id, account, sec, "buy", batch_id=None, quantity="10", price_native="10.00"
    )

    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "corporate_action_adjustment",
        batch_id=pending.id,
        quantity="40",
        price_native="0",
    )

    positions = valuation.compute_positions(session, user_id, today=_today())
    assert len(positions) == 1
    assert positions[0]["quantity"] == Decimal("10.000000")


def test_registered_account_book_cost_replay_excludes_staged_buys(
    client: TestClient, session: Session
) -> None:
    """Registered accounts have no pool, so their book cost is a pure replay of
    `trades` — the staged filter must apply there too (w12 pin 1)."""
    _headers, user_id = _auth(client)
    registered = _account(session, user_id, "rrsp")
    sec = _security(session, user_id, "ABCD")
    _price(session, user_id, sec, _today(), "20.00")
    _trade(
        session, user_id, registered, sec, "buy", batch_id=None, quantity="10", price_native="20.00"
    )

    pending = _batch(session, user_id, registered.id, "pending_review")
    _trade(
        session,
        user_id,
        registered,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="100",
        price_native="200.00",
    )

    positions = valuation.compute_positions(session, user_id, today=_today())
    assert len(positions) == 1
    assert positions[0]["quantity"] == Decimal("10.000000")
    assert positions[0]["book_cost_cad"] == Decimal("200.00")


# ---------------------------------------------------------------------------
# Dividends / dividend calendar
# ---------------------------------------------------------------------------


def test_dividend_report_ignores_staged_income(client: TestClient, session: Session) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _trade(
        session,
        user_id,
        account,
        sec,
        "dividend",
        batch_id=None,
        quantity="0",
        price_native="100.00",
        day=_today(),
    )
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "dividend",
        batch_id=pending.id,
        quantity="0",
        price_native="900.00",
        day=_today(),
    )

    report = dividends.dividend_report(session, user_id)
    assert report["totals"]["dividend_cad"] == Decimal("100.00")
    assert report["items"][0]["payment_count"] == 1


def test_dividend_report_available_years_exclude_staged_only_years(
    client: TestClient, session: Session
) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "dividend",
        batch_id=pending.id,
        quantity="0",
        price_native="50.00",
        day=_today().replace(month=1, day=15),
    )

    report = dividends.dividend_report(session, user_id, year=_today().year)
    assert report["available_years"] == []
    assert report["items"] == []


def test_dividend_calendar_ignores_staged_income(client: TestClient, session: Session) -> None:
    """The calendar is a LIVE projection built from real payment history; a
    staged payment must not become a projected upcoming one."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    pending = _batch(session, user_id, account.id, "pending_review")
    for offset in (0, 40):
        _trade(
            session,
            user_id,
            account,
            sec,
            "dividend",
            batch_id=pending.id,
            quantity="0",
            price_native="25.00",
            day=_today() - dt.timedelta(days=offset),
        )

    result = dividend_calendar.compute_dividend_calendar(session, user_id, today=_today())
    assert result["upcoming"] == []
    assert result["summary"]["this_year"] is None
    assert result["summary"]["by_currency"] == []


def test_dividend_calendar_uses_committed_history(client: TestClient, session: Session) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    for offset in (10, 40):
        _trade(
            session,
            user_id,
            account,
            sec,
            "dividend",
            batch_id=None,
            quantity="0",
            price_native="25.00",
            day=_today() - dt.timedelta(days=offset),
        )
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "dividend",
        batch_id=pending.id,
        quantity="0",
        price_native="900.00",
        day=_today(),
    )

    result = dividend_calendar.compute_dividend_calendar(session, user_id, today=_today())
    assert len(result["upcoming"]) == 1
    entry = result["upcoming"][0]
    assert entry["avg_amount"] == Decimal("25.00")
    assert entry["num_payments"] == 2


# ---------------------------------------------------------------------------
# Tax-loss alerts
# ---------------------------------------------------------------------------


def test_dividend_calendar_http_money_is_decimal_string_and_scalar_null(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    security = _security(session, user_id, "WIRE")
    today = _today()
    for offset in (10, 40):
        _trade(
            session,
            user_id,
            account,
            security,
            "dividend",
            batch_id=None,
            price_native="25.00",
            day=today - dt.timedelta(days=offset),
            currency="USD",
        )

    response = client.get(f"{API}/reports/dividend-calendar", headers=headers)
    assert response.status_code == 200
    body = response.json()
    entry = body["upcoming"][0]
    assert entry["currency"] == "USD"
    assert entry["avg_amount"] == "25.00"
    assert entry["annual_projection"] == "300.00"
    assert body["summary"]["this_month"] is None
    assert body["summary"]["this_quarter"] is None
    assert body["summary"]["this_year"] is None
    subtotal = body["summary"]["by_currency"][0]
    assert subtotal["currency"] == "USD"
    assert all(
        isinstance(subtotal[key], str) for key in ("this_month", "this_quarter", "this_year")
    )


def test_dividend_calendar_subtotals_are_currency_denominated_and_withhold_mixed(
    client: TestClient, session: Session
) -> None:
    _, user_id = _auth(client)
    account = _account(session, user_id)
    today = dt.date(2026, 6, 15)
    last_payment = dt.date(2026, 5, 31)  # next monthly date = June 30
    previous_payment = dt.date(2026, 5, 1)

    # A CAD account with a USD listing and legacy NULL trade currency remains CAD.
    cad = _security(session, user_id, "CADX")
    cad.listing_currency = "USD"
    session.add(cad)
    session.commit()
    for day in (previous_payment, last_payment):
        _trade(
            session,
            user_id,
            account,
            cad,
            "dividend",
            batch_id=None,
            price_native="118.40",
            day=day,
        )

    # An explicit USD trade denomination wins over the CAD account.
    usd = _security(session, user_id, "USDX")
    for day in (previous_payment, last_payment):
        _trade(
            session,
            user_id,
            account,
            usd,
            "dividend",
            batch_id=None,
            price_native="85.00",
            day=day,
            currency="USD",
        )

    # All contributing rows must agree; the mixed security is withheld as one
    # projection, while dates and payment count survive.
    mixed = _security(session, user_id, "MIX")
    _trade(
        session,
        user_id,
        account,
        mixed,
        "dividend",
        batch_id=None,
        price_native="10.00",
        day=previous_payment,
        currency="CAD",
    )
    _trade(
        session,
        user_id,
        account,
        mixed,
        "dividend",
        batch_id=None,
        price_native="10.00",
        day=last_payment,
        currency="USD",
    )

    # A known denomination still gets zero-filled summary keys when the next
    # yearly payment is outside the month, quarter and year windows.
    future = _security(session, user_id, "FUTURE")
    for day in (dt.date(2026, 1, 1), last_payment):
        _trade(
            session,
            user_id,
            account,
            future,
            "dividend",
            batch_id=None,
            price_native="20.00",
            day=day,
            currency="EUR",
        )

    result = dividend_calendar.compute_dividend_calendar(session, user_id, today=today)
    entries = {entry["symbol"]: entry for entry in result["upcoming"]}
    assert entries["CADX"]["currency"] == "CAD"
    assert entries["CADX"]["avg_amount"] == Decimal("118.40")
    assert entries["USDX"]["currency"] == "USD"
    assert entries["USDX"]["avg_amount"] == Decimal("85.00")
    assert entries["MIX"]["currency"] is None
    assert entries["MIX"]["avg_amount"] is None
    assert entries["MIX"]["annual_projection"] is None
    assert entries["MIX"]["num_payments"] == 2
    assert entries["MIX"]["next_expected_date"] == dt.date(2026, 6, 30)

    summary = result["summary"]
    assert summary["this_month"] is summary["this_quarter"] is summary["this_year"] is None
    assert summary["by_currency"] == [
        {
            "currency": "CAD",
            "this_month": "118.40",
            "this_quarter": "118.40",
            "this_year": "118.40",
        },
        {"currency": "EUR", "this_month": "0.00", "this_quarter": "0.00", "this_year": "0.00"},
        {"currency": "USD", "this_month": "85.00", "this_quarter": "85.00", "this_year": "85.00"},
    ]
    assert summary["unresolved_counts"] == {"this_month": 1, "this_quarter": 1, "this_year": 1}


def test_dividend_calendar_unresolved_only_has_no_fabricated_currency_bucket(
    client: TestClient, session: Session
) -> None:
    _, user_id = _auth(client)
    account = _account(session, user_id)
    security = _security(session, user_id, "ONLYMIX")
    for day, currency in ((dt.date(2026, 5, 1), "CAD"), (dt.date(2026, 5, 31), "USD")):
        _trade(
            session,
            user_id,
            account,
            security,
            "dividend",
            batch_id=None,
            price_native="12.00",
            day=day,
            currency=currency,
        )

    result = dividend_calendar.compute_dividend_calendar(
        session, user_id, today=dt.date(2026, 6, 15)
    )
    assert result["upcoming"][0]["currency"] is None
    assert result["upcoming"][0]["avg_amount"] is None
    assert result["upcoming"][0]["annual_projection"] is None
    assert result["summary"]["by_currency"] == []
    assert result["summary"]["unresolved_counts"] == {
        "this_month": 1,
        "this_quarter": 1,
        "this_year": 1,
    }


def test_dividend_calendar_sums_unrounded_means_then_half_even_quantizes_once(
    client: TestClient, session: Session
) -> None:
    _, user_id = _auth(client)
    account = _account(session, user_id)
    today = dt.date(2026, 6, 15)
    dates = (dt.date(2026, 5, 1), dt.date(2026, 5, 31))
    for symbol in ("ROUND1", "ROUND2"):
        security = _security(session, user_id, symbol)
        for day, amount in zip(dates, ("0.00", "0.01"), strict=True):
            _trade(
                session,
                user_id,
                account,
                security,
                "dividend",
                batch_id=None,
                price_native=amount,
                day=day,
            )

    result = dividend_calendar.compute_dividend_calendar(session, user_id, today=today)
    assert [row["avg_amount"] for row in result["upcoming"]] == [Decimal("0.00"), Decimal("0.00")]
    assert result["summary"]["by_currency"] == [
        {"currency": "CAD", "this_month": "0.01", "this_quarter": "0.01", "this_year": "0.01"}
    ]


def test_dividend_calendar_summary_excludes_other_tenants(
    client: TestClient, session: Session
) -> None:
    _, user_id = _auth(client)
    _, other_user_id = _auth(client)
    today = dt.date(2026, 6, 15)
    dates = (dt.date(2026, 5, 1), dt.date(2026, 5, 31))

    own_account = _account(session, user_id)
    own_security = _security(session, user_id, "OWN")
    for day in dates:
        _trade(
            session,
            user_id,
            own_account,
            own_security,
            "dividend",
            batch_id=None,
            price_native="18.00",
            day=day,
        )

    other_account = _account(session, other_user_id)
    other_security = _security(session, other_user_id, "OTHER")
    for day in dates:
        _trade(
            session,
            other_user_id,
            other_account,
            other_security,
            "dividend",
            batch_id=None,
            price_native="999.00",
            day=day,
            currency="USD",
        )

    result = dividend_calendar.compute_dividend_calendar(session, user_id, today=today)
    assert [entry["symbol"] for entry in result["upcoming"]] == ["OWN"]
    assert result["summary"]["by_currency"] == [
        {"currency": "CAD", "this_month": "18.00", "this_quarter": "18.00", "this_year": "18.00"}
    ]


def test_tax_loss_alerts_ignore_staged_cost_basis(client: TestClient, session: Session) -> None:
    """A staged expensive buy must not inflate book cost and manufacture a
    (wrong) harvesting opportunity — nor suppress a real one."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _trade(
        session, user_id, account, sec, "buy", batch_id=None, quantity="10", price_native="20.00"
    )
    # A manual close BELOW cost → a real, committed unrealized loss of ~$100+.
    _price(session, user_id, sec, _today(), "5.00")
    rebuild_user_pools(session, user_id)
    session.commit()

    # A staged buy at a much higher price would push book cost up and hide
    # the alert if it leaked into the pool.
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="100",
        price_native="500.00",
    )

    alerts = tax_alerts.compute_tax_loss_alerts(session, user_id, today=_today())
    assert len(alerts) == 1
    assert alerts[0]["book_cost_cad"] == Decimal("200.00")
    assert alerts[0]["unrealized_loss_cad"] == Decimal("150.00")


def test_tax_loss_alerts_ignore_staged_buyback_when_flagging_risk(
    client: TestClient, session: Session
) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    # Bought 90 days ago — OUTSIDE the ±30-day window, so no risk today.
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=None,
        quantity="10",
        price_native="20.00",
        day=_today() - dt.timedelta(days=90),
    )
    _price(session, user_id, sec, _today(), "5.00")
    rebuild_user_pools(session, user_id)
    session.commit()
    assert (
        tax_alerts.compute_tax_loss_alerts(session, user_id, today=_today())[0][
            "superficial_loss_risk"
        ]
        is False
    )

    # A staged buyback inside ±30 days is not a repurchase yet.
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="1",
        price_native="5.00",
        day=_today() - dt.timedelta(days=5),
    )
    alerts = tax_alerts.compute_tax_loss_alerts(session, user_id, today=_today())
    assert len(alerts) == 1
    assert alerts[0]["superficial_loss_risk"] is False


# ---------------------------------------------------------------------------
# Benchmarks (portfolio value at two dates)
# ---------------------------------------------------------------------------


def test_benchmark_comparison_excludes_staged_value(client: TestClient, session: Session) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    start = _today() - dt.timedelta(days=30)
    # A close on BOTH dates keeps the benchmark window cache-only.
    _price(session, user_id, sec, start, "10.00")
    _price(session, user_id, sec, _today(), "10.00")
    _trade(
        session, user_id, account, sec, "buy", batch_id=None, quantity="10", price_native="10.00"
    )
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="100",
        price_native="10.00",
    )

    result = benchmarks.compute_benchmark_comparison(session, user_id, start, _today())
    assert Decimal(result["portfolio_start"]) == Decimal("100.00")
    assert Decimal(result["portfolio_end"]) == Decimal("100.00")
    assert result["portfolio_return_pct"] == "0.00"


# ---------------------------------------------------------------------------
# Security detail
# ---------------------------------------------------------------------------


def test_security_overview_recent_trades_exclude_staged(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _price(session, user_id, sec, _today(), "20.00")
    _trade(
        session, user_id, account, sec, "buy", batch_id=None, quantity="10", price_native="20.00"
    )
    pending = _batch(session, user_id, account.id, "pending_review")
    staged = _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="100",
        price_native="20.00",
    )

    r = client.get(f"{API}/securities/{sec.id}/overview", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [t["id"] for t in body["recent_trades"]]
    assert staged.id not in ids
    assert len(ids) == 1
    # And the overview's own totals come from the same filtered positions.
    assert Decimal(body["totals"]["quantity"]) == Decimal("10.00")


def test_security_overview_hides_a_position_created_only_by_a_staged_buy(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _price(session, user_id, sec, _today(), "20.00")
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="100",
        price_native="20.00",
    )

    r = client.get(f"{API}/securities/{sec.id}/overview", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["positions"] == []
    assert body["recent_trades"] == []
    assert body["totals"]["quantity"] == "0.00"


# ---------------------------------------------------------------------------
# The one place staged rows MUST stay visible: import review
# ---------------------------------------------------------------------------


def test_trade_import_preview_still_returns_staged_rows(
    client: TestClient, session: Session
) -> None:
    """The upload response is the review window; hiding rows there would leave
    the user reviewing nothing."""
    from app.services import market_data

    csv = (
        "activity_type,activity_sub_type,symbol,name,currency,description,"
        "effective_date,quantity,unit_price,commission,net_cash_amount,account_id\n"
        f"Buy,,ABCD,ABCD Inc,CAD,BUY,{_today():%m/%d/%Y},10,20,0,200,acc-1\n"
    )
    monkey_loose = pytest.MonkeyPatch()
    monkey_loose.setattr(market_data, "lookup_symbol", lambda s, **kw: None)
    monkey_loose.setattr(market_data, "fetch_latest_close", lambda *a, **k: None)
    try:
        headers, user_id = _auth(client)
        account = _account(session, user_id)
        r = client.post(
            f"{API}/trades/import",
            files={"file": ("inv.csv", csv.encode(), "text/csv")},
            data={"account_id": str(account.id)},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert len(body["preview"]) == 1

        # The staged row exists in `trades`, invisible to the live scope...
        staged_id = body["preview"][0]["id"]
        assert session.get(Trade, staged_id) is not None
        assert valuation.compute_positions(session, user_id, today=_today()) == []

        # ...and the review endpoints still see it.
        commit = client.post(f"{API}/trades/import/{body['batch_id']}/commit", headers=headers)
        assert commit.status_code == 200, commit.text
        assert commit.json()["status"] == "committed"

        # After commit it becomes part of the live scope.
        positions = valuation.compute_positions(session, user_id, today=_today())
        assert len(positions) == 1
        assert positions[0]["quantity"] == Decimal("10.000000")
    finally:
        monkey_loose.undo()


def test_trade_import_discard_removes_staged_rows_and_rebuilds(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _price(session, user_id, sec, _today(), "20.00")
    _trade(
        session, user_id, account, sec, "buy", batch_id=None, quantity="10", price_native="20.00"
    )
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="100",
        price_native="20.00",
    )
    rebuild_user_pools(session, user_id)
    session.commit()
    before = session.exec(select(AcbPool).where(AcbPool.user_id == user_id)).one()
    assert before.quantity == Decimal("10")

    r = client.post(f"{API}/trades/import/{pending.id}/discard", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "discarded"
    assert session.exec(select(Trade).where(Trade.import_batch_id == pending.id)).all() == []


def test_dividend_suggestions_ignore_staged_buys_and_income(
    client: TestClient, session: Session
) -> None:
    """A staged import must not make a phantom position look open.

    `GET /trades/dividend-suggestions` built its open-position timeline and its
    "already recorded" set from ALL trades, so a pending CSV made a
    non-existent holding look open (and marked dividend dates as recorded
    before the user committed them).
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "PHD")
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="50",
        price_native="10.00",
    )
    # A staged income row would otherwise mark today as already-recorded.
    _trade(
        session,
        user_id,
        account,
        sec,
        "dividend",
        batch_id=pending.id,
        quantity="0",
        price_native="5.00",
        day=_today(),
    )
    resp = client.get(f"{API}/trades/dividend-suggestions", headers=headers)

    assert resp.status_code == 200, resp.text
    # No committed holding exists, so there is nothing to suggest.
    assert resp.json() == []


def test_committed_batch_trades_are_visible_to_live_scope(
    client: TestClient, session: Session
) -> None:
    """Guard the other direction: the filter must not hide committed imports."""
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ABCD")
    _price(session, user_id, sec, _today(), "20.00")
    committed = _batch(session, user_id, account.id, "committed")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=committed.id,
        quantity="10",
        price_native="20.00",
    )

    positions = valuation.compute_positions(session, user_id, today=_today())
    assert len(positions) == 1
    assert positions[0]["quantity"] == Decimal("10.000000")
    state = compute_user_tax_state(session, user_id)
    assert state.pools[(account.id, sec.id)].total_acb_cad == Decimal("200.00")


def test_list_trades_committed_only_hides_staged_but_default_keeps_them(
    client: TestClient, session: Session
) -> None:
    """GET /trades gains an opt-in committed scope for report input loaders.

    The client-side report engine (docs/33 S2b) feeds itself from
    GET /trades, so it needs the exact population the server reports read
    (committed_trade_conditions). The DEFAULT list must keep returning
    staged rows — it is also the review window (docs/03).
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "SCPE")
    manual = _trade(
        session, user_id, account, sec, "buy", batch_id=None, quantity="1", price_native="10.00"
    )
    pending = _batch(session, user_id, account.id, "pending_review")
    staged = _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="2",
        price_native="10.00",
    )
    committed = _batch(session, user_id, account.id, "committed")
    imported = _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=committed.id,
        quantity="3",
        price_native="10.00",
    )

    default_ids = {
        row["id"] for row in client.get(f"{API}/trades", headers=headers).json()["items"]
    }
    assert {manual.id, staged.id, imported.id} <= default_ids  # review window intact

    scoped = client.get(f"{API}/trades?committed_only=true", headers=headers)
    assert scoped.status_code == 200, scoped.text
    body = scoped.json()
    scoped_ids = {row["id"] for row in body["items"]}
    assert staged.id not in scoped_ids
    assert {manual.id, imported.id} == scoped_ids
    assert body["total"] == 2


# --- report surfaces (review finding: the filter was missed here) -----------
#
# `services/reports.py` read `Trade.user_id` directly in three report
# functions, so a staged import moved REPORTED money while `compute_positions`
# (correctly) ignored it: the same user saw two different portfolios
# depending on which screen they looked at.


def test_investment_performance_report_ignores_staged_trades(
    client: TestClient, session: Session
) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "ZZZ")
    _price(session, user_id, sec, _today(), "10.00")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=None,
        quantity="10",
        price_native="10.00",
    )
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="1000",
        price_native="10.00",
    )

    today = _today()
    report = reports_service.investment_performance_report(
        session, user_id, today - dt.timedelta(days=365), today
    )

    quantities = {item["symbol"]: item["quantity"] for item in report["items"]}
    assert quantities.get("ZZZ") == Decimal("10.000000"), report
    # And it must agree with the positions view — one portfolio, one answer.
    positions = valuation.compute_positions(session, user_id, today=today)
    assert positions[0]["quantity"] == Decimal("10.000000")


def test_net_worth_report_positions_ignore_staged_trades(
    client: TestClient, session: Session
) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "STG")
    _price(session, user_id, sec, _today(), "10.00")
    pending = _batch(session, user_id, account.id, "pending_review")
    # A position that exists ONLY in a pending import must not be reported.
    _trade(
        session,
        user_id,
        account,
        sec,
        "buy",
        batch_id=pending.id,
        quantity="100",
        price_native="10.00",
    )

    today = _today()
    report = reports_service.net_worth_report(
        session, user_id, today - dt.timedelta(days=365), today, "month"
    )

    positions_cad = sum(
        (Decimal(str(bucket["positions_cad"])) for bucket in report["buckets"]), Decimal("0")
    )
    assert positions_cad == Decimal("0.00"), report


def test_cashflow_report_ignores_staged_income(client: TestClient, session: Session) -> None:
    _headers, user_id = _auth(client)
    account = _account(session, user_id)
    sec = _security(session, user_id, "DVS")
    _price(session, user_id, sec, _today(), "10.00")
    _trade(
        session,
        user_id,
        account,
        sec,
        "dividend",
        batch_id=None,
        quantity="0",
        price_native="100.00",
        day=_today(),
    )
    pending = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        sec,
        "dividend",
        batch_id=pending.id,
        quantity="0",
        price_native="900.00",
        day=_today(),
    )

    today = _today()
    report = reports_service.cashflow_report(
        session, user_id, today - dt.timedelta(days=7), today, "day"
    )

    income = sum((Decimal(str(bucket["income_cad"])) for bucket in report["buckets"]), Decimal("0"))
    assert income == Decimal("100.00"), report

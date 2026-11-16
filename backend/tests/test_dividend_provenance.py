"""Dividend-view provenance contract (docs/07 §M184, docs/12, docs/24 §M29).

Every dividend view answers the same three questions — WHICH source produced
these numbers, WHEN it last answered, and whether it is stale or missing — with
ONE shape. These tests pin the wire shape and the two rules that keep the
vocabulary honest:

* severity ``missing > stale > ok``, so the top-level ``status`` is the WORST
  source's verdict;
* the top-level ``as_of`` is the OLDEST source timestamp, because a view is
  only as fresh as its least fresh evidence;
* a ledger source is a recorded fact: it answers ok/missing and is never stale
  (``age_days`` is ``None``), only a market-data source ages.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_dividends import _get
from test_dividends import _seed as seed_income_ledger
from test_markets_securities_trades import _post_holding_trade, _seed_holding
from test_tax_acb import _user, add_trade, make_account, make_security

from app.core.security import create_access_token
from app.models.accounts import AccountType
from app.models.dividend_history import DividendHistoryRefresh, DividendHistoryState
from app.models.fx_rates import FxRate, FxSource
from app.models.trades import TradeAction as TA
from app.models.users import User

DIVIDENDS_URL = "/api/v1/reports/dividends"
CALENDAR_URL = "/api/v1/reports/dividend-calendar"
SUGGESTIONS_URL = "/api/v1/trades/dividend-suggestions"
HEADER_KEYS = {
    "pending",
    "unavailable",
    "empty",
    "stale",
    "quote_miss",
    "missing",
    "source",
    "as_of",
    "age_days",
}


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _sources(report: dict) -> dict[str, dict]:
    return {source["key"]: source for source in report["provenance"]["sources"]}


def _get_json(client: TestClient, url: str, user: User) -> dict:
    response = client.get(url, headers=_headers(user))
    assert response.status_code == 200, response.text
    return response.json()


def _seed_foreign_income(session: Session) -> User:
    """A current-year dividend in a reporting-foreign denomination — the view
    then has a real market-data dependency (its trade-date FX cache)."""
    user = _user(session)
    account = make_account(session, user, "Offshore", AccountType.brokerage_non_registered, True)
    security = make_security(session, user, "DKKQ", "DKK")
    add_trade(session, user, account, security, TA.dividend, date.today().isoformat(), "0", "50.00")
    return user


def _add_rate(session: Session, rate_date: date) -> None:
    session.add(
        FxRate(
            base="DKK",
            quote="CAD",
            date=rate_date,
            rate=Decimal("0.19"),
            source=FxSource.boc_valet,
        )
    )
    session.commit()


# --- GET /reports/dividends ---------------------------------------------------


def test_dividends_report_states_both_sources_and_the_oldest_timestamp(
    client: TestClient, session: Session
) -> None:
    """2024 income converts USD (rate present) and EUR (rate absent): the ledger
    source is ok, the market-data source is missing, and the block reports the
    OLDEST timestamp across them."""
    user, _ = seed_income_ledger(session)
    report = _get(client, user, year=2024)

    provenance = report["provenance"]
    assert provenance["status"] == "missing"  # EUR payment excluded → worst verdict
    assert provenance["as_of"] == "2024-02-08"  # oldest evidence = newest USD rate used
    assert {source["key"] for source in provenance["sources"]} == {
        "income_trades",
        "trade_date_fx",
    }

    ledger = _sources(report)["income_trades"]
    assert ledger["kind"] == "ledger"
    assert ledger["provider"] is None
    assert ledger["status"] == "ok"
    assert ledger["as_of"] == "2024-05-01"  # newest recorded payment that year
    assert ledger["age_days"] is None  # a recorded fact does not age

    fx = _sources(report)["trade_date_fx"]
    assert fx["kind"] == "market_data"
    assert fx["provider"] == "manual"  # _seed's USD rows are manual gap-fills
    assert fx["status"] == "missing"
    assert fx["as_of"] == "2024-02-08"
    assert fx["age_days"] == (date.today() - date(2024, 2, 8)).days
    assert "no cached rate" in fx["detail"] and "no rate was fetched" in fx["detail"]


def test_dividends_report_fx_source_goes_stale_past_the_carry_forward_window(
    client: TestClient, session: Session
) -> None:
    """A rate older than docs/07's 7-day carry-forward window is reported
    stale — the report resolved it, and the user still gets to know."""
    user = _seed_foreign_income(session)
    _add_rate(session, date.today() - timedelta(days=9))
    report = _get(client, user)

    provenance = report["provenance"]
    assert report["unresolved_fx"] == 0
    assert provenance["status"] == "stale"

    fx = _sources(report)["trade_date_fx"]
    assert fx["status"] == "stale"
    assert fx["provider"] == "boc_valet"
    assert fx["as_of"] == (date.today() - timedelta(days=9)).isoformat()
    assert fx["age_days"] == 9
    assert "carry-forward window" in fx["detail"]
    # Oldest evidence across sources: the rate, not today's payment.
    assert provenance["as_of"] == fx["as_of"]


def test_dividends_report_fx_source_is_current_when_the_rate_is_fresh(
    client: TestClient, session: Session
) -> None:
    user = _seed_foreign_income(session)
    _add_rate(session, date.today() - timedelta(days=1))
    report = _get(client, user)

    provenance = report["provenance"]
    assert report["unresolved_fx"] == 0
    assert provenance["status"] == "ok"

    fx = _sources(report)["trade_date_fx"]
    assert fx["status"] == "ok"
    assert fx["age_days"] == 1
    assert "carry-forward" not in fx["detail"]
    ledger = _sources(report)["income_trades"]
    assert ledger["status"] == "ok" and ledger["kind"] == "ledger"


def test_dividends_report_without_a_foreign_denomination_has_no_fx_source(
    client: TestClient, session: Session
) -> None:
    """A CAD-only report has no market-data dependency, and says so by simply
    not listing one — never by inventing a source it does not read."""
    user, _ = seed_income_ledger(session)
    report = _get(client, user, year=2026)  # _seed's only 2026 income is CAD

    provenance = report["provenance"]
    assert provenance["status"] == "ok"
    assert [source["key"] for source in provenance["sources"]] == ["income_trades"]
    ledger = _sources(report)["income_trades"]
    assert ledger["status"] == "ok" and ledger["as_of"] is not None
    assert provenance["as_of"] == ledger["as_of"]


def test_dividends_report_for_an_empty_year_reports_missing(
    client: TestClient, session: Session
) -> None:
    user, _ = seed_income_ledger(session)
    report = _get(client, user, year=2019)

    provenance = report["provenance"]
    assert provenance["status"] == "missing"
    assert provenance["as_of"] is None
    assert report["items"] == []
    ledger = _sources(report)["income_trades"]
    assert ledger["status"] == "missing"
    assert ledger["as_of"] is None and ledger["age_days"] is None
    assert "nothing to report" in ledger["detail"]
    assert "trade_date_fx" not in _sources(report)


# --- GET /reports/dividend-calendar ------------------------------------------


def test_dividend_calendar_provenance_is_a_ledger_source(
    client: TestClient, session: Session
) -> None:
    """The projection is built from recorded payments: one ledger source, the
    day of the newest payment inside the window, and never a stale verdict."""
    user = _user(session)
    account = make_account(session, user, "Brokerage", AccountType.brokerage_non_registered, True)
    security = make_security(session, user, "PAY", "CAD")
    newest = date.today() - timedelta(days=30)
    add_trade(session, user, account, security, TA.dividend, newest.isoformat(), "0", "25.00")
    # Outside the trailing 13-month window: it must not move the timestamp.
    add_trade(
        session,
        user,
        account,
        security,
        TA.dividend,
        (date.today() - timedelta(days=400)).isoformat(),
        "0",
        "25.00",
    )

    payload = _get_json(client, CALENDAR_URL, user)
    provenance = payload["provenance"]
    assert provenance["status"] == "ok"
    assert provenance["as_of"] == newest.isoformat()
    assert [source["key"] for source in provenance["sources"]] == ["income_trades"]

    ledger = provenance["sources"][0]
    assert ledger["kind"] == "ledger"
    assert ledger["provider"] is None
    assert ledger["age_days"] is None
    assert ledger["status"] == "ok"
    assert "recorded income payment" in ledger["detail"]
    assert "not a market-data feed" in ledger["detail"]


def test_dividend_calendar_provenance_is_missing_without_recorded_payments(
    client: TestClient, session: Session
) -> None:
    user = _user(session)
    payload = _get_json(client, CALENDAR_URL, user)

    provenance = payload["provenance"]
    assert payload["upcoming"] == []
    assert provenance["status"] == "missing"
    assert provenance["as_of"] is None
    ledger = provenance["sources"][0]
    assert ledger["key"] == "income_trades" and ledger["kind"] == "ledger"
    assert ledger["status"] == "missing" and ledger["as_of"] is None
    assert "nothing to project" in ledger["detail"]


# --- GET /trades/dividend-suggestions (the additive header) -------------------


def _state(response) -> dict:
    assert "X-Dividend-History-State" in response.headers
    state = json.loads(response.headers["X-Dividend-History-State"])
    assert set(state) == HEADER_KEYS, sorted(state)
    return state


def test_suggestions_header_names_the_provider_when_history_never_answered(
    client: TestClient, session: Session
) -> None:
    """An open position with no refresh row: the route queues work and reports
    the source it is queuing FOR, with `missing` (never answered) distinct from
    `stale` (answered over 24h ago) and from `unavailable`."""
    headers, account_id, security_id = _seed_holding(client, session, "VUN")
    _post_holding_trade(client, headers, account_id, security_id, "buy", "10", "2023-01-01")

    response = client.get(SUGGESTIONS_URL, headers=headers)
    assert response.status_code == 200, response.text

    state = _state(response)
    assert state["source"] == "yfinance"
    assert state["pending"] == 1
    assert state["missing"] == 1  # no successful refresh has ever happened
    assert state["stale"] == 0  # never-successful is missing, not stale
    assert state["unavailable"] == 0 and state["empty"] == 0
    assert state["as_of"] is None and state["age_days"] is None


def test_suggestions_header_reports_a_stale_source_with_its_timestamp(
    client: TestClient, session: Session
) -> None:
    """A source that answered two days ago is stale — reported with the exact
    timestamp of that answer and its age, not as a bare count."""
    headers, account_id, security_id = _seed_holding(client, session, "VCN")
    _post_holding_trade(client, headers, account_id, security_id, "buy", "10", "2023-01-01")
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    now = datetime.now(UTC).replace(tzinfo=None)
    session.add(
        DividendHistoryRefresh(
            user_id=user_id,
            security_id=security_id,
            state=DividendHistoryState.available,
            last_success_at=now - timedelta(hours=48),
            last_attempt_at=now,  # already attempted → not re-queued by this read
        )
    )
    session.commit()

    response = client.get(SUGGESTIONS_URL, headers=headers)
    assert response.status_code == 200, response.text

    state = _state(response)
    assert state["source"] == "yfinance"
    assert state["stale"] == 1
    assert state["missing"] == 0  # it HAS answered, however old the answer is
    assert state["pending"] == 0
    assert state["as_of"] is not None and state["as_of"].endswith("Z")
    assert state["age_days"] == 2


def test_suggestions_header_stays_complete_on_a_scope_without_positions(
    client: TestClient, session: Session
) -> None:
    """The early return still answers the whole contract — a client must not
    have to special-case a portfolio with nothing open."""
    headers, _account_id, _security_id = _seed_holding(client, session, "XEQT")

    response = client.get(SUGGESTIONS_URL, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == []

    state = _state(response)
    assert state["source"] == "yfinance"
    assert state["missing"] == 0 and state["stale"] == 0 and state["pending"] == 0
    assert state["as_of"] is None and state["age_days"] is None

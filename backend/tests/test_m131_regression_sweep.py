"""M131 regression sweep — every committed profile CSV vs its expected
outputs, in ONE pass (docs/41).

de14346 (M131) changed the SHARED importer: `investment_profile.build_trades`
is the one place trade-mutation semantics live, and every brokerage profile —
plus the legacy Wealthsimple fallback — funnels through it. The per-fixture
smokes in `test_brokerage_profiles.py` prove each profile in ISOLATION (a
fresh user per fixture), which cannot see the state the profiles SHARE when
one household imports them all: the security rows, the `CASH` security, the
dedup hash, and the ACB rebuild that runs over the whole user on every
commit.

This module runs the whole committed corpus as ONE user:

* every smoke fixture, in registry order, into its own account — dry run →
  stage → commit → `positions`/`cash_effect` recomputed from the database →
  re-import proving net-new 0 — each fixture held to its own sidecar, so a
  parser change that shifts ANY profile's rows, notices or signs fails here
  with the fixture named;
* the 2 refusal fixtures (`422 unknown_corporate_action`) and the 2 stuck
  fixtures (dry run lists the bad row, staging answers `422 parse_error`
  naming line 3) in the same pass, with row counts proving nothing was
  written;
* the cross-profile pins isolation cannot express: ONE `SHOP` security
  carries every broker's rows (its `listing_currency` is whichever profile
  imported first — the rows keep their OWN currency in `trades.currency`,
  M132), one `CASH` security, and a currency-BLIND dedup hash that makes a
  second broker's identical trade a duplicate rather than a double-count.

`test_committed_fixture_corpus_is_complete` makes "every committed profile
CSV" arithmetic: a fixture CSV added to `tests/fixtures/brokerage/` without a
classification here or in the per-fixture module fails the sweep instead of
drifting untested.

The legacy Wealthsimple path and the M113 real-export clips keep their own
suites (`test_investment_import.py`, `test_m113_ws_cash_mappings.py`,
`test_m131_ws_cash_signs.py`); this sweep owns the PROFILE corpus.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from test_brokerage_profiles import (
    REFUSAL_FILES,
    SMOKE_FILES,
    STUCK_FILES,
    _account,
    _auth,
    _counts,
    _sidecar,
    _upload,
    _user_id,
)

from app.models.fx_rates import FxRate, FxSource
from app.models.securities import Security
from app.models.trades import CASH_OUTFLOW_ACTIONS, Trade
from app.services import market_data
from app.services.total_return import _trade_cash_effect

FIXTURES = Path(__file__).parent / "fixtures" / "brokerage"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No yfinance: the sweep's security-currency pins assume the import-time
    heuristic (docs/05), not a live lookup."""
    monkeypatch.setattr(market_data, "lookup_symbol", lambda symbol, **kw: None)
    monkeypatch.setattr(market_data, "fetch_latest_close", lambda symbol, today=None: None)


@pytest.fixture(name="fx_cache")
def fx_cache_fixture(session: Session) -> None:
    """Seed USD/CAD daily across every fixture trade date (docs/06/docs/07).

    The ACB rebuild at commit is exact-date + cache-only for a taxable
    account, so the suite supplies the rates a real install gets from BoC —
    on its own isolated database, which is the isolation AGENTS §5 requires
    of any suite that writes `fx_rates`.
    """
    day = date(2025, 12, 1)
    while day <= date(2026, 4, 30):
        session.add(
            FxRate(
                base="USD",
                quote="CAD",
                rate=Decimal("1.35000000"),
                date=day,
                source=FxSource.boc_valet,
            )
        )
        day += timedelta(days=1)
    session.flush()


def _cash_by_row_currency(session: Session, account_id: int) -> dict[str, Decimal]:
    """`cash_effect` recomputed the way the report engines read it (M132).

    `_trade_cash_effect` returns the amount in the ROW's own currency
    (`trades.currency`), so the sweep buckets by that currency — not by the
    security's `listing_currency`. In per-fixture isolation the two
    groupings coincide; here they deliberately do not: the Wealthsimple
    fixture's CAD SHOP rows land on the SHOP security the Questrade import
    created as USD, and the CAD cash must still bucket as CAD.
    """
    trades = session.exec(select(Trade).where(Trade.account_id == account_id)).all()
    cash: dict[str, Decimal] = {}
    for trade in trades:
        effect = _trade_cash_effect(trade)
        if effect == 0:
            continue
        currency = trade.currency or "CAD"  # every sweep account is CAD
        cash[currency] = cash.get(currency, Decimal("0")) + effect
    return cash


def _positions(session: Session, account_id: int) -> dict[str, Decimal]:
    """Signed per-symbol quantities for an account's committed rows."""
    rows = session.exec(
        select(Trade.quantity, Security.symbol)
        .join(Security, Trade.security_id == Security.id)
        .where(Trade.account_id == account_id)
    ).all()
    positions: dict[str, Decimal] = {}
    for quantity, symbol in rows:
        positions[symbol] = positions.get(symbol, Decimal("0")) + quantity
    return {symbol: total for symbol, total in positions.items() if total != 0}


# --------------------------------------------------------------------------- #
# 1. "Every committed profile CSV" is a closed, classified set
# --------------------------------------------------------------------------- #


def test_committed_fixture_corpus_is_complete() -> None:
    """The sweep's file lists must cover the fixtures directory exactly.

    A CSV committed to `tests/fixtures/brokerage/` without a classification
    (smoke + sidecar, refusal, or stuck) would otherwise sit unexercised:
    nothing else enumerates the directory. A sidecar without a smoke file, or
    a smoke file without a sidecar, is the same drift seen from the other
    side.
    """
    on_disk = {path.name for path in FIXTURES.glob("*.csv")}
    classified = set(SMOKE_FILES) | set(REFUSAL_FILES) | set(STUCK_FILES)
    assert on_disk == classified, (
        f"fixture CSVs not classified by the sweep: {sorted(on_disk - classified)}; "
        f"classified but missing on disk: {sorted(classified - on_disk)}"
    )

    sidecars = {
        path.name.removesuffix(".expected.json") + ".csv"
        for path in FIXTURES.glob("*.expected.json")
    }
    assert sidecars == set(SMOKE_FILES), (
        f"sidecars without a smoke fixture: {sorted(sidecars - set(SMOKE_FILES))}; "
        f"smoke fixtures without a sidecar: {sorted(set(SMOKE_FILES) - sidecars)}"
    )


# --------------------------------------------------------------------------- #
# 2. The one-pass sweep: every committed CSV, one user, against sidecars
# --------------------------------------------------------------------------- #


def test_all_committed_profiles_in_one_pass(
    client: TestClient, session: Session, fx_cache: None
) -> None:
    """Import the whole committed corpus as ONE user and hold every fixture
    to its sidecar — then pin the state the profiles share.

    Registry order matters and is asserted: `questrade` imports first, so its
    USD SHOP rows create the SHOP security that every later CAD profile
    attaches to. The per-fixture smokes cannot see that sharing; this sweep
    both exercises it and proves it does not corrupt a single fixture's
    stated output.
    """
    headers = _auth(client)
    accounts: dict[str, dict] = {}

    for name in SMOKE_FILES:
        expected = _sidecar(name)
        slug = expected["profile"]
        account = _account(client, headers, expected.get("account_currency", "CAD"))
        accounts[name] = account

        # --- stage: sidecar counts, from the shared parse path ---------------
        staged = _upload(client, headers, account["id"], name, slug, preview=False)
        assert staged.status_code == 201, staged.text
        body = staged.json()
        assert body["profile"] == slug
        assert body["imported"] == expected["imported"], name
        assert body["duplicates_skipped"] == expected["duplicates_skipped"], name
        assert len(body["unmapped"]) == expected["unmapped"], name
        assert len(body["notices"]) == expected["notices"], name
        assert len(body["preview"]) == expected["rows"], name

        # --- commit ----------------------------------------------------------
        committed = client.post(f"/api/v1/trades/import/{body['batch_id']}/commit", headers=headers)
        assert committed.status_code == 200, committed.text
        assert committed.json()["imported"] == expected["imported"], name

        # --- balances recomputed from the database ---------------------------
        positions = _positions(session, account["id"])
        assert positions == {
            symbol: Decimal(quantity) for symbol, quantity in expected["positions"].items()
        }, name
        cash = _cash_by_row_currency(session, account["id"])
        assert cash == {
            currency: Decimal(total) for currency, total in expected["cash_effect"].items()
        }, name

        # --- re-import: net-new must be 0 ------------------------------------
        again = _upload(client, headers, account["id"], name, slug, preview=False)
        assert again.status_code == 201, again.text
        assert again.json()["imported"] == 0, name
        assert again.json()["duplicates_skipped"] == expected["rows"], name

        # --- the M131 stored-sign contract the sidecar CANNOT see -------------
        # `cash_effect` is deliberately `-abs` for fee/withdrawal (docs/05), so
        # the sidecar totals are sign-INSENSITIVE: a fee row stored back at its
        # file's positive amount produces the same cash figure while the
        # transactions surface, exports and the event log render an inflationary
        # "+$6.50 fee" — the exact M131 defect. The stored sign is asserted
        # here, against the database, per fixture.
        outflows = session.exec(
            select(Trade).where(
                Trade.account_id == account["id"],
                Trade.action.in_(CASH_OUTFLOW_ACTIONS),
            )
        ).all()
        for row in outflows:
            assert row.price_native < 0, (
                f"{name}: {row.action} stored positive ({row.price_native})"
            )

    # --- the refusal and stuck fixtures, in the same pass --------------------
    # Every other committed CSV is here too: the refusals must answer 422
    # with their own code, the stuck files must be LISTED on the dry run and
    # REFUSED on staging — and none of the four may move a single row.
    user_id = _user_id(session, accounts[SMOKE_FILES[0]]["id"])
    before = _counts(session, user_id)
    for name in REFUSAL_FILES:
        refused = _upload(
            client, headers, accounts[SMOKE_FILES[0]]["id"], name, None, preview=False
        )
        assert refused.status_code == 422, name
        assert refused.json()["code"] == "unknown_corporate_action", name
        assert "line" in refused.json()["detail"], name
    for name in STUCK_FILES:
        listed = _upload(
            client, headers, accounts[SMOKE_FILES[0]]["id"], name, "questrade", preview=True
        )
        assert listed.status_code == 201, name
        unmapped = listed.json()["unmapped"]
        assert len(unmapped) == 1 and unmapped[0]["code"] == "stuck", name
        refused = _upload(
            client, headers, accounts[SMOKE_FILES[0]]["id"], name, "questrade", preview=False
        )
        assert refused.status_code == 422, name
        assert refused.json()["code"] == "parse_error", name
        assert "line 3" in refused.json()["detail"], name
    assert _counts(session, user_id) == before

    # --- the shared state every fixture above touched ------------------------
    # Every committed row is here, once per fixture — no profile silently
    # contributed more or fewer rows than its own sidecar states. The
    # re-imports above staged their duplicate rows (never committed, so still
    # in the table flagged `is_duplicate`); the book is the rest.
    all_trades = session.exec(
        select(Trade).where(Trade.user_id == user_id, Trade.is_duplicate == False)  # noqa: E712
    ).all()
    assert len(all_trades) == sum(_sidecar(name)["imported"] for name in SMOKE_FILES)

    # One SHOP, one AAPL, one VEQT, one CASH security for the whole user —
    # the profiles share the book instead of forking it per import.
    securities = session.exec(select(Security).where(Security.user_id == user_id)).all()
    assert {security.symbol for security in securities} == {"SHOP", "AAPL", "VEQT", "CASH"}

    # SHOP's listing currency is whichever profile imported FIRST (questrade,
    # USD) — a documented consequence of the shared book, not an accident.
    # The rows themselves keep their OWN currency (M132), so the CAD
    # brokers' cash still buckets as CAD above; both facts together are the
    # contract.
    shop = next(security for security in securities if security.symbol == "SHOP")
    assert shop.listing_currency == "USD"
    shop_currencies = {
        trade.currency
        for trade in session.exec(select(Trade).where(Trade.security_id == shop.id)).all()
    }
    assert shop_currencies == {"USD", "CAD"}

    # M132 invariant under a shared book: every committed row carries its own
    # currency — a NULL would fall back to the account's and mis-bucket.
    assert all(trade.currency for trade in all_trades)


# --------------------------------------------------------------------------- #
# 3. The cross-profile pins isolation cannot express
# --------------------------------------------------------------------------- #


def test_cross_profile_dedup_is_currency_blind(
    client: TestClient, session: Session, fx_cache: None
) -> None:
    """Two brokers' identical trades in ONE account are ONE trade.

    `_trade_dedup_hash` spans `account|security|date|action|quantity|price`
    — currency is deliberately NOT in it (docs/03), so a USD Questrade SHOP
    buy and an RBC CAD SHOP buy with the same date/quantity/price in the same
    account collapse to a single row instead of double-counting the holding.
    M131's stored-sign changes (a fee row now stores `-abs`) move a row's
    `price_native`, hence its hash — this pin proves a same-code re-import
    still dedupes, which is the round trip every user makes.
    """
    headers = _auth(client)
    account = _account(client, headers, "CAD")

    first = _upload(
        client, headers, account["id"], "questrade_activity.csv", "questrade", preview=False
    )
    assert first.status_code == 201, first.text
    assert first.json()["imported"] == 4
    client.post(f"/api/v1/trades/import/{first.json()['batch_id']}/commit", headers=headers)

    # RBC's four rows are numerically identical to Questrade's — same account,
    # same securities, same dates, same quantities, same prices. Only the
    # currency cells differ, and those are not part of the hash.
    second = _upload(client, headers, account["id"], "rbc_activity.csv", "rbc-di", preview=False)
    assert second.status_code == 201, second.text
    body = second.json()
    assert body["imported"] == 0
    assert body["duplicates_skipped"] == 4

    committed = client.post(f"/api/v1/trades/import/{body['batch_id']}/commit", headers=headers)
    assert committed.status_code == 200, committed.text

    # The duplicates were dropped, not force-included: the account still
    # holds exactly Questrade's book, not a doubled one.
    assert _positions(session, account["id"]) == {"SHOP": Decimal("6")}
    assert _cash_by_row_currency(session, account["id"]) == {
        "CAD": Decimal("10000.00"),
        "USD": Decimal("-857.40"),
    }

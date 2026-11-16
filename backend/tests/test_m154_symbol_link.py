"""M154 — a BARE holding ticker must find the user's exchange-SUFFIXED security.

Docs: docs/05 § "Fund composition (M17, providers M147)", docs/07 § "Fund
composition providers".

Providers publish holding tickers BARE — `005930`, `9984`, `HSBA`,
`LRCX` (measured on prod: 17,530 of the demo user's non-NULL holding
symbols carry no dot) — while the user's `securities` rows are frequently
EXCHANGE-SUFFIXED (`005930.KS`, `9984.T`, `HSBA.L`; 825 of that user's
1,496 rows), because `lookup_symbol` resolves a bare ticker to the
canonical listing when the row is created.

Both fund-composition seams keyed their symbol→security map on the RAW
spelling, so the two spellings never met:

* `market_data.import_fund_composition` looked the provider's bare ticker
  up in a map keyed by `005930.KS` → None → `holding_security_id` stayed
  NULL for every holding the user actually owned (measured on prod: 3,117
  of 23,042 rows linked, 13.5%);
* `market_data.ensure_sub_fund_composition` then called `lookup_symbol`,
  got the RESOLVED row back (`{"symbol": "005930.KS", "exchange": "KSC"}`)
  and INSERTED it — colliding with the row that already existed: **231**
  caught `uq_securities_symbol` violations in one prod refresh run, each a
  wasted network call plus a failed INSERT. Its recovery re-queried the
  BARE spelling (`Security.symbol == "005930"`), found nothing, and
  `continue`d, so the sub-fund was skipped silently.

M154 normalizes ONCE, at `market_data.security_symbol_index` — the single
index every matching seam reads (this module's `import_fund_composition`,
`ensure_sub_fund_composition` and `api/securities.update_holdings`):

* the EXACT spelling always wins (`VUN` finds the bare `VUN` row even when
  `VUN.TO` also exists — the CSV import's twin rule, docs/05);
* a bare ticker falls back to the stem of a stored
  `005930.KS`-style row, but ONLY when that stem is unambiguous: two
  listings sharing a stem (`005930.KS` on the KSC and `005930.KQ` on the
  KOSDAQ) never link by guesswork — an ambiguous stem stays unlinked, and
  the sub-fund creator lets `lookup_symbol`'s answer pick the listing;
* only KNOWN exchange suffixes are stripped (`market_data.EXCHANGE_SUFFIXES`,
  the venue codes prod actually stores), so a US share class `BRK.B` is
  never mistaken for the common `BRK`;
* the creator REUSES the row `lookup_symbol` resolved to instead of
  inserting it again (card requirement: a resolved key that already exists
  is used, never re-inserted — the constraint is correct, the lookup was
  wrong).

Weight math is untouched by every assertion here: linking sets one FK
column and never a `weight_pct`, so a fund's imported weights (and the
docs/16 unclassified-residual rule that reads them) are byte-identical
before and after.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

import app.services.market_data as market_data
from app.api.deps import get_engine
from app.models.etf_holdings import EtfHolding, EtfHoldingSource
from app.models.securities import Security, SecurityKind
from app.models.users import User

API = "/api/v1"
SEC_URL = f"{API}/securities"

#: The two Korean listings prod actually carries for stem `005930` — a
#: stock on the KSC and a fund-facts row on the KOSDAQ. Their coexistence is
#: exactly why an ambiguous stem may not link by guesswork.
_KSC = "KSC"
_KOE = "KOE"


@pytest.fixture(autouse=True)
def offline_market(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Default no-ops keep the module offline (docs/07 network discipline).

    Tests that need a provider answer replace the ONE seam they need
    (`fetch_fund_composition`, `lookup_symbol`) themselves.
    """

    def nothing(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(market_data, "fetch_issuer_holdings", nothing)
    monkeypatch.setattr(market_data, "fetch_vanguard_holdings", nothing)
    monkeypatch.setattr(market_data, "fetch_fund_holdings", nothing)
    monkeypatch.setattr(market_data, "fetch_latest_close", nothing)
    monkeypatch.setattr(market_data, "ensure_fundamentals", nothing)
    monkeypatch.setattr(market_data, "backfill_range", nothing)
    monkeypatch.setattr(market_data, "ensure_recent_price", nothing)
    monkeypatch.setattr(market_data, "lookup_symbol", lambda symbol: None)
    return {}


class _SecurityInserts:
    """Every `INSERT INTO securities` the engine executes INSIDE the `with`.

    Counting the STATEMENTS is what makes "zero `uq_securities_symbol`
    violations" measurable rather than inferred: no INSERT reaches the
    server, so no unique constraint can be tried — and one INSERT where the
    resolved row already existed is precisely the defect. Only the call
    under test is inside the block, so a test's own setup rows are not
    mistaken for the code's.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self) -> list[str]:
        engine: Engine = get_engine()

        def _record(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: object,
        ) -> None:
            if statement.lstrip().upper().startswith("INSERT INTO SECURITIES"):
                self.statements.append(statement)

        self._record = _record
        event.listen(engine, "before_cursor_execute", _record)
        return self.statements

    def __exit__(self, *exc: object) -> None:
        event.remove(get_engine(), "before_cursor_execute", self._record)


@pytest.fixture(name="security_inserts")
def security_inserts_fixture() -> _SecurityInserts:
    return _SecurityInserts()


def _security(
    session: Session,
    user_id: int,
    symbol: str,
    *,
    kind: SecurityKind = SecurityKind.etf,
    exchange: str = "TSX",
    listing_currency: str = "CAD",
) -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind=kind,
        listing_currency=listing_currency,
        exchange=exchange,
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _user(session: Session, email: str) -> int:
    user = User(email=email, hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None
    return user.id


def _provider_rows(*rows: tuple[str, str, str]) -> Any:
    """``(symbol, name, weight)`` triples as the provider chain's answer."""
    return (
        EtfHoldingSource.issuer,
        [
            {
                "holding_symbol": symbol,
                "holding_name": name,
                "weight_pct": Decimal(weight),
            }
            for symbol, name, weight in rows
        ],
    )


def _resolved(
    symbol: str, exchange: str, kind: SecurityKind = SecurityKind.stock
) -> dict[str, object]:
    """`lookup_symbol`'s answer for a ticker Yahoo resolves to a SUFFIXED
    listing — the shape that used to drive the duplicate INSERT."""
    return {
        "symbol": symbol,
        "name": f"{symbol} resolved name",
        "kind": kind,
        "listing_currency": "KRW",
        "exchange": exchange,
    }


# --- the one index -----------------------------------------------------------


def test_symbol_index_exact_wins_unambiguous_stems_resolve_and_ambiguous_stems_do_not() -> None:
    """The normalization rule, asked as a pure function (no database):

    exact spelling first, a KNOWN venue suffix stripped for the stem
    fallback, and an ambiguous stem absent from the index entirely —
    `005930` must not silently become `005930.KQ`.
    """

    def sec(pk: int, symbol: str, exchange: str) -> Security:
        return Security(
            id=pk,
            user_id=1,
            symbol=symbol,
            name=symbol,
            kind=SecurityKind.stock,
            listing_currency="USD",
            exchange=exchange,
        )

    # Unambiguous stem: `005930` finds the suffixed row it is the stem of.
    index = market_data.security_symbol_index([sec(1, "005930.KS", _KSC)])
    assert index["005930.KS"].id == 1
    assert index["005930"].id == 1

    # Two listings sharing a stem: neither may answer for the bare ticker.
    index = market_data.security_symbol_index(
        [sec(1, "005930.KS", _KSC), sec(2, "005930.KQ", _KOE)]
    )
    assert index["005930.KS"].id == 1
    assert index["005930.KQ"].id == 2
    assert index.get("005930") is None

    # Exact beats stem: the bare row wins its own spelling even when a
    # suffixed twin exists (the CSV import's twin rule, docs/05).
    index = market_data.security_symbol_index([sec(1, "VUN.TO", "TSX"), sec(2, "VUN", "TSX")])
    assert index["VUN"].id == 2
    assert index["VUN.TO"].id == 1

    # Only KNOWN venue suffixes are stripped: a US share class is not a
    # venue, so `BRK.B` never offers itself as the answer to `BRK`.
    index = market_data.security_symbol_index([sec(1, "BRK.B", "NYSE")])
    assert index["BRK.B"].id == 1
    assert index.get("BRK") is None

    # First id wins among duplicate spellings, as the old setdefault did.
    index = market_data.security_symbol_index([sec(1, "SHOP", "NYSE"), sec(2, "SHOP", "TSX")])
    assert index["SHOP"].id == 1


# --- seam 1: the provider write path ----------------------------------------


def test_import_fund_composition_links_a_bare_holding_to_the_suffixed_security(
    session: Session, monkeypatch: pytest.MonkeyPatch, security_inserts: _SecurityInserts
) -> None:
    """The card's pin: `EtfHolding(holding_symbol="005930")` written over a
    `Security(symbol="005930.KS", exchange="KSC")` ends up LINKED, with no
    `INSERT INTO securities` at all and the provider's weights untouched."""
    user_id = _user(session, "m154-import@example.com")
    fund = _security(session, user_id, "VEQT")
    samsung = _security(session, user_id, "005930.KS", kind=SecurityKind.stock, exchange=_KSC)

    monkeypatch.setattr(
        market_data,
        "fetch_fund_composition",
        lambda symbol: _provider_rows(
            ("005930", "Samsung Electronics", "60"), ("CASH", "Cash", "40")
        ),
    )

    with security_inserts as inserts:
        written = market_data.import_fund_composition(session, fund)

    assert written is not None and len(written) == 2
    rows = {row.holding_symbol: row for row in written}
    assert rows["005930"].holding_security_id == samsung.id
    assert rows["CASH"].holding_security_id is None  # nothing to link it to
    # Weight math is the same rows it always was: 60 + 40.
    assert sum((row.weight_pct for row in written), Decimal("0")) == Decimal("100")
    assert inserts == []  # nothing was created, so nothing collided


def test_ensure_sub_fund_composition_reuses_the_resolved_row_and_never_re_inserts_it(
    session: Session, monkeypatch: pytest.MonkeyPatch, security_inserts: _SecurityInserts
) -> None:
    """The 231-violations-per-run seam: the bare probe finds the suffixed
    row through the index (no provider call), the existing unlinked holding
    is back-linked, and no `INSERT INTO securities` is issued."""
    user_id = _user(session, "m154-deepen@example.com")
    fund = _security(session, user_id, "VEQT")
    samsung = _security(session, user_id, "005930.KS", kind=SecurityKind.stock, exchange=_KSC)
    holding = EtfHolding(
        user_id=user_id,
        security_id=fund.id,
        holding_symbol="005930",
        holding_name="Samsung Electronics",
        weight_pct=Decimal("5"),
        source=EtfHoldingSource.issuer,
    )
    session.add(holding)
    session.commit()

    lookups: list[str] = []

    def _spy_lookup(symbol: str) -> dict[str, object] | None:
        lookups.append(symbol)
        return _resolved("005930.KS", _KSC)

    monkeypatch.setattr(market_data, "lookup_symbol", _spy_lookup)

    with security_inserts as inserts:
        enriched = market_data.ensure_sub_fund_composition(session, fund, ["005930"])

    assert enriched == 0  # a stock: nothing to enrich, and nothing created
    assert lookups == []  # the index answered without a network call
    assert inserts == []
    session.refresh(holding)
    assert holding.holding_security_id == samsung.id
    # One row for the listing, not a spelling duplicate beside it.
    assert len(session.exec(select(Security).where(Security.user_id == user_id)).all()) == 2


def test_an_ambiguous_stem_never_links_by_guesswork_and_the_resolver_picks_the_listing(
    session: Session, monkeypatch: pytest.MonkeyPatch, security_inserts: _SecurityInserts
) -> None:
    """`005930.KS` (stock) and `005930.KQ` (fund-facts row) share a stem, so
    the write path leaves the holding UNLINKED — and when the deepening
    loop's `lookup_symbol` names the KSC listing, that answer is what links,
    still without an INSERT.

    The holding under test is the IMPORT's own row: a provider row seeded on
    the same fund would be replaced by that import (one write path, docs/05).
    """
    user_id = _user(session, "m154-ambiguous@example.com")
    fund = _security(session, user_id, "VEQT")
    samsung = _security(session, user_id, "005930.KS", kind=SecurityKind.stock, exchange=_KSC)
    facts = _security(session, user_id, "005930.KQ", kind=SecurityKind.mutual_fund, exchange=_KOE)

    monkeypatch.setattr(
        market_data,
        "fetch_fund_composition",
        lambda symbol: _provider_rows(("005930", "Samsung", "100")),
    )
    written = market_data.import_fund_composition(session, fund)
    assert written is not None and len(written) == 1
    assert written[0].holding_security_id is None  # ambiguous: no guesswork

    monkeypatch.setattr(market_data, "lookup_symbol", lambda symbol: _resolved("005930.KS", _KSC))
    with security_inserts as inserts:
        market_data.ensure_sub_fund_composition(session, fund, ["005930"])

    session.refresh(written[0])
    assert written[0].holding_security_id == samsung.id  # the RESOLVER's listing
    assert written[0].holding_security_id != facts.id
    assert inserts == []


def test_a_genuine_concurrent_create_recovers_by_the_resolved_symbol(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real race (the constraint is correct): another task commits
    `005930.KS` between this loop's map build and its INSERT. The INSERT
    still collides — and the recovery must now re-query the RESOLVED
    spelling, so the sub-fund is enriched and the holding links instead of
    being skipped silently."""
    user_id = _user(session, "m154-race@example.com")
    fund = _security(session, user_id, "VEQT")
    holding = EtfHolding(
        user_id=user_id,
        security_id=fund.id,
        holding_symbol="005930",
        holding_name="Samsung Electronics",
        weight_pct=Decimal("5"),
        source=EtfHoldingSource.issuer,
    )
    session.add(holding)
    session.commit()

    def _racing_lookup(symbol: str) -> dict[str, object] | None:
        winner = _security(session, user_id, "005930.KS", kind=SecurityKind.stock, exchange=_KSC)
        assert winner.symbol == "005930.KS"
        return _resolved("005930.KS", _KSC)

    monkeypatch.setattr(market_data, "lookup_symbol", _racing_lookup)

    market_data.ensure_sub_fund_composition(session, fund, ["005930"])

    session.refresh(holding)
    ksc_rows = [
        row
        for row in session.exec(select(Security).where(Security.user_id == user_id)).all()
        if row.symbol == "005930.KS"
    ]
    assert len(ksc_rows) == 1  # the loser's INSERT was rolled back, not duplicated
    assert holding.holding_security_id == ksc_rows[0].id


# --- seam 2: the manual bulk-replace ----------------------------------------


def test_put_holdings_links_a_manual_bare_ticker_to_the_suffixed_security(
    client: TestClient, session: Session
) -> None:
    """`PUT /securities/{id}/holdings` builds the same index, so a manually
    typed bare ticker links to the suffixed security too."""
    body = client.post(
        f"{API}/auth/register",
        json={"email": "m154-put@example.com", "password": "correct-horse-battery"},
    )
    assert body.status_code in (200, 201), body.text
    login = client.post(
        f"{API}/auth/login",
        json={"email": "m154-put@example.com", "password": "correct-horse-battery"},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    user = session.exec(select(User).where(User.email == "m154-put@example.com")).one()
    assert user.id is not None
    user_id = user.id

    fund = _security(session, user_id, "VEQT")
    samsung = _security(session, user_id, "005930.KS", kind=SecurityKind.stock, exchange=_KSC)

    response = client.put(
        f"{SEC_URL}/{fund.id}/holdings",
        headers=headers,
        json={
            "items": [
                {
                    "holding_symbol": "005930",
                    "holding_name": "Samsung Electronics",
                    "asset_class": "stock",
                    "weight_pct": "100",
                }
            ]
        },
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["holding_security_id"] == samsung.id
    assert response.json()["total_weight_pct"] == "100.00"  # weights untouched


# --- the deepening loop's own guard -----------------------------------------


def test_ensure_sub_fund_composition_still_creates_a_row_that_does_not_exist(
    session: Session, monkeypatch: pytest.MonkeyPatch, security_inserts: _SecurityInserts
) -> None:
    """The reuse guard must not stop the creator from doing its job: a
    ticker no row answers for is still resolved and created ONCE.

    A FUND here on purpose — a created stock is deliberately NOT counted in
    the return value (`enriched` counts enriched sub-FUNDS), so only the
    fund shape pins both halves: one INSERT, one row, one enrichment.
    """
    user_id = _user(session, "m154-create@example.com")
    fund = _security(session, user_id, "VEQT")

    monkeypatch.setattr(
        market_data,
        "lookup_symbol",
        lambda symbol: _resolved("005930.KS", _KSC, kind=SecurityKind.etf),
    )

    with security_inserts as inserts:
        enriched = market_data.ensure_sub_fund_composition(session, fund, ["005930"])

    created = [
        row
        for row in session.exec(select(Security).where(Security.user_id == user_id)).all()
        if row.symbol == "005930.KS"
    ]
    assert len(created) == 1
    assert created[0].exchange == _KSC
    assert enriched == 1
    assert len(inserts) == 1  # exactly one INSERT, exactly one row


def test_lookup_symbol_is_skipped_entirely_when_the_index_answers(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sub-fund the user already owns under a suffixed spelling costs no
    provider call — the whole reason the 231-violation run wasted 231
    network round-trips."""
    user_id = _user(session, "m154-nocal@example.com")
    fund = _security(session, user_id, "VEQT")
    _security(session, user_id, "VUN.TO", kind=SecurityKind.etf, exchange="TSX")

    calls: list[str] = []

    def _spy_lookup(symbol: str) -> None:
        calls.append(symbol)
        return None

    monkeypatch.setattr(market_data, "lookup_symbol", _spy_lookup)

    market_data.ensure_sub_fund_composition(session, fund, ["VUN"])

    assert calls == []  # the bare `VUN` found the stored `VUN.TO`


def test_provider_rows_keep_their_weights_when_everything_links(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Money-adjacent guard: linking changes one FK column and nothing else
    — the imported weights still sum exactly as the provider published them
    (docs/16's unclassified residual reads that sum)."""
    user_id = _user(session, "m154-weights@example.com")
    fund = _security(session, user_id, "VEQT")
    _security(session, user_id, "AAPL", kind=SecurityKind.stock, exchange="NASDAQ")
    _security(session, user_id, "NVDA", kind=SecurityKind.stock, exchange="NASDAQ")

    monkeypatch.setattr(
        market_data,
        "fetch_fund_composition",
        lambda symbol: _provider_rows(("AAPL", "Apple", "7.41"), ("NVDA", "Nvidia", "8.32")),
    )
    written = market_data.import_fund_composition(session, fund)

    assert written is not None
    assert [row.holding_security_id is not None for row in written] == [True, True]
    assert sum((row.weight_pct for row in written), Decimal("0")) == Decimal("15.73")
    stored = session.exec(
        select(EtfHolding).where(
            EtfHolding.security_id == fund.id, EtfHolding.source != EtfHoldingSource.manual
        )
    ).all()
    assert sum((row.weight_pct for row in stored), Decimal("0")) == Decimal("15.73")

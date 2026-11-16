"""M162 — a provider NAME that is a machine-joined ID artifact must never
become a security (and must never be adopted by any lookup seam).

Docs: docs/07 § "Symbol search fallback" / § "Candidate resolution".

The defect this pins, measured on prod 2026-10-11 (card t_08adfa14):

* Yahoo answers the KOSDAQ twin ``005930.KQ`` with
  ``quoteType=MUTUALFUND, shortName="005930.KQ,0P0000B2XZ,18569122",
  longName="Samsung Electronics Co Ltd", exchange=KOE, currency=KRW``
  (live probe in the api container; ``000660.KQ`` answers
  ``000660.KQ,0P0000AZ1B,4519170`` / "SK Hynix Inc").
* ``_payload_from_info`` adopts ``shortName`` FIRST, so that triple was
  persisted verbatim as the security's NAME: six rows reached prod
  (``kind=mutual_fund``, ``exchange=KOE``, ``listing_currency=USD`` —
  the currency heuristic does not know KRW either).
* The rows were created by ``ensure_sub_fund_composition`` (the only
  seam that INSERTs a Security straight from a ``lookup_symbol`` answer
  for a provider leg), and while they existed the stems ``005930`` /
  ``000660`` were AMBIGUOUS under M154's rule, so 10 bare legs stayed
  unlinked and 9 more legs linked to the junk rows by exact match. A
  2026-10-11 11:46 UTC refresh run re-linked freshly imported ``.KQ``
  legs to them again — the treadmill this guard stops.

The guard: ``market_data.provider_name_is_artifact`` — TWO or more commas,
every part non-empty, NO whitespace anywhere. Applied where a payload
becomes an answer:

* ``_is_real_quote`` refuses a ``Ticker.info`` body whose adopted name
  (shortName, else longName) is an artifact — the whole BODY is refused,
  not just the name: the record is a fund-facts mirror of an equity, and
  adopting it under its clean longName would re-create the mutual-fund
  twin that shadows the stem. The search fallback answers this symbol with
  ZERO rows (live-probed), so the lookup degrades to ``none`` and the
  deepening loop skips the leg — no row, no ambiguity, no treadmill.
* ``_search_equity_quotes`` skips a search row whose adopted name is an
  artifact, so neither the single-hit adopter nor the human chooser can
  re-introduce one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

import app.services.market_data as market_data
from app.api.deps import get_engine
from app.models.etf_holdings import EtfHolding, EtfHoldingSource
from app.models.securities import Security, SecurityKind
from app.models.users import User

# --- the live-probed payloads (api container, 2026-10-11) --------------------

#: `Ticker("005930.KQ").info` — the artifact record verbatim.
KQ_INFO: dict[str, object] = {
    "quoteType": "MUTUALFUND",
    "shortName": "005930.KQ,0P0000B2XZ,18569122",
    "longName": "Samsung Electronics Co Ltd",
    "exchange": "KOE",
    "fullExchangeName": "KOSDAQ",
    "currency": "KRW",
    "symbol": "005930.KQ",
}

#: `Ticker("000660.KQ").info` — the second artifact record.
KQ_INFO_660: dict[str, object] = {
    "quoteType": "MUTUALFUND",
    "shortName": "000660.KQ,0P0000AZ1B,4519170",
    "longName": "SK Hynix Inc",
    "exchange": "KOE",
    "fullExchangeName": "KOSDAQ",
    "currency": "KRW",
    "symbol": "000660.KQ",
}

#: `Ticker("005930.KS").info` — the REAL listing the guard must keep.
KS_INFO: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "SamsungElec",
    "longName": "Samsung Electronics Co., Ltd.",
    "exchange": "KSC",
    "currency": "KRW",
    "symbol": "005930.KS",
}

#: A search row carrying the same artifact name (the shape that must be
#: filtered out of the chooser).
ARTIFACT_SEARCH_ROW: dict[str, object] = {
    "symbol": "005930.KQ",
    "shortname": "005930.KQ,0P0000B2XZ,18569122",
    "longname": "Samsung Electronics Co Ltd",
    "exchange": "KOE",
    "quoteType": "MUTUALFUND",
}


@pytest.fixture(autouse=True)
def offline_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the module offline except for the seams each test replaces.

    Deliberately does NOT stub ``lookup_symbol``: the end-to-end tests run
    the REAL resolver over stubbed low-level transports (``yfinance.Ticker``
    and ``_http_search_json``), which is what proves the guard reaches the
    deepening loop through every layer it has to cross.
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


class _FakeTicker:
    """`Ticker.info` that only answers for symbols given at construction."""

    answers: ClassVar[dict[str, dict]] = {}

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    @property
    def info(self) -> dict:
        return self.answers.get(self.symbol, {})


def _patch_ticker(monkeypatch: pytest.MonkeyPatch, answers: dict[str, dict]) -> None:
    _FakeTicker.answers = answers
    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", _FakeTicker)


def _patch_empty_search(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the search FALLBACK's queries against an endpoint that answers
    no rows (the live answer for `005930.KQ`), keeping the real
    `_search_equity_quotes` filter in the loop."""
    calls: list[str] = []

    def fake(_query: str) -> dict[str, object]:
        calls.append(_query)
        return {}

    monkeypatch.setattr(market_data, "_http_search_json", fake)
    return calls


class _SecurityInserts:
    """Every `INSERT INTO securities` the engine executes inside the `with`.

    Same statement-counting harness M154 pinned (counting the STATEMENTS is
    what makes "no row was created" measurable rather than inferred).
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


def _user(session: Session, email: str) -> int:
    user = User(email=email, hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None
    return user.id


def _security(
    session: Session,
    user_id: int,
    symbol: str,
    *,
    kind: SecurityKind = SecurityKind.etf,
    exchange: str = "TSX",
    listing_currency: str = "CAD",
    name: str | None = None,
) -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=name or f"{symbol} long name",
        kind=kind,
        listing_currency=listing_currency,
        exchange=exchange,
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


# --- the predicate -----------------------------------------------------------


def test_provider_name_is_artifact_matches_the_live_triples_and_nothing_real() -> None:
    """The shape, asked as a pure function (no database, no network)."""
    # The two live-probed artifact names — the ONLY names this predicate
    # exists to catch, verbatim from the 2026-10-11 probe.
    assert market_data.provider_name_is_artifact("005930.KQ,0P0000B2XZ,18569122")
    assert market_data.provider_name_is_artifact("000660.KQ,0P0000AZ1B,4519170")

    # Real names, including the one that HAS a comma: a fund name is prose,
    # and prose carries whitespace.
    assert not market_data.provider_name_is_artifact("Principal Fds, Inc. California ...")
    assert not market_data.provider_name_is_artifact("SamsungElec")
    assert not market_data.provider_name_is_artifact("Samsung Electronics Co., Ltd.")
    assert not market_data.provider_name_is_artifact("Vanguard Total International Stock ETF")
    assert not market_data.provider_name_is_artifact("")

    # Near-misses must stay out: fewer commas, or an empty part, or any
    # whitespace at all.
    assert not market_data.provider_name_is_artifact("AAPL,USD")
    assert not market_data.provider_name_is_artifact("a,,c")
    assert not market_data.provider_name_is_artifact("a, b, c")
    assert not market_data.provider_name_is_artifact(None)


# --- the info path -----------------------------------------------------------


def test_real_quote_refuses_an_artifact_body_even_when_longname_is_clean() -> None:
    """The ``005930.KQ`` body verbatim: shortName is the triple, longName is
    a real company name. The WHOLE body is refused — adopting it under the
    longName would re-create the mutual-fund twin that shadows the stem."""
    assert not market_data._is_real_quote(dict(KQ_INFO))
    assert not market_data._is_real_quote(dict(KQ_INFO_660))

    # And the shapes that must keep passing (M141/M143's pins included).
    assert market_data._is_real_quote(dict(KS_INFO))
    assert market_data._is_real_quote({"shortName": "VGRO", "quoteType": "ETF"})
    assert market_data._is_real_quote({"longName": "Some Fund Only Long Name"})
    assert not market_data._is_real_quote({"trailingPegRatio": None})
    # A whitespace-only shortName falls through to a clean longName, exactly
    # as the pre-M162 gate's per-key strip did.
    assert market_data._is_real_quote({"shortName": "   ", "longName": "Real Name"})


def test_resolve_symbol_answers_none_for_the_artifact_twin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lookup contract the deepening loop reads: an artifact body is a
    MISS, the search fallback (live: ZERO rows for this symbol) cannot
    re-adopt it, and the answer is ``none`` — not a junk payload."""
    _patch_ticker(monkeypatch, {"005930.KQ": dict(KQ_INFO), "005930.KQ.TO": {}})
    calls = _patch_empty_search(monkeypatch)

    resolution = market_data.resolve_symbol("005930.KQ")
    assert resolution == {"match": "none", "result": None, "candidates": []}
    assert market_data.lookup_symbol("005930.KQ") is None
    # The fallback DID run — once per resolution above, and it found nothing
    # (live: `q=005930.KQ` answers zero rows).
    assert calls == ["005930.KQ", "005930.KQ"]


def test_resolve_still_adopts_the_real_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-regression half: the REAL ``005930.KS`` body resolves exactly
    as before (guard must not over-refuse), with its clean shortName."""
    _patch_ticker(monkeypatch, {"005930.KS": dict(KS_INFO)})
    _patch_empty_search(monkeypatch)

    resolution = market_data.resolve_symbol("005930.KS")
    assert resolution["match"] == "direct"
    result = resolution["result"]
    assert result is not None
    assert result["name"] == "SamsungElec"
    assert result["exchange"] == "KSC"


# --- the search path ---------------------------------------------------------


def test_search_rows_with_artifact_names_never_reach_the_chooser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A search row carrying the artifact name is dropped; a clean row in the
    same answer survives, so filtering never over-filters."""
    monkeypatch.setattr(
        market_data,
        "_http_search_json",
        lambda _q: {
            "quotes": [
                dict(ARTIFACT_SEARCH_ROW),
                {"symbol": "SSNLF", "shortname": "SAMSUNG ELECTRONICS CO", "quoteType": "EQUITY"},
            ]
        },
    )

    matches = market_data._search_equity_quotes("005930.KQ")
    assert [row["symbol"] for row in matches] == ["SSNLF"]


# --- the deepening loop, end to end ------------------------------------------


def test_deepening_loop_skips_the_artifact_leg_and_creates_no_row(
    session: Session, monkeypatch: pytest.MonkeyPatch, security_inserts: _SecurityInserts
) -> None:
    """THE production shape: the user already holds ``005930.KS`` (the M152
    seed created it), a yfinance-composed fund's leg spells ``005930.KQ``,
    and the probe runs the REAL resolver over the live-probed artifact body.

    Before M162 this INSERTed the junk ``mutual_fund`` row (prod: ids
    378/388/457) — after it, zero INSERTs, no new securities row, the leg
    stays unlinked, and the ``005930`` stem stays UNAMBIGUOUS.
    """
    user_id = _user(session, "m161-skip@example.com")
    fund = _security(session, user_id, "XEC.TO")
    samsung = _security(session, user_id, "005930.KS", kind=SecurityKind.stock, exchange="KSC")
    holding = EtfHolding(
        user_id=user_id,
        security_id=fund.id,
        holding_symbol="005930.KQ",
        holding_name="Samsung Electronics Co Ltd",
        asset_class="stock",
        weight_pct=Decimal("6.4428"),
        source=EtfHoldingSource.yfinance,
    )
    session.add(holding)
    session.commit()
    session.refresh(holding)

    # The live transports: the artifact body answers the `.KQ` probe, the
    # search endpoint answers zero rows (both live-measured 2026-10-11).
    _patch_ticker(monkeypatch, {"005930.KQ": dict(KQ_INFO), "005930.KQ.TO": {}})
    _patch_empty_search(monkeypatch)

    with security_inserts as inserts:
        enriched = market_data.ensure_sub_fund_composition(session, fund, ["005930.KQ"])

    assert enriched == 0
    assert inserts == []  # no securities row was created
    session.refresh(holding)
    assert holding.holding_security_id is None  # nothing to link to — honestly
    rows = [
        row
        for row in session.exec(select(Security).where(Security.user_id == user_id)).all()
        if row.symbol == "005930.KQ"
    ]
    assert rows == []  # the junk twin does not exist
    # The stem is unambiguous: the user's ONLY `005930` row is the real one.
    index = market_data.security_symbol_index(
        session.exec(
            select(Security).where(Security.user_id == user_id).order_by(Security.id)
        ).all()
    )
    assert index["005930"].id == samsung.id


def test_deepening_loop_still_creates_a_real_listing(
    session: Session, monkeypatch: pytest.MonkeyPatch, security_inserts: _SecurityInserts
) -> None:
    """The guard must not break normal creation: a bare leg whose lookup
    answers a CLEAN payload still creates the linking security, once."""
    user_id = _user(session, "m161-create@example.com")
    fund = _security(session, user_id, "VEQT")

    _patch_ticker(monkeypatch, {"005930": dict(KS_INFO)})
    _patch_empty_search(monkeypatch)

    with security_inserts as inserts:
        enriched = market_data.ensure_sub_fund_composition(session, fund, ["005930"])

    assert enriched == 0  # a STOCK leg is created for linking, never "enriched"
    assert len(inserts) == 1
    created = [
        row
        for row in session.exec(select(Security).where(Security.user_id == user_id)).all()
        if row.symbol == "005930"
    ]
    assert len(created) == 1
    assert created[0].name == "SamsungElec"
    assert created[0].exchange == "KSC"

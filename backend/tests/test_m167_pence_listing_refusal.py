"""M167 — a minor-unit listing is refused: the quoting unit is a fact about
the listing, never about the venue.

Docs: docs/07 § "A minor-unit listing is refused (M167)" (under §Symbol
search fallback), docs/02 §securities, docs/10 §M167.

The defect this pins, measured LIVE in the api container 2026-10-11 in one
run (`m167_lse_probe.json`, card t_8b9d7b6e) and on prod the same day:

* M163 refused LSE's ``GBp`` **as a code** and fell back to the venue
  heuristic, so every London listing adopted through any path carried a
  **USD** label — the last wrong label the app kept by design.
* London has NO single quoting unit. Of the six ``.L`` listings prod's
  ``securities`` table holds plus the four more probed, **six answer
  ``GBp``** (pence — prices 1395.8 / 3793.5 / 11998.0 / 93.0 / 118.35 /
  7126.0) and **two answer ``USD``** on the very same ``exchange: LSE``
  (``AV98.L`` 100.432, ``HKLD.L`` 7.41). So a per-venue unit factor —
  option (b) on the card — would have divided a correct USD price by 100,
  and the history path (``Ticker.history`` carries no currency) had no
  signal to divide BY at all.
* Prod carries **9** ``.L`` rows across three tenants (the card's premise
  of "zero" was false): all ``exchange=LSE``, all ``listing_currency=USD``,
  zero trades / prices / pools / watchlist / fundamentals, 21
  ``etf_holdings`` weight rows pointing at them — look-through legs, not
  holdings.

The contract (docs/07 §A minor-unit listing is refused):

* ``market_data.provider_currency_is_minor_unit`` — three ASCII letters in
  MIXED case is the provider's one spelling of a minor unit. M163's
  ``_normalize_currency_code`` still refuses the CODE; this is the reading
  that lets the adoption seam tell a minor unit from an absent or type-junk
  field, which must keep falling back to the venue heuristic.
* ``_is_real_quote`` refuses the WHOLE BODY, like M162's artifact rule:
  ``resolve_symbol`` → ``none``, ``lookup_symbol`` → ``None``, the lookup
  endpoint 404s, the add form falls into the UNRESOLVED path, and the
  deepening loop creates no row.
* ``_search_equity_quotes`` DROPS a London row (``.L``) and a Cboe-UK row
  (``CXE``) through ``provider_row_unit_is_unknown``: a search row carries
  no currency at all, so adopting one is a coin flip between "pence label"
  and "100x error". ``HSBAL.XC`` was probed answering ``GBp`` at HSBC's
  own pence price (1395.8, the same number ``HSBA.L`` reads).

Every ``currency`` and ``regularMarketPrice`` value below is verbatim from
the live probe; nothing here opens a socket.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest
from sqlalchemy.orm import Session
from sqlmodel import select

import app.services.market_data as market_data
from app.models.securities import Security
from app.models.users import User
from app.services.deep_backfill import TradeEvidence
from app.services.repair_report import Check, OwnedRow, Severity, live_findings

# --- the live-probed London bodies (api container, 2026-10-11) ----------------

#: `Ticker("HSBA.L").info` — HSBC, London. Pence.
LSE_HSBC_PENCE: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "HSBC HOLDINGS PLC ORD $0.50 (UK",
    "longName": "HSBC Holdings plc",
    "exchange": "LSE",
    "currency": "GBp",
    "symbol": "HSBA.L",
    "regularMarketPrice": 1395.8,
}

#: `Ticker("SHEL.L").info` — Shell, London. Pence.
LSE_SHELL_PENCE: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "SHELL PLC ORD EUR0.07",
    "longName": "Shell plc",
    "exchange": "LSE",
    "currency": "GBp",
    "symbol": "SHEL.L",
    "regularMarketPrice": 3793.5,
}

#: `Ticker("AZN.L").info` — AstraZeneca, London. Pence. (A prod look-through
#: leg with no `securities` row of its own.)
LSE_ASTRAZENECA_PENCE: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "ASTRAZENECA PLC ORD SHS $0.25",
    "longName": "AstraZeneca PLC",
    "exchange": "LSE",
    "currency": "GBp",
    "symbol": "AZN.L",
    "regularMarketPrice": 11998.0,
}

#: `Ticker("PEEL.L").info` — Peel Hunt, London. Pence.
LSE_PEEL_PENCE: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "PEEL HUNT LIMITED ORD NPV",
    "longName": "Peel Hunt Limited",
    "exchange": "LSE",
    "currency": "GBp",
    "symbol": "PEEL.L",
    "regularMarketPrice": 93.0,
}

#: `Ticker("VOD.L").info` — Vodafone, London. Pence.
LSE_VODAFONE_PENCE: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "VODAFONE GROUP PLC ORD USD0.20 ",
    "longName": "Vodafone Group Public Limited Company",
    "exchange": "LSE",
    "currency": "GBp",
    "symbol": "VOD.L",
    "regularMarketPrice": 118.35,
}

#: `Ticker("RIO.L").info` — Rio Tinto, London. Pence.
LSE_RIO_PENCE: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "RIO TINTO PLC ORD 10P",
    "longName": "Rio Tinto Group",
    "exchange": "LSE",
    "currency": "GBp",
    "symbol": "RIO.L",
    "regularMarketPrice": 7126.0,
}

#: `Ticker("HSBAL.XC").info` — the SAME HSBC listing on Cboe UK, probed
#: 2026-10-11: `fullExchangeName: "Cboe UK"`, `currency: "GBp"` and
#: `regularMarketPrice` 1395.8 — byte-identical to `HSBA.L`. Every other
#: `.XC` spelling tried (AZN/RIO/VOD/SHEL/BP/ULVR) 404s.
CXE_HSBC_PENCE: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "HSBC Holdings PLC",
    "exchange": "CXE",
    "currency": "GBp",
    "symbol": "HSBAL.XC",
    "regularMarketPrice": 1395.8,
}

#: `Ticker("AV98.L").info` — ADIB Sukuk II, London. **USD-quoted on the LSE**
#: (a 53-key stub body, which is itself why the refusal keys off `currency`
#: and never off the venue).
LSE_ADIB_USD: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "ADIB SUKUK COMPANY II LTD TST C",
    "exchange": "LSE",
    "currency": "USD",
    "symbol": "AV98.L",
    "regularMarketPrice": 100.432,
}

#: `Ticker("HKLD.L").info` — Hongkong Land, London. **USD-quoted on the LSE.**
LSE_HONGKONG_LAND_USD: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "HONG KONG LAND HOLDINGS LD ORD ",
    "longName": "Hongkong Land Holdings Limited",
    "exchange": "LSE",
    "currency": "USD",
    "symbol": "HKLD.L",
    "regularMarketPrice": 7.41,
}

#: `Ticker("SHEL").info` — the NYSE listing of the same company (control).
NYSE_SHELL: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "Shell PLC",
    "longName": "Shell plc",
    "exchange": "NYQ",
    "currency": "USD",
    "symbol": "SHEL",
    "regularMarketPrice": 100.18,
}

#: The six London bodies the provider answers `GBp` for.
PENCE_BODIES = [
    LSE_HSBC_PENCE,
    LSE_SHELL_PENCE,
    LSE_ASTRAZENECA_PENCE,
    LSE_PEEL_PENCE,
    LSE_VODAFONE_PENCE,
    LSE_RIO_PENCE,
    CXE_HSBC_PENCE,  # Cboe UK: the same listing, the same pence unit
]

#: The two London bodies the provider answers `USD` for — the anti-over-refusal half.
USD_LONDON_BODIES = [LSE_ADIB_USD, LSE_HONGKONG_LAND_USD]

#: The type-junk corpus M163's body-contract fuzzer mutates a real body's
#: `currency` with. None of these is a minor unit, so none may refuse a body.
JUNK_CURRENCIES = [
    None,
    "",
    "   ",
    42,
    3.5,
    True,
    b"USD",
    b"GBp",
    ["USD"],
    {"code": "USD"},
    float("nan"),
    "GB",
    "GBpx",
    "GB p",
    "US",
]


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


def _patch_search(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> None:
    """The Yahoo search endpoint answering `rows` verbatim."""

    def fake(_query: str) -> dict[str, object]:
        return {"quotes": rows}

    monkeypatch.setattr(market_data, "_http_search_json", fake)


def _patch_empty_search(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(_query: str) -> dict[str, object]:
        return {}

    monkeypatch.setattr(market_data, "_http_search_json", fake)


# --- 1. the predicate ---------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("GBp", True),  # the live LSE spelling
        ("Usd", True),  # MIXED case is the reading, not the name "GBp"
        ("gbp", False),  # all-lower normalizes UP to a base (M163's rule)
        ("GBP", False),  # a base, adoptable
        ("usd", False),
        ("USD", False),
        ("", False),
        ("   ", False),
        (None, False),
        (42, False),
        (3.5, False),
        (True, False),
        (b"GBp", False),  # a bytes value is type-junk, never a minor unit
        (["GBp"], False),
        ({"code": "GBp"}, False),
        ("GB", False),
        ("GBpx", False),
        ("GB p", False),
    ],
    ids=repr,
)
def test_the_minor_unit_predicate_reads_exactly_the_provider_spelling(
    value: object, expected: bool
) -> None:
    """Shape-based on purpose: three ASCII letters in MIXED case is the one
    spelling a provider uses for a minor unit. `Usd` counts — Yahoo never
    sends it, and the cost of that reading (refusing a body) can never
    become a wrong number, while the cost of missing one is a 100x price."""
    assert market_data.provider_currency_is_minor_unit(value) is expected


# --- 2. the adoption seam -----------------------------------------------------


@pytest.mark.parametrize("body", PENCE_BODIES, ids=lambda b: str(b["symbol"]))
def test_a_pence_body_is_a_miss_rather_than_a_usd_label(body: dict[str, object]) -> None:
    """The whole BODY is refused — M162's artifact rule, one condition
    further down the chain — so nothing can adopt it and nothing can carry
    the venue heuristic's USD label."""
    assert market_data._is_real_quote(body) is False


def test_the_lookup_answers_none_for_a_pence_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`HSBA.L` and its `.TO` twin both refuse, and the search fallback has
    no London row to offer: `none`, exactly the answer every caller already
    handles (the deepening loop `continue`s, the endpoint 404s)."""
    _patch_ticker(
        monkeypatch,
        {"HSBA.L": dict(LSE_HSBC_PENCE)},
    )
    _patch_empty_search(monkeypatch)

    assert market_data.resolve_symbol("HSBA.L") == {
        "match": "none",
        "result": None,
        "candidates": [],
    }
    assert market_data.lookup_symbol("HSBA.L") is None


@pytest.mark.parametrize("body", USD_LONDON_BODIES, ids=lambda b: str(b["symbol"]))
def test_the_usd_quoted_london_listings_are_still_adopted(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    """The anti-over-refusal half, LIVE-PROBED: `HKLD.L` and `AV98.L` are
    London listings quoted in USD, and their USD label plus USD closes are
    correct today. A venue rule would have refused both; the body's own
    `currency` adopts them exactly as before."""
    symbol = str(body["symbol"])
    _patch_ticker(monkeypatch, {symbol: dict(body)})
    _patch_empty_search(monkeypatch)

    resolution = market_data.resolve_symbol(symbol)
    assert resolution["match"] == "direct"
    result = resolution["result"]
    assert result is not None
    assert result["listing_currency"] == "USD"
    assert result["exchange"] == "LSE"


@pytest.mark.parametrize("junk", JUNK_CURRENCIES, ids=lambda v: repr(v)[:24])
def test_an_absent_or_type_junk_currency_keeps_falling_back(
    monkeypatch: pytest.MonkeyPatch, junk: object
) -> None:
    """The load-bearing non-regression: only a real minor-unit spelling
    refuses a body. The fuzzer's corpus (and every body Yahoo answers
    without the field) must keep the M163 fallback — the venue heuristic."""
    body = dict(NYSE_SHELL)
    body["currency"] = junk
    assert market_data._is_real_quote(body)  # the name is clean: a quote
    payload = market_data._payload_from_info("SHEL", body, "SHEL")
    assert payload["listing_currency"] == "USD"  # the heuristic answered


def test_the_m162_artifact_rule_still_wins_before_the_currency_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body refused on its NAME is refused whatever its currency says —
    the artifact triple carries `currency=KRW` and must stay a miss."""
    artifact = {
        "quoteType": "MUTUALFUND",
        "shortName": "005930.KQ,0P0000B2XZ,18569122",
        "longName": "Samsung Electronics Co Ltd",
        "exchange": "KOE",
        "currency": "KRW",
        "symbol": "005930.KQ",
    }
    _patch_ticker(monkeypatch, {"005930.KQ": artifact})
    _patch_empty_search(monkeypatch)

    assert market_data._is_real_quote(artifact) is False
    assert market_data.lookup_symbol("005930.KQ") is None


# --- 3. the name-search fallback ----------------------------------------------

#: The LIVE `yfinance.Search("HSBC").quotes` head (2026-10-11): a New York
#: row, a Hong Kong row, the London row, its Cboe-UK twin and a pink-sheet
#: row. Both venue-ambiguous rows are dropped.
LIVE_SEARCH_HSBC = [
    {"symbol": "HSBC", "exchange": "NYQ", "quoteType": "EQUITY", "shortname": "HSBC"},
    {"symbol": "0005.HK", "exchange": "HKG", "quoteType": "EQUITY", "shortname": "HSBC"},
    {
        "symbol": "HSBA.L",
        "exchange": "LSE",
        "quoteType": "EQUITY",
        "shortname": "HSBC HOLDINGS PLC ORD $0.50 (UK",
    },
    {"symbol": "HSBAL.XC", "exchange": "CXE", "quoteType": "EQUITY", "shortname": "HSBC"},
    {"symbol": "HBCYF", "exchange": "PNK", "quoteType": "EQUITY", "shortname": "HSBC"},
]


def test_london_never_survives_the_name_search_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A search row carries NO currency (live-measured, M142), and both UK
    venues answer pence (LSE: `HSBA.L` GBp / `HKLD.L` USD; CXE: `HSBAL.XC`
    GBp — probed the same run), so the rows are dropped before any builder
    can adopt them — the same seam M162 uses for artifact rows."""
    _patch_search(monkeypatch, list(LIVE_SEARCH_HSBC))

    symbols = [row["symbol"] for row in market_data._search_equity_quotes("HSBC")]
    assert symbols == ["HSBC", "0005.HK", "HBCYF"]  # no HSBA.L, no HSBAL.XC


def test_a_london_only_search_answer_is_none_not_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single-hit adopter would have stored `HSBA.L` labelled USD. With
    the row dropped the search answers zero rows and the lookup is `none`."""
    _patch_ticker(monkeypatch, {})
    _patch_search(
        monkeypatch,
        [
            {
                "symbol": "HSBA.L",
                "exchange": "LSE",
                "quoteType": "EQUITY",
                "shortname": "HSBC HOLDINGS PLC ORD $0.50 (UK",
            }
        ],
    )

    assert market_data.resolve_symbol("HSBC HOLDINGS") == {
        "match": "none",
        "result": None,
        "candidates": [],
    }


def test_the_chooser_never_offers_a_uk_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Several answers is still a chooser — with both UK venues removed from
    it, so a human pick cannot reintroduce a row the single-hit adopter
    lost (`HSBA.L` LSE and `HSBAL.XC` CXE are ONE HSBC listing, pence)."""
    _patch_ticker(monkeypatch, {})
    _patch_search(monkeypatch, list(LIVE_SEARCH_HSBC[:4]))

    resolution = market_data.resolve_symbol("HSBC")
    assert resolution["match"] == "ambiguous"
    symbols = [str(c["symbol"]) for c in resolution["candidates"]]
    assert symbols == ["HSBC", "0005.HK"]


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"symbol": "HSBA.L", "exchange": "LSE"}, True),  # pence
        ({"symbol": "HKLD.L", "exchange": "LSE"}, True),  # USD — the venue says both
        ({"symbol": "hsba.l", "exchange": "lse"}, True),  # case-insensitive
        ({"symbol": "HSBA.L", "exchange": ""}, True),  # the suffix alone is enough
        ({"symbol": "HSBC", "exchange": "LSE"}, True),  # the venue alone is enough
        ({"symbol": "HSBAL.XC", "exchange": "CXE"}, True),  # Cboe UK, GBp live
        ({"symbol": "HSBC", "exchange": "CXE"}, True),  # the venue, whatever the ticker
        ({"symbol": "HSBC", "exchange": "NYQ"}, False),
        ({"symbol": "SHEL", "exchange": "NYQ"}, False),
        ({"symbol": "RY.TO", "exchange": "TOR"}, False),
        ({"symbol": "600900.SS", "exchange": "SHH"}, False),
        ({"symbol": "005930.KS", "exchange": "KSC"}, False),
        ({"symbol": "SHELL.AS", "exchange": "AMS"}, False),  # Amsterdam, unprobed: no rule
        ({"symbol": "", "exchange": ""}, False),
        (None, False),
        ("HSBA.L", False),
    ],
    ids=repr,
)
def test_the_row_predicate_is_the_venue_reading_it_documents(row: object, expected: bool) -> None:
    assert market_data.provider_row_unit_is_unknown(row) is expected


# --- 4. the loops that would have created the row ------------------------------


def test_the_deepening_loop_creates_no_row_for_a_pence_leg(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    """The prod creation path: every `.L` row was a fund look-through leg the
    deepening loop INSERTed out of `lookup_symbol`. That seam answers `None`
    for a pence listing (pinned above), so the leg is skipped and the
    `etf_holdings` weight row simply stays unlinked — the state 11 of the 21
    London legs are already in."""
    user = User(email="m165@example.com", hashed_password="x")
    fund = Security(
        user_id=0,
        symbol="XIU",
        name="iShares Core S&P/TSX 60",
        kind="etf",
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None
    fund.user_id = user.id
    session.add(fund)
    session.commit()
    session.refresh(fund)

    monkeypatch.setattr(market_data, "lookup_symbol", lambda _symbol: None)

    enriched = market_data.ensure_sub_fund_composition(session, fund, ["HSBA.L"])

    assert enriched == 0
    legs = session.exec(
        select(Security).where(Security.user_id == user.id, Security.symbol != "XIU")
    ).all()
    assert legs == []


# --- 5. the standing repair report sees the class ------------------------------


def test_the_standing_report_calls_a_legacy_pence_row_what_it_is() -> None:
    """Prod's five pence rows carried `listing_currency=USD` and a live probe
    that answered the same USD — the mismatch was INVISIBLE, exactly M163's
    KRW problem. M167 seals adoption, so the probe now answers `none` and the
    report names the row instead of agreeing with it. After the prod repair
    (empty currency + dead_symbol) the same row reads as the UNRESOLVED state
    the add form writes — info, not review."""
    none = market_data.SymbolResolution(match="none", result=None, candidates=[])
    evidence = (TradeEvidence(date=date(2026, 10, 10), price=Decimal("1395.8"), currency="USD"),)

    legacy = OwnedRow(
        id=392,
        user_id=343,
        symbol="HSBA.L",
        name="HSBC HOLDINGS PLC ORD $0.50 (UK",
        kind="stock",
        listing_currency="USD",  # what prod stored before the repair
        exchange="LSE",
        dead_symbol=False,
        quote_miss_count=0,
        dead_symbol_reason=None,
        privacy_mode=False,
        trades=0,
        watched=False,
        evidence=evidence,
    )
    findings = live_findings(legacy, none)
    assert [f.check for f in findings] == [Check.no_listing]
    assert findings[0].severity is Severity.review

    repaired = OwnedRow(
        id=392,
        user_id=343,
        symbol="HSBA.L",
        name="HSBC HOLDINGS PLC ORD $0.50 (UK",
        kind="stock",
        listing_currency="",  # the UNRESOLVED marker the repair applies
        exchange="LSE",
        dead_symbol=True,
        quote_miss_count=0,
        dead_symbol_reason="London listings are quoted in a unit this app cannot name (M167)",
        privacy_mode=False,
        trades=0,
        watched=False,
        evidence=evidence,
    )
    findings = live_findings(repaired, none)
    assert [f.check for f in findings] == [Check.unresolved_still]
    assert findings[0].severity is Severity.info

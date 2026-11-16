"""Vanguard full-holdings providers (M147-followup, docs/07 §Fund composition providers).

Every fixture is a LIVE capture from 2026-10-11, trimmed only in list length —
the rows themselves are verbatim:

* `vanguard_us_additionalfunddata.json` — the `holdingDetails` envelope of
  `GET https://investor.vanguard.com/irr/funds/profile/VXUS-AdditionalFundData`
  (the bond row comes from BND's payload, because VXUS publishes its three
  bonds without a weight).
* `vanguard_ca_holdings_vcn.json` / `..._veqt.json` — the
  `data.borHoldings[0].holdings` envelope of
  `POST https://www.vanguard.ca/gpx/graphql` (FundsHoldingsQuery, VCN portId
  9561 / VEQT portId 9692).

No test in this module performs a network call: the fetchers are exercised
with a monkeypatched `httpx`, and the parsers are pure functions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlmodel import Session, select

from app.models.etf_holdings import EtfHolding, EtfHoldingSource
from app.models.provider_registry import ProviderFundFamily, ProviderFundStatus
from app.models.securities import Security, SecurityKind
from app.models.users import User
from app.services import market_data, provider_registry

_FIXTURES = Path(__file__).parent / "fixtures"
API = "/api/v1"


def _load(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text())


def _ca_suffixes(session: Session) -> dict[str, str | None]:
    """The venue-suffix map the parser is handed in production (M168).

    It used to be a module constant; it is the `vanguard_ca` family of
    `provider_fund_registry` now, loaded once per fetch rather than per row.
    """
    return {
        ticker: row.venue_suffix
        for ticker, row in provider_registry.load_family(
            ProviderFundFamily.vanguard_ca, session=session
        ).items()
    }


# --- US payload parser (docs/07 pinned shape) --------------------------------


def test_vanguard_us_parses_the_pinned_live_shape() -> None:
    payload = _load("vanguard_us_additionalfunddata.json")
    rows = market_data._parse_vanguard_us_payload(payload)
    assert rows is not None and len(rows) == 8
    assert sum(row["weight_pct"] for row in rows) == Decimal("10.6300")

    by_symbol = {row.get("holding_symbol"): row for row in rows}
    tsmc = by_symbol["2330"]
    assert tsmc["holding_name"] == "Taiwan Semiconductor Manufacturing Co Ltd"
    assert tsmc["weight_pct"] == Decimal("3.9400")  # percent of NAV -> Numeric(7,4)
    assert tsmc["country"] == "Taiwan"  # ISIN prefix TW
    assert tsmc["asset_class"] == "stock"
    assert by_symbol["005930"]["country"] == "South Korea"
    assert by_symbol["000660"]["country"] == "South Korea"

    # A bond, cash and a derivative each map to their own asset class.
    assert by_symbol["T"]["asset_class"] == "bond"
    assert by_symbol["T"]["country"] == "United States"
    cash = by_symbol[None]
    assert cash["asset_class"] == "cash" and cash["holding_name"] == "SLCMT1142"
    assert by_symbol["VGU6"]["asset_class"] == "other"
    assert by_symbol["VGU6"]["country"] == "Germany"

    # An offshore incorporation domicile is not a geography we will assert.
    assert by_symbol["700"].get("country") is None  # Tencent, ISIN KYG...
    # A fund leg carries no country either: the wrapper's domicile is not its
    # look-through geography (the engine recurses or leaves it Unclassified).
    assert by_symbol["VWO"].get("country") is None

    # Rows the column cannot store are dropped, not written and failed later:
    # a 0.00% row, a row with no weight, and a `CT.*` currency-hedge leg.
    assert "8381" not in by_symbol  # 0.00%
    assert "3395WD" not in by_symbol  # blank marketValuePercentage
    assert "EUR" not in by_symbol  # CT.FOREX hedge leg


def test_vanguard_us_error_body_and_empty_payloads_are_none() -> None:
    assert market_data._parse_vanguard_us_payload({"message": "Fund not found"}) is None
    assert market_data._parse_vanguard_us_payload("<html>Failed to fetch</html>") is None
    assert market_data._parse_vanguard_us_payload(None) is None
    assert market_data._parse_vanguard_us_payload({"holdingDetails": {}}) is None


def test_vanguard_weight_quantizes_and_drops_unstorable_rows() -> None:
    assert market_data._vanguard_weight("3.94%") == Decimal("3.9400")
    assert market_data._vanguard_weight(" 7.76333 ") == Decimal("7.7633")
    assert market_data._vanguard_weight(0.00102) == Decimal("0.0010")
    # The column is Numeric(7,4) CHECK (weight_pct > 0): anything that lands on
    # zero or outside (0, 100] would abort the whole import at write time.
    assert market_data._vanguard_weight("0.00") is None
    assert market_data._vanguard_weight("0.00001") is None
    assert market_data._vanguard_weight(-0.14) is None
    assert market_data._vanguard_weight("100.50%") is None
    assert market_data._vanguard_weight(None) is None
    assert market_data._vanguard_weight("") is None
    assert market_data._vanguard_weight("n/a") is None


def test_vanguard_asset_class_covers_the_published_code_table() -> None:
    assert market_data._vanguard_asset_class("EQ.STOCK", "EQ") == "stock"
    assert market_data._vanguard_asset_class("EQ.REIT") == "stock"
    assert market_data._vanguard_asset_class("FI.CORP", "FI") == "bond"
    assert market_data._vanguard_asset_class("MM.CASH", "MM") == "cash"
    assert market_data._vanguard_asset_class("CT.FOREX") == "cash"
    assert market_data._vanguard_asset_class("CRNY") == "cash"
    assert market_data._vanguard_asset_class("DE.IND", "DE") == "other"
    assert market_data._vanguard_asset_class("MF.MF") == "other"
    assert market_data._vanguard_asset_class(None) == "other"


# --- CA payload parser (docs/07 pinned shape) --------------------------------


def test_vanguard_ca_parses_the_pinned_live_shape(session: Session) -> None:
    payload = _load("vanguard_ca_holdings_vcn.json")
    items = payload["data"]["borHoldings"][0]["holdings"]["items"]
    rows = market_data._parse_vanguard_ca_items(items, _ca_suffixes(session))
    assert rows is not None and len(rows) == 6  # three 0-weight rows dropped
    assert sum(row["weight_pct"] for row in rows) == Decimal("18.9444")

    by_symbol = {row["holding_symbol"]: row for row in rows}
    ry = by_symbol["RY"]
    assert ry["weight_pct"] == Decimal("7.7633")  # JSON number, percent of NAV
    assert ry["country"] == "Canada"  # bloombergIsoCountry
    assert ry["sector"] == "Financials"  # gicsSectorDescription
    assert ry["asset_class"] == "stock"
    assert by_symbol["SHOP"]["sector"] == "Information Technology"
    assert by_symbol["CAD"]["asset_class"] == "cash"
    # A futures contract maps to `other`, and the site publishes no GICS sector.
    assert by_symbol["PTU6"]["asset_class"] == "other"
    assert by_symbol["PTU6"].get("sector") is None
    # A Canadian equity ticker is stored exactly as the issuer prints it — only
    # fund legs get the venue suffix (below).
    assert "RY.TO" not in by_symbol


def test_vanguard_ca_fund_legs_get_the_venue_suffix_and_no_country(
    session: Session,
) -> None:
    payload = _load("vanguard_ca_holdings_veqt.json")
    items = payload["data"]["borHoldings"][0]["holdings"]["items"]
    rows = market_data._parse_vanguard_ca_items(items, _ca_suffixes(session))
    assert rows is not None and len(rows) == 6  # the two CT.* hedge legs dropped
    assert sum(row["weight_pct"] for row in rows) == Decimal("99.9999")

    by_symbol = {row["holding_symbol"]: row for row in rows}
    # VEQT is a fund-of-funds: its legs are stored with the venue suffix the CA
    # registry names, because the look-through engine matches the user's
    # stored `VCN.TO` securities exactly (docs/05 recursion).
    assert set(by_symbol) == {"VUN.TO", "VCN.TO", "VIU.TO", "VEE.TO", "CAD", "USD"}
    assert by_symbol["VUN.TO"]["weight_pct"] == Decimal("44.7727")
    assert by_symbol["VUN.TO"].get("country") is None
    assert by_symbol["VUN.TO"].get("sector") is None
    assert by_symbol["VUN.TO"]["asset_class"] == "stock"
    # Cash keeps its country: it is not a wrapper.
    assert by_symbol["CAD"]["country"] == "Canada"
    assert by_symbol["USD"]["country"] == "United States"


def test_vanguard_ca_empty_200_is_a_failure_not_an_empty_fund() -> None:
    """Negative control, measured live 2026-10-11: an unknown portId answers
    HTTP 200 with EMPTY arrays — emptiness, not the status line, is the signal."""
    calls: list[dict[str, object]] = []

    class _Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    def post(url: str, **kwargs: object) -> _Response:
        calls.append(kwargs)
        return _Response({"data": {"borHoldings": []}})

    monkey = pytest.MonkeyPatch()
    monkey.setattr(httpx, "post", post)
    try:
        assert market_data._fetch_vanguard_ca("VCN") is None
        assert calls, "a registered ticker must attempt exactly one call"
    finally:
        monkey.undo()


# --- registries: no network for anything unregistered ------------------------


def test_a_miss_row_stops_every_further_probe(session: Session) -> None:
    """The M168 negative cache, both halves of it.

    A ticker NO registry row knows about may be probed exactly ONCE — that is
    what discovery is for — and the verdict it earns is recorded. From then on
    the row answers, the network is never touched again, and a ticker the
    issuer already called a miss never gets a single request.

    A registry `miss` row is the only thing that makes "no repeated 404
    probes" true; before M168 the guarantee came from a literal the ticker
    simply was not in.
    """
    calls: dict[str, int] = {"get": 0, "post": 0}

    class _Response:
        def __init__(self, status: int, payload: dict[str, object]) -> None:
            self.status_code = status
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

        @property
        def text(self) -> str:
            return json.dumps(self._payload)

    def fake_get(url: object = "", **kwargs: object) -> _Response:
        calls["get"] += 1
        if "AdditionalFundData" in str(url):
            return _Response(404, {"message": "Fund not found"})  # a verdict
        # The CA catalogue must ANSWER for "SPY is not ours" to be a verdict;
        # a 404 here would say only "the catalogue is down".
        return _Response(200, {"site-config": {"portIds": "9561"}})

    def fake_post(url: object = "", **kwargs: object) -> _Response:
        calls["post"] += 1
        return _Response(
            200,
            {
                "data": {
                    "funds": [
                        {
                            "portId": "9561",
                            "profile": {
                                "listings": [
                                    {
                                        "stockExchangeMarketIdentifierCode": "XTSE",
                                        "identifiers": [
                                            {"altId": "Ticker - Canada", "altIdValue": "VCN"}
                                        ],
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
        )

    # start from "no opinion": the fixture put the seed back after the test.
    for family in (ProviderFundFamily.vanguard_us, ProviderFundFamily.vanguard_ca):
        provider_registry.delete(family, "SPY", session=session)
    market_data._reset_discovery_cache()

    monkey = pytest.MonkeyPatch()
    monkey.setattr(httpx, "get", fake_get)
    monkey.setattr(httpx, "post", fake_post)
    try:
        assert market_data.fetch_vanguard_holdings("SPY") is None  # not a product
        first_probe = dict(calls)
        assert first_probe["get"] >= 1, "a first encounter must probe to learn anything"
        us = provider_registry.get(ProviderFundFamily.vanguard_us, "SPY", session=session)
        assert us is not None and us.status is ProviderFundStatus.miss
        ca = provider_registry.get(ProviderFundFamily.vanguard_ca, "SPY", session=session)
        assert ca is not None and ca.status is ProviderFundStatus.miss

        # …and every later encounter is answered by the row, never the wire.
        assert market_data.fetch_vanguard_holdings("SPY") is None
        assert market_data.fetch_vanguard_holdings("SPY") is None
        assert calls == first_probe, "a miss row must stop every further probe"

        # A ticker EMPTY on the registry (never probed) also answers with no
        # network call — `""` and a whitespace-only symbol are not tickers.
        assert market_data.fetch_vanguard_holdings("") is None
        assert market_data.fetch_vanguard_holdings("   ") is None
        assert calls == first_probe
    finally:
        monkey.undo()
        market_data._reset_discovery_cache()


def test_a_registered_ticker_is_probed_once_and_never_reregistered(
    session: Session,
) -> None:
    """The other half: a `verified` row is probed (it still has to fetch the
    holdings!) but its verdict is not rewritten, so `verified_at` stays the
    sweep's to move."""
    before = provider_registry.get(ProviderFundFamily.vanguard_us, "VXUS", session=session)
    assert before is not None and before.status is ProviderFundStatus.verified
    assert before.verified_by_run == "seed:0050"

    attempts = {"get": 0}

    class _Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return _load("vanguard_us_additionalfunddata.json")

    def fake_get(*args: object, **kwargs: object) -> _Response:
        attempts["get"] += 1
        return _Response()

    monkey = pytest.MonkeyPatch()
    monkey.setattr(httpx, "get", fake_get)
    try:
        rows = market_data.fetch_vanguard_holdings("VXUS")
        assert rows, "a verified row must still reach its endpoint"
        assert attempts["get"] == 1
    finally:
        monkey.undo()
    after = provider_registry.get(ProviderFundFamily.vanguard_us, "VXUS", session=session)
    assert after is not None and after.verified_by_run == "seed:0050"


def test_registries_are_the_live_checked_pairs(session: Session) -> None:
    """The registry is DATA now (M168), so the pairs are asserted against the
    table migration 0050 seeded with them — the same live-checked values the
    literals held, and the same reason: an entry exists only because the
    issuer answered 200 with a parseable payload.

    The CI grep-assert that goes with this (`git grep VANGUARD_US_FUNDS
    backend/app` returning nothing) is what keeps the pattern retired: the
    code no longer knows a single ticker.
    """
    us = provider_registry.load_family(ProviderFundFamily.vanguard_us, session=session)
    ca = provider_registry.load_family(ProviderFundFamily.vanguard_ca, session=session)
    # US: profile JSON products (each verified HTTP 200 + parseable payload).
    for ticker in ("VXUS", "VTI", "VT", "VOO", "VEA", "VGT", "VWO", "BND"):
        assert ticker in us
    assert "SPY" not in us
    assert "IVV" not in us
    # CA: Canada ticker -> (portId, Yahoo venue suffix) from the issuer's own
    # product model + FundFinderFunds listing metadata.
    assert (ca["VCN"].port_id, ca["VCN"].venue_suffix) == ("9561", ".TO")
    assert (ca["VBG"].port_id, ca["VBG"].venue_suffix) == ("9568", ".NE")
    assert (ca["VEQT"].port_id, ca["VEQT"].venue_suffix) == ("9692", ".TO")
    # The two registries never collide, so the chain order cannot change meaning.
    assert not set(us) & set(ca)
    # Seeded, not discovered on this run: the sweep's clock starts at the
    # migration, not at whichever test happens to run first.
    assert all(row.verified_by_run == "seed:0050" for row in [*us.values(), *ca.values()])


def test_vanguard_ca_key_strips_the_exchange_suffix() -> None:
    assert market_data._vanguard_ca_key("VCN.TO") == "VCN"
    assert market_data._vanguard_ca_key("VBG.NE") == "VBG"
    assert market_data._vanguard_ca_key("VEQT") == "VEQT"
    assert market_data._vanguard_ca_key("RY.TO") == "RY"  # not registered -> no call


# --- the chain (docs/07 order) ------------------------------------------------


def test_provider_chain_prefers_issuer_then_vanguard_then_yfinance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def issuer(symbol: str) -> list[dict[str, object]] | None:
        if symbol.upper() == "IVV":
            return [{"holding_name": "NVIDIA", "weight_pct": Decimal("8.32")}]
        return None

    def vanguard(symbol: str) -> list[dict[str, object]] | None:
        if symbol.upper().startswith("VXUS") or symbol.upper().startswith("VCN"):
            return [{"holding_name": "Royal Bank of Canada", "weight_pct": Decimal("7.76")}]
        return None

    def yf(symbol: str) -> list[dict[str, object]] | None:
        return [{"holding_name": "Sub fund", "weight_pct": Decimal("100")}]

    monkeypatch.setattr(market_data, "fetch_issuer_holdings", issuer)
    monkeypatch.setattr(market_data, "fetch_vanguard_holdings", vanguard)
    monkeypatch.setattr(market_data, "fetch_fund_holdings", yf)

    chain = market_data.fetch_fund_composition("IVV")
    assert chain is not None and chain[0] == EtfHoldingSource.issuer
    chain = market_data.fetch_fund_composition("VXUS")
    assert chain is not None and chain[0] == EtfHoldingSource.vanguard
    chain = market_data.fetch_fund_composition("VCN.TO")
    assert chain is not None and chain[0] == EtfHoldingSource.vanguard
    chain = market_data.fetch_fund_composition("VEQT")
    assert chain is not None and chain[0] == EtfHoldingSource.yfinance

    # Every provider missing -> the chain answers None, never an empty list.
    monkeypatch.setattr(market_data, "fetch_issuer_holdings", lambda symbol: None)
    monkeypatch.setattr(market_data, "fetch_vanguard_holdings", lambda symbol: None)
    monkeypatch.setattr(market_data, "fetch_fund_holdings", lambda symbol: None)
    assert market_data.fetch_fund_composition("VEQT") is None


@pytest.mark.parametrize(
    ("outcome", "symbol"),
    [
        ("raise", "VXUS"),  # US endpoint gone
        ("404", "VXUS"),
        ("raise", "VCN.TO"),  # CA endpoint gone
        ("400", "VCN.TO"),  # missing X-Consumer-ID
        ("empty", "VCN.TO"),  # 200 with EMPTY arrays
    ],
)
def test_vanguard_endpoint_failure_degrades_to_the_yfinance_slice(
    monkeypatch: pytest.MonkeyPatch, outcome: str, symbol: str
) -> None:
    """The required negative: an endpoint that answers badly (or not at all)
    must leave the chain on the yfinance top-holdings slice, source=yfinance."""
    called = {"get": 0, "post": 0}

    class _Response:
        def __init__(self, status: int, payload: dict[str, object]) -> None:
            self.status_code = status
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    def _respond() -> _Response:
        if outcome == "404":
            return _Response(404, {"message": "Fund not found"})
        if outcome == "400":
            return _Response(400, {"errors": [{"message": "x-consumer-id must be provided"}]})
        if outcome == "empty":
            return _Response(200, {"data": {"borHoldings": []}})
        raise httpx.ConnectError("endpoint gone")

    def fake_get(*args: object, **kwargs: object) -> _Response:
        called["get"] += 1
        return _respond()

    def fake_post(*args: object, **kwargs: object) -> _Response:
        called["post"] += 1
        return _respond()

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(market_data, "fetch_issuer_holdings", lambda symbol: None)
    monkeypatch.setattr(
        market_data,
        "fetch_fund_holdings",
        lambda symbol: [{"holding_name": "Top slice", "weight_pct": Decimal("33.45")}],
    )

    chain = market_data.fetch_fund_composition(symbol)
    assert chain is not None and chain[0] == EtfHoldingSource.yfinance
    expected = "get" if symbol == "VXUS" else "post"
    assert called[expected] >= 1


def test_privacy_lock_blocks_the_vanguard_fetch_before_any_network() -> None:
    market_data.bind_privacy_lock(True)
    try:
        with pytest.raises(Exception) as excinfo:
            market_data.fetch_vanguard_holdings("VXUS")
        assert "privacy" in str(excinfo.value).lower()
    finally:
        market_data.bind_privacy_lock(False)


# --- rows reach the single write path with the new provenance -----------------


def test_import_fund_composition_stores_vanguard_rows_and_source(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email=f"vg-{datetime.now(UTC).timestamp()}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    fund = Security(
        user_id=user.id,
        symbol="VXUS",
        name="Vanguard Total International Stock ETF",
        kind=SecurityKind.stock,  # the import-created default the path repairs
        listing_currency="USD",
        exchange="NASDAQ",
    )
    session.add(fund)
    session.commit()
    session.refresh(fund)

    rows = market_data._parse_vanguard_us_payload(_load("vanguard_us_additionalfunddata.json"))
    assert rows is not None
    monkeypatch.setattr(market_data, "fetch_issuer_holdings", lambda symbol: None)
    monkeypatch.setattr(market_data, "fetch_vanguard_holdings", lambda symbol: rows)
    monkeypatch.setattr(market_data, "fetch_fund_holdings", lambda symbol: None)
    # --- and the write path itself (the seed's per-fund call) ----------------
    written = market_data.import_fund_composition(session, fund)
    assert written is not None and len(written) == len(rows)
    stored = session.exec(select(EtfHolding).where(EtfHolding.security_id == fund.id)).all()
    assert {row.source for row in stored} == {EtfHoldingSource.vanguard}
    assert {row.country for row in stored if row.country} >= {"Taiwan", "United States"}
    session.refresh(fund)
    assert fund.kind == SecurityKind.etf  # provider proved it is a fund


# --- the deepening loop stays bounded on a full-holdings list ----------------


def test_sub_fund_deepening_probes_are_capped_on_full_holdings(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M147-followup: a full-holdings provider hands the M19 deepening
    THOUSANDS of tickers (VXUS 2,283, VIU 3,663) and every non-fund ticker it
    RESOLVES creates a linking security WITHOUT counting toward the 12
    enrichments — so the enrichment cap alone let the loop walk the whole fund
    (measured on prod: 1,458 securities created for one tenant). The probe cap
    bounds both the lookups and the creations to the heaviest rows."""
    user = User(email="vg-probe@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    fund = Security(
        user_id=user.id,
        symbol="VXUS",
        name="Vanguard Total International Stock ETF",
        kind=SecurityKind.etf,
        listing_currency="USD",
        exchange="NASDAQ",
    )
    session.add(fund)
    session.commit()
    session.refresh(fund)

    probes: list[str] = []

    def fake_lookup(symbol: str) -> dict[str, object]:
        probes.append(symbol)
        return {
            "symbol": symbol.upper(),
            "name": f"{symbol} Inc",
            "kind": SecurityKind.stock,
            "listing_currency": "USD",
            "exchange": "NYSE",
        }

    monkeypatch.setattr(market_data, "lookup_symbol", fake_lookup)
    symbols = [f"T{i}" for i in range(500)]  # a full-holdings-scale list
    enriched = market_data.ensure_sub_fund_composition(session, fund, symbols)

    assert enriched == 0  # none of them is a fund
    assert len(probes) == market_data.SUB_FUND_PROBE_CAP
    created = session.exec(select(Security).where(Security.user_id == user.id)).all()
    assert len(created) == market_data.SUB_FUND_PROBE_CAP + 1  # + the fund itself

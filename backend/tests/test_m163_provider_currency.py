"""M163 — the provider's own `currency` field wins over the venue heuristic.

Docs: docs/07 § "Listing currency (M163)" (under §Symbol search fallback).

The defect this pins, measured LIVE in the api container 2026-10-11 and on
prod the same day (card t_cbd488cf, found during M162 t_08adfa14):

* ``Ticker("005930.KS").info`` answers ``currency=KRW``, ``exchange=KSC``
  (Samsung Electronics, EQUITY) — and ``_listing_currency``, which only
  knows Canadian venues, stamped the row **USD**. ``9984.T`` (JPY, JPX) and
  every other non-North-American listing were stamped USD the same way.
* That wrong label is spent by every FX conversion of the row's prices and
  trades (docs/06 pool math, docs/12 valuation, docs/15 exposure), so a
  KRW holding is silently valued at the KRW/USD ratio — the security-296
  class (a CAD holding stamped USD), generalised to every market the
  heuristic does not name.
* Prod's two real KSC rows (1381/1669, 1382/1670) carry USD and reference
  zero trades, so nothing was mis-valued yet; the M162 artifact rows were
  the junk this produced and are deleted.

The contract (docs/07 §Listing currency):

* a ``Ticker.info`` body's ``currency`` is BELIEVED when it is a well-formed
  ISO-4217 code — a **fact** about the listing;
* ``_listing_currency``'s ``.TO``/Canadian-venue rule survives as the
  FALLBACK, for the payloads that carry no such field: every SEARCH row
  (live-measured: they carry none) and any body whose ``currency`` is
  absent or type-junk;
* LSE's minor-unit code ``GBp`` (pence — ``HSBA.L``, ``VOD.L`` live) is
  deliberately NOT adopted: storing ``GBP`` would multiply a pence-quoted
  price by 100 (the M87 split class). **M167 (``test_m167_pence_listing_refusal``)
  takes that further and refuses the LISTING**; the M163 assertion below
  is re-pinned to the refusal, and this module keeps proving the other
  half — an absent or type-junk ``currency`` is NOT a minor unit and must
  keep falling back to the venue heuristic.

Every ``currency`` value below is verbatim from a live probe in the deployed
api container; nothing here opens a socket.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest
from sqlalchemy.orm import Session
from sqlmodel import select

import app.services.market_data as market_data
import app.tax.acb as acb
from app.core.errors import APIError
from app.models.acb_pools import AcbPool
from app.models.accounts import Account, AccountType
from app.models.fx_rates import FxRate, FxSource
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.services import fx, fx_status
from app.services.deep_backfill import TradeEvidence
from app.services.repair_report import Check, OwnedRow, Severity, live_findings

# --- the live-probed bodies (api container, 2026-10-11) -----------------------
# Seven listings across four venues, probed in ONE run so the currency set is
# one measurement rather than seven anecdotes.

#: `Ticker("005930.KS").info` — Samsung Electronics, KRX main board.
KS_SAMSUNG: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "SamsungElec",
    "longName": "Samsung Electronics Co., Ltd.",
    "exchange": "KSC",
    "currency": "KRW",
    "symbol": "005930.KS",
}

#: `Ticker("000660.KS").info` — SK hynix, KRX.
KS_HYNIX: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "SK hynix",
    "longName": "SK hynix Inc.",
    "exchange": "KSC",
    "currency": "KRW",
    "symbol": "000660.KS",
}

#: `Ticker("9984.T").info` — SoftBank Group, Tokyo.
TOKYO_SOFTBANK: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "SOFTBANK GROUP CORP",
    "longName": "SoftBank Group Corp.",
    "exchange": "JPX",
    "currency": "JPY",
    "symbol": "9984.T",
}

#: `Ticker("HSBA.L").info` — HSBC, London. Yahoo prices most LSE listings in
#: the MINOR unit and says so in the code (``GBp`` = pence). M167: refused
#: outright (docs/07 §A minor-unit listing is refused).
LSE_HSBC: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "HSBC HOLDINGS PLC ORD $0.50 (UK",
    "longName": "HSBC Holdings plc",
    "exchange": "LSE",
    "currency": "GBp",
    "symbol": "HSBA.L",
}

#: `Ticker("AAPL").info` — the US side of the M143 collision.
NASDAQ_AAPL: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "Apple Inc.",
    "longName": "Apple Inc.",
    "exchange": "NMS",
    "currency": "USD",
    "symbol": "AAPL",
}

#: `Ticker("AAPL.TO").info` — Apple CDR (CAD hedged), TSX: the other side.
TSX_AAPL_CDR: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "APPLE CDR (CAD HEDGED)",
    "longName": "Apple Inc.",
    "exchange": "TOR",
    "currency": "CAD",
    "symbol": "AAPL.TO",
}

#: `Ticker("RY.TO").info` — Royal Bank, TSX (the `.TO`/suffix path).
TSX_RY: dict[str, object] = {
    "quoteType": "EQUITY",
    "shortName": "ROYAL BANK OF CANADA",
    "longName": "Royal Bank of Canada",
    "exchange": "TOR",
    "currency": "CAD",
    "symbol": "RY.TO",
}

#: `Ticker("005930.KQ").info` — the M162 artifact record, still a MISS.
KQ_ARTIFACT: dict[str, object] = {
    "quoteType": "MUTUALFUND",
    "shortName": "005930.KQ,0P0000B2XZ,18569122",
    "longName": "Samsung Electronics Co Ltd",
    "exchange": "KOE",
    "currency": "KRW",
    "symbol": "005930.KQ",
}

#: Every value the fuzz suite can put in a body's `currency` field, plus the
#: shapes Yahoo itself has been seen to use. None of them may reach a payload.
JUNK_CURRENCIES: list[object] = [
    None,
    "",
    "   ",
    0,
    42,
    -1.5,
    True,
    [],
    ["KRW"],
    {"code": "KRW"},
    b"KRW",
    float("nan"),
    float("inf"),
    "005930.KQ,0P0000B2XZ,18569122",  # the M162 artifact, as a value
    "KR",  # two letters
    "KRWON",  # five letters
    "K1W",  # a digit
]


@pytest.fixture(autouse=True)
def offline_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may reach the network: every transport is a double."""

    def nothing(*_args: object, **_kwargs: object) -> None:
        return None

    for seam in (
        "fetch_issuer_holdings",
        "fetch_vanguard_holdings",
        "fetch_fund_holdings",
        "fetch_latest_close",
        "fetch_history",
        "fetch_history_candidates",
        "ensure_fundamentals",
        "backfill_range",
        "ensure_recent_price",
    ):
        monkeypatch.setattr(market_data, seam, nothing)


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
    """The search fallback answering no rows — the live answer for `.KS`/`.T`.

    Recorded so a test can prove the answer did NOT come from search (a
    search row carries no currency, so a search answer could never be KRW).
    """
    calls: list[str] = []

    def fake(_query: str) -> dict[str, object]:
        calls.append(_query)
        return {}

    monkeypatch.setattr(market_data, "_http_search_json", fake)
    return calls


def _user_id(session: Session, email: str) -> int:
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
    listing_currency: str,
    exchange: str,
    kind: SecurityKind = SecurityKind.stock,
) -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} listing",
        kind=kind,
        listing_currency=listing_currency,
        exchange=exchange,
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


# --- 1. the payload seam: the provider's code is believed ---------------------


@pytest.mark.parametrize(
    ("symbol", "body", "expected_currency", "expected_exchange"),
    [
        ("005930.KS", KS_SAMSUNG, "KRW", "KSC"),  # the card's subject
        ("000660.KS", KS_HYNIX, "KRW", "KSC"),
        ("9984.T", TOKYO_SOFTBANK, "JPY", "JPX"),
        ("RY.TO", TSX_RY, "CAD", "TSX"),  # `TOR` normalizes to the TSX convention
        ("AAPL", NASDAQ_AAPL, "USD", "NMS"),
    ],
    ids=["005930.KS", "000660.KS", "9984.T", "RY.TO", "AAPL"],
)
def test_the_direct_path_stores_the_currency_the_body_stated(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    body: dict[str, object],
    expected_currency: str,
    expected_exchange: str,
) -> None:
    """One probe, one payload: the listing's OWN currency, not a venue guess.

    Before M163 every one of these except the Canadian pair answered USD.
    """
    _patch_ticker(monkeypatch, {symbol: dict(body)})
    calls = _patch_empty_search(monkeypatch)

    resolution = market_data.resolve_symbol(symbol)
    assert resolution["match"] == "direct"
    result = resolution["result"]
    assert result is not None
    assert result["listing_currency"] == expected_currency
    assert result["exchange"] == expected_exchange
    # The single-result contract reads the SAME payload (docs/07), so the
    # CSV-import autofill and the deepening loop get the fix for free.
    assert market_data.lookup_symbol(symbol) == result
    # And the answer came from the QUOTE probe: a body that resolved never
    # reaches the search fallback (which carries no currency to read).
    assert calls == []


def test_the_suffix_path_states_the_currency_of_the_listing_it_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare `RY` 404s, `RY.TO` answers — the payload carries CAD from the
    body (which is also what the heuristic would say: a non-regression on
    the branch that only the `.TO` probe reached)."""
    _patch_ticker(monkeypatch, {"RY.TO": dict(TSX_RY)})
    _patch_empty_search(monkeypatch)

    resolution = market_data.resolve_symbol("RY")
    assert resolution["match"] == "suffix"
    result = resolution["result"]
    assert result is not None
    assert result["symbol"] == "RY"  # the typed spelling is kept (M142)
    assert result["listing_currency"] == "CAD"


def test_the_two_sides_of_a_collision_each_carry_their_own_currency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M143's chooser is create-ready, so each row must name the currency of
    the LISTING it names — live: `AAPL` is USD and `AAPL.TO` CAD."""
    _patch_ticker(monkeypatch, {"AAPL": dict(NASDAQ_AAPL), "AAPL.TO": dict(TSX_AAPL_CDR)})
    _patch_empty_search(monkeypatch)

    resolution = market_data.resolve_symbol("AAPL")
    assert resolution["match"] == "ambiguous"
    by_symbol = {str(c["symbol"]): c for c in resolution["candidates"]}
    assert by_symbol["AAPL"]["listing_currency"] == "USD"
    assert by_symbol["AAPL.TO"]["listing_currency"] == "CAD"


def test_prefer_currency_selects_the_side_the_body_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CSV import's own currency cell now disambiguates against REAL
    currencies (docs/07 §Candidate resolution, `prefer_currency`)."""
    _patch_ticker(monkeypatch, {"AAPL": dict(NASDAQ_AAPL), "AAPL.TO": dict(TSX_AAPL_CDR)})
    _patch_empty_search(monkeypatch)

    resolution = market_data.resolve_symbol("AAPL", prefer_currency="CAD")
    assert resolution["match"] == "suffix"
    result = resolution["result"]
    assert result is not None
    assert result["symbol"] == "AAPL"
    assert result["listing_currency"] == "CAD"


# --- 2. the fallback: no usable code on the body ------------------------------


def test_a_body_with_no_currency_field_falls_back_to_the_venue_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-M163 shape (and every body Yahoo answers without the field)
    keeps the documented heuristic: `.TO`/Canadian venue → CAD, else USD."""
    _patch_ticker(
        monkeypatch,
        {"005930.KS": {"quoteType": "EQUITY", "shortName": "SamsungElec", "exchange": "KSC"}},
    )
    _patch_empty_search(monkeypatch)

    result = market_data.resolve_symbol("005930.KS")["result"]
    assert result is not None
    assert result["listing_currency"] == "USD"  # the old answer, on purpose
    assert market_data._listing_currency("005930.KS", "KSC") == "USD"
    assert market_data._listing_currency("VEQT.TO") == "CAD"
    assert market_data._listing_currency("APTX.NE", "NEO") == "CAD"


@pytest.mark.parametrize("junk", JUNK_CURRENCIES, ids=lambda v: repr(v)[:24])
def test_type_junk_in_the_currency_field_never_reaches_the_payload(
    monkeypatch: pytest.MonkeyPatch, junk: object
) -> None:
    """The body-contract fuzzer mutates `currency` with type-junk; a payload
    must never carry what a body put there raw, and must never raise."""
    body = dict(KS_SAMSUNG)
    body["currency"] = junk
    assert market_data._is_real_quote(body)  # the name is clean: this is a quote
    payload = market_data._payload_from_info("005930.KS", body, "005930")
    candidate = market_data._candidate_from_info("005930.KS", body)
    assert payload["listing_currency"] == "USD"  # the heuristic answered
    assert candidate["listing_currency"] == "USD"


def test_the_code_reader_normalizes_and_refuses_exactly_as_documented() -> None:
    """The ONE reader, asked directly: an all-lowercase spelling is
    normalized up, and MIXED case — the provider's minor-unit form — is
    refused along with everything that is not three letters."""
    assert market_data._normalize_currency_code("KRW") == "KRW"
    assert market_data._normalize_currency_code(" krw ") == "KRW"
    assert market_data._normalize_currency_code("usd") == "USD"
    assert market_data._normalize_currency_code("GBp") == ""  # pence: NOT a base
    assert market_data._normalize_currency_code(None) == ""
    assert market_data._normalize_currency_code(42) == ""
    assert market_data._normalize_currency_code("KRWON") == ""
    assert market_data._normalize_currency_code("KR") == ""


def test_a_pence_listing_is_refused_rather_than_mislabelled_usd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIVE-PROBED: `HSBA.L` answers `currency=GBp` (pence, the LSE minor
    unit). Storing `GBP` would multiply the row's prices by 100 — the M87
    split class of silent wrong number — and M163's fallback stored **USD**
    instead, which is the same defect with the venue changed. M167 refuses
    the whole BODY, so the lookup answers `none`: nothing to adopt, nothing
    to mislabel (docs/07 §A minor-unit listing is refused)."""
    _patch_ticker(monkeypatch, {"HSBA.L": dict(LSE_HSBC)})
    _patch_empty_search(monkeypatch)

    assert market_data.resolve_symbol("HSBA.L") == {
        "match": "none",
        "result": None,
        "candidates": [],
    }
    assert market_data.lookup_symbol("HSBA.L") is None
    assert market_data._normalize_currency_code(LSE_HSBC["currency"]) == ""
    assert market_data.provider_currency_is_minor_unit(LSE_HSBC["currency"])


# --- 3. the paths that carry no provider code at all --------------------------


def test_a_search_row_still_derives_its_currency_from_the_venue() -> None:
    """Search rows carry NO currency (live-measured 2026-10-10), so this is
    the path `_listing_currency` still exists for: a `.TO` row is CAD, a
    KSC-venue row is USD — never a code the row did not state."""
    assert (
        market_data._payload_from_search_row(
            {
                "symbol": "APTX.TO",
                "shortname": "APOTEX HEALTH CORP",
                "exchange": "TOR",
                "quoteType": "EQUITY",
            }
        )["listing_currency"]
        == "CAD"
    )
    assert (
        market_data._candidate_from_search_row(
            {
                "symbol": "005930.KS",
                "shortname": "SamsungElec",
                "exchange": "KSC",
                "quoteType": "EQUITY",
            }
        )["listing_currency"]
        == "USD"
    )


def test_the_m162_artifact_body_is_still_a_miss_after_the_currency_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-regression: believing `currency` must not resurrect the body M162
    refuses — the artifact triple is refused on its NAME, before any other
    field of it could be read."""
    _patch_ticker(monkeypatch, {"005930.KQ": dict(KQ_ARTIFACT), "005930.KQ.TO": {}})
    _patch_empty_search(monkeypatch)

    assert market_data.resolve_symbol("005930.KQ") == {
        "match": "none",
        "result": None,
        "candidates": [],
    }
    assert market_data.lookup_symbol("005930.KQ") is None


# --- 4. the FX seams serve whatever the provider said (docs/06, M116) ---------


def test_krw_is_a_rate_the_fx_book_can_actually_serve() -> None:
    """The ruling on "fetch-on-demand vs refuse-at-create": SERVE.

    KRW is in `CURATED_FX_BASES` because Bank of Canada publishes a live
    `FXKRWCAD` series — live-probed 2026-10-11 from this host:
    ``/valet/observations/FXKRWCAD/json`` answers 200 with
    ``2026-10-01 → 0.001046``, while the DKK control 404s (docs/07
    §Curated board membership). So the 6-hourly worker keeps the pair warm,
    `latest_rate` fetches it on a true miss, and `FxRateBook` backfills a
    trade-date span the same way. A currency nobody can serve is a DKK, and
    that state already answers 422 `fx_rate_missing` rather than guessing.
    """
    assert "KRW" in fx.CURATED_FX_BASES


def test_a_krw_holding_rebuilds_once_the_rate_is_cached(session: Session) -> None:
    """The whole seam end to end: a KRW security, a KRW-priced buy in a
    taxable account, and the ACB sweep. With the rate cached the pool is
    built in CAD; with the rate absent the SAME rebuild refuses loudly
    (`fx_rate_missing`) instead of stamping a wrong number."""
    user_id = _user_id(session, "m163-krw@example.com")
    security = _security(session, user_id, "005930.KS", listing_currency="KRW", exchange="KSC")
    account = Account(
        user_id=user_id,
        name="KRX",
        type=AccountType.brokerage_non_registered,
        currency="CAD",
        # Taxability is DERIVED from the type server-side (docs/02), so a
        # direct model insert has to say it itself — otherwise this is a
        # registered account and buys deliberately build no ACB pool.
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    assert account.id is not None
    day = date(2026, 9, 15)
    session.add(
        Trade(
            user_id=user_id,
            account_id=account.id,
            security_id=security.id,
            date=day,
            action=TradeAction.buy,
            quantity=Decimal("10"),
            price_native=Decimal("71000"),
            currency="KRW",
        )
    )
    session.commit()

    # No rate cached → the documented refusal, never a guess.
    with pytest.raises(APIError) as refused:
        acb.rebuild_user_pools(session, user_id, allow_fetch=False)
    assert refused.value.code == "fx_rate_missing"

    session.add(
        FxRate(
            base="KRW",
            quote="CAD",
            rate=Decimal("0.00104600"),
            date=day,
            source=FxSource.boc_valet,
        )
    )
    session.commit()

    rebuilt = acb.rebuild_user_pools(session, user_id, allow_fetch=False)
    assert rebuilt == {"pools": 1, "trades": 1}  # the KRW holding has a CAD ACB pool
    pool = session.exec(select(AcbPool).where(AcbPool.user_id == user_id)).one()
    # 10 shares x ₩71,000 x 0.001046 KRW/CAD = C$742.66 — the conversion the
    # USD label would have got wrong by ~1,350x.
    assert pool.quantity == Decimal("10")
    assert pool.total_acb_cad == pytest.approx(Decimal("742.66"), abs=Decimal("0.01"))
    rows = session.exec(select(Security).where(Security.user_id == user_id)).all()
    assert [row.listing_currency for row in rows] == ["KRW"]

    # The rate-book STATUS reads the same population, so the chip reports the
    # pair the moment such a row exists (docs/07 §Rate-book status).
    assert "KRW" in fx_status.currencies_in_use(session, user_id, "CAD")


def test_the_repair_report_now_sees_a_krw_listing_stored_as_usd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator's own detector: before M163 the live probe answered USD
    for `005930.KS`, so a row stamped USD looked IDENTICAL to the market and
    the mismatch was invisible. Now the report names it — which is also the
    repair path for the two prod rows this card found."""
    row = OwnedRow(
        id=1381,
        user_id=1,
        symbol="005930.KS",
        name="SamsungElec",
        kind="stock",
        listing_currency="USD",  # what prod actually stores today
        exchange="KSC",
        dead_symbol=False,
        quote_miss_count=0,
        dead_symbol_reason=None,
        privacy_mode=False,
        trades=0,
        watched=False,
        evidence=(TradeEvidence(date=date(2026, 9, 15), price=Decimal("71000"), currency="KRW"),),
    )
    _patch_ticker(monkeypatch, {"005930.KS": dict(KS_SAMSUNG)})
    _patch_empty_search(monkeypatch)

    resolution = market_data.resolve_symbol("005930.KS")
    assert resolution["match"] == "direct"

    findings = live_findings(row, resolution)
    mismatch = [f for f in findings if f.check is Check.currency_mismatch]
    assert len(mismatch) == 1
    # The row's one recorded trade is KRW — the SAME currency the listing
    # quotes — so the mismatch is CORROBORATED and the report prescribes the
    # repair (docs/07 §Repair-needed report: the corroboration is what makes
    # it actionable). Prod's two KSC rows reference zero trades, so theirs
    # would be `review` instead of `repair`.
    assert mismatch[0].severity is Severity.repair
    assert "USD" in mismatch[0].detail and "KRW" in mismatch[0].detail
    assert mismatch[0].suggestion and "--apply" in mismatch[0].suggestion
    assert "--security-id 1381" in mismatch[0].suggestion

"""Malformed market-data body contract: a junk or empty body is a MISS, never a quote.

M142's junk-``.info`` fix (``5fd7433``) was caught by a LIVE probe against the
deployed stack, not by a test: yfinance answers an unlisted symbol with a
NON-EMPTY body (``{"trailingPegRatio": None}``), so "the payload is non-empty"
is not evidence of a quote. That defect class recurs wherever a provider body
becomes app data, and every instance so far was found in production rather than
by the suite — the venue stub (M143, security 296), the machine-joined ID
triple (M162, six junk rows), the split-adjusted cache (M149, a real position
valued an eighth of its worth).

So the invariant is pinned ONCE, here, for **every** Yahoo parser path in
``app/services/market_data.py``:

    a junk / empty / unreadable body is always a MISS, never a quote.

The seams, in the order the module reads a provider body:

1. ``Ticker.info``         — ``_is_real_quote`` (the gate) and the payload /
                             candidate builders that read a body it accepted
2. the resolver            — ``resolve_symbol`` / ``lookup_symbol``
3. the search endpoint     — ``_http_search_json`` / ``_search_equity_quotes``
                             and the row builders
4. ``Ticker.history``      — ``_fetch_one_history`` / ``fetch_history`` /
                             ``fetch_history_candidates`` / ``probe_quote``
5. ``Ticker.funds_data``   — ``fetch_fund_holdings``
6. the fundamentals body   — ``fetch_fundamentals`` / ``_dec``
7. verdict folding         — ``_classify_history_error`` / ``_combine_outcomes``

Two kinds of evidence back the corpus: bodies YAHOO HAS REALLY ANSWERED with
(live probes, named where they come from) and a **seeded fuzzer** that mutates
a real body with type-junk, so the suite searches the space instead of only
checking the shapes somebody already thought of. Seeds are fixed — the run is
deterministic.

"Miss" means the documented degradation, not merely "no quote": never a
payload, never a chooser row, never a stored number, never an ``ok`` verdict
(``({}, ok)`` would clear M141's dead-symbol counter on evidence of nothing),
and never an exception escaping to the caller — the module's own contract is
"ANY failure degrades to 'no data' rather than raising".

No test here opens a socket (docs/07 network discipline): the autouse fixture
forbids it and fails loudly at teardown when a body was needed but never stubbed.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import httpx
import pandas as pd
import pytest
import yfinance

import app.services.market_data as market_data
from app.models.securities import QuoteOutcome, SecurityKind

#: Captured BEFORE any fixture can replace them, so a test can put the real
#: seam back after the module-wide "no search hit" default.
REAL_SEARCH_QUOTES = market_data._search_equity_quotes
REAL_HTTP_SEARCH_JSON = market_data._http_search_json

START = date(2026, 10, 1)
END = date(2026, 10, 10)

#: The only two fields a ``Ticker.info`` body may name a listing with.
NAME_KEYS = ("shortName", "longName")

# --- the corpus ---------------------------------------------------------------

#: Bodies Yahoo has REALLY answered an unlisted/odd symbol with (live probes).
LIVE_JUNK_INFO: dict[str, dict[str, object]] = {
    # `Ticker("APOTEX").info`, api container, 2026-10-10 (the M142 defect):
    # Apotex went private in 2022 and this is still a NON-EMPTY body.
    "peg-only": {"trailingPegRatio": None},
    # `Ticker("VFV").info` bare, 2026-10-11 (the M143 defect): a venue stub
    # that names no security — it passed the old four-key test and became
    # "VFV, USD", security 296.
    "venue-stub": {
        "quoteType": "ECNQUOTE",
        "exchange": "NMS",
        "fullExchangeName": "NasdaqGS",
    },
    # `Ticker("005930.KQ").info`, 2026-10-11 (the M162 defect): the NAME is a
    # machine-joined ID triple; the clean longName belongs to the equity this
    # fund-facts record mirrors, and adopting it re-creates the twin.
    "artifact-name": {
        "quoteType": "MUTUALFUND",
        "shortName": "005930.KQ,0P0000B2XZ,18569122",
        "longName": "Samsung Electronics Co Ltd",
        "exchange": "KOE",
    },
    "empty": {},
    "blank-names": {"shortName": "", "longName": "   ", "quoteType": "EQUITY"},
    "non-listing-family": {"quoteType": "NEWS", "title": "a headline about AAPL"},
}

#: Bodies that are not a ``dict`` at all — an unofficial API may answer
#: anything, and the module contract is "degrade, never raise".
NON_DICT_INFO: list[object] = [
    None,
    "",
    "junk body",
    7,
    1.5,
    [{"shortName": "AAPL"}],
    ("shortName", "AAPL"),
    True,
]

#: A value that is NOT a name. Every one of these in ``shortName``/``longName``
#: must read as "this body names no listing" — or, where a second valid name
#: field exists, must never be the name the payload carries.
NON_NAME_VALUES: list[object] = [
    None,
    "",
    "   ",
    "\t",
    0,
    42,
    -1.5,
    True,
    [],
    ["AAPL"],
    {},
    {"shortName": "Nested"},
    ("AAPL",),
    b"AAPL",
    float("nan"),
    float("inf"),
    "005930.KQ,0P0000B2XZ,18569122",  # the artifact shape, as a value
]

#: A value that is NOT a number. `_dec` must read all of them as "no value":
#: ``Decimal('NaN')`` and ``Decimal('Infinity')`` PARSE without raising and
#: Postgres stores them in a ``Numeric`` column, and ``NaN <= 0`` RAISES
#: ``InvalidOperation`` in the issuer-CSV weight check.
NON_NUMERIC_VALUES: list[object] = [
    None,
    "",
    "   ",
    float("nan"),
    float("inf"),
    -float("inf"),
    "NaN",
    "Infinity",
    "-Infinity",
    "not a number",
    Decimal("NaN"),
    Decimal("Infinity"),
    [],
    {},
    {"value": 1},
]

#: Search-answer bodies that must never yield a tradable row.
JUNK_SEARCH_PAYLOADS: dict[str, object] = {
    "no-quotes-key": {"error": "Too Many Requests"},
    "empty-answer": {"quotes": []},
    "quotes-null": {"quotes": None},
    "quotes-not-a-list": {"quotes": {"symbol": "AAPL", "quoteType": "EQUITY"}},
    "quotes-nested-dicts": {"quotes": [{"quotes": [{"symbol": "AAPL"}]}]},
    "quotes-blank-symbol": {"quotes": [{"symbol": "   ", "quoteType": "EQUITY"}]},
    "quotes-non-string-symbol": {
        "quotes": [
            {"symbol": 12345, "quoteType": "EQUITY", "shortname": "Numeric Row"},
            {"symbol": {"ticker": "AAPL"}, "quoteType": "EQUITY"},
        ]
    },
    "quotes-non-listing-family": {
        "quotes": [
            {"symbol": "^GSPTSE", "quoteType": "INDEX", "shortname": "S&P/TSX"},
            {"symbol": "USD-CAD", "quoteType": "CURRENCY", "shortname": "USD/CAD"},
            {"symbol": "APTX.TO", "quoteType": "NEWS", "shortname": "Apotex in the news"},
        ]
    },
    "quotes-non-string-family": {
        "quotes": [{"symbol": "AAPL", "quoteType": 7, "shortname": "typed wrong"}]
    },
    "quotes-artifact-name": {
        "quotes": [
            {
                "symbol": "005930.KQ",
                "quoteType": "MUTUALFUND",
                "shortname": "005930.KQ,0P0000B2XZ,18569122",
            }
        ]
    },
}

#: Bodies Yahoo may hand back from `history` that carry no usable close.
JUNK_HISTORY_FRAMES: dict[str, object] = {
    "empty-frame": pd.DataFrame(),
    "all-nan-closes": pd.DataFrame(
        {"Close": [float("nan"), float("nan")], "Stock Splits": [0.0, 0.0]},
        index=pd.to_datetime(["2026-10-09", "2026-10-10"]),
    ),
    "no-close-column": pd.DataFrame(
        {"Open": [1.0, 2.0]}, index=pd.to_datetime(["2026-10-09", "2026-10-10"])
    ),
    "non-numeric-closes": pd.DataFrame(
        {"Close": ["n/a", "n/a"], "Stock Splits": [0.0, 0.0]},
        index=pd.to_datetime(["2026-10-09", "2026-10-10"]),
    ),
    "non-date-index": pd.DataFrame(
        {"Close": [1.0, 2.0], "Stock Splits": [0.0, 0.0]}, index=["2026-10-09", "2026-10-10"]
    ),
    "not-a-frame-dict": {"Close": [1.0, 2.0]},
    "not-a-frame-object": object(),
    "not-a-frame-string": "junk",
    "not-a-frame-none": None,
}

#: The REAL listing bodies — the non-regression half. A contract that only
#: refuses things is worthless; these must keep resolving exactly as before.
REAL_INFO: dict[str, dict[str, object]] = {
    "ks-listing": {
        "quoteType": "EQUITY",
        "shortName": "SamsungElec",
        "longName": "Samsung Electronics Co., Ltd.",
        "exchange": "KSC",
        "fullExchangeName": "KOSDAQ",
        "symbol": "005930.KS",
        # M163: the live probe's `currency` field — it was missing here, and
        # that is how the payload builders' blind spot stayed invisible.
        "currency": "KRW",
    },
    "nasdaq-listing": {
        "quoteType": "EQUITY",
        "shortName": "Apple Inc.",
        "longName": "Apple Inc.",
        "exchange": "NMS",
        "fullExchangeName": "NasdaqGS",
        "symbol": "AAPL",
        "currency": "USD",
    },
    "long-name-only": {"quoteType": "MUTUALFUND", "longName": "Vanguard Total International"},
}


# --- doubles ------------------------------------------------------------------


class _NoDataTicker:
    """The offline default: no body, no rows. Never a network call."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    @property
    def info(self) -> object:
        return {}

    def history(self, **_kwargs):
        return pd.DataFrame()


class _BodyTicker:
    """`Ticker.info` / `.history` answering a fixed body for ANY symbol.

    An ``Exception`` in either slot is RAISED rather than returned, so one
    class covers "the body is junk" and "the call failed".
    """

    def __init__(self, body: object = None, frame: object = None) -> None:
        self._body = body
        self._frame = frame

    @property
    def info(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def history(self, **_kwargs):
        if isinstance(self._frame, Exception):
            raise self._frame
        return self._frame


class _FundsTicker:
    """`funds_data.top_holdings` answering one frame (or raising)."""

    def __init__(self, body: object) -> None:
        self._body = body

    @property
    def funds_data(self):
        if isinstance(self._body, Exception):
            raise self._body
        if hasattr(self._body, "top_holdings"):
            return self._body
        return type("FundsData", (), {"top_holdings": self._body})()


class _HttpAnswer:
    """A stand-in `httpx.Response` carrying one body (or one exception)."""

    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if isinstance(self._payload, Exception):
            raise self._payload
        if self.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    """No socket, no session, no sleep — and a LOUD failure if a test forgets.

    `httpx.get` is replaced by a recorder that raises AND is counted, because
    `_http_search_json` swallows everything and would otherwise turn a missing
    stub into a silent, passing ``[]``; `yfinance.Ticker` by a no-data double;
    the deadline session and the backoff sleep are never built/run. A test
    that MEANS to answer a body replaces `httpx.get` with its own fake, so its
    answer never lands in this list either.
    """
    attempts: list[object] = []

    def _forbidden(*args: object, **kwargs: object) -> object:
        attempts.append(args[0] if args else kwargs.get("url"))
        raise AssertionError("network attempted by a body-contract test")

    monkeypatch.setattr(httpx, "get", _forbidden)
    monkeypatch.setattr(yfinance, "Ticker", _NoDataTicker)
    monkeypatch.setattr(market_data, "_yf_session", lambda: None)
    monkeypatch.setattr(market_data, "_search_equity_quotes", lambda _query: [])
    monkeypatch.setattr(market_data.time, "sleep", lambda _seconds: None)
    yield attempts
    assert not attempts, f"an unstubbed HTTP call reached the network: {attempts}"


def _answer_search(monkeypatch: pytest.MonkeyPatch, payload: object, status: int = 200) -> None:
    """Answer the REAL search parser from a fake response body."""
    monkeypatch.setattr(market_data, "_search_equity_quotes", REAL_SEARCH_QUOTES)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _HttpAnswer(payload, status))


def _expected_name(body: dict[str, object]) -> str:
    """The contract's own reading of a name field, re-implemented here so the
    test never calls the helper it is checking."""
    for key in NAME_KEYS:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _assert_is_a_real_listing(payload: dict[str, object]) -> None:
    """Postcondition of EVERY body the module accepts: a create-ready listing.

    If a junk value can get past this, a junk value can be stored as a
    security's name or symbol, or as a number in a ``Numeric`` column.
    """
    symbol = payload["symbol"]
    assert isinstance(symbol, str), f"symbol is {type(symbol).__name__}: {symbol!r}"
    assert symbol and symbol.strip() == symbol, f"symbol is not a clean string: {symbol!r}"

    name = payload["name"]
    assert isinstance(name, str), f"name is {type(name).__name__}: {name!r}"
    assert name and name.strip() == name, f"name is not a clean non-blank string: {name!r}"
    assert not market_data.provider_name_is_artifact(name), f"artifact name adopted: {name!r}"

    assert payload["kind"] in set(SecurityKind), f"unknown kind: {payload['kind']!r}"
    # M163: any well-formed ISO-4217 code may arrive from the provider's own
    # `currency` field (KRW, JPY, GBp-refused→USD), so this is a SHAPE check:
    # three uppercase ASCII letters. A junk value read raw out of a mutated
    # body would be a string of any length at all — that is the case the
    # fuzzer above hunts.
    currency = payload["listing_currency"]
    assert isinstance(currency, str), f"listing_currency is {type(currency).__name__}: {currency!r}"
    assert (
        len(currency) == 3 and currency.isascii() and currency.isalpha() and currency.isupper()
    ), f"listing_currency is not a well-formed currency code: {currency!r}"
    assert isinstance(payload["exchange"], str), payload["exchange"]
    display = payload["exchange_display"]
    assert display is None or isinstance(display, str), display


# ==============================================================================
# 1. the `.info` gate
# ==============================================================================


@pytest.mark.parametrize("label", sorted(LIVE_JUNK_INFO))
def test_a_live_junk_info_body_is_a_miss(label: str) -> None:
    """The bodies Yahoo has really answered with — every one a MISS (M141/143/162)."""
    assert market_data._is_real_quote(dict(LIVE_JUNK_INFO[label])) is False


@pytest.mark.parametrize("key", NAME_KEYS)
@pytest.mark.parametrize(
    "value",
    NON_NAME_VALUES,
    ids=[f"{v!r}"[:24] for v in NON_NAME_VALUES],
)
def test_a_name_field_that_is_not_a_clean_string_is_a_miss(key: str, value: object) -> None:
    """`shortName`/`longName` carrying a type-junk value names NO listing.

    The builders read `shortName` first, so a body may be accepted only on a
    name a builder would actually adopt — otherwise the gate says "real" and
    the payload stores ``str(value)`` (``"{'ticker': 'AAPL'}"``, ``"42"``,
    ``"nan"``) as a security's name.
    """
    body: dict[str, object] = {key: value, "quoteType": "EQUITY"}
    assert market_data._is_real_quote(body) is False, body


@pytest.mark.parametrize("body", NON_DICT_INFO, ids=[f"{b!r}"[:20] for b in NON_DICT_INFO])
def test_a_body_that_is_not_a_dict_is_a_miss_and_never_raises(body: object) -> None:
    """An unofficial API may answer anything; a non-dict body is a MISS.

    It used to raise ``AttributeError`` straight out of `resolve_symbol` —
    neither a quote nor a miss, but a 500 on the lookup endpoint.
    """
    assert market_data._is_real_quote(body) is False


@pytest.mark.parametrize("label", sorted(REAL_INFO))
def test_a_real_listing_body_still_passes_the_gate(label: str) -> None:
    """The other half of the contract: a real body must NOT be refused."""
    body = dict(REAL_INFO[label])
    assert market_data._is_real_quote(body) is True
    assert _expected_name(body)


def test_a_whitespace_short_name_falls_through_to_the_real_long_name() -> None:
    """Pinned by M162 — and now the PAYLOAD carries that long name too.

    The gate used to read the fields with a per-key strip while the builders
    read them raw, so this body was *accepted* on its longName and then
    *stored* with ``name = "   "``.
    """
    body: dict[str, object] = {"shortName": "   ", "longName": "Real Name", "quoteType": "EQUITY"}
    assert market_data._is_real_quote(body) is True
    payload = market_data._payload_from_info("APTX", body, "APTX")
    assert payload["name"] == "Real Name"
    _assert_is_a_real_listing(payload)


# ==============================================================================
# 2. gate ↔ builder agreement
# ==============================================================================


@pytest.mark.parametrize("label", sorted(REAL_INFO))
def test_the_payload_carries_exactly_the_name_the_gate_accepted(label: str) -> None:
    """One body, one name reader: the gate and both builders must agree.

    The disagreement is the M143 defect in miniature — the gate keys on the
    longName while the builder adopts whatever `shortName` held.
    """
    body = dict(REAL_INFO[label])
    expected = _expected_name(body)
    assert market_data._is_real_quote(body) is True

    typed = "005930"  # the typed spelling is the payload's symbol, never the name
    from_info = market_data._payload_from_info("005930.KS", body, typed)
    candidate = market_data._candidate_from_info("005930.KS", body)

    assert from_info["name"] == expected
    assert candidate["name"] == expected
    assert from_info["symbol"] == typed  # M142: the typed spelling is kept
    assert candidate["symbol"] == "005930.KS"  # a chooser row is the probe's own
    _assert_is_a_real_listing(from_info)
    _assert_is_a_real_listing(candidate)


def test_a_body_accepted_on_one_field_never_pays_for_the_other() -> None:
    """A non-string `shortName` next to a real `longName`: the gate keys on
    the longName, so the payload must carry the longName — not ``"42"``."""
    body: dict[str, object] = {"shortName": 42, "longName": "Real Name", "quoteType": "ETF"}
    assert market_data._is_real_quote(body) is True
    assert market_data._payload_from_info("VGRO", body, "VGRO")["name"] == "Real Name"
    assert market_data._candidate_from_info("VGRO", body)["name"] == "Real Name"


# ==============================================================================
# 3. the resolver
# ==============================================================================


@pytest.mark.parametrize("label", sorted(LIVE_JUNK_INFO))
def test_the_resolver_turns_a_live_junk_body_into_none(
    monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """Both candidates answer junk, search answers nothing -> `none`."""
    body = dict(LIVE_JUNK_INFO[label])
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(body=body))

    assert market_data.resolve_symbol("APOTEX") == {
        "match": "none",
        "result": None,
        "candidates": [],
    }
    assert market_data.lookup_symbol("APOTEX") is None


@pytest.mark.parametrize("body", NON_DICT_INFO, ids=[f"{b!r}"[:20] for b in NON_DICT_INFO])
def test_a_non_dict_body_is_a_miss_in_the_resolver_too(
    monkeypatch: pytest.MonkeyPatch, body: object
) -> None:
    """The whole lookup must degrade, never raise (the module's own contract)."""
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(body=body))

    assert market_data.resolve_symbol("APOTEX") == {
        "match": "none",
        "result": None,
        "candidates": [],
    }
    assert market_data.lookup_symbol("APOTEX") is None


@pytest.mark.parametrize(
    ("body_field", "seam"),
    [("body", "resolve"), ("body", "fundamentals"), ("frame", "history"), ("frame", "probe")],
)
def test_a_provider_call_that_raises_is_a_miss(
    monkeypatch: pytest.MonkeyPatch, body_field: str, seam: str
) -> None:
    """A raising `.info`/`.history` (rate limit, parse error) degrades:
    `none` / `None` / `({}, not_found)` / `unavailable` — never a traceback."""
    failure = RuntimeError("yfinance is down")
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(**{body_field: failure}))

    if seam == "resolve":
        assert market_data.resolve_symbol("APOTEX")["match"] == "none"
    elif seam == "fundamentals":
        assert market_data.fetch_fundamentals("APOTEX") is None
    elif seam == "history":
        assert market_data.fetch_history("APOTEX", START, END) == {}
        assert market_data.last_quote_verdict()[0] is QuoteOutcome.unavailable
    else:
        outcome, _reason = market_data.probe_quote("APOTEX")
        assert outcome is QuoteOutcome.unavailable


def test_an_accepted_body_resolves_to_a_create_ready_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-regression: a real body still resolves `direct` with every field."""
    body = dict(REAL_INFO["nasdaq-listing"])
    # Only the BARE symbol answers: a body that answered BOTH candidates is a
    # collision (M143), which is not this test's subject.
    monkeypatch.setattr(
        yfinance, "Ticker", lambda symbol: _BodyTicker(body=body if symbol == "AAPL" else {})
    )

    resolution = market_data.resolve_symbol("AAPL")
    assert resolution["match"] == "direct"
    assert resolution["result"] is not None
    assert resolution["candidates"] == []
    _assert_is_a_real_listing(resolution["result"])
    assert resolution["result"]["name"] == "Apple Inc."
    assert resolution["result"]["listing_currency"] == "USD"
    assert market_data.lookup_symbol("AAPL") == resolution["result"]


# ==============================================================================
# 3b. fuzz: mutations of a real body
# ==============================================================================

_FUZZ_BODY = dict(REAL_INFO["ks-listing"])
_FUZZ_SEED = 20261011  # the day the last live probe was taken
_FUZZ_MUTATIONS = 400


def _mutate(body: dict[str, object], rng: random.Random) -> object:
    """One deterministic mutation: a real Yahoo body, one junk edit."""
    choice = rng.randrange(5)
    if choice == 4:  # the body itself is not a dict
        return rng.choice(NON_DICT_INFO)
    mutated = dict(body)
    if choice == 0:  # a field takes type-junk
        mutated[rng.choice(sorted(mutated))] = rng.choice(NON_NAME_VALUES)
    elif choice == 1:  # an unrelated field takes type-junk
        mutated[rng.choice(["exchange", "quoteType", "currency", "marketCap"])] = rng.choice(
            NON_NAME_VALUES
        )
    elif choice == 2:  # a junk key appears
        mutated[rng.choice(["trailingPegRatio", "fiftyTwoWeekHigh", "junk"])] = rng.choice(
            NON_NUMERIC_VALUES
        )
    else:  # a key is dropped
        if mutated:
            mutated.pop(rng.choice(sorted(mutated)))
    return mutated


def test_fuzz_a_real_body_never_produces_a_junk_payload() -> None:
    """Seeded mutations of a real body: accepted -> well-formed, or refused.

    400 mutations over a fixed seed, so a red names the mutation that broke
    the contract and the run is reproducible.
    """
    rng = random.Random(_FUZZ_SEED)  # noqa: S311 -- seeded fuzz data
    accepted = 0
    for _ in range(_FUZZ_MUTATIONS):
        mutated = _mutate(_FUZZ_BODY, rng)
        try:
            is_quote = market_data._is_real_quote(mutated)
        except Exception as exc:  # a raise IS the bug here
            pytest.fail(f"the gate raised on {mutated!r}: {exc!r}")
        if not is_quote:
            continue
        accepted += 1
        payload = market_data._payload_from_info("005930.KS", mutated, "005930")
        candidate = market_data._candidate_from_info("005930.KS", mutated)
        _assert_is_a_real_listing(payload)
        _assert_is_a_real_listing(candidate)
        assert payload["name"] == _expected_name(mutated)
        assert candidate["name"] == _expected_name(mutated)
    # The corpus must actually exercise the accepting branch, or the loop
    # above proves nothing.
    assert accepted > 100, f"only {accepted}/{_FUZZ_MUTATIONS} mutations were accepted"


def test_fuzz_the_resolver_never_raises_and_never_answers_junk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same mutations, run through the WHOLE resolver (search = no hit).

    Both caller shapes are exercised: the default (a collision is a choice)
    and `disambiguate=False` (a background fetch that takes the first hit) —
    the second is the one that hands a payload straight back.
    """
    rng = random.Random(_FUZZ_SEED)  # noqa: S311 -- seeded fuzz data
    for _ in range(_FUZZ_MUTATIONS):
        mutated = _mutate(_FUZZ_BODY, rng)
        monkeypatch.setattr(yfinance, "Ticker", lambda symbol, _b=mutated: _BodyTicker(body=_b))
        try:
            as_choice = market_data.resolve_symbol("005930")
            as_fetch = market_data.resolve_symbol("005930", disambiguate=False)
        except Exception as exc:
            pytest.fail(f"resolve_symbol raised on {mutated!r}: {exc!r}")
        for resolution in (as_choice, as_fetch):
            result = resolution["result"]
            if result is not None:
                _assert_is_a_real_listing(result)
                assert result["name"] == _expected_name(mutated)
            for candidate in resolution["candidates"]:
                _assert_is_a_real_listing(candidate)


# ==============================================================================
# 4. the search endpoint's body
# ==============================================================================


@pytest.mark.parametrize("label", sorted(JUNK_SEARCH_PAYLOADS))
def test_a_junk_search_body_never_yields_a_row(monkeypatch: pytest.MonkeyPatch, label: str) -> None:
    """The answer body, read by the REAL filter: no tradable row, no payload."""
    _answer_search(monkeypatch, JUNK_SEARCH_PAYLOADS[label])

    assert REAL_SEARCH_QUOTES("ap otox") == []
    assert market_data.resolve_symbol("APOTEX")["match"] == "none"


@pytest.mark.parametrize(
    "row",
    [
        {"symbol": 12345, "quoteType": "EQUITY", "shortname": "Numeric Row"},
        {"symbol": {"ticker": "AAPL"}, "quoteType": "EQUITY"},
        {"symbol": None, "quoteType": "EQUITY"},
        {"symbol": "   ", "quoteType": "EQUITY"},
        {"symbol": "", "quoteType": "EQUITY"},
        {"quoteType": "EQUITY", "shortname": "no symbol at all"},
        {"symbol": "AAPL", "quoteType": 7},
        {"symbol": "AAPL", "quoteType": {"name": "EQUITY"}},
        {"symbol": "AAPL"},
    ],
    ids=lambda row: f"{row.get('symbol', '<missing>')!r}/{row.get('quoteType', '<missing>')!r}"[
        :40
    ],
)
def test_a_row_without_a_string_symbol_and_family_is_dropped(
    monkeypatch: pytest.MonkeyPatch, row: dict[str, object]
) -> None:
    """A row is a LISTING because it names a ticker in a tradable family.

    Anything else — a numeric symbol, a symbol that is an object, a family
    that is not EQUITY/ETF/MUTUALFUND — is a malformed row, and a malformed
    row never becomes a security.
    """
    _answer_search(monkeypatch, {"quotes": [row]})

    assert REAL_SEARCH_QUOTES("q") == []
    assert market_data.resolve_symbol("q")["match"] == "none"


def test_a_row_with_a_malformed_name_field_falls_back_to_its_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one junk FIELD that must not kill a good row — but must not be
    adopted either: `{"symbol": "AAPL", "shortname": {...}}` is a listing
    named AAPL, not a listing named ``"{'ticker': 'AAPL'}"``."""
    _answer_search(
        monkeypatch,
        {"quotes": [{"symbol": "AAPL", "quoteType": "EQUITY", "shortname": {"ticker": "AAPL"}}]},
    )

    rows = REAL_SEARCH_QUOTES("apple")
    assert len(rows) == 1
    payload = market_data._payload_from_search_row(rows[0])
    assert payload["name"] == "AAPL"
    _assert_is_a_real_listing(payload)
    candidate = market_data._candidate_from_search_row(rows[0])
    assert candidate["name"] == "AAPL"
    _assert_is_a_real_listing(candidate)


def test_the_search_row_payload_carries_the_row_s_own_symbol_and_venue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-regression: the live M142 row still adopts exactly as before."""
    _answer_search(
        monkeypatch,
        {
            "quotes": [
                {
                    "symbol": "APTX.TO",
                    "shortname": "APOTEX HEALTH CORP",
                    "exchange": "TOR",
                    "exchDisp": "Toronto",
                    "quoteType": "EQUITY",
                }
            ]
        },
    )

    rows = REAL_SEARCH_QUOTES("apotex")
    assert market_data.resolve_symbol("APOTEX")["match"] == "search"
    payload = market_data._payload_from_search_row(rows[0])
    assert payload == {
        "symbol": "APTX.TO",
        "name": "APOTEX HEALTH CORP",
        "kind": SecurityKind.stock,
        "listing_currency": "CAD",
        "exchange": "TSX",
        "exchange_display": "Toronto",
    }
    _assert_is_a_real_listing(payload)


@pytest.mark.parametrize(
    "payload",
    [[], ["quotes"], "junk body", 7, None, True],
    ids=lambda payload: f"{type(payload).__name__}:{str(payload)[:24]}",
)
def test_a_search_body_that_is_not_a_dict_is_no_answer(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """`_http_search_json` answers None for anything that is not a dict, so
    the filter has nothing to read and the lookup degrades to `none`."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _HttpAnswer(payload))
    assert REAL_HTTP_SEARCH_JSON("q") is None
    monkeypatch.setattr(market_data, "_search_equity_quotes", REAL_SEARCH_QUOTES)
    assert REAL_SEARCH_QUOTES("q") == []
    assert market_data.resolve_symbol("q")["match"] == "none"


@pytest.mark.parametrize(
    "payload",
    [ValueError("Expecting value: line 1 column 1 (char 0)"), "not json at all"],
    ids=["bad-json", "html-error-page"],
)
def test_a_body_that_cannot_be_parsed_is_no_answer(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """A body that is not JSON (an HTML error page, a truncated answer) is
    "no answer", never a quote."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _HttpAnswer(payload))
    assert REAL_HTTP_SEARCH_JSON("q") is None


@pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
def test_an_error_status_is_no_answer_after_the_documented_retries(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A non-200 answer is "Yahoo did not tell us", never a quote — and the
    429 retry path ends the same way (its sleeps are stubbed here)."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _HttpAnswer({}, status))
    assert REAL_HTTP_SEARCH_JSON("q") is None


def test_a_junk_search_body_never_reaches_the_price_heal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fetch_history`'s M142 heal adopts ONE search row's symbol — with a
    junk answer there is no row, so no third probe ever happens."""
    probes: list[str] = []

    class _Ticker:
        def __init__(self, symbol: str) -> None:
            probes.append(symbol)

        @property
        def info(self) -> dict[str, object]:
            return {}

        def history(self, **_kwargs):
            return pd.DataFrame()

    monkeypatch.setattr(yfinance, "Ticker", _Ticker)
    _answer_search(monkeypatch, JUNK_SEARCH_PAYLOADS["quotes-non-string-symbol"])

    assert market_data.fetch_history("APOTEX", START, END) == {}
    assert probes == ["APOTEX", "APOTEX.TO"]  # the heal never fired
    outcome, _reason = market_data.last_quote_verdict()
    assert outcome is QuoteOutcome.not_found


# ==============================================================================
# 5. the `history` frame
# ==============================================================================


@pytest.mark.parametrize("label", sorted(JUNK_HISTORY_FRAMES))
def test_a_history_body_with_no_usable_close_is_a_miss(
    monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """Empty, all-NaN, wrong columns, wrong index, not-a-frame: all MISSES.

    And never an exception — `_fetch_one_history` parses OUTSIDE the try that
    wraps the fetch, so a junk body used to escape as ``InvalidOperation`` /
    ``AttributeError`` straight up through `fetch_history`.
    """
    frame = JUNK_HISTORY_FRAMES[label]
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(frame=frame))

    closes, outcome = market_data._fetch_one_history("AAPL", START, END)
    assert closes == {}
    assert outcome is QuoteOutcome.not_found, f"{label} read as {outcome}"


@pytest.mark.parametrize("label", sorted(JUNK_HISTORY_FRAMES))
def test_a_junk_history_body_never_raises_out_of_the_public_seams(
    monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """The public seams: `{} + not_found`, whatever Yahoo answered."""
    frame = JUNK_HISTORY_FRAMES[label]
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(frame=frame))

    assert market_data.fetch_history("AAPL", START, END) == {}
    outcome, reason = market_data.last_quote_verdict()
    assert outcome is QuoteOutcome.not_found
    assert reason and "No price data" in reason
    assert market_data.fetch_history_candidates("AAPL", START, END) == []
    assert market_data.fetch_latest_close("AAPL", END) is None


def test_a_frame_that_answers_nothing_never_books_an_ok_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE verdict half of the contract (the hole the miss-count missed).

    A frame of in-window rows with no usable close produced ``({}, ok)``:
    `backfill_range` then booked ``ok`` against the row, which RESETS M141's
    dead-symbol counter on evidence of nothing — the symbol could never die.
    """
    frame = JUNK_HISTORY_FRAMES["all-nan-closes"]
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(frame=frame))

    assert market_data.fetch_history("AAPL", START, END) == {}
    outcome, _reason = market_data.last_quote_verdict()
    assert outcome is QuoteOutcome.not_found
    # The fold agrees with it: an unknown never upgrades a miss, and one
    # failure to ASK dominates a miss (M141).
    assert market_data._combine_outcomes([outcome, QuoteOutcome.unknown]) is QuoteOutcome.unknown
    assert (
        market_data._combine_outcomes([QuoteOutcome.not_found, QuoteOutcome.unavailable])
        is QuoteOutcome.unavailable
    )


def test_a_partially_junk_frame_keeps_the_good_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Junk cells are SKIPPED, not fatal: a bad `Close` costs one day and a
    bad `Stock Splits` cell costs one event — the rest of the frame stands."""
    frame = pd.DataFrame(
        {
            "Close": ["n/a", 2.0, None, float("nan")],
            "Stock Splits": [8.0, "junk", float("nan"), 0.0],
        },
        index=pd.to_datetime(["2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]),
    )
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(frame=frame))

    closes, outcome = market_data._fetch_one_history("AAPL", START, date(2026, 10, 11))
    assert outcome is QuoteOutcome.ok
    # Only 10-09 carries a readable close; the split is dated BEFORE it, so
    # the as-traded factor for that day is 1 (M149's strictly-before rule).
    assert closes == {date(2026, 10, 9): Decimal("2.0")}
    assert market_data.last_splits() == ((date(2026, 10, 8), Decimal("8.0")),)


@pytest.mark.parametrize(
    "cell",
    [None, "", "  ", 0, -8.0, float("nan"), float("inf"), "junk", [], {}],
    ids=lambda cell: f"{cell!r}"[:20],
)
def test_a_junk_split_cell_is_never_an_event(monkeypatch: pytest.MonkeyPatch, cell: object) -> None:
    """A split cell reads as a ratio only when it IS one (M149's rule).

    A non-finite or negative ratio would multiply every pre-split close into
    ``NaN``/inverted prices and poison the price cache.
    """
    frame = pd.DataFrame(
        {"Close": [1.0, 2.0], "Stock Splits": [cell, 0.0]},
        index=pd.to_datetime(["2026-10-09", "2026-10-10"]),
    )
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(frame=frame))

    closes, outcome = market_data._fetch_one_history("AAPL", START, END)
    assert outcome is QuoteOutcome.ok
    assert closes == {date(2026, 10, 9): Decimal("1.0"), date(2026, 10, 10): Decimal("2.0")}
    assert market_data.last_splits() == ()


def test_an_empty_frame_is_a_verdict_about_the_symbol_not_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The M141 pin, restated here as the base case of this contract."""
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(frame=pd.DataFrame()))

    closes, outcome = market_data._fetch_one_history("APOTEX", START, END)
    assert (closes, outcome) == ({}, QuoteOutcome.not_found)
    _outcome, reason = market_data.last_quote_verdict()
    assert reason and "No price data" in reason


@pytest.mark.parametrize("label", sorted(JUNK_HISTORY_FRAMES))
def test_probe_quote_is_not_ok_when_the_frame_answers_nothing(
    monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """The add form's "will this ever have a price?" must not answer ok for a
    body that carries no price — `not frame.empty` is not evidence of a quote,
    exactly as `info` being non-empty was not."""
    frame = JUNK_HISTORY_FRAMES[label]
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(frame=frame))

    outcome, reason = market_data.probe_quote("APOTEX")
    assert outcome is not QuoteOutcome.ok, f"{label} probed as ok"
    assert outcome is QuoteOutcome.not_found
    assert reason and "No public quote" in reason


def test_probe_quote_still_answers_ok_for_a_real_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-regression: one readable close is a quote."""
    frame = pd.DataFrame({"Close": [30.79]}, index=pd.to_datetime(["2026-10-10"]))
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(frame=frame))

    assert market_data.probe_quote("APTX.TO") == (QuoteOutcome.ok, None)


def test_probe_quote_still_classifies_a_network_failure_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure to ASK says nothing about the symbol (M141)."""
    from yfinance.exceptions import YFRateLimitError

    monkeypatch.setattr(
        yfinance, "Ticker", lambda symbol: _BodyTicker(frame=YFRateLimitError("429"))
    )

    outcome, _reason = market_data.probe_quote("AAPL")
    assert outcome is QuoteOutcome.unavailable


def test_a_classification_error_is_never_a_verdict_about_the_symbol() -> None:
    """A JSON/parse failure is the NETWORK's verdict; only yfinance's own
    "quote not found" class may advance the dead-symbol counter."""
    from yfinance.exceptions import YFTzMissingError

    # The live-probed shape yfinance 1.6.0 raises for an unlisted symbol —
    # a subclass of YFTickerMissingError, the class the classifier keys on.
    missing = YFTzMissingError("$APOTEX: possibly delisted; no timezone found")
    outcome, _reason = market_data._classify_history_error(missing)
    assert outcome is QuoteOutcome.not_found
    for exc in (
        ValueError("Expecting value: line 1 column 1"),
        ConnectionResetError("connection reset"),
        RuntimeError("yfinance is down"),
        KeyError("error"),
    ):
        classified, _reason = market_data._classify_history_error(exc)
        assert classified is QuoteOutcome.unavailable, f"{exc!r} classified as {classified}"


def test_an_empty_outcome_list_never_becomes_a_hard_verdict() -> None:
    """No candidate asked = no verdict (`unknown`), never `not_found`."""
    assert market_data._combine_outcomes([]) is QuoteOutcome.unknown
    assert market_data._combine_outcomes([QuoteOutcome.ok]) is QuoteOutcome.ok
    assert (
        market_data._combine_outcomes([QuoteOutcome.not_found, QuoteOutcome.not_found])
        is QuoteOutcome.not_found
    )


# ==============================================================================
# 6. fundamentals and holdings
# ==============================================================================


@pytest.mark.parametrize(
    "value",
    NON_NUMERIC_VALUES,
    ids=[f"{v!r}"[:20] for v in NON_NUMERIC_VALUES],
)
def test_a_number_that_is_not_a_number_is_never_stored(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    """`_dec` is the only door a provider number comes through.

    ``Decimal('NaN')`` and ``Decimal('Infinity')`` are VALID ``Decimal``s —
    they parse without raising and Postgres stores them in a ``Numeric``
    column — so finiteness is part of "this is a number", and a value that
    fails it reads as "no value" (as it already did for a split cell).
    """
    assert market_data._dec(value) is None

    body: dict[str, object] = {"marketCap": value, "beta": value, "dividendYield": value}
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(body=body))
    assert market_data.fetch_fundamentals("AAPL") is None


@pytest.mark.parametrize(
    "body",
    [*NON_DICT_INFO, {"trailingPegRatio": None}, {"sector": "   "}, {}],
    ids=lambda body: f"{body!r}"[:24],
)
def test_a_fundamentals_body_that_names_no_value_is_none(
    monkeypatch: pytest.MonkeyPatch, body: object
) -> None:
    """No mapped key -> None -> the caller keeps its cached row (docs/07)."""
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(body=body))
    assert market_data.fetch_fundamentals("AAPL") is None


def test_a_real_fundamentals_body_still_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-regression: the live-probed AAPL shape, values untouched."""
    body: dict[str, object] = {
        "trailingEps": 6.11,
        "trailingPE": 38.31,
        "marketCap": 3_090_000_000_000,
        "beta": 1.4,
        "fiftyTwoWeekHigh": 260.1,
        "fiftyTwoWeekLow": 169.21,
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "country": "United States",
        "dividendYield": 0.35,  # PERCENT-shaped, passed through as-is (pinned)
    }
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _BodyTicker(body=body))

    data = market_data.fetch_fundamentals("AAPL")
    assert data is not None
    assert data["market_cap"] == Decimal("3090000000000")
    assert data["dividend_yield_pct"] == Decimal("0.35")
    assert data["sector"] == "Technology"
    assert data["beta"] == Decimal("1.4")
    for key, value in data.items():
        if isinstance(value, Decimal):
            assert value.is_finite(), f"{key} is {value}"


@pytest.mark.parametrize(
    "weight",
    [float("nan"), float("inf"), -float("inf"), "NaN", "Infinity", -0.5],
    ids=lambda weight: f"{weight!r}",
)
def test_a_weight_that_is_not_a_number_never_becomes_a_holding_row(
    monkeypatch: pytest.MonkeyPatch, weight: object
) -> None:
    """`etf_holdings.weight_pct` is a column; a NaN row would poison the
    look-through totals (and `NaN <= 0` RAISES in the issuer-CSV check).

    The rule matches `_parse_issuer_csv`'s: a weight is a share of NAV, so it
    must be a finite POSITIVE number — anything else skips the row.
    """
    frame = pd.DataFrame(
        {"Name": ["Good Holding", "Bad Holding"], "Holding Percent": [0.075494, weight]},
        index=["NVDA", "JUNK"],
    )
    monkeypatch.setattr(market_data, "_ticker", lambda symbol: _FundsTicker(frame))

    rows = market_data.fetch_fund_holdings("SPY")
    assert rows is not None, "the good row was dropped along with the bad one"
    assert len(rows) == 1, rows
    assert rows[0]["holding_name"] == "Good Holding"
    assert rows[0]["weight_pct"] == Decimal("7.5494")
    assert rows[0]["weight_pct"].is_finite()


@pytest.mark.parametrize(
    "body",
    [None, {}, {"top_holdings": None}, object(), RuntimeError("funds_data is down")],
    ids=lambda body: f"{type(body).__name__}:{str(body)[:20]}",
)
def test_a_funds_body_that_is_not_a_frame_is_none(
    monkeypatch: pytest.MonkeyPatch, body: object
) -> None:
    """`funds_data`/`top_holdings` vary across yfinance versions — None, a
    raise, or junk: the import degrades to a miss, never a crash."""
    monkeypatch.setattr(market_data, "_ticker", lambda symbol: _FundsTicker(body))
    assert market_data.fetch_fund_holdings("SPY") is None


def test_an_empty_funds_frame_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market_data, "_ticker", lambda symbol: _FundsTicker(pd.DataFrame()))
    assert market_data.fetch_fund_holdings("SPY") is None


def test_a_real_funds_frame_still_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-regression: the live-probed SPY shape (a FRACTION of NAV, x100)."""
    frame = pd.DataFrame(
        {"Name": ["NVIDIA Corp", "Apple Inc."], "Holding Percent": [0.075494, 0.0612]},
        index=["NVDA", "AAPL"],
    )
    monkeypatch.setattr(market_data, "_ticker", lambda symbol: _FundsTicker(frame))

    rows = market_data.fetch_fund_holdings("SPY")
    assert rows == [
        {
            "holding_name": "NVIDIA Corp",
            "weight_pct": Decimal("7.5494"),
            "holding_symbol": "NVDA",
        },
        {"holding_name": "Apple Inc.", "weight_pct": Decimal("6.12"), "holding_symbol": "AAPL"},
    ]

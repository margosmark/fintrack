"""yfinance quotes: symbol lookup autofill + price caching (docs/07).

Call discipline: 5 s timeout on every request, handshake included (M149 — the
deadline is ours, installed on yfinance's shared session via ``_yf_session``,
not inherited from yfinance's own 30 s default), capped exponential backoff, 429s
retryable; ANY failure degrades to "no data" ({}/None) rather than raising —
the UI offers manual entry. Historical backfills cache into prices with source=yfinance,
insert ONLY dates lacking a row, and never touch manual rows (manual wins).

M141 (docs/07 §Dead symbols): a fetch failure is CLASSIFIED, not swallowed.
Yahoo's "quote not found" is a verdict about the symbol — no retry will change
it — while a rate-limit or a timeout says nothing about the symbol at all.
Three consecutive verdicts set ``Security.dead_symbol`` and the automatic
fan-out stops touching the row; any later close clears it again by itself.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Literal, NamedTuple, TypedDict

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.errors import api_error
from app.models.etf_holdings import EtfAssetClass, EtfHolding, EtfHoldingSource
from app.models.fundamentals import Fundamentals, FundamentalsSource
from app.models.prices import Price, PriceSource
from app.models.provider_registry import (
    ProviderFundFamily,
    ProviderFundRegistry,
    ProviderFundStatus,
)
from app.models.securities import QuoteOutcome, Security, SecurityKind
from app.services import provider_registry
from app.services.company_country_map import country_for_iso2
from app.services.session_release import release_before_provider_wait
from app.services.venue_codes import canonical_exchange

# M149: ONE deadline for every yfinance request this module makes, handshake
# included. docs/01 § "Sessions and the event loop" and docs/07 both promise
# "market data 5 s"; before M149 that promise was not ours to keep — yfinance
# applied its own 30 s default and this constant (then named
# HISTORY_TIMEOUT_SECONDS) was never read by anything.
YF_TIMEOUT_SECONDS = 5
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0
# Stale = newest usable price older than 7 calendar days (docs/07).
PRICE_STALE_DAYS = 7
FUNDAMENTALS_STALE_DAYS = 7
# How far back the latest-close fetch looks.
LATEST_LOOKBACK_DAYS = 21
# M141: consecutive HARD "quote not found" outcomes before the automatic
# fan-out gives up on a symbol. Rate-limits and timeouts never reach this
# counter, so three really does mean "Yahoo said no, three times".
DEAD_SYMBOL_MISSES = 3
# Kinds whose composition the recursive look-through can descend into (M19).
_FUND_KINDS_LOOKUP = frozenset({SecurityKind.etf, SecurityKind.mutual_fund})


# Privacy lockdown (docs/11 step 6): when the requesting user has
# privacy_mode=true, NO yfinance call may leave the machine. The flag rides
# the request context (bound per-request in deps.get_current_user); guarding
# lookup_symbol and fetch_history covers every current and future fetch path
# (fetch_latest_close, backfill_range, ensure_recent_price all route through
# fetch_history). Cached reads and manual prices never reach the guard.
_privacy_lock: ContextVar[bool] = ContextVar("market_data_privacy_lock", default=False)

# M141: the classification of the fetch that just ran in THIS context. It rides
# the same ContextVar mechanism as the privacy lock, because the seams that
# need it (`backfill_range`, `ensure_recent_price`) call `fetch_history` and
# then have a `Security` row to bookkeep against — the fetch layer itself has
# no session and must stay pure. A seam that never reaches yfinance (a
# monkeypatched test double, the privacy guard) leaves it `unknown`, which is
# exactly right: no verdict was reached about the symbol.
_last_quote_outcome: ContextVar[QuoteOutcome] = ContextVar(
    "market_data_last_quote_outcome", default=QuoteOutcome.unknown
)
_last_quote_reason: ContextVar[str | None] = ContextVar(
    "market_data_last_quote_reason", default=None
)

# M149: the split events the fetch that just ran in THIS context saw, as
# ``(effective date, ratio)``. They ride the request context for the same
# reason the M141 verdict does — the fetch layer has no session — and they are
# what ``POST /securities/{id}/split-repair`` reports as applied
# (docs/07 §Price frame).
_last_splits: ContextVar[tuple[tuple[date, Decimal], ...]] = ContextVar(
    "market_data_last_splits", default=()
)

# Option A (t_bd5ec95f): the UTC instant the provider response that just ran
# in THIS context actually ARRIVED — captured inside `_fetch_one_history`
# immediately after `Ticker.history()` answers, never inside a cache-only
# read. It rides the request context for the same reason the M141 verdict and
# the M149 splits do (the fetch layer has no session), and it is the ONLY
# legal source of `prices.fetched_at`: a stamp assigned at cache time would
# re-create the exact bug this provenance column exists to fix (docs/07
# §Fetch provenance).
_last_fetch_at: ContextVar[datetime | None] = ContextVar("market_data_last_fetch_at", default=None)


def _reset_fetch_provenance() -> None:
    """A fetch timestamp belongs only to the provider call currently running."""
    _last_fetch_at.set(None)


@lru_cache(maxsize=1)
def _yf_session():
    """The session yfinance's process-wide singleton ends up holding (M149).

    yfinance is NOT a timeout-free seam: 1.6.0 passes its own ``timeout=30``
    down ``YfData._make_request`` on every request, handshake included. But
    that 30 s is yfinance's number, not ours — undocumented, version-dependent
    (AGENTS §6), and six times the call discipline this module and docs/07
    promise. Three attempts, times two candidates, times 30 s is minutes of a
    sync route's request held on one stalled Yahoo.

    ``Session.request`` is the one method every yfinance call — the cookie and
    crumb handshake included — ends in, so pinning OUR deadline there covers
    everything. The override FORCES the value rather than defaulting it:
    yfinance always passes its own, so a ``setdefault`` would never take effect
    (a test says so out loud).

    The instance is yfinance's own default session — curl_cffi impersonating
    Chrome where that backend is installed, a plain requests session where it
    is not — re-typed into a subclass, so the transport, headers and cookie jar
    are exactly what yfinance would have built and only the deadline differs.

    It is INSTALLED on the ``YfData`` singleton rather than handed to each
    ``Ticker(...)``: yfinance keeps ONE session for the whole process and
    re-uses it for every ticker, so the singleton is the only place the
    deadline has to land — and leaving the constructor's signature alone keeps
    every test double in the suite working unchanged.
    """
    import yfinance.data as yf_data  # lazily: yfinance is an unofficial API

    session = yf_data.new_session()
    backend = type(session)

    class _DeadlineSession(backend):
        def request(self, *args, **kwargs):
            kwargs["timeout"] = YF_TIMEOUT_SECONDS
            return super().request(*args, **kwargs)

    session.__class__ = _DeadlineSession  # type: ignore[misc]
    yf_data.YfData(session)  # positionally: the singleton's session-setting path
    return session


def _ticker(symbol: str):
    """A ``Ticker`` on the deadline session (M149, docs/07 call discipline).

    Looks yfinance up at call time, so a test that swaps ``yfinance.Ticker``
    for a double still wins.
    """
    import yfinance  # unofficial API; imported lazily so it can die gracefully

    _yf_session()
    return yfinance.Ticker(symbol)


def bind_privacy_lock(enabled: bool) -> None:
    """Bind the requesting user's privacy_mode to this request context."""
    _privacy_lock.set(enabled)


def _guard_external_fetch() -> None:
    """The single yfinance seam guard — cache reads never reach it."""
    _reset_fetch_provenance()
    if _privacy_lock.get():
        raise api_error(
            403,
            "Privacy mode blocks yfinance lookups and backfills; "
            "manual entries and cached prices keep working",
            "privacy_mode_blocks_external_api",
        )


def _classify_history_error(exc: BaseException) -> tuple[QuoteOutcome, str]:
    """Map a yfinance exception to a verdict about the SYMBOL (M141, docs/07).

    yfinance raises ``YFTickerMissingError`` (subclasses ``YFTzMissingError``,
    ``YFPricesMissingError``) when Yahoo reports no data for a ticker — the
    live probe against yfinance 1.6.0 answers APOTEX with
    ``YFTzMissingError("$APOTEX: possibly delisted; no timezone found")`` and
    the underlying HTTP layer logs
    ``404 {"error":{"code":"Not Found","description":"Quote not found for symbol: APOTEX"}}``.

    That is a verdict: the symbol has no public listing. Everything else —
    ``YFRateLimitError``, a socket error, a JSON parse failure — is a verdict
    about the NETWORK, not the symbol, and must never advance the miss counter.
    """
    try:
        from yfinance.exceptions import YFTickerMissingError
    except Exception:  # unofficial API may drop the module
        return QuoteOutcome.unavailable, str(exc)[:300]

    if isinstance(exc, YFTickerMissingError):
        return QuoteOutcome.not_found, str(exc)[:300]
    return QuoteOutcome.unavailable, str(exc)[:300]


def _combine_outcomes(outcomes: list[QuoteOutcome]) -> QuoteOutcome:
    """Fold the per-candidate verdicts of one fetch into a single one.

    A symbol resolves if ANY candidate returned closes. Otherwise a single
    ``unavailable`` anywhere dominates the ``not_found``s: one flaky request
    out of two means we never actually learned whether the symbol exists, and
    guessing "dead" off that would flag live symbols on a bad network day.
    """
    if QuoteOutcome.ok in outcomes:
        return QuoteOutcome.ok
    if QuoteOutcome.unavailable in outcomes:
        return QuoteOutcome.unavailable
    if outcomes and all(outcome is QuoteOutcome.not_found for outcome in outcomes):
        return QuoteOutcome.not_found
    return QuoteOutcome.unknown


_QUOTE_TYPE_TO_KIND = {
    "EQUITY": SecurityKind.stock,
    "ETF": SecurityKind.etf,
    "MUTUALFUND": SecurityKind.mutual_fund,
}


def _backoff(attempt: int) -> float:
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempt)


# --- M142: the Yahoo symbol-SEARCH fallback -----------------------------------
# A typed NAME ("APOTEX") 404s on both quote candidates: it is a
# name-not-ticker problem, and Yahoo's own search endpoint answers it in ONE
# call:
#   GET https://query2.finance.yahoo.com/v1/finance/search?q=Apotex
#     -> quotes: [{symbol: APTX.TO, shortname: "APOTEX HEALTH CORP",
#                  exchange: TOR, exchDisp: Toronto, quoteType: EQUITY},
#                 {symbol: APTX.NE, exchange: NEO, ...}]
# Field names and values LIVE-PROBED 2026-10-10 (from this host and from the
# api container): `exchange` is the venue code, `exchDisp` the display name,
# `quoteType` the family `_QUOTE_TYPE_TO_KIND` already keys off, and the row
# carries NO currency — the `.TO`/venue heuristic below derives it.
SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
SEARCH_TIMEOUT_SECONDS = 5
# Only these families are adoptable listings. A name matching only an
# INDEX/CURRENCY/NEWS row is not something this app can hold a price for, so
# it counts as NO hit (docs/07) and stays in the dead-symbol state.
_SEARCH_QUOTE_TYPES = frozenset({"EQUITY", "ETF", "MUTUALFUND"})
_USER_AGENT = "Mozilla/5.0"


def _http_search_json(query: str) -> dict[str, object] | None:
    """One Yahoo search call; None on any failure (degrade, never raise).

    Shares the module's call discipline (5 s timeout, capped exponential
    backoff, 429 retryable). Only 429s retry: every other failure is a
    terminal "no answer" exactly like the quote probes, and a lookup must
    never turn into a retry storm — repetition is the dead-symbol guard's
    job (M141), not this seam's.
    """
    import httpx  # pinned runtime dep; imported lazily like yfinance

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = httpx.get(
                SEARCH_URL,
                params={"q": query, "quotesCount": 10, "newsCount": 0},
                headers={"User-Agent": _USER_AGENT},
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
            if response.status_code == 429 and attempt < MAX_ATTEMPTS - 1:
                time.sleep(_backoff(attempt))
                continue
            response.raise_for_status()
            payload = response.json()
        except Exception:  # unofficial API may break arbitrarily
            return None
        return payload if isinstance(payload, dict) else None
    return None


#: Venues whose quoting unit a Yahoo SEARCH row cannot state (M167,
#: docs/07 §A minor-unit listing is refused). A search row carries no
#: ``currency`` field at all (live-measured, M142), so the row's venue is
#: the only thing a payload builder could key a label off — and these two
#: venues have been probed enough to know that is not enough:
#:
#: * ``LSE`` — the provider answers BOTH units on it, ``HSBA.L`` ``GBp``
#:   and ``HKLD.L`` ``USD``, same ``exchange: LSE``;
#: * ``CXE`` (Cboe UK) — answers ``GBp`` too: LIVE-PROBED 2026-10-11,
#:   ``HSBAL.XC`` (``fullExchangeName: "Cboe UK"``) reads ``GBp`` with
#:   ``regularMarketPrice`` 1395.8 — the SAME pence price ``HSBA.L`` reads,
#:   one listing on a second venue. Every other ``.XC`` spelling tried
#:   (``AZN.XC``, ``RIO.XC``, ``VOD.XC``, ``SHEL.XC``, ``BP.XC``,
#:   ``ULVR.XC``) 404s, so the venue is rare but not imaginary.
#:
#: A venue enters this set only with a live probe: the guess that is wrong
#: on either of them is a 100x price, and the refusal costs one row in one
#: chooser.
_UNIT_UNKNOWN_VENUES = frozenset({"LSE", "CXE"})


def provider_row_unit_is_unknown(row: object) -> bool:
    """Does this Yahoo SEARCH row come from a venue whose quoting unit the
    row itself cannot state? (M167 — the London rule.)

    The symbol half (``.L``) and the venue half (``LSE`` after
    :func:`_normalize_exchange`) are the same reading from two directions:
    a London listing, however the provider spelled its ticker. It is a
    REFUSAL, not a label: :func:`_search_equity_quotes` drops such a row so
    neither the single-hit adopter nor the human chooser can store a
    listing whose prices would arrive in a unit the app cannot name.

    Not a claim that London quotes pence — LIVE-PROBED 2026-10-11, four of
    the six ``.L`` listings prod holds answer ``USD`` — and not a rule
    about any other venue: ``CXE`` (Cboe UK) is in the set because
    ``HSBAL.XC`` was probed answering ``GBp`` at HSBC's own pence price,
    while no other European venue has been probed at all. Whatever a
    not-yet-probed venue answers, the standing repair report
    (``currency_mismatch``) is what names it if it ever lands wrong.
    """
    if not isinstance(row, dict):
        return False
    symbol = str(row.get("symbol") or "").strip().upper()
    if symbol.endswith(".L"):
        return True
    return _normalize_exchange(str(row.get("exchange") or "")) in _UNIT_UNKNOWN_VENUES


def _search_equity_quotes(query: str) -> list[dict[str, object]]:
    """Tradable-listing rows for a name-or-symbol query; [] on any failure.

    Rows keep Yahoo's own field names (symbol/shortname/longname/exchange/
    exchDisp/quoteType) — they are mapped by the two builders below, never
    spread into a payload raw.
    """
    _guard_external_fetch()
    clean = query.strip()
    if not clean:
        return []
    payload = _http_search_json(clean)
    if payload is None:
        return []
    rows = payload.get("quotes")
    if not isinstance(rows, list):
        return []
    matches: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # A row is a LISTING because it names a ticker in a tradable family —
        # and both must be the STRINGS Yahoo documents. A numeric symbol, an
        # object-shaped symbol or a family that is not a string is a
        # malformed row, and a malformed row never becomes a security
        # (`{"symbol": 12345}` used to adopt as ticker "12345"; a dict
        # symbol adopted as "{'TICKER': 'AAPL'}").
        quote_type = row.get("quoteType")
        if not isinstance(quote_type, str):
            continue
        if quote_type.strip().upper() not in _SEARCH_QUOTE_TYPES:
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        # M162: a row whose NAME is a machine-joined ID artifact is a provider
        # artifact, not an adoptable listing — never offer it to the single-
        # hit adopter nor to the human chooser (docs/07 §Symbol search
        # fallback). The live probe: `q=005930.KQ` answers zero rows today,
        # and this keeps a future artifact row from re-entering that way.
        if provider_name_is_artifact(_adopted_row_name(row)):
            continue
        # M167: a London row carries no quoting unit and the venue answers
        # both (`HSBA.L` GBp, `HKLD.L` USD — same `exchange: LSE`), so
        # adopting it would store a listing whose closes arrive in a unit
        # the label cannot name. Dropped like the artifact row above:
        # never offered to the single-hit adopter nor to the chooser
        # (docs/07 §A minor-unit listing is refused).
        if provider_row_unit_is_unknown(row):
            continue
        matches.append(row)
    return matches


def _normalize_exchange(exchange: str) -> str:
    """Store one spelling per venue: `strip().upper()` + the venue-alias table.

    `exchange` is part of `uq_securities_symbol (user_id, symbol, exchange)`
    (docs/02), so every consumer of `lookup_symbol` — `_resolve_security`, the
    sub-fund creator — must receive the same spelling `POST /securities`
    stores, or `nms` and `NMS` become two rows for one listing.

    M164 (docs/07 §Venue aliases): the alias half is
    `venue_codes.canonical_exchange`, which owns the table and its evidence.
    The provider is not consistent about a venue's code — it answered `SHH`
    for `600900.SS` on one run and `SHSE` on another (prod, both stored), and
    answers a bare Japanese ticker `TOKY1` while its `.T` form says `JPX` —
    so a second spelling of a venue must never reach the table. The Canadian
    TSX-family MICs (`TOR`/`TSE`/`XTSE`/`XTSX`/`TSXV`) are the oldest entries
    of that same table.
    """
    return canonical_exchange(exchange)


def _listing_currency(symbol: str, exchange: str = "") -> str:
    """CAD for the `.TO` suffix and Yahoo's Canadian venue codes, else USD.

    **M163: this is the FALLBACK, not the source of truth.** A ``Ticker.info``
    body states the listing's real currency and :func:`_info_listing_currency`
    believes it; the heuristic only still decides for a payload that carries
    no usable code — every SEARCH row (they carry none, live-measured), and
    any body whose ``currency`` field is absent or type-junk. See
    docs/07 §Listing currency (M163).

    The `.TO` half is the w2-contract §2 heuristic ``lookup_symbol`` has
    always used. M142 adds the venue half because a SEARCH row carries the
    venue (``TOR``/``NEO``) and a NEO-listed Canadian has no `.TO` suffix at
    all — APTX.NE reports currency CAD live (probed 2026-10-10). The set is
    ``_normalize_exchange``'s TSX/TSXV convention plus NEO.

    It is a venue heuristic, so it is blind to every venue it does not name:
    it answered **USD** for ``005930.KS`` (KRW, KSC), ``9984.T`` (JPY, JPX)
    and ``HSBA.L`` — a wrong label every FX conversion of that row's prices
    and trades then spends.
    """
    if symbol.strip().upper().endswith(".TO"):
        return "CAD"
    return "CAD" if _normalize_exchange(exchange) in {"TSX", "TSXV", "NEO"} else "USD"


def provider_currency_is_minor_unit(value: object) -> bool:
    """Is this provider ``currency`` value a MINOR-unit spelling — ``GBp``?

    M167 (docs/07 §A minor-unit listing is refused). M163's
    :func:`_normalize_currency_code` already REFUSES mixed case, because
    ``GBP`` is 100x a pence-quoted price (the M87 split class of silent
    wrong number) — but a refusal that falls through to the venue heuristic
    still stores a wrong LABEL (``HSBA.L`` answered USD). This predicate is
    the reading that tells that case apart from the ones that must keep
    falling back: **three ASCII letters in MIXED case** is the provider's
    one spelling of a minor unit, while an absent field, a blank, a number,
    a list, a dict, ``b"GBp"`` or an all-lower/all-upper code is not.

    LIVE-PROBED 2026-10-11 in the api container, one run: London answers
    BOTH units on the same ``exchange: LSE`` — ``HSBA.L``/``SHEL.L``/
    ``AZN.L``/``PEEL.L``/``VOD.L``/``RIO.L`` → ``GBp`` (pence; prices
    1395.8 / 3793.5 / 11998.0 / 93.0 / 118.35 / 7126.0) while ``AV98.L``
    and ``HKLD.L`` → ``USD`` (100.432 / 7.41). So the quoting unit is a
    fact about the LISTING and never about the venue: only the body's own
    code can name it, and when it names a minor unit this app does not
    adopt the listing at all.
    """
    if not isinstance(value, str):
        return False
    raw = value.strip()
    if len(raw) != 3 or not raw.isascii() or not raw.isalpha():
        return False
    return not (raw.islower() or raw.isupper())


def _normalize_currency_code(value: object) -> str:
    """The ONE reader of a provider body's ``currency`` field (M163).

    LIVE-PROBED 2026-10-11 in the api container, nine listings across five
    venues: ``005930.KS``/``000660.KS`` → ``KRW`` (KSC), ``RY.TO``/``AAPL.TO``
    → ``CAD`` (TOR), ``AAPL`` → ``USD`` (NMS), ``9984.T`` → ``JPY`` (JPX),
    ``HSBA.L``/``VOD.L`` → ``GBp`` (LSE). Yahoo reports the REAL currency and
    the heuristic was throwing it away.

    Accepted only when the value is a **string of exactly three ASCII
    letters** — what an ISO-4217 code looks like. Upper- or lower-case is
    normalized up (``usd`` → ``USD``); **mixed case is refused**, because
    that is how the provider spells a MINOR unit: ``GBp`` is pence, not
    pounds, and ``GBP`` is 100x the price Yahoo quoted. Everything else —
    absent, blank, a number, a list, a dict, the M162 artifact triple —
    answers ``""`` so the CALLER falls back to :func:`_listing_currency`.
    The body-contract fuzzer mutates a real body's ``currency`` with
    type-junk (``float("nan")``, ``True``, ``b"AAPL"``), and a payload may
    never carry what a body put there raw.

    The minor-unit reading is LIVE-PROBED 2026-10-11: both LSE listings in
    the probe (``HSBA.L``, ``VOD.L``) answer ``currency: "GBp"``. Adopting
    it as ``GBP`` would multiply a pence-quoted price by 100 — the M87
    split class of silent wrong number — and ``GBp`` itself is a base the
    FX book cannot serve. **M167 gives pence listings their contract**
    (docs/07 §A minor-unit listing is refused): this reader still answers
    ``""`` for the code, and :func:`provider_currency_is_minor_unit` is
    what lets the ADOPTION seam tell a minor unit from an absent or
    type-junk field — the minor-unit case refuses the whole body instead
    of falling back to the venue heuristic's USD.
    """
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if len(raw) != 3 or not raw.isascii() or not raw.isalpha():
        return ""
    if raw.islower():
        return raw.upper()
    return raw if raw.isupper() else ""


def _info_listing_currency(cand: str, info: dict[str, object], exchange: str) -> str:
    """The listing currency a ``Ticker.info`` body states — else the heuristic.

    M163 (docs/07 §Listing currency): the provider's own ``currency`` field
    WINS when it is a well-formed code, because it is a fact about the
    listing while :func:`_listing_currency` is a guess from the venue — a
    guess that answers USD for every non-North-American market it does not
    name. ``cand`` and ``exchange`` are what the FALLBACK keys off (the
    resolved candidate's spelling and venue, never the typed string).
    """
    return _normalize_currency_code(info.get("currency")) or _listing_currency(cand, exchange)


def _display_exchange(raw: str) -> str | None:
    """Yahoo's human venue name ("Toronto"), or None when it sent none —
    never a fabricated label (the UI falls back to the normalized code)."""
    clean = raw.strip()
    return clean or None


def provider_name_is_artifact(name: object) -> bool:
    """Is this provider-supplied NAME a machine-joined ID artifact rather
    than an instrument name? (M162, docs/07 §Symbol search fallback.)

    LIVE-PROBED 2026-10-11 in the api container: Yahoo answers the KOSDAQ
    twin ``005930.KQ`` with ``shortName`` ``005930.KQ,0P0000B2XZ,18569122``
    — the symbol, a Morningstar-style fund id and a numeric secId, joined
    with commas (``000660.KQ`` → ``000660.KQ,0P0000AZ1B,4519170``). A
    payload builder that adopts ``shortName`` verbatim therefore persisted
    that triple AS the security's name: six such rows reached prod
    (kind ``mutual_fund``, exchange ``KOE``), and while they existed the
    bare ``005930`` / ``000660`` stems could never link deterministically
    (M154's ambiguous-stem rule) and every name-keyed reader saw garbage.

    The shape is falsifiable and deliberately tight: TWO or more commas,
    every part non-empty, and NO whitespace anywhere. No human instrument
    name lacks spaces — ``Principal Fds, Inc. California ...`` (SRCMX) has
    a comma and still fails this test on its whitespace, and the real
    Samsung answer (``SamsungElec`` / ``Samsung Electronics Co., Ltd.``)
    has neither.
    """
    clean = str(name or "").strip()
    if not clean:
        return False
    if any(char.isspace() for char in clean):
        return False
    parts = clean.split(",")
    return len(parts) >= 3 and all(part.strip() for part in parts)


def _adopted_name(info: object) -> str:
    """The name a payload built from this ``Ticker.info`` body would carry.

    ``shortName`` first — the order the payload builders have always read —
    else ``longName``, each accepted only when it is a non-blank STRING.

    The type test is the point. A junk body may put anything at all in a name
    field (LIVE-PROBED junk carries ``None``; a fuzzed body carries objects,
    numbers, ``NaN``), and ``str(value)`` on those is not a name — it is how a
    security ends up called ``"{'ticker': 'AAPL'}"``, ``"42"`` or ``"nan"``.

    One reader for the gate AND the builders, because they used to disagree:
    `_is_real_quote` stripped each field before looking at it while the
    builders read them raw, so a body with a whitespace-only ``shortName``
    was *accepted* on its ``longName`` and then *stored* with
    ``name = "   "``. Here the strip is the read, so a body can only be
    accepted on a name it would also be adopted under (docs/07).
    """
    if not isinstance(info, dict):
        return ""
    for key in ("shortName", "longName"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _payload_from_info(cand: str, info: dict[str, object], typed: str) -> dict[str, object]:
    """The autofill payload from a ``Ticker.info`` body (direct/suffix hit).

    ``cand`` is the candidate that ANSWERED (it drives the currency
    fallback); ``typed`` is what the user typed and becomes the payload's
    ``symbol``: the CSV-import dedup (docs/05 ``_resolve_security``) matches
    existing rows on the RAW symbol cell, so adopting `VEQT.TO` here would
    put a second securities row next to every `VEQT` a Questrade file
    imports. The currency still comes from the RESOLVED candidate's body —
    its own ``currency`` field when it states one (M163), else its venue —
    which is what the FX seams (docs/06, M116) actually key off.
    """
    raw_exchange = str(info.get("exchange") or "")
    return {
        "symbol": typed,
        "name": _adopted_name(info) or cand,
        "kind": _QUOTE_TYPE_TO_KIND.get(
            str(info.get("quoteType") or "").upper(), SecurityKind.stock
        ),
        "listing_currency": _info_listing_currency(cand, info, raw_exchange),
        "exchange": _normalize_exchange(raw_exchange),
        "exchange_display": _display_exchange(str(info.get("fullExchangeName") or "")),
    }


def _adopted_row_name(row: object) -> str:
    """The name a payload built from ONE Yahoo search row would carry.

    ``shortname`` first, else ``longname``, each accepted only when it is a
    non-blank STRING — the search-row twin of :func:`_adopted_name`, for the
    same reason: ``str(value)`` on a junk field is not a name. A row whose
    name fields are all unusable still identifies a LISTING (it has a ticker
    in a tradable family), so the builders fall back to the symbol; a junk
    name must never reach the payload, but a junk name field alone must not
    throw away a good ticker either.
    """
    if not isinstance(row, dict):
        return ""
    for key in ("shortname", "longname"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _payload_from_search_row(row: dict[str, object]) -> dict[str, object]:
    """The autofill payload from ONE Yahoo search row (M142 canonical adopt).

    Everything comes from the row itself, so adopting it costs no second
    network call. Unlike the direct path the SYMBOL here is the row's
    canonical ticker — the typed string was a name, and persisting it
    (`APOTEX`) is what left the row unresolvable forever (docs/07).

    M163: the currency is the venue HEURISTIC, not a provider field — a
    search row carries none (live-measured 2026-10-10), so this path is
    exactly the fallback :func:`_listing_currency` still exists for.
    """
    symbol = str(row.get("symbol") or "").strip().upper()
    raw_exchange = str(row.get("exchange") or "")
    return {
        "symbol": symbol,
        "name": _adopted_row_name(row) or symbol,
        "kind": _QUOTE_TYPE_TO_KIND.get(
            str(row.get("quoteType") or "").upper(), SecurityKind.stock
        ),
        "listing_currency": _listing_currency(symbol, raw_exchange),
        "exchange": _normalize_exchange(raw_exchange),
        "exchange_display": _display_exchange(str(row.get("exchDisp") or "")),
    }


def _candidate_from_info(cand: str, info: dict[str, object]) -> dict[str, object]:
    """One row of a pick-one chooser built from a ``Ticker.info`` body (M143).

    A chooser row IS a create-ready listing: symbol, name, kind, listing
    currency and exchange all come from the probe that found it, so picking
    costs no second network call. That matters because M143 made the chooser
    reachable from a bare-vs-``.TO`` collision — re-resolving the PICKED
    symbol would land on the same collision and hand the user the chooser
    they just answered (docs/07 §Candidate resolution).

    M163: the currency is the body's own ``currency`` field when it states
    one (``AAPL`` → USD, ``AAPL.TO`` → CAD, both live-probed), so the two
    sides of a collision each carry the currency of the LISTING they name
    rather than two guesses from the venue.
    """
    raw_exchange = str(info.get("exchange") or "")
    return {
        "symbol": cand,
        "name": _adopted_name(info) or cand,
        "kind": _QUOTE_TYPE_TO_KIND.get(
            str(info.get("quoteType") or "").upper(), SecurityKind.stock
        ),
        "listing_currency": _info_listing_currency(cand, info, raw_exchange),
        "exchange": _normalize_exchange(raw_exchange),
        "exchange_display": _display_exchange(str(info.get("fullExchangeName") or "")),
    }


def _candidate_from_search_row(row: dict[str, object]) -> dict[str, object]:
    """One row of a pick-one chooser (M142), full-payload since M143.

    Same contract as :func:`_candidate_from_info`: the row is create-ready,
    so a pick never re-resolves (and so a collision pick can never bounce
    back to the same chooser). Kind and currency are derived here from the
    search row's own venue — a search row carries no currency field.
    """
    symbol = str(row.get("symbol") or "").strip().upper()
    raw_exchange = str(row.get("exchange") or "")
    return {
        "symbol": symbol,
        "name": _adopted_row_name(row) or symbol,
        "kind": _QUOTE_TYPE_TO_KIND.get(
            str(row.get("quoteType") or "").upper(), SecurityKind.stock
        ),
        "listing_currency": _listing_currency(symbol, raw_exchange),
        "exchange": _normalize_exchange(raw_exchange),
        "exchange_display": _display_exchange(str(row.get("exchDisp") or "")),
    }


class SymbolResolution(TypedDict):
    """What one lookup answered (M142, docs/07/08).

    ``result`` is the single-hit autofill payload (null for ``ambiguous`` and
    ``none``); ``candidates`` is the pick-one chooser, populated ONLY for
    ``ambiguous`` — a chooser and a resolved answer never appear together.
    """

    match: Literal["direct", "suffix", "search", "ambiguous", "none"]
    result: dict[str, object] | None
    candidates: list[dict[str, object]]


def _is_real_quote(info: object) -> bool:
    """Does this ``Ticker.info`` body name an actual listing?

    yfinance answers a mistyped or unlisted symbol with a NON-EMPTY junk
    body — LIVE-PROBED 2026-10-10 in the api container: ``Ticker("APOTEX").info``
    is ``{"trailingPegRatio": None}`` — so "the payload is non-empty" is not
    evidence of a quote.

    M143: it is not enough to see a ``quoteType``/``exchange`` pair either.
    LIVE-PROBED 2026-10-11: ``Ticker("VFV").info`` answers
    ``{"quoteType": "ECNQUOTE", "exchange": "NMS", "fullExchangeName":
    "NasdaqGS"}`` — a venue stub that NAMES no security. Under the old
    four-key ``any`` that passed as a real quote, so the payload builder
    fell back to the typed string for the name and the currency heuristic
    answered USD: the app stored a brand-new instrument out of a body that
    never mentioned one ("VFV, USD" — security 296).

    So the test is the name itself: a listing has a ``shortName``/``longName``
    (probed real: VGRO, VGRO.TO, VFV.TO, AAPL all carry one), a body that
    names nothing is a MISS, and the resolver moves on to the ``.TO``
    candidate and then to the search (docs/07).

    M162: a body whose ADOPTED name (``shortName`` first, the order the
    payload builder reads) is a machine-joined ID artifact is a MISS too —
    the provider did not name an instrument, it echoed its own identifiers
    back. LIVE-PROBED 2026-10-11: ``Ticker("005930.KQ").info`` answers
    ``quoteType=MUTUALFUND, shortName="005930.KQ,0P0000B2XZ,18569122",
    longName="Samsung Electronics Co Ltd"``. The longName is clean, but the
    record is a fund-facts MIRROR of the equity, not a listing: adopting it
    (even under the longName) re-creates the ``mutual_fund`` twin whose
    existence is what made the ``005930`` stem ambiguous in the first place
    (docs/07 §Symbol search fallback). Refusing the whole body is the
    point — the search fallback answers this symbol with ZERO rows, so the
    lookup degrades to ``none`` and the deepening loop skips the leg.

    Two further readings the contract suite pins
    (``tests/test_market_data_body_contract.py``):

    * a body that is not a DICT at all is a MISS — an unofficial API may
      answer a string, a list or a number, and ``info.get`` used to raise
      ``AttributeError`` straight out of ``resolve_symbol`` (a 500 on the
      lookup endpoint instead of the documented 404);
    * the name must be a non-blank STRING (see :func:`_adopted_name`), and it
      is read through the SAME helper the payload builders use, so the name a
      body is accepted on is exactly the name it would be adopted under.

    M167 (docs/07 §A minor-unit listing is refused): a body whose own
    ``currency`` field is a MINOR-UNIT spelling is a MISS too. ``GBp`` is
    pence, and a pence-quoted listing is one this app cannot value —
    storing ``GBP`` would multiply its prices by 100 while ``GBp`` itself is
    a base the FX book cannot serve — so the whole BODY is refused rather
    than falling back to the venue heuristic, which is what used to leave
    London carrying a USD label. This is deliberately the last rule in the
    chain: an absent or type-junk ``currency`` is not a minor unit
    (:func:`provider_currency_is_minor_unit`), so every body the M163/M162
    fixtures pin keeps its answer.
    """
    name = _adopted_name(info)
    if not name:
        return False
    if provider_name_is_artifact(name):
        return False
    return not provider_currency_is_minor_unit(
        info.get("currency") if isinstance(info, dict) else None
    )


def resolve_symbol(
    symbol: str,
    *,
    disambiguate: bool = True,
    prefer_currency: str | None = None,
) -> SymbolResolution:
    """The full answer behind ``GET /securities/lookup`` (M142, docs/07).

    Resolution order — every step costs at most one network call per
    candidate:

    1. ``direct`` — the bare symbol answers a ``Ticker.info`` probe;
    2. ``suffix`` — the ``.TO`` form does (Canadian tickers are usually
       queried without the suffix yfinance requires);
    3. ``search`` — both 404'd, so the string is likely a NAME: one Yahoo
       ``v1/finance/search`` call, and when it returns EXACTLY ONE equity
       quote that row's canonical ticker is adopted;
    4. ``ambiguous`` — several listings matched: the candidates come back
       as a pick-one chooser. A silent guess is never allowed to become a
       stored symbol (docs/09).

    **M143 — both quote candidates are probed, and a collision is a choice,
    not a coin flip.** Steps 1 and 2 used to run in order and return on the
    FIRST hit, so a bare ticker that also exists on the TSX lost to whatever
    the bare probe said: ``VGRO`` answered with Virtus Silvant (NYSEArca,
    USD) and Vanguard's ``VGRO.TO`` was never asked, which is how security
    294 got the wrong instrument, the wrong currency and three wrong prices
    (docs/07 §Candidate resolution). Now BOTH candidates are probed, and
    when both name a real listing the answer is ``ambiguous`` — the chooser
    shows symbol, name, currency and venue for each side and a human picks.

    Two callers legitimately opt out of the chooser:

    - ``disambiguate=False`` — a caller with NO picker (a background price
      fetch, an operator repair naming its target explicitly) keeps the old
      bare-first order rather than being handed a choice it cannot present.
    - ``prefer_currency=`` — a caller that already knows the listing's
      currency from the USER'S OWN DATA (the CSV import's currency cell,
      docs/05) is not ambiguous: when exactly one candidate carries that
      currency it is selected outright, and the chooser only appears if
      neither (or both) fit.

    Zero hits (or a search Yahoo cannot answer) is ``none``: callers degrade
    to 404 ``lookup_unavailable`` / ``{}``, which is the state M141's
    dead-symbol guard consumes. The privacy guard runs before the first
    probe, so a privacy-mode user gets no search call either.
    """
    _guard_external_fetch()

    clean = symbol.strip().upper()
    if not clean:
        return {"match": "none", "result": None, "candidates": []}
    candidates = [clean]
    if not clean.endswith(".TO"):
        candidates.append(clean + ".TO")

    hits: list[tuple[str, dict[str, object]]] = []
    for cand in candidates:
        try:
            info = _ticker(cand).info or {}
        except Exception:  # unofficial API may break arbitrarily
            info = {}
        if info and _is_real_quote(info):
            hits.append((cand, info))

    if len(hits) == 1:
        cand, info = hits[0]
        return {
            "match": "direct" if cand == clean else "suffix",
            "result": _payload_from_info(cand, info, clean),
            "candidates": [],
        }
    if len(hits) > 1:
        chosen = _currency_match(hits, prefer_currency)
        if chosen is not None:
            cand, info = chosen
            return {
                "match": "direct" if cand == clean else "suffix",
                "result": _payload_from_info(cand, info, clean),
                "candidates": [],
            }
        if not disambiguate:
            cand, info = hits[0]
            return {
                "match": "direct" if cand == clean else "suffix",
                "result": _payload_from_info(cand, info, clean),
                "candidates": [],
            }
        return {
            "match": "ambiguous",
            "result": None,
            "candidates": [_candidate_from_info(cand, info) for cand, info in hits],
        }

    rows = _search_equity_quotes(clean)
    if len(rows) == 1:
        return {"match": "search", "result": _payload_from_search_row(rows[0]), "candidates": []}
    if len(rows) > 1:
        return {
            "match": "ambiguous",
            "result": None,
            "candidates": [_candidate_from_search_row(row) for row in rows],
        }
    return {"match": "none", "result": None, "candidates": []}


def _currency_match(
    hits: list[tuple[str, dict[str, object]]], prefer_currency: str | None
) -> tuple[str, dict[str, object]] | None:
    """The single candidate carrying ``prefer_currency`` — else None.

    None means "no unique answer from the caller's own data", which sends
    the caller back to the chooser: a hint that matches both (or neither)
    side of a collision disambiguates nothing.
    """
    if not prefer_currency:
        return None
    want = prefer_currency.strip().upper()
    matches = [hit for hit in hits if _candidate_from_info(*hit)["listing_currency"] == want]
    return matches[0] if len(matches) == 1 else None


def lookup_symbol(symbol: str, *, prefer_currency: str | None = None) -> dict[str, object] | None:
    """yfinance autofill {symbol, name, kind, listing_currency, exchange}.

    Returns None on any failure or empty payload — callers degrade to
    404 {"code": "lookup_unavailable"} (docs/07), never a 500. Listing
    currency is the provider's own when it states one (M163: ``currency``
    field on the answering ``Ticker.info`` body — ``005930.KS`` → KRW),
    else the `.TO` / Canadian-venue heuristic (w2-contract §2); a SEARCH
    answer has no provider currency to read, so it is always the heuristic.

    Canadian (TSX/TSXV) tickers are frequently queried without the `.TO`
    suffix yfinance requires; when the bare symbol yields nothing we retry
    with the suffix, mirroring fetch_history (docs/07).

    M142: when BOTH candidates 404 the string may be a company NAME rather
    than a ticker, so one Yahoo search call runs and a single equity hit is
    adopted as the canonical symbol (`APOTEX` -> `APTX.TO`). An AMBIGUOUS
    search answers None here — several listings mean a human must pick, and
    this single-result contract has no way to say that; the API endpoint
    calls :func:`resolve_symbol` directly to surface the chooser.

    M143: a bare-vs-``.TO`` collision answers None here too, UNLESS the
    caller states the listing's currency (``prefer_currency``) — the CSV
    import's own currency cell then selects the matching side instead of
    losing the row's exchange/currency to the fallback heuristic
    (docs/07 §Candidate resolution).
    """
    result = resolve_symbol(symbol, prefer_currency=prefer_currency)["result"]
    return result


def _split_ratio(value: object) -> Decimal | None:
    """One ``Stock Splits`` cell as a positive ratio, or ``None`` if not an event.

    Yahoo stores ``numerator / denominator``: ``8.0`` for an 8:1 split, ``0.1``
    for a 1:10 reverse split. Zero means "no event that day"; NaN/None/non-numeric
    are treated the same way rather than being allowed to raise inside a fetch
    that would otherwise succeed.
    """
    try:
        ratio = Decimal(str(value))
    except Exception:  # the cell may hold anything at all
        return None
    if not ratio.is_finite() or ratio <= 0:
        return None
    return ratio


def _close_cell(value: object) -> Decimal | None:
    """One ``Close`` cell as a finite Decimal, or None when it is not a price.

    A cell may hold anything: NaN is yfinance's "no quote that day", a junk
    body may hold a string or an object, and a truncated answer may hold
    ``"n/a"``. ``Decimal`` would happily accept ``'NaN'``/``'Infinity'`` (they
    parse without raising and a ``Numeric`` column stores them), so
    finiteness is part of "this is a price" — the reading :func:`_split_ratio`
    already applies to a split cell.

    Positivity is deliberately NOT enforced: a zero close is still data, and
    refusing it here would silently drop a day the provider did answer.
    """
    if value is None:
        return None
    try:
        close = Decimal(str(value))
    except Exception:  # the cell may hold anything at all
        return None
    return close if close.is_finite() else None


def _history_rows(frame: object) -> Iterable | None:
    """``frame.iterrows()`` for a body we can read; None for one we cannot.

    ``Ticker.history`` is an unofficial API: it answers a DataFrame, and (across
    versions, and on failure modes) ``None``, an error stub, or an object of
    any shape at all. Parsing such a body used to raise ``AttributeError``
    / ``InvalidOperation`` straight OUT of ``fetch_history`` — the module's own
    contract is "ANY failure degrades to 'no data' rather than raising", so a
    body we cannot read is a body with no rows in it.
    """
    iterator = getattr(frame, "iterrows", None)
    if iterator is None or getattr(frame, "empty", True):
        return None
    return iterator()


def _history_has_close(frame: object) -> bool:
    """Does this ``history`` body carry at least one USABLE, DATABLE close?

    "Not empty" is not evidence of a quote — the same reading
    :func:`_is_real_quote` applies to ``info`` being non-empty: a frame of
    NaN closes is yfinance's shape for a day the symbol did not trade, and a
    body with no readable price in it has no quote in it.

    The index counts too, because ``probe_quote`` promises the add form "the
    SAME verdict from the same seam" as the fan-out (docs/07 §Symbol check):
    `_fetch_one_history` skips a row it cannot date, so a frame whose rows
    cannot be dated must not probe as ``ok`` and fetch as ``not_found``.
    """
    rows = _history_rows(frame)
    if rows is None:
        return False
    for index, row in rows:
        if hasattr(index, "date") and _close_cell(row.get("Close")) is not None:
            return True
    return False


def to_as_traded(
    closes: dict[date, Decimal], splits: list[tuple[date, Decimal]]
) -> dict[date, Decimal]:
    """Yahoo's split-ADJUSTED closes -> the as-traded prices the ledger means (M149).

    Yahoo's ``Close`` is ALWAYS split-adjusted — ``auto_adjust=False`` removes
    only the *dividend* adjustment, so there is no "raw" series to read
    (live-probed against yfinance 1.6.0, 2026-10-11: VGT 2025-11-18 reads
    ``Close`` 91.5738 / ``Adj Close`` 91.1711 while the trade that day was
    $732.59, and ``Stock Splits`` = 8.0 dated 2026-04-21).

    The ledger records what was actually bought and sold, so the cache has to
    be brought back into that frame:

    ``as_traded(d) = yahoo(d) x Π{ratio : split_date > d}``

    The split's own effective date is the FIRST day already quoted post-split
    (measured: VGT 2026-04-20 reads 101.1413 — 1/8 of the 809.13 actually
    traded — while 2026-04-21 reads 101.0200, its real close), so the
    comparison is STRICTLY BEFORE. The same formula covers a reverse split:
    Yahoo's ``0.1`` for 1:10 divides the pre-split closes by 0.1, and
    multiplying them back by 0.1 restores the traded price.

    docs/06 pins a split to move the ledger's SHARE COUNT (the manual
    ``corporate_action_adjustment``); this is the other half of that contract
    — quantity and price must be in the same frame, or a real position is
    silently valued an 8th of its worth.
    """
    if not splits:
        return closes
    ordered = sorted(splits)
    out: dict[date, Decimal] = {}
    for day, close in closes.items():
        factor = Decimal("1")
        for split_day, ratio in ordered:
            if day < split_day:
                factor *= ratio
        out[day] = close * factor
    return out


def _fetch_one_history(
    symbol: str, start_date: date, end_date: date, *, as_traded: bool = True
) -> tuple[dict[date, Decimal], QuoteOutcome]:
    """One yfinance history attempt set (with backoff).

    Returns ``({closes}, outcome)``: ``{}`` on total failure, paired with the
    M141 verdict for that failure so the caller can bookkeep it (docs/07).

    M149 (docs/07 §Price frame): the frame is fetched with ``period="max"``
    and the caller's window is applied by slicing. A window alone cannot say
    whether a split already happened before it started, and the full frame
    costs the same — measured against yfinance 1.6.0 (2026-10-11) a
    ``period="max"`` fetch of VGT (5710 rows, 2004 → today) was 0.15 s against
    0.26 s for the 252-row window. Every ``Stock Splits`` event the frame
    carries is published on ``_last_splits`` and applied through
    :func:`to_as_traded`, so what leaves here is always in the ledger's frame.

    ``as_traded=False`` (M173, the BENCHMARK path) skips that un-adjustment and
    returns the provider's own split-adjusted closes. A ledger close has to
    match traded quantities (hence un-adjusting); a benchmark has no ledger,
    and un-adjusting a price index would read a 2:1 split as a -50% return.
    """

    _reset_fetch_provenance()
    frame = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            frame = _ticker(symbol).history(
                period="max",
                auto_adjust=False,
                raise_errors=True,
            )
            # The provider ANSWERED — capture the arrival instant now, while it
            # is still the truth about this response (Option A, t_bd5ec95f).
            # Set even for a frame that turns out empty: the provider did
            # respond; what the frame says about the symbol is the M141
            # verdict's business, not the stamp's.
            _last_fetch_at.set(datetime.now(UTC))
            break
        except Exception as exc:  # unofficial API may break arbitrarily
            if attempt == MAX_ATTEMPTS - 1:
                outcome, reason = _classify_history_error(exc)
                _last_quote_reason.set(reason)
                _last_splits.set(())
                return {}, outcome
            time.sleep(_backoff(attempt))
    rows = _history_rows(frame)
    if rows is None:
        # An empty frame — or a body we cannot read at all — is yfinance's
        # other shape for an unlisted ticker (the live probe:
        # `Ticker("APOTEX").info` returns just `{"trailingPegRatio": None}`
        # while `.history` answers an empty frame). It is a verdict about the
        # SYMBOL, not a network failure.
        _last_quote_reason.set(f"No price data for {symbol} in the requested range")
        _last_splits.set(())
        return {}, QuoteOutcome.not_found
    closes: dict[date, Decimal] = {}
    splits: list[tuple[date, Decimal]] = []
    for index, row in rows:
        # A junk frame may carry a non-date index; a row we cannot date is a
        # row we cannot price, and must not raise out of a fetch.
        day = index.date() if hasattr(index, "date") else None
        if day is None:
            continue
        # Split events are read OUTSIDE the window too — that is the whole
        # point of the max fetch: a pre-split window has no event of its own.
        ratio = _split_ratio(row.get("Stock Splits"))
        if ratio is not None:
            splits.append((day, ratio))
        if day < start_date or day > end_date:
            continue
        close = _close_cell(row.get("Close"))
        if close is None:
            continue
        closes[day] = close
    if not closes:
        # NOT the same as `in_window`: a frame whose in-window rows carry no
        # readable close (all NaN, no `Close` column) produced NOTHING, and
        # `({}, ok)` would book a success against the row — resetting M141's
        # dead-symbol counter on evidence of nothing, so the symbol could
        # never be flagged. No price for this window is the M141 miss.
        _last_quote_reason.set(f"No price data for {symbol} in the requested range")
        _last_splits.set(())
        return {}, QuoteOutcome.not_found
    _last_splits.set(tuple(sorted(splits)))
    if not as_traded:
        return closes, QuoteOutcome.ok
    return to_as_traded(closes, splits), QuoteOutcome.ok


def fetch_benchmark_history(
    symbol: str, start_date: date, end_date: date
) -> tuple[dict[date, Decimal], QuoteOutcome]:
    """One EXACT-ticker history fetch for a benchmark series (M173, docs/07).

    Two deliberate differences from :func:`fetch_history`:

    * **no candidate fan-out and no M142 search heal.** A benchmark's venue
      ticker is pinned in ``services/benchmarks.py::BENCHMARKS``
      (``XIU.TO``/``XBB.TO``/``XAW.TO`` on the TSX, ``SPY`` in the US), and
      resolving a bare ticker by search can answer as a DIFFERENT instrument —
      the M144 ``VUS`` = *Virtus U.S. Dividend ETF* vs ``VUS.TO`` = the TSX
      holding case. Comparing a Canadian investor against the wrong index is
      worse than answering nothing, so the pinned spelling is the only one
      tried.
    * **the provider's split-adjusted closes** (``as_traded=False``), because a
      benchmark has no ledger quantities to match (see ``_fetch_one_history``).

    The privacy guard runs first: under ``privacy_mode`` this raises the
    documented 403 envelope, exactly like every other yfinance seam.
    """
    _guard_external_fetch()
    closes, outcome = _fetch_one_history(
        symbol.strip().upper(), start_date, end_date, as_traded=False
    )
    _last_quote_outcome.set(outcome)
    return closes, outcome


def last_splits() -> tuple[tuple[date, Decimal], ...]:
    """The split events the fetch that just ran in THIS context saw (M149)."""
    return _last_splits.get()


def fetch_history(symbol: str, start_date: date, end_date: date) -> dict[date, Decimal]:
    """Daily closes for [start_date, end_date]; any failure degrades to {}.

    Canadian (TSX/TSXV) tickers are frequently stored without the `.TO`
    suffix yfinance requires. When the bare symbol yields nothing we retry
    with the suffix, so CDN securities resolve without manual exchange
    normalization (docs/07). The privacy guard runs once before any
    external call.

    M141: the verdict of this fetch is published on the request context
    (``_last_quote_outcome``) for the session-aware seams to bookkeep against.
    A candidate that produced closes wins outright; otherwise a single
    "unavailable" anywhere dominates the "quote not found"s (docs/07).

    M142 heal: when every candidate is empty the stored symbol may be a NAME
    rather than a ticker (a row added before the search fallback existed), so
    one Yahoo search call runs and a SINGLE equity hit is fetched under its
    canonical ticker — the closes land and the resolution is logged once, so
    an operator can see the row that still spells its symbol as a name.
    Several hits are never guessed at (a background fetch has no picker) and
    answer {} like a miss. The heal deliberately does NOT touch the M141
    verdict: a dead-symbol counter stays a verdict about the STORED spelling,
    and M141's guard clears it by itself the moment any close lands.
    """
    _guard_external_fetch()
    clean = symbol.strip().upper()
    candidates = [clean]
    if not clean.endswith(".TO"):
        candidates.append(clean + ".TO")
    outcomes: list[QuoteOutcome] = []
    reason: str | None = None
    for candidate in candidates:
        closes, outcome = _fetch_one_history(candidate, start_date, end_date)
        outcomes.append(outcome)
        if closes:
            _last_quote_outcome.set(QuoteOutcome.ok)
            _last_quote_reason.set(None)
            return closes
        reason = _last_quote_reason.get() or reason
    rows = _search_equity_quotes(clean)
    if len(rows) == 1:
        canonical = str(rows[0].get("symbol") or "").strip().upper()
        if canonical and canonical not in candidates:
            closes, _ = _fetch_one_history(canonical, start_date, end_date)
            if closes:
                logging.getLogger(__name__).info(
                    "symbol search healed the price fetch: %s -> %s (%s)",
                    clean,
                    canonical,
                    str(rows[0].get("shortname") or "").strip() or "name unknown",
                )
                _last_quote_outcome.set(QuoteOutcome.ok)
                _last_quote_reason.set(None)
                return closes
    _last_quote_outcome.set(_combine_outcomes(outcomes))
    _last_quote_reason.set(reason)
    return {}


class CandidateSeries(NamedTuple):
    """One listing candidate that answered for a window (M144, docs/07).

    ``currency`` is the currency its closes are QUOTED in, derived by the
    ``_listing_currency`` heuristic — NOT read from a provider body: this
    path fetches closes (``Ticker.history``), which carry no currency, and
    an extra ``.info`` call per candidate is not worth it. Since M163 the
    row's OWN ``listing_currency`` may be the provider's (KRW for
    ``005930.KS``) while this label stays the heuristic's (USD) — which is
    harmless here and only costs the wide converted reading:
    ``score_series`` tests the trade price RAW first (currency-agnostic,
    the reading a KRW trade priced in KRW hits) and only then fx-converts,
    so a mismatch degrades to the honest ``no_listing_agrees`` gap rather
    than a wrong cache. The caller needs the label to compare a close
    against a ledger row priced in another currency.
    """

    symbol: str
    currency: str
    closes: dict[date, Decimal]


def fetch_history_candidates(
    symbol: str, start_date: date, end_date: date
) -> list[CandidateSeries]:
    """Every listing candidate that produced closes for the window, in the
    documented candidate order (docs/07 §Symbol search fallback): the stored
    spelling, its ``.TO`` form, then the M142 single-search canonical ticker.

    ``fetch_history`` STOPS at the first candidate that answers — right for a
    background refresh, wrong for a caller that has to tell WHICH listing a
    bare ticker means. Measured M144 (2026-10-10): bare ``VUS`` answers as
    *Virtus U.S. Dividend ETF* (NYSEArca, USD) at $30.23 while ``VUS.TO`` is
    the TSX holding at $130.70, and bare ``VGRO`` as *Virtus Silvant Growth*
    at $25.99 against ``VGRO.TO``'s $48.10 — so the first answer can be a
    DIFFERENT instrument. :func:`app.services.deep_backfill` compares each
    answering candidate against the row's own trade prices and caches one.

    The privacy guard runs once before the first call; the M141 verdict of the
    whole probe is published on the request context for the caller to bookkeep
    (a candidate that produced closes wins, else the combined outcome).
    """
    _guard_external_fetch()
    clean = symbol.strip().upper()
    candidates = [clean]
    if not clean.endswith(".TO"):
        candidates.append(clean + ".TO")
    found: list[CandidateSeries] = []
    outcomes: list[QuoteOutcome] = []
    reason: str | None = None
    # Option A (t_bd5ec95f): every candidate runs, so the context stamp ends
    # up on the LAST probe — but the closes we cache came from the FIRST
    # candidate that ANSWERED. Remember that one's arrival instant and
    # republish it below, so `prices.fetched_at` records the fetch whose
    # closes actually landed, not a later probe's.
    answered_at: datetime | None = None
    for candidate in candidates:
        closes, outcome = _fetch_one_history(candidate, start_date, end_date)
        outcomes.append(outcome)
        if closes:
            found.append(CandidateSeries(candidate, _listing_currency(candidate), closes))
            if answered_at is None:
                answered_at = _last_fetch_at.get()
        reason = _last_quote_reason.get() or reason
    if not found:
        rows = _search_equity_quotes(clean)
        if len(rows) == 1:
            canonical = str(rows[0].get("symbol") or "").strip().upper()
            if canonical and canonical not in candidates:
                closes, _ = _fetch_one_history(canonical, start_date, end_date)
                if closes:
                    logging.getLogger(__name__).info(
                        "symbol search healed the price fetch: %s -> %s (%s)",
                        clean,
                        canonical,
                        str(rows[0].get("shortname") or "").strip() or "name unknown",
                    )
                    found.append(CandidateSeries(canonical, _listing_currency(canonical), closes))
                    answered_at = _last_fetch_at.get()
    if found:
        if answered_at is not None:
            _last_fetch_at.set(answered_at)
        _last_quote_outcome.set(QuoteOutcome.ok)
        _last_quote_reason.set(None)
    else:
        _last_quote_outcome.set(_combine_outcomes(outcomes))
        _last_quote_reason.set(reason)
    return found


def cache_closes(
    session: Session,
    security: Security,
    closes: dict[date, Decimal],
    *,
    fetched_at: datetime | None = None,
) -> int:
    """Public seam for a caller that has ALREADY chosen its series.

    Identical rules to every other writer (:func:`_cache_closes`): inserts only
    missing ``(security, date)`` rows, never touches a manual row, commits what
    it added. The deep backfill uses it after its own candidate check.

    ``fetched_at`` (Option A, t_bd5ec95f) is the provider-response arrival
    instant for ``closes`` — pass ``last_fetch_at()`` when the closes came
    from this context's fetch; None means no evidence and stamps nothing.
    """
    return _cache_closes(session, security, closes, fetched_at=fetched_at)


def last_quote_verdict() -> tuple[QuoteOutcome, str | None]:
    """The M141 verdict of the fetch that just ran in THIS context."""
    return _last_quote_outcome.get(), _last_quote_reason.get()


def last_fetch_at() -> datetime | None:
    """The UTC instant the provider response that just ran in THIS context
    arrived (Option A, t_bd5ec95f), or None when no provider answered here —
    a cache-only read, a monkeypatched double, or the privacy guard. This is
    the ONLY legal source of ``prices.fetched_at``.
    """
    return _last_fetch_at.get()


def reset_quote_verdict() -> None:
    """Clear a previous fetch verdict before a provider seam begins work."""
    _last_quote_outcome.set(QuoteOutcome.unknown)
    _last_quote_reason.set(None)


def probe_quote(symbol: str) -> tuple[QuoteOutcome, str | None]:
    """One-call existence probe for a symbol (M141, docs/07 §Symbol check).

    The add-form's "will this symbol ever have a price?" affordance. A single
    ``history(period="1d")`` — the same call the price fan-out makes — so the
    verdict the form shows and the verdict the fan-out later books are the
    same verdict from the same seam.

    Returns ``(outcome, reason)``; ``not_found`` means Yahoo said the symbol
    does not exist, ``unavailable`` means we could not ask (never a block on
    the add). The privacy guard raises from here — callers surface the 403.
    """
    _guard_external_fetch()

    clean = symbol.strip().upper()
    if not clean:
        return QuoteOutcome.unknown, None
    candidates = [clean]
    if not clean.endswith(".TO"):
        candidates.append(clean + ".TO")
    outcomes: list[QuoteOutcome] = []
    reason: str | None = None
    for candidate in candidates:
        try:
            frame = _ticker(candidate).history(period="1d", raise_errors=True)
        except Exception as exc:  # unofficial API may break arbitrarily
            outcome, err = _classify_history_error(exc)
            outcomes.append(outcome)
            reason = err or reason
            continue
        if _history_has_close(frame):
            return QuoteOutcome.ok, None
        reason = f"No public quote found for {candidate}"
        outcomes.append(QuoteOutcome.not_found)
    return _combine_outcomes(outcomes), reason


def fetch_latest_close(symbol: str, today: date | None = None) -> tuple[date, Decimal] | None:
    today = today or date.today()
    closes = fetch_history(symbol, today - timedelta(days=LATEST_LOOKBACK_DAYS), today)
    if not closes:
        return None
    day = max(closes)
    return day, closes[day]


def newest_price(session: Session, security_id: int, on_or_before: date) -> Price | None:
    return session.exec(
        select(Price)
        .where(Price.security_id == security_id, Price.date <= on_or_before)
        .order_by(Price.date.desc())
    ).first()


def previous_close(session: Session, security_id: int, before: date) -> Price | None:
    """Newest cached close STRICTLY before ``before`` — the M129 day-Δ
    baseline: the session the latest row's move is measured against. A
    first-day listing (one cached row) returns None, which the callers render
    as "no delta" rather than a fabricated zero. Cache-only, no fetch
    (docs/07); the sweep's knowledge of prior sessions is server-side, which
    is why this lives here and not in any client arithmetic.
    """
    return session.exec(
        select(Price)
        .where(Price.security_id == security_id, Price.date < before)
        .order_by(Price.date.desc())
    ).first()


def _cache_closes(
    session: Session,
    security: Security,
    closes: dict[date, Decimal],
    *,
    intraday_update: bool = False,
    today: date | None = None,
    fetched_at: datetime | None = None,
) -> int:
    """Insert ONLY missing (security_id, date) rows as yfinance; manual wins.

    ``intraday_update`` (M187, docs/07 §Scheduled price refresh) is the ONE
    opt-in that may ADVANCE an existing row: a close dated ``today`` whose row
    is ``source=yfinance`` moves to the newly fetched value, because the
    scheduled refresh exists precisely to walk today's bar forward. Every
    OTHER date keeps the insert-only rule (a historical bar is a fact the
    provider printed once — a re-fetch must never rewrite the cache), and a
    ``source=manual`` row is never touched on any path: human data wins
    permanently (docs/02, models/prices.py).

    Fetch provenance (Option A, t_bd5ec95f): ``fetched_at`` is the provider-
    response ARRIVAL instant (``last_fetch_at()`` — captured at the fetch
    site, never assigned here), and this is the ONLY site that writes
    ``prices.fetched_at``. Every INSERT is stamped — the provider returned
    those closes in that one response. Among EXISTING rows the stamp is
    refreshed on exactly ONE row: ``max(closes)``, the newest bar, which is
    the row every surface displays. That bounds the request-path fan-out to at
    most one stamp write per fetch instead of per-page-load churn across the
    whole history, and it covers the no-op refresh: the provider was really
    contacted even though the value did not move, so the displayed row's stamp
    MUST advance — a no-op fetch must never render as "never fetched". A
    ``source=manual`` row is never stamped on any path (manual wins, and a
    human value has no provider fetch time). A stamp-only refresh is NOT
    counted in the return value (the count is rows whose PRICE moved) but
    DOES commit, so the evidence survives the caller's session.

    Returns the number of rows written (inserted or advanced).
    """
    rows = {
        row.date: row
        for row in session.exec(select(Price).where(Price.security_id == security.id)).all()
    }
    newest_day = max(closes) if closes else None
    changed = 0
    stamped = False
    for day, price in sorted(closes.items()):
        existing = rows.get(day)
        if existing is not None:
            if (
                intraday_update
                and today is not None
                and day == today
                and existing.source == PriceSource.yfinance
                and existing.price != price
                and fetched_at is not None
            ):
                existing.price = price
                if fetched_at is not None and day == newest_day:
                    existing.fetched_at = fetched_at
                    stamped = True
                session.add(existing)
                changed += 1
            elif (
                existing.source == PriceSource.yfinance
                and day == newest_day
                and fetched_at is not None
                and existing.fetched_at != fetched_at
            ):
                # The no-op re-fetch (the provider answered, the value held):
                # refresh ONLY the displayed row's evidence, nothing else.
                existing.fetched_at = fetched_at
                session.add(existing)
                stamped = True
            continue
        session.add(
            Price(
                user_id=security.user_id,
                security_id=security.id,
                date=day,
                price=price,
                source=PriceSource.yfinance,
                fetched_at=fetched_at,
            )
        )
        changed += 1
    if changed or stamped:
        session.commit()
    return changed


def record_quote_outcome(
    session: Session, security: Security, outcome: QuoteOutcome, reason: str | None
) -> None:
    """Advance the M141 dead-symbol bookkeeping for one fetch (docs/07).

    Three consecutive HARD "quote not found" outcomes set ``dead_symbol``; the
    counter is otherwise left strictly alone — a rate-limit, a timeout or a
    cached read says nothing about the symbol and must not march a live
    security toward the flag. Any successful fetch clears both the counter and
    the flag, so a symbol that starts resolving again (a relisting, a typo the
    user fixed via ``PATCH /securities/{id}``) heals without manual action.

    Commits, because the seams that call this run in background tasks with
    their own session and the flag must survive the request.
    """
    if outcome is QuoteOutcome.ok:
        if security.quote_miss_count or security.dead_symbol or security.dead_symbol_reason:
            security.quote_miss_count = 0
            security.dead_symbol = False
            security.dead_symbol_reason = None
            session.add(security)
            session.commit()
        return
    if outcome is not QuoteOutcome.not_found:
        return
    security.quote_miss_count = (security.quote_miss_count or 0) + 1
    if reason:
        security.dead_symbol_reason = reason
    if security.quote_miss_count >= DEAD_SYMBOL_MISSES:
        security.dead_symbol = True
    session.add(security)
    session.commit()


def _skip_dead_symbol(security: Security, *, probe_dead: bool) -> bool:
    """True when the automatic fan-out must not fetch this row at all (M141).

    ``probe_dead`` is the manual "Try refresh" path — the one affordance that
    stays enabled on a dead symbol, because it is the only way the flag can
    ever be disproved. The automatic sweep, the market-hours fan-out and the
    security-overview auto-refresh all leave it False, which is what stops the
    404 noise (docs/07 §Dead symbols).

    Deliberately a SEPARATE parameter from ``ensure_recent_price``'s
    ``force``: the market-hours fan-out passes ``force=True`` purely as a
    FRESHNESS knob and must still honour the dead-symbol gate. One boolean
    doing both jobs would silently turn that fan-out back into the 404
    machine this flag exists to stop.
    """
    return bool(security.dead_symbol) and not probe_dead


# M175: the release seam lives in `app/services/session_release.py` — the class
# it closes spans the fx client, the LLM batch worker and the notification
# dispatcher too, so it is not a market-data thing. Re-exported here because
# every price/enrichment path (and its docs) names it as
# `market_data.release_before_provider_wait`; the full ledger is docs/07
# §Provider-wait ledger (M175).


def backfill_range(
    session: Session,
    security: Security,
    start_date: date,
    end_date: date,
    *,
    probe_dead: bool = False,
) -> int:
    """Historical daily-close backfill into prices (source=yfinance).

    M141: skips a ``dead_symbol`` row unless ``probe_dead`` (the manual
    "Try refresh"), and books the fetch's verdict either way.
    """
    _reset_fetch_provenance()
    # ...and the verdict that goes with it. Both ride the request context, so
    # both outlive the call that produced them: without this a fetch that
    # never reached yfinance (a test double) would hand this call a PREVIOUS
    # fetch's verdict to book — a stale miss that also commits whatever the
    # caller had pending. The ContextVar default is `unknown` for exactly this
    # reason ("no verdict was reached about the symbol"); the reset restores it
    # for a fetch that publishes nothing.
    reset_quote_verdict()
    if _skip_dead_symbol(security, probe_dead=probe_dead):
        return 0
    closes = fetch_history(security.symbol, start_date, end_date)
    # Option A (t_bd5ec95f): the provider-response arrival instant rides the
    # context from the fetch above — pass it down, never derive it here.
    stamp = last_fetch_at()
    record_quote_outcome(session, security, _last_quote_outcome.get(), _last_quote_reason.get())
    return _cache_closes(session, security, closes, fetched_at=stamp)


def ensure_recent_price(
    session: Session,
    security: Security,
    today: date,
    *,
    force: bool = False,
    probe_dead: bool = False,
    intraday_update: bool = False,
) -> Price | None:
    """Newest price <= today, refreshed via yfinance when missing or stale.

    This is the valuation seam where a network fetch is allowed
    (w2-contract §BackendMarkets 6); failures degrade to the cached row.

    ``force`` is the "Refresh prices" semantics (docs/18): skip the
    ``PRICE_STALE_DAYS`` freshness window so a close dated *before* today is
    always advanced to today. The window stays in force for the opportunistic
    read paths (``compute_positions``, ``security_overview``), which must not
    turn every page load into a yfinance call.

    A close already dated ``today`` is never re-fetched on any request path:
    the daily bar for a security is fetched at most once per calendar day,
    which is what keeps the market-hours background refresh from polling
    yfinance once per positions request. The ONE exception is the explicit
    ``intraday_update`` opt-in (M187, docs/07 §Scheduled price refresh),
    which the scheduled worker passes so a later tick can ADVANCE today's
    bar: without it the day's close freezes at whatever the first page load
    happened to see. The advance itself is ``_cache_closes``' job and moves
    only a ``source=yfinance`` row — a manual close dated today survives any
    tick (docs/07 §Manual price overrides).

    M141: ``probe_dead`` is the manual "Try refresh" — the ONE path that still
    probes a ``dead_symbol`` row, so the flag can be disproved. It is
    deliberately NOT ``force`` (nor ``intraday_update``): both are freshness
    knobs and must still honour the gate (docs/07 §Dead symbols).
    """
    _reset_fetch_provenance()
    # Same reset as `backfill_range`: the verdict this function reads after
    # its fetch must be the one THAT fetch published, never a previous call's.
    reset_quote_verdict()
    price = newest_price(session, security.id, today)
    if price is not None and price.date == today and not intraday_update:
        return price
    if (
        not force
        and price is not None
        and price.date != today
        and (today - price.date).days <= PRICE_STALE_DAYS
    ):
        return price
    if _skip_dead_symbol(security, probe_dead=probe_dead):
        return price
    # M155 (docs/07 §Price-path sessions): `newest_price` above autobegan, so
    # hand the pool connection back before the quote. The release has to sit
    # HERE — between that SELECT and this wait — and cannot be left to the
    # callers: this function does its own read immediately before fetching,
    # and `valuation.compute_positions` calls it once per held security, where
    # each call's SELECT reopens the transaction the previous one left behind.
    #
    # It releases a READ transaction and nothing else. Every caller reaches
    # this point with nothing uncommitted — `_run_ensure_recent_price` (own
    # session), `GET /securities/{id}/overview` and
    # `valuation.compute_positions` are read-only up to here — so the commit
    # costs a positions request at most one per security that actually
    # fetched. A caller that is NOT read-only here is outside this contract:
    # `services/symbol_repair.repair_symbol` is exactly why `backfill_range`
    # releases at its own callers instead of inside itself.
    release_before_provider_wait(session, security)
    latest = fetch_latest_close(security.symbol, today)
    # Option A (t_bd5ec95f): the provider-response arrival instant rides the
    # context from the fetch above — pass it down, never derive it here.
    stamp = last_fetch_at()
    record_quote_outcome(session, security, _last_quote_outcome.get(), _last_quote_reason.get())
    if latest is not None:
        _cache_closes(
            session,
            security,
            {latest[0]: latest[1]},
            intraday_update=intraday_update,
            today=today,
            fetched_at=stamp,
        )
        price = newest_price(session, security.id, today)
    return price


def _dec(value: object) -> Decimal | None:
    """Best-effort Decimal coercion; None for missing/non-finite/non-numeric.

    ``Decimal('NaN')`` and ``Decimal('Infinity')`` PARSE without raising and
    Postgres stores them in a ``Numeric`` column, so finiteness is part of
    "this is a number": a provider that answers a missing number with ``NaN``
    would otherwise land it in ``fundamentals`` / ``etf_holdings.weight_pct``
    (and ``NaN <= 0`` RAISES ``InvalidOperation`` in the issuer-CSV weight
    check). Same reading as a split cell already gets (:func:`_split_ratio`).
    """
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except Exception:  # unofficial API may return anything
        return None
    return number if number.is_finite() else None


def fetch_fundamentals(symbol: str) -> dict[str, object] | None:
    """yfinance research snapshot mapped to the fundamentals columns (M16).

    Returns None on any failure or when the payload maps to no keys — callers
    degrade to the cached row / null block (docs/07), never a 500.

    dividendYield units (pinned by LIVE PROBE against yfinance 1.6.0 from
    inside the api container, 2026-08-26): Yahoo sends PERCENT-shaped values
    unscaled — RY.TO dividendYield=2.49 (≈6.36/286 trailing math) and AAPL
    0.35 (1.05/309.9 = 0.34%). A ≤1→x100 "fraction" rule is therefore WRONG
    here: it would corrupt AAPL's genuine 0.35% into 35%. Pass through as-is.
    """
    _guard_external_fetch()

    clean = symbol.strip().upper()
    if not clean:
        return None
    try:
        info = _ticker(clean).info or {}
    except Exception:  # unofficial API may break arbitrarily
        return None
    # A body that is NOT a dict cannot be mapped, and `.get` on a string /
    # list / number RAISES — a junk body is "no fundamentals", never a 500.
    if not isinstance(info, dict) or not info:
        return None

    numeric_keys = (
        ("trailingEps", "trailing_eps"),
        ("trailingPE", "pe_ratio"),
        ("marketCap", "market_cap"),
        ("beta", "beta"),
        ("fiftyTwoWeekHigh", "fifty_two_week_high"),
        ("fiftyTwoWeekLow", "fifty_two_week_low"),
    )
    string_keys = (("sector", "sector"), ("industry", "industry"), ("country", "country"))
    data: dict[str, object] = {}
    for source_key, column in numeric_keys:
        value = _dec(info.get(source_key))
        if value is not None:
            data[column] = value
    for source_key, column in string_keys:
        raw = info.get(source_key)
        if isinstance(raw, str) and raw.strip():
            data[column] = raw.strip()
    dividend_yield = _dec(info.get("dividendYield"))
    if dividend_yield is not None and dividend_yield > 0:
        data["dividend_yield_pct"] = dividend_yield
    return data or None


def ensure_fundamentals(session: Session, security: Security, today: date) -> Fundamentals | None:
    """Cached fundamentals row, refreshed via yfinance when stale (M16).

    Manual rows win permanently; fresh yfinance rows (age ≤ 7 days) are
    returned as-is. The privacy guard may raise from the refresh path — this
    function does NOT swallow it; callers degrade like they do around
    ensure_recent_price.

    M141: a ``dead_symbol`` row is served from cache only — `Ticker.info` is
    the same 404 fan-out as the price seam (the live probe: APOTEX answers
    `.info` with nothing but ``{"trailingPegRatio": None}``) and burning a call
    per page load on a symbol Yahoo already disowned is exactly the noise the
    flag exists to stop. The flag is only ever set by the PRICE seam, which
    owns the classification.
    """
    row = session.exec(select(Fundamentals).where(Fundamentals.security_id == security.id)).first()
    if row is not None and row.source == FundamentalsSource.manual:
        return row
    if row is not None and (today - row.fetched_at).days <= FUNDAMENTALS_STALE_DAYS:
        return row
    if security.dead_symbol:
        return row
    # M153 (docs/01 §Sessions and the event loop): the SELECT above autobegan,
    # so hand the connection back before the provider wait — this function runs
    # inside the background enrichment task's session, where a parked wait is a
    # parked pool connection.
    release_before_provider_wait(session, security)
    data = fetch_fundamentals(security.symbol)
    if data is None:
        return row
    if row is None:
        row = Fundamentals(
            user_id=security.user_id,
            security_id=security.id,
            fetched_at=today,
            source=FundamentalsSource.yfinance,
            **data,
        )
        session.add(row)
    else:
        for key, value in data.items():
            setattr(row, key, value)
        row.source = FundamentalsSource.yfinance
        row.fetched_at = today
        session.add(row)
    session.commit()
    session.refresh(row)
    return row


def fetch_fund_holdings(symbol: str) -> list[dict[str, object]] | None:
    """yfinance top-holdings composition for a fund, mapped to etf_holdings
    rows (M17). Returns None on any failure, an empty payload, or a ticker
    that is clearly not a fund — the import endpoint degrades to 503.

    Shape (pinned by LIVE PROBE against yfinance 1.6.0 from inside the api
    container, 2026-08-26): ``Ticker(symbol).funds_data.top_holdings`` is a
    pandas DataFrame indexed by Symbol with columns ``Name`` and
    ``Holding Percent``. Holding Percent is a FRACTION of NAV (SPY: NVDA
    0.075494 ≈ 7.55%; VFV.TO: VOO 1.0 = 100%) — multiply by 100 for
    weight_pct. funds_data may be None / raise for non-fund tickers.
    """
    _guard_external_fetch()

    clean = symbol.strip().upper()
    if not clean:
        return None
    try:
        funds_data = _ticker(clean).funds_data
        frame = getattr(funds_data, "top_holdings", None)
        if frame is None or len(frame) == 0:
            return None
        rows: list[dict[str, object]] = []
        for idx, row in frame.iterrows():
            name_raw = row.get("Name")
            weight_raw = row.get("Holding Percent")
            weight = _dec(weight_raw)
            # A weight is a share of NAV: non-blank name, finite POSITIVE
            # number. Non-finite (NaN/Infinity) already reads as None in
            # `_dec`; `<= 0` matches `_parse_issuer_csv`'s rule, so the two
            # providers agree on what a row needs before it may be stored.
            if (
                not isinstance(name_raw, str)
                or not name_raw.strip()
                or weight is None
                or weight <= 0
            ):
                continue  # skip malformed rows rather than fail the import
            entry: dict[str, object] = {
                "holding_name": name_raw.strip(),
                "weight_pct": weight * Decimal("100"),
            }
            symbol_raw = idx if isinstance(idx, str) else row.get("Symbol")
            if isinstance(symbol_raw, str) and symbol_raw.strip():
                entry["holding_symbol"] = symbol_raw.strip().upper()[:20]
            rows.append(entry)
        return rows or None
    except Exception:  # funds_data shape varies across yfinance
        return None  # versions and may break arbitrarily; degrade to None.


# --- Full-holdings issuer CSVs (M147, docs/07 §Fund composition providers) ---

# yfinance `funds_data.top_holdings` only ever answers the TOP SLICE of a
# fund (typically 10-25 rows covering 12-65% of NAV), so a yfinance-only
# import leaves the long tail Unclassified. The second source is the
# issuer's own full-holdings CSV: iShares/BlackRock publishes one per US
# product at ``{product-url}/latest-holdings.csv`` (verified live
# 2026-10-10: IVV answered 509 rows with per-holding Location + Sector).
# The registry maps the ticker we store to the product page stem — the
# slug is part of the URL and a bare product id 404s (verified), so an
# entry is added only with a live-checked pair. Canadian iShares products
# still ship their CSVs behind a JS app with no stable URL (the gaps degrade
# to the yfinance slice; docs/05 §Coverage expectations) — Vanguard's own
# gap is closed by the NEXT section, which reads the issuer's JSON APIs
# instead of hunting for a CSV.
# The registry is a ROW now (M168): `provider_fund_registry` family
# `ishares_us`, keyed by ticker, `endpoint` = the product stem below. The
# literal that used to live here held ONE pair (IVV), so every other iShares
# product answered "no issuer endpoint registered" until somebody edited
# code. Discovery replaces the edit: on a first-encounter miss the ticker is
# resolved against iShares' OWN product screener (LIVE-verified 2026-10-11:
# `GET /us/products/etf-investments` answers a server-rendered table of 526
# rows — `<td class="links"><a href="/us/products/239726/…">IVV</a>` — one
# per US product, ticker unique in the live parse, product id in the URL and
# the ticker as the link text). That IS the fund-id lookup docs/07 asked
# about: it exists, so iShares coverage is automated the same way Vanguard's
# is. A ticker the screener does not name writes a `miss` row and is never
# probed again.
ISHARES_PRODUCT_TABLE_URL = "https://www.ishares.com/us/products/etf-investments"
ISHARES_CSV_SUFFIX = "/latest-holdings.csv"
ISHARES_TABLE_ROW = (
    # The screener's first two cells are both links to the product page: the
    # first holds the TICKER, the second the fund name. Pinned from the live
    # parse 2026-10-11 (526/526 rows matched, 0 duplicate tickers).
    '<td class="links"><a href="(/us/products/(\\d+)/[^"]+)">([^<]+)</a></td>'
)


# The two issuer CATALOGUES are memoised by hand rather than with
# `lru_cache`, because the two answers must stay distinguishable: a failed
# fetch returns None and is NOT cached (the next miss tries again), while a
# successful fetch — even an empty one — is an ANSWER and is. `lru_cache`
# would have pinned a failure as happily as a result, and one flaky morning
# would have retired every iShares product.
_ishares_table_cache: dict[str, str] | None = None
_vanguard_ca_catalogue_cache: dict[str, tuple[str, str | None]] | None = None


def _reset_discovery_cache() -> None:
    """Forget both issuer catalogues (tests; and nothing else needs it)."""
    global _ishares_table_cache, _vanguard_ca_catalogue_cache
    _ishares_table_cache = None
    _vanguard_ca_catalogue_cache = None


def _ishares_product_table() -> dict[str, str] | None:
    """`{TICKER: product stem}` from the issuer's own screener (M168 discovery).

    One request per worker process — the page is ~500 KB, so a successful
    fetch is memoised, not repeated per missed ticker. None means "the
    screener did not answer", which is NOT a verdict: the caller writes no
    row and the next miss asks again. A ticker the ANSWER does not name is a
    different thing entirely, and the caller records it as a miss.
    """
    global _ishares_table_cache
    if _ishares_table_cache is not None:
        return _ishares_table_cache
    _guard_external_fetch()
    import httpx  # pinned runtime dep; imported lazily like yfinance

    try:
        response = httpx.get(
            ISHARES_PRODUCT_TABLE_URL,
            headers={"User-Agent": _ISSUER_USER_AGENT},
            timeout=ISSUER_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        if response.status_code != 200:
            return None
        text = response.text
    except Exception:  # issuer site may break arbitrarily
        return None
    import re as re_mod

    table: dict[str, str] = {}
    for slug, _product_id, ticker in re_mod.findall(ISHARES_TABLE_ROW, text):
        key = ticker.strip().upper()
        if key and key not in table:
            table[key] = f"https://www.ishares.com{slug}"
    _ishares_table_cache = table
    return table


ISSUER_TIMEOUT_SECONDS = 20  # a full CSV is ~80-250 KB; the quote seam's 5 s is too tight
ISSUER_ROW_CAP = 3000  # sanity bound — no fund publishes more; a runaway parse stops here

# iShares "Asset Class" column → our EtfAssetClass (deterministic table,
# verified against the live IVV CSV: Equity / Fixed Income / Cash / Other).
_ISSUER_ASSET_CLASS: dict[str, EtfAssetClass] = {
    "equity": EtfAssetClass.stock,
    "fixed income": EtfAssetClass.bond,
    "cash": EtfAssetClass.cash,
    "currency": EtfAssetClass.cash,
    "alternative": EtfAssetClass.commodity,
    "multi asset": EtfAssetClass.other,
    "other": EtfAssetClass.other,
}

_ISSUER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def fetch_issuer_holdings(symbol: str, run: str | None = None) -> list[dict[str, object]] | None:
    """Full-holdings CSV from the fund's issuer (M147 second source, M168 registry).

    Resolution order (docs/07 §Fund composition providers):

    1. a ``miss`` row for this ticker → None, NO network call. The negative
       cache, so an issuer that answered 404 once is never asked again;
    2. a ``verified`` row → its stored ``endpoint`` is probed;
    3. no row at all → first encounter: the ticker is resolved against the
       issuer's OWN screener (``_ishares_product_table``), which either names
       a product stem — probed, and a live 200 with rows WRITES the registry
       row — or does not, which writes a ``miss`` row.

    A non-404 failure (timeout, 500, an error page) writes NOTHING: it is a
    verdict about the network, not about the ticker, and a transient outage
    must not retire a real fund. Returns the rows in the same shape
    ``fetch_fund_holdings`` produces (plus ``sector`` / ``country`` /
    ``asset_class`` when the CSV states them), or None — the caller falls
    back to yfinance.
    """
    _guard_external_fetch()
    import httpx  # pinned runtime dep; imported lazily like yfinance

    clean = symbol.strip().upper()
    if not clean:
        return None
    row = provider_registry.get(ProviderFundFamily.ishares_us, clean)
    if row is not None and row.status is ProviderFundStatus.miss:
        return None
    stem = row.endpoint if row is not None else None
    if not stem:
        # First encounter — the screener is the fund-id lookup (M168).
        table = _ishares_product_table()
        if table is None:
            return None  # the screener did not answer — no verdict, no row
        stem = table.get(clean)
        if not stem:
            provider_registry.upsert(
                ProviderFundFamily.ishares_us,
                clean,
                status=ProviderFundStatus.miss,
                run=run or "discovery:ishares_screener",
            )
            return None
    try:
        response = httpx.get(
            f"{stem}{ISHARES_CSV_SUFFIX}",
            headers={"User-Agent": _ISSUER_USER_AGENT},
            timeout=ISSUER_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        if response.status_code == 404:
            provider_registry.upsert(
                ProviderFundFamily.ishares_us,
                clean,
                status=ProviderFundStatus.miss,
                run=run or "discovery:ishares_csv",
            )
            return None
        if response.status_code != 200:
            return None  # outage, not a verdict — no row is written
        text = response.text
    except Exception:  # issuer site may break arbitrarily
        return None
    rows = _parse_issuer_csv(text)
    if not rows:
        return None  # a 200 that does not parse proves nothing either way
    if row is None or row.status is not ProviderFundStatus.verified or row.endpoint != stem:
        provider_registry.upsert(
            ProviderFundFamily.ishares_us,
            clean,
            status=ProviderFundStatus.verified,
            endpoint=stem,
            run=run or "discovery:ishares_csv",
        )
    return rows


def _parse_issuer_csv(text: str) -> list[dict[str, object]] | None:
    """Parse an iShares `latest-holdings.csv` body into etf_holdings rows.

    Pure function (docs/07 pinned shape, live-captured 2026-10-10): a
    preamble of fund-level facts, then the ``Ticker,Name,Sector,Asset
    Class,Market Value,Weight (%),…,Location,…`` header row, then quoted
    CSV lines. Weight is a PERCENT of NAV already (8.32 = 8.32%).
    Returns None when no header row is found (an error page, not a CSV)
    or no usable row parses.
    """
    import csv as csv_mod
    from io import StringIO

    lines = text.splitlines()
    header_index = next(
        (i for i, line in enumerate(lines) if line.startswith("Ticker,Name,")), None
    )
    if header_index is None:
        return None
    rows: list[dict[str, object]] = []
    reader = csv_mod.DictReader(StringIO("\n".join(lines[header_index:])))
    for row in reader:
        if len(rows) >= ISSUER_ROW_CAP:
            break
        name = (row.get("Name") or "").strip()
        weight = _dec((row.get("Weight (%)") or "").strip().rstrip("%"))
        if not name or weight is None or weight <= 0:
            continue  # skip malformed rows rather than fail the import
        entry: dict[str, object] = {
            "holding_name": name[:160],
            "weight_pct": weight,
        }
        ticker = (row.get("Ticker") or "").strip()
        if ticker and ticker != "-":
            entry["holding_symbol"] = ticker.upper()[:20]
        sector = (row.get("Sector") or "").strip()
        if sector and sector != "-":
            entry["sector"] = sector[:80]
        location = (row.get("Location") or "").strip()
        if location and location != "-":
            entry["country"] = location[:80]
        asset_class = (row.get("Asset Class") or "").strip().lower()
        if asset_class in _ISSUER_ASSET_CLASS:
            entry["asset_class"] = _ISSUER_ASSET_CLASS[asset_class].value
        rows.append(entry)
    return rows or None


# --- Vanguard full holdings (M147-followup, docs/07 §Fund composition providers) ---
#
# Vanguard publishes no `latest-holdings.csv`: both of its sites are JS apps
# whose data arrives from first-party JSON APIs. Two of them answer with the
# fund's FULL composition over plain HTTP — each verified LIVE 2026-10-11 from
# this host AND from inside the example.internal `api` container (~0.3 s/call):
#
# * US — `GET {profile}/{TICKER}-AdditionalFundData` -> `.holdingDetails`
#   with `equityHoldings` / `fixedIncomeHoldings` / `shortTermReservesHoldings`
#   / `derivativeHoldings` (a fund publishes only the groups it holds — BND has
#   no equity block at all). Rows carry `marketValuePercentage` ("3.94%" — a
#   percent of NAV), `ticker` and `isin`, but NO country column, so geography
#   comes from the ISIN's ISO3166 prefix (`country_for_iso2`). An
#   unregistered ticker answers 404 `{"message": "Fund not found"}`.
# * CA — `POST {gpx}`, GraphQL `FundsHoldingsQuery` -> `.data.borHoldings[0]
#   .holdings.items`, keyed by the product's `portId`, paged at 1500 rows until
#   `lastItemKey` is null. `X-Consumer-ID` is mandatory (400 without it) and an
#   unknown portId answers 200 with EMPTY arrays — emptiness, not the status
#   line, is the failure signal. Rows carry `marketValuePercentage` (a NUMBER,
#   percent of NAV), `gicsSectorDescription` and `bloombergIsoCountry` (the
#   same alpha-2 key space).
#
# Both lookups are registry-gated: a ticker neither registry lists is a miss
# with NO network call (the iShares discipline), and an entry joins a registry
# only after a live 200 with a parseable payload. Rows land through the single
# write path with `source=vanguard` (docs/02).
VANGUARD_US_PROFILE_URL = "https://investor.vanguard.com/irr/funds/profile"

# US-listed Vanguard products are ROWS now (M168): `provider_fund_registry`
# family `vanguard_us` holds a row per ticker the endpoint has ANSWERED for,
# seeded by the 50 literals this used to be (each an HTTP 200 with a
# parseable payload on 2026-10-11). The probe below is parametrized by
# ticker, so a fund nobody hand-listed resolves on first encounter: a
# hitting ticker joins the registry, a 404 writes a `miss` row and is never
# probed again (the `dead_symbol` discipline, docs/07 §Registry discovery).
# LIVE 2026-10-11: VOT, VONE, VTWO and VTIP all answer 200 today and were in
# no literal — they are exactly what discovery picks up.

VANGUARD_CA_GPX_URL = "https://www.vanguard.ca/gpx/graphql"
VANGUARD_CA_CONSUMER_ID = "ca0"  # app config `consumerId` (the gateway 400s without it)

# Canada-listed Vanguard products are ROWS too (M168): `provider_fund_registry`
# family `vanguard_ca`, carrying `port_id` + `venue_suffix`, seeded with the 42
# pairs the literal this replaced held (portIds from the issuer's own product
# pages, tickers + exchanges from its `FundFinderFunds` listing metadata: 39 on
# the Toronto Stock Exchange, 3 on CBOE Canada -> `.NE`).
#
# The suffix is what a fund-LEG ticker gets stored with (a `VCN` leg inside
# VEQT lands as `VCN.TO`), because the exposure engine matches composition
# tickers against the user's securities EXACTLY and Canadian funds are stored
# venue-suffixed (`VCN.TO`, `VBG.NE`).
#
# DISCOVERY (docs/07 §Registry discovery) reads the same two facts the hand
# seed read, straight from the issuer, on a first-encounter miss:
#
#   1. `GET {VANGUARD_CA_PRODUCT_URL}` — the AEM page model's
#      `site-config.portIds`, the issuer's PUBLISHED list of every product
#      portId (54 live on 2026-10-11: the 42 listed ETFs plus 12 unlisted
#      mutual-fund series that carry no exchange ticker and so never resolve
#      to a ticker here);
#   2. `POST {VANGUARD_CA_GPX_URL}` with `VANGUARD_CA_FUND_CATALOGUE_QUERY`
#      over those portIds — one request that answers each fund's `portId`
#      and, per listing, the `Ticker - Canada` identifier and the
#      `stockExchangeMarketIdentifierCode`.
#
# The MIC, not a venue-name guess, is the suffix's source of truth; an MIC
# this table does not know registers the fund with NO suffix (its legs then
# land bare, exactly as an unregistered fund's do today) and
# `fintrackerctl provider-funds set --venue-suffix` corrects it. LIVE
# 2026-10-11 the catalogue reproduces all 42 seeded pairs exactly — same
# portIds, XTSE -> `.TO`, NEOE -> `.NE`.
VANGUARD_CA_PRODUCT_URL = "https://www.vanguard.ca/en/product.model.json"
VANGUARD_CA_MIC_SUFFIX: dict[str, str] = {"XTSE": ".TO", "NEOE": ".NE"}

# The issuer's OWN ticker -> portId lookup, pinned the way the holdings query
# below is: the operation the site's fund finder sends, reduced to the fields
# discovery needs. `$portIds` is REQUIRED by the schema, which is why step 1
# exists — the published portId list is what makes the lookup possible at all.
VANGUARD_CA_FUND_CATALOGUE_QUERY = """
    query FundCatalogue($portIds: [String!]!) {
      funds(portIds: $portIds) {
        portId
        profile {
          portId fundFullName
          listings {
            portId exchange stockExchangeMarketIdentifierCode
            identifiers(altIds: ["Ticker - Canada", "Ticker"]) {
              altId altIdValue altIdCode
            }
          }
        }
      }
    }
"""


def _find_key(payload: object, key: str) -> object:
    """First `key` anywhere in a nested JSON document (the page model's shape
    is AEM components all the way down, so there is no fixed path)."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k == key:
                return v
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def _vanguard_ca_catalogue() -> dict[str, tuple[str, str | None]] | None:
    """`{TICKER: (portId, venue suffix)}` from the issuer's own catalogue.

    Two requests, memoised for the life of the process (one catalogue answers
    every ticker a run can meet, so a burst of first-encounter misses costs
    these two calls once) — but only a SUCCESSFUL fetch is memoised: None
    means "the catalogue did not answer", which is no verdict at all, so the
    next miss asks again instead of inheriting a failure as fact.

    A mutual-fund series has no exchange listing and therefore no ticker
    here — it simply never matches, which is correct: nothing a user stores
    carries its FundServ code as a symbol.
    """
    global _vanguard_ca_catalogue_cache
    if _vanguard_ca_catalogue_cache is not None:
        return _vanguard_ca_catalogue_cache
    _guard_external_fetch()
    import httpx  # pinned runtime dep; imported lazily like yfinance

    headers = {"User-Agent": _ISSUER_USER_AGENT, "Accept": "application/json"}
    try:
        model_response = httpx.get(
            VANGUARD_CA_PRODUCT_URL,
            headers=headers,
            timeout=ISSUER_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        if model_response.status_code != 200:
            return None
        published = _find_key(model_response.json(), "portIds")
        if not isinstance(published, str) or not published.strip():
            return None
        port_ids = [p.strip() for p in published.split(",") if p.strip()]
        catalogue_response = httpx.post(
            VANGUARD_CA_GPX_URL,
            json={
                "operationName": "FundCatalogue",
                "variables": {"portIds": port_ids},
                "query": VANGUARD_CA_FUND_CATALOGUE_QUERY,
            },
            headers={
                **headers,
                "Content-Type": "application/json",
                "X-Consumer-ID": VANGUARD_CA_CONSUMER_ID,
            },
            timeout=VANGUARD_TIMEOUT_SECONDS,
        )
        if catalogue_response.status_code != 200:
            return None
        payload = catalogue_response.json()
    except Exception:  # the issuer site may break arbitrarily
        return None
    funds = payload.get("data", {}).get("funds") if isinstance(payload, dict) else None
    if not isinstance(funds, list):
        return None

    table: dict[str, tuple[str, str | None]] = {}
    for fund in funds:
        if not isinstance(fund, dict):
            continue
        port_id = fund.get("portId")
        profile = fund.get("profile")
        listings = profile.get("listings") if isinstance(profile, dict) else None
        if not port_id or not isinstance(listings, list):
            continue
        # A fund may list on two venues: prefer the senior exchange so the
        # suffix is deterministic rather than API-order dependent.
        best: tuple[int, str, str | None] | None = None
        for listing in listings:
            if not isinstance(listing, dict):
                continue
            ticker = None
            for ident in listing.get("identifiers") or []:
                if (
                    isinstance(ident, dict)
                    and ident.get("altId") in ("Ticker - Canada", "Ticker")
                    and ident.get("altIdValue")
                ):
                    ticker = str(ident["altIdValue"]).strip().upper()
            if not ticker:
                continue
            mic = str(listing.get("stockExchangeMarketIdentifierCode") or "").upper()
            suffix = VANGUARD_CA_MIC_SUFFIX.get(mic)
            priority = 0 if mic == "XTSE" else (1 if suffix else 2)
            if best is None or priority < best[0]:
                best = (priority, ticker, suffix)
        if best is not None and best[1] not in table:
            table[best[1]] = (str(port_id), best[2])
    _vanguard_ca_catalogue_cache = table
    return table


# Pinned verbatim (a whitespace-normalised copy of the document the site's own
# Apollo client sends; `limit: 1500` is a literal in it). `securityTypes` is
# declared but deliberately NOT sent — the gateway then returns every type
# (cash and derivatives included: VCN 215/215 rows summing 99.99999%).
VANGUARD_CA_HOLDINGS_QUERY = """
    query FundsHoldingsQuery($portIds: [String!], $securityTypes: [String!], $lastItemKey: String) {
      funds(portIds: $portIds) {
        profile { fundFullName fundCurrency primarySectorEquityClassification }
      }
      borHoldings(portIds: $portIds) {
        holdings(limit: 1500, securityTypes: $securityTypes, lastItemKey: $lastItemKey) {
          items {
            issuerName securityLongDescription gicsSectorDescription
            icbSectorDescription icbIndustryDescription marketValuePercentage
            sedol1 quantity ticker securityType finalMaturity effectiveDate
            marketValueBaseCurrency bloombergIsoCountry couponRate
          }
          totalHoldings
          lastItemKey
        }
      }
    }
"""

VANGUARD_TIMEOUT_SECONDS = 30  # largest payload seen: 5.1 MB (VT), ~1 s on the prod container
# Sanity bound; the largest group seen is BND's 16,278 bond rows, 3,121 of
# them positive-weight.
VANGUARD_ROW_CAP = 20000
VANGUARD_CA_MAX_PAGES = 8  # 8 x 1500; the largest Canadian fund seen is VIU at 3,752 rows
_VANGUARD_WEIGHT_QUANTUM = Decimal("0.0001")  # etf_holdings.weight_pct is Numeric(7,4)


def _vanguard_weight(raw: object) -> Decimal | None:
    """Percent-of-NAV weight for one Vanguard row, ready for `weight_pct`.

    None when the row cannot be stored at all: the column carries
    `CHECK (weight_pct > 0)` with four decimals, and BOTH payloads publish
    rows that quantize to zero (the US side alone answers thousands of `0.00%`
    rows plus a handful of negatives — measured on VXUS: 6,586 of 8,983). A row
    dropped HERE can never fail the whole import at write time.
    """
    if raw is None:
        return None
    text = str(raw).strip().rstrip("%")
    if not text:
        return None
    weight = _dec(text)
    if weight is None:
        return None
    weight = weight.quantize(_VANGUARD_WEIGHT_QUANTUM)
    if weight <= 0 or weight > 100:
        return None
    return weight


def _vanguard_asset_class(security_type: object, main_type: object = None) -> str:
    """`securityType` (fallback `securityMainType`) -> an EtfAssetClass value.

    Deterministic table over the codes both payloads publish (LIVE census
    2026-10-11: EQ.*/FI.*/MM.*/CT.*/CRNY/DE.*/MF.*): the same mapping the
    iShares CSV gets, so one fund never lands in two asset-class pies.
    """
    code = str(security_type or "").upper()
    main = str(main_type or "").upper()
    if code.startswith("EQ.") or main == "EQ":
        return "stock"
    if code.startswith("FI.") or main == "FI":
        return "bond"
    if code.startswith(("MM.", "CT.")) or code == "CRNY" or main in {"MM", "CT", "CRNY"}:
        return "cash"
    return "other"  # DE.* derivatives, MF.* wrappers, unpublished codes


_VANGUARD_US_GROUPS = (
    "equityHoldings",
    "fixedIncomeHoldings",
    "shortTermReservesHoldings",
    "derivativeHoldings",
)


def _hedge_row(item: dict[str, object]) -> bool:
    """True for a currency-hedge leg (`CT.*`), which must NOT be imported.

    A CAD-hedged fund publishes BOTH sides of every forward and the legs
    cancel (LIVE 2026-10-11: VUS CT +291.7 / -290.7, VBG +301.3 / -300.3).
    `weight_pct > 0` lets us keep only the positive side, which would store a
    390-400% total and make the exposure engine renormalize every real holding
    down to a quarter of the fund. Dropping BOTH sides costs at most ~1% of
    weight (the forwards' net), which lands honestly in Unclassified.
    """
    return str(item.get("securityType") or "").upper().startswith("CT.")


def _vanguard_us_entry(item: dict[str, object]) -> dict[str, object] | None:
    if _hedge_row(item):
        return None
    name = (
        str(item.get("securityLongDescription") or "").strip()
        or str(item.get("securityShortDescription") or "").strip()
    )
    weight = _vanguard_weight(item.get("marketValuePercentage"))
    if not name or weight is None:
        return None
    entry: dict[str, object] = {
        "holding_name": name[:160],
        "weight_pct": weight,
        "asset_class": _vanguard_asset_class(
            item.get("securityType"), item.get("securityMainType")
        ),
    }
    ticker = str(item.get("ticker") or "").strip().upper()
    if ticker:
        entry["holding_symbol"] = ticker[:20]
    security_type = str(item.get("securityType") or "").upper()
    if not security_type.startswith("EQ.ETF"):
        # A wrapper's own domicile is not its geography: fund legs carry no
        # country, so the engine either recurses into the leg's own rows or
        # honestly leaves it Unclassified (docs/05).
        country = country_for_iso2(str(item.get("isin") or "")[:2])
        if country:
            entry["country"] = country
    return entry


def _parse_vanguard_us_payload(payload: object) -> list[dict[str, object]] | None:
    """`{TICKER}-AdditionalFundData` body -> etf_holdings rows.

    Pinned shape (docs/07, LIVE capture 2026-10-11 — VXUS, 4,554,747 bytes):
    a top-level `holdingDetails` dict holding `asOfDate` plus whichever of the
    four holding groups the fund publishes, each row `securityType` /
    `securityMainType` / `securityLongDescription` / `marketValuePercentage` /
    `ticker` / `isin`. A body without `holdingDetails` (an error page) is None
    and the chain falls through.
    """
    if not isinstance(payload, dict):
        return None
    details = payload.get("holdingDetails")
    if not isinstance(details, dict):
        return None
    rows: list[dict[str, object]] = []
    for group in _VANGUARD_US_GROUPS:
        block = details.get(group)
        if not isinstance(block, list):
            continue  # a fund publishes only its own groups (BND has NO equity block)
        for item in block:
            if len(rows) >= VANGUARD_ROW_CAP:
                return rows
            if not isinstance(item, dict):
                continue
            entry = _vanguard_us_entry(item)
            if entry is not None:
                rows.append(entry)
    return rows or None


def _parse_vanguard_ca_items(
    items: object,
    venue_suffix_by_ticker: dict[str, str | None],
) -> list[dict[str, object]] | None:
    """`borHoldings[0].holdings.items` -> etf_holdings rows.

    Pinned shape (docs/07, LIVE capture 2026-10-11 — VCN: 215 rows, weight sum
    99.99999): `securityLongDescription` (fallback `issuerName`),
    `marketValuePercentage` (a NUMBER, percent of NAV), `gicsSectorDescription`
    (null for bonds/cash/fund legs), `bloombergIsoCountry` (alpha-2), plus
    `securityType` and `ticker`. An `EQ.ETF` leg's ticker is stored with the
    venue suffix its registry entry names (`VCN` -> `VCN.TO`) so the exact-match
    look-through finds the user's security; a non-list falls through unchanged.

    The suffix map is an ARGUMENT, not a module constant (M168): it is the
    ``vanguard_ca`` family of ``provider_fund_registry``, loaded ONCE by the
    caller rather than consulted per row — a 3,663-row fund would otherwise
    open 3,663 sessions. It holds VERIFIED rows only, so the map a re-import
    sees is exactly the one the old literal held (coverage tables moved, not
    altered), and a leg whose fund nobody has registered keeps the bare
    ticker it arrives with.
    """
    if not isinstance(items, list):
        return None
    rows: list[dict[str, object]] = []
    for item in items:
        if len(rows) >= VANGUARD_ROW_CAP:
            break
        if not isinstance(item, dict):
            continue
        name = (
            str(item.get("securityLongDescription") or "").strip()
            or str(item.get("issuerName") or "").strip()
        )
        weight = _vanguard_weight(item.get("marketValuePercentage"))
        if not name or weight is None:
            continue
        security_type = str(item.get("securityType") or "").upper()
        if security_type.startswith("CT."):
            continue  # currency-hedge leg — see _hedge_row
        ticker = str(item.get("ticker") or "").strip().upper()
        if security_type == "EQ.ETF":
            suffix = venue_suffix_by_ticker.get(ticker)
            if suffix:
                ticker = f"{ticker}{suffix}"
        entry: dict[str, object] = {
            "holding_name": name[:160],
            "weight_pct": weight,
            "asset_class": _vanguard_asset_class(security_type),
        }
        if ticker:
            entry["holding_symbol"] = ticker[:20]
        if not security_type.startswith("EQ.ETF"):
            country = country_for_iso2(str(item.get("bloombergIsoCountry") or ""))
            if country:
                entry["country"] = country
        sector = str(item.get("gicsSectorDescription") or "").strip()
        if sector:
            entry["sector"] = sector[:80]
        rows.append(entry)
    return rows or None


def _vanguard_ca_key(symbol: str) -> str:
    """`VCN.TO` / `vbG.NE` -> the Canada ticker the CA registry is keyed by.

    Canadian funds are stored BOTH ways (bare `VEQT`/`VUN`, venue-suffixed
    `VCN.TO`/`VBG.NE`), and the suffix is an exchange code — so a trailing
    `.<1-3 alphanumerics>` is dropped before the lookup. A miss costs nothing:
    no network call happens for a key no registry lists.
    """
    head, dot, tail = symbol.rpartition(".")
    if dot and tail and len(tail) <= 3 and tail.isalnum():
        return head
    return symbol


def _fetch_vanguard_us(clean: str, run: str | None = None) -> list[dict[str, object]] | None:
    """The fund-profile JSON for one US ticker, through the M168 registry.

    A ``miss`` row short-circuits with NO network call (the negative cache).
    Otherwise the endpoint — which is parametrized by ticker — is probed: a
    404 is a VERDICT about the ticker and writes a ``miss`` row, a 200 with a
    parseable payload writes the ``verified`` row that joins the registry, and
    anything else (timeout, 500, a body with no ``holdingDetails``) writes
    nothing at all, because an outage must not retire a real fund.
    """
    row = provider_registry.get(ProviderFundFamily.vanguard_us, clean)
    if row is not None and row.status is ProviderFundStatus.miss:
        return None  # negative cache: no repeated 404 probes
    _guard_external_fetch()
    import httpx  # pinned runtime dep; imported lazily like yfinance

    try:
        response = httpx.get(
            f"{VANGUARD_US_PROFILE_URL}/{clean}-AdditionalFundData",
            headers={"User-Agent": _ISSUER_USER_AGENT, "Accept": "application/json"},
            timeout=VANGUARD_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        if response.status_code == 404:
            # LIVE negative control: an unregistered ticker answers
            # 404 {"message": "Fund not found"} — a verdict, so it is recorded.
            provider_registry.upsert(
                ProviderFundFamily.vanguard_us,
                clean,
                status=ProviderFundStatus.miss,
                run=run or "discovery:vanguard_us_probe",
            )
            return None
        if response.status_code != 200:
            return None  # outage, not a verdict — no row is written
        payload = response.json()
    except Exception:  # the issuer site may break arbitrarily
        return None
    rows = _parse_vanguard_us_payload(payload)
    if not rows:
        return None  # a 200 without holdingDetails proves nothing either way
    if row is None or row.status is not ProviderFundStatus.verified:
        provider_registry.upsert(
            ProviderFundFamily.vanguard_us,
            clean,
            status=ProviderFundStatus.verified,
            run=run or "discovery:vanguard_us_probe",
        )
    return rows


def _vanguard_ca_holdings(port_id: str) -> tuple[list[object] | None, bool]:
    """Paged `FundsHoldingsQuery` rows for one portId: ``(rows, verdict)``.

    ``verdict`` is what separates "the gateway said NO such fund" from "the
    gateway did not answer": an unknown portId answers **HTTP 200 with EMPTY
    arrays** (the live negative control docs/07 pins), so emptiness — not the
    status line — is the failure signal and the only outcome that may write a
    ``miss`` row. A non-200 or an exception returns ``(None, False)``: no
    verdict, no registry write, and the chain falls through to yfinance.
    """
    _guard_external_fetch()
    import httpx  # pinned runtime dep; imported lazily like yfinance

    items: list[object] = []
    last_item_key: object = None
    for _page in range(VANGUARD_CA_MAX_PAGES):
        try:
            response = httpx.post(
                VANGUARD_CA_GPX_URL,
                json={
                    "operationName": "FundsHoldingsQuery",
                    "variables": {"portIds": [port_id], "lastItemKey": last_item_key},
                    "query": VANGUARD_CA_HOLDINGS_QUERY,
                },
                headers={
                    "User-Agent": _ISSUER_USER_AGENT,
                    "Content-Type": "application/json",
                    "X-Consumer-ID": VANGUARD_CA_CONSUMER_ID,
                },
                timeout=VANGUARD_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                return None, False
            payload = response.json()
        except Exception:  # the issuer site may break arbitrarily
            return None, False
        blocks = payload.get("data", {}).get("borHoldings") if isinstance(payload, dict) else None
        if not isinstance(blocks, list) or not blocks or not isinstance(blocks[0], dict):
            return [], True  # empty arrays = the gateway's own "no such fund"
        holdings = blocks[0].get("holdings") or {}
        batch = holdings.get("items")
        if not isinstance(batch, list) or not batch:
            return [], True
        items.extend(batch)
        last_item_key = holdings.get("lastItemKey")
        if not last_item_key:
            break
    # Page cap reached with rows in hand: keep them. A truncated weight total
    # lands as Unclassified by the renormalization rule (honest); dropping the
    # fetch would throw away a full first page to no benefit.
    return items, True


def _fetch_vanguard_ca(clean: str, run: str | None = None) -> list[dict[str, object]] | None:
    """Canadian gateway holdings for one stored ticker (M168 registry + discovery).

    Resolution order mirrors the US side: a ``miss`` row answers with no
    network call; a ``verified`` row supplies its stored ``port_id``; no row
    at all means first encounter, where the issuer's own catalogue
    (``_vanguard_ca_catalogue``) resolves the ticker to a portId, the gateway
    is probed ONCE, and only then is the registry row written — ``verified``
    with the portId and venue suffix when rows came back, ``miss`` when the
    gateway's empty arrays said there is no such fund.
    """
    key = _vanguard_ca_key(clean)
    row = provider_registry.get(ProviderFundFamily.vanguard_ca, key)
    if row is not None and row.status is ProviderFundStatus.miss:
        return None  # negative cache: no repeated probes
    port_id: str | None = row.port_id if row is not None else None
    venue_suffix: str | None = row.venue_suffix if row is not None else None
    if not port_id:
        # First encounter — the issuer publishes the ticker -> portId lookup.
        _guard_external_fetch()
        catalogue = _vanguard_ca_catalogue()
        if catalogue is None:
            return None  # the catalogue did not answer — no verdict, no row
        discovered = catalogue.get(key)
        if discovered is None:
            provider_registry.upsert(
                ProviderFundFamily.vanguard_ca,
                key,
                status=ProviderFundStatus.miss,
                run=run or "discovery:vanguard_ca_catalogue",
            )
            return None
        port_id, venue_suffix = discovered

    items, verdict = _vanguard_ca_holdings(port_id)
    if not verdict:
        return None  # the gateway did not answer — nothing proven either way
    if not items:
        provider_registry.upsert(
            ProviderFundFamily.vanguard_ca,
            key,
            status=ProviderFundStatus.miss,
            run=run or "discovery:vanguard_ca_gateway",
        )
        return None
    if row is None or row.status is not ProviderFundStatus.verified or row.port_id != port_id:
        provider_registry.upsert(
            ProviderFundFamily.vanguard_ca,
            key,
            status=ProviderFundStatus.verified,
            port_id=port_id,
            venue_suffix=venue_suffix,
            run=run or "discovery:vanguard_ca_catalogue",
        )
    # The venue-suffix map for fund legs: the family's verified rows, read
    # AFTER the write so the fund just discovered is in its own map.
    verified_ca = provider_registry.load_family(ProviderFundFamily.vanguard_ca)
    suffix_by_ticker = {ticker: entry.venue_suffix for ticker, entry in verified_ca.items()}
    return _parse_vanguard_ca_items(items, suffix_by_ticker)


def fetch_vanguard_holdings(symbol: str) -> list[dict[str, object]] | None:
    """Vanguard's OWN full holdings (M147-followup, docs/07): the US
    fund-profile JSON when the ticker is a registered US product, else the
    Canadian `gpx/graphql` gateway when it is a registered Canadian product
    (a stored `VCN.TO` resolves to the `VCN` registry key).

    Same row shape `fetch_fund_holdings` produces, plus `country` from the
    ISIN prefix / `bloombergIsoCountry` and `sector` from the Canadian
    payload's GICS column. Returns None on any failure or for a ticker no
    registry lists (no network call), and the chain falls through to yfinance.
    """
    clean = symbol.strip().upper()
    if not clean:
        return None
    rows = _fetch_vanguard_us(clean)
    if rows:
        return rows
    return _fetch_vanguard_ca(clean)


def registry_key(family: ProviderFundFamily | str, ticker: str) -> str:
    """The key a STORED ticker resolves to in ``provider_fund_registry``.

    One helper so the CLI, the fetchers and the tests can never disagree
    about which row a ``VCN.TO`` is: the Canada family is keyed by the bare
    Canada ticker (docs/07), every other family by the ticker as stored.
    """
    clean = ticker.strip().upper()
    if ProviderFundFamily(family) is ProviderFundFamily.vanguard_ca:
        return _vanguard_ca_key(clean)
    return clean


def probe_provider(
    family: ProviderFundFamily | str,
    ticker: str,
    *,
    run: str | None = None,
) -> ProviderFundStatus | None:
    """Run ONE provider for this ticker and return the verdict it booked.

    The maintenance seam behind ``fintrackerctl provider-funds verify``
    (M168, docs/07 §Provider registry): it drives the very functions the
    import chain drives, so what the CLI books is exactly what the next
    import will read — there is no second path to drift.

    It does NOT bypass the negative cache: a ``miss`` row still
    short-circuits, which is why the CLI's ``verify`` deletes the row it
    means to re-ask about. The quarterly sweep never needs to — it only ever
    receives ``verified`` rows, which the fetchers probe rather than skip.
    """
    resolved = ProviderFundFamily(family)
    clean = ticker.strip().upper()
    if not clean:
        return None
    if resolved is ProviderFundFamily.vanguard_us:
        _fetch_vanguard_us(clean, run=run)
    elif resolved is ProviderFundFamily.vanguard_ca:
        _fetch_vanguard_ca(clean, run=run)
    elif resolved is ProviderFundFamily.ishares_us:
        fetch_issuer_holdings(clean, run=run)
    else:
        return None  # classification rows have no endpoint to probe
    row = provider_registry.get(resolved, registry_key(resolved, clean))
    return row.status if row is not None else None


def reverify_provider_row(
    row: ProviderFundRegistry,
    *,
    run: str | None = None,
) -> ProviderFundStatus | None:
    """Re-probe ONE registry row — the quarterly sweep's single-row action.

    Only ever handed ``verified`` rows (``provider_registry.stale``), which
    the fetchers PROBE rather than short-circuit, so nothing is deleted
    first: no verdict means no write, and an outage during a sweep can never
    cost a row its coverage. A ``static_sector`` / ``fund_sector_mix`` row
    has no endpoint at all and answers None untouched — those two families
    are hand-curated classification data, not issuer endpoints (docs/07).
    """
    if row.family in (ProviderFundFamily.static_sector, ProviderFundFamily.fund_sector_mix):
        return None
    return probe_provider(row.family, row.ticker, run=run or "cli:reverify")


def fetch_fund_composition(
    symbol: str,
) -> tuple[EtfHoldingSource, list[dict[str, object]]] | None:
    """The provider chain for one fund's composition: the issuer's FULL
    holdings CSV when the ticker is registered (M147), else Vanguard's own
    FULL holdings for a registered US/Canadian product (M147-followup), else
    yfinance's top-holdings slice. None when every probe fails (callers
    degrade — 503 from the endpoint, a log from the background task). The
    privacy guard may raise from any fetch.

    The bare symbol is always probed FIRST; only when it answers nothing
    does the chain retry the ``.TO`` form (the prod seed caught this:
    Canadian funds are commonly stored BARE — VEQT/VUN/XSP/XGRO — while
    yfinance answers only ``VEQT.TO``; the same suffix step
    ``resolve_symbol`` takes, docs/07). The stored row is never rewritten
    by the probe — only the composition comes from the suffixed listing.
    """
    rows = fetch_issuer_holdings(symbol)
    if rows:
        return (EtfHoldingSource.issuer, rows)
    rows = fetch_vanguard_holdings(symbol)
    if rows:
        return (EtfHoldingSource.vanguard, rows)
    rows = fetch_fund_holdings(symbol)
    if rows:
        return (EtfHoldingSource.yfinance, rows)
    clean = symbol.strip().upper()
    if clean and "." not in clean:
        rows = fetch_fund_holdings(f"{clean}.TO")
        if rows:
            return (EtfHoldingSource.yfinance, rows)
    return None


# --- M154: bare-vs-suffixed symbol matching (docs/05 §Fund composition,
# docs/07 §Fund composition providers) ---------------------------------------
#
# A provider publishes holding tickers BARE (`005930`, `HSBA`, `9984`,
# `LRCX`) while the user's own rows are frequently EXCHANGE-SUFFIXED
# (`005930.KS`, `HSBA.L`, `9984.T`), because `lookup_symbol` resolves a bare
# ticker to the canonical listing when the row is created. Measured on prod
# (2026-10-11): 17,530 of the demo user's non-NULL holding symbols carry no
# dot, while 825 of its 1,496 securities are suffixed. Matching one
# spelling against the other by the RAW string is what left
# `holding_security_id` NULL on 86.5% of the rows and made the deepening
# loop INSERT rows that already existed — 231 caught `uq_securities_symbol`
# violations in a single refresh run, each a wasted provider call plus a
# failed INSERT.
#
# Only KNOWN venue suffixes are stripped — the same idea
# `company_country_map.country_for_symbol` applies to its map keys, widened
# here to every suffix prod's `securities.symbol` carried when this was
# fixed (37 observed, plus the 20 that map already knew). The list is
# deliberately venue-only: warrants, units, rights and PREFERRED share
# suffixes (`.WS`, `.U`, `.RT`, `.PR` …) are absent because those are
# different instruments that must never inherit the common share's row.
# An unknown suffix simply offers no stem, so the worst a new venue can do
# is not link — the exact spelling still matches, which is all it did
# before M154.
EXCHANGE_SUFFIXES = frozenset(
    {
        ".AE",
        ".AS",
        ".AT",
        ".AX",
        ".BO",
        ".BR",
        ".CL",
        ".CN",
        ".CO",
        ".DE",
        ".F",
        ".HE",
        ".HK",
        ".IL",
        ".IS",
        ".JO",
        ".KL",
        ".KQ",
        ".KS",
        ".KW",
        ".L",
        ".MC",
        ".MI",
        ".MX",
        ".NE",
        ".PA",
        ".QA",
        ".QC",
        ".SA",
        ".SI",
        ".SR",
        ".SS",
        ".ST",
        ".SW",
        ".SZ",
        ".T",
        ".TA",
        ".TO",
        ".TW",
        ".TWO",
        ".V",
        ".VX",
        ".XA",
        ".XC",
        ".XD",
    }
)

#: Longest first, so `.TWO` can never be read as `.TW` + "O".
_SUFFIX_BY_LENGTH = tuple(sorted(EXCHANGE_SUFFIXES, key=len, reverse=True))


def symbol_stem(symbol: str) -> str:
    """The provider-facing spelling of a stored ticker: uppercased, with a
    KNOWN exchange suffix removed (`005930.KS` → `005930`, `RY.TO` → `RY`).

    A US share class keeps its dot (`BRK.B` — `.B` is not a venue) and an
    empty input stays empty.
    """
    clean = symbol.strip().upper()
    for suffix in _SUFFIX_BY_LENGTH:
        if clean.endswith(suffix) and len(clean) > len(suffix):
            return clean[: -len(suffix)]
    return clean


def security_symbol_index(securities: Iterable[Security]) -> dict[str, Security]:
    """THE shared symbol→security index every fund-composition seam matches
    through (M154): `import_fund_composition`, `ensure_sub_fund_composition`
    and `api/securities.update_holdings`. Two rules, both load-bearing:

    1. the EXACT spelling always wins — `VUN` answers for the bare `VUN`
       row even when a `VUN.TO` twin exists (the CSV import's twin rule,
       docs/05);
    2. a stored `005930.KS` also answers for the bare `005930`, but ONLY
       when that stem is unambiguous. Two listings sharing a stem
       (`005930.KS` on the KSC and `005930.KQ` on the KOSDAQ) never link by
       guesswork: the index carries no stem entry for them at all, and the
       deepening loop lets `lookup_symbol`'s answer pick the listing.

    Ties resolve to the lowest id (input order decides), matching the
    `setdefault` behaviour this replaces — pass rows `order_by(Security.id)`.
    """
    index: dict[str, Security] = {}
    first_of_stem: dict[str, Security] = {}
    rows_per_stem: dict[str, int] = {}
    for security in securities:
        key = (security.symbol or "").strip().upper()
        if not key:
            continue
        index.setdefault(key, security)
        stem = symbol_stem(key)
        if not stem:
            continue
        rows_per_stem[stem] = rows_per_stem.get(stem, 0) + 1
        first_of_stem.setdefault(stem, security)
    for stem, owner in first_of_stem.items():
        # `stem not in index`: when the stem IS a stored spelling (its own or
        # another row's) that exact entry already answers for it — a stem
        # entry may never overwrite an exact one.
        if rows_per_stem[stem] == 1 and stem not in index:
            index[stem] = owner
    return index


def import_fund_composition(session: Session, fund: Security) -> list[EtfHolding] | None:
    """THE single provider→``etf_holdings`` write path (M17 + M147, docs/05).

    Fetches the fund's composition through ``fetch_fund_composition``,
    deletes the fund's provider-sourced rows (``source != manual`` —
    manual rows are permanent), inserts the fresh set, links
    ``holding_security_id`` where a ticker resolves to one of the user's
    securities through ``security_symbol_index`` (M154: the provider's
    BARE ``005930`` links to the user's ``005930.KS``), and repairs
    ``kind=stock`` to ``kind=etf`` when the
    provider just proved the row is a fund (import-created rows often
    carry the stock default — see ``trades._resolve_security``).

    Returns the rows written, or None when no provider answered. The
    privacy guard's 403 passes through uncaught; commits on success so
    background callers do not have to.
    """
    composition = fetch_fund_composition(fund.symbol)
    if composition is None:
        return None
    source, entries = composition
    for row in session.exec(
        select(EtfHolding).where(
            EtfHolding.security_id == fund.id,
            EtfHolding.source != EtfHoldingSource.manual,
        )
    ).all():
        session.delete(row)
    # M154: ONE index for every matching seam — exact spelling first, then a
    # known venue suffix stripped for the bare-stem fallback, so the
    # provider's `005930` finds the user's `005930.KS` (docs/05 §Fund
    # composition). Ordered by id so first-wins ties are deterministic.
    index = security_symbol_index(
        session.exec(
            select(Security).where(Security.user_id == fund.user_id).order_by(Security.id)
        ).all()
    )
    written: list[EtfHolding] = []
    for entry in entries:
        sym = entry.get("holding_symbol")
        matched = index.get(str(sym).strip().upper()) if sym else None
        row = EtfHolding(
            user_id=fund.user_id,
            security_id=fund.id,
            holding_security_id=matched.id if matched is not None else None,
            holding_symbol=sym,
            holding_name=str(entry["holding_name"]),
            asset_class=EtfAssetClass(str(entry.get("asset_class") or "other")),
            region=entry.get("region"),
            country=entry.get("country"),
            sector=entry.get("sector"),
            weight_pct=Decimal(str(entry["weight_pct"])),
            source=source,
        )
        session.add(row)
        written.append(row)
    if fund.kind == SecurityKind.stock:
        logging.getLogger(__name__).info(
            "Provider returned fund holdings for %s — repairing kind stock → etf",
            fund.symbol,
        )
        fund.kind = SecurityKind.etf
        session.add(fund)
    session.commit()
    return written


SUB_FUND_ENRICHMENT_CAP = 12

# M147-followup: how many DISTINCT holding tickers the deepening loop may
# probe for ONE fund. The enrichment cap alone no longer bounds the work:
# a full-holdings provider hands this function thousands of tickers (live:
# VXUS 2,283, VIU 3,663) and every non-fund ticker it RESOLVES creates a
# linking security without counting toward the 12 — so the loop walked the
# whole fund (measured on prod: 1,458 securities created for one tenant and
# minutes of yfinance fan-out per import). Providers publish weight-first,
# so the first 60 probes cover the heaviest rows — every fund-of-fund leg
# in production sits in the first four — and the rest are micro-caps whose
# rows already carry their own country/sector.
SUB_FUND_PROBE_CAP = 60


def ensure_sub_fund_composition(
    session: Session,
    fund: Security,
    symbols: list[str],
    max_sub_funds: int = SUB_FUND_ENRICHMENT_CAP,
) -> int:
    """Eagerly load composition + fundamentals for a fund's sub-fund
    holdings (M19, docs/05): the recursive look-through needs each inner
    fund's rows, so a holdings import/PUT queues this in the background
    with the loaded fund and the tickers it just wrote -- the caller passes
    both because a background task's own session cannot rely on seeing the
    request's rows (same discipline as the price-backfill task).

    For every distinct ticker — bounded BOTH by ``max_sub_funds``
    enrichments and by ``SUB_FUND_PROBE_CAP`` probes (docs/07):
    resolve the user's security with that symbol -- creating one via
    lookup_symbol when the lookup reports a fund kind -- then fetch and
    insert yfinance composition rows when the sub-fund has NONE yet
    (existing rows are never touched), then ensure its fundamentals row.
    The triggering fund's own rows are never modified. The privacy guard
    may raise from any fetch -- callers wrap the whole call (background
    task degrades silently, docs/07). Returns the number of sub-funds
    enriched.

    M154 (docs/07 § Symbol matching): resolution goes through
    ``security_symbol_index``, so a BARE probe finds the user's
    exchange-suffixed row (``005930`` → ``005930.KS``) with NO provider
    call, and when ``lookup_symbol`` is the one that names the listing its
    resolved row is REUSED rather than re-INSERTed — that INSERT was the
    `uq_securities_symbol` violation an older comment here mislabelled a
    concurrent race. A stem shared by two listings (``005930.KS`` +
    ``005930.KQ``) is resolved by the lookup, never by a guess.

    M153 (docs/01 § Sessions and the event loop): this function OWNS the
    transaction it runs in and releases it — ``release_before_provider_wait``
    — before every provider wait, so a deepening run never parks a pool
    connection `idle in transaction` the way M152's seed script did. That is
    safe for BOTH callers, and they are the only two there are
    (``fund_enrichment._run_enrichment`` and ``api/securities._run_deepen``):
    each is a FastAPI BackgroundTask that opened its own session, so there is
    no request's partial work on this session to commit early or roll back.
    Every write the loop makes already commits itself, so a release here can
    only ever be releasing a read.
    """
    user_id = fund.user_id
    own_symbol = fund.symbol.upper()
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = (raw or "").upper()
        if symbol and symbol != own_symbol and symbol not in seen:
            seen.add(symbol)
            ordered.append(symbol)

    # M154: the shared index — exact spelling first, then an unambiguous
    # bare stem — replaces the raw-spelling map, so the bare probe `005930`
    # FINDS the stored `005930.KS` instead of resolving it over the network
    # and re-INSERTing the row it then collided with.
    index = security_symbol_index(
        session.exec(
            select(Security).where(Security.user_id == user_id).order_by(Security.id)
        ).all()
    )
    # The map build above autobegan; hand the connection back before the
    # loop's first provider wait.
    release_before_provider_wait(session)

    enriched = 0
    touched_fund_ids: list[int] = [fund.id]  # track funds to backfill
    for probe_index, symbol in enumerate(ordered):
        if enriched >= max_sub_funds or probe_index >= SUB_FUND_PROBE_CAP:
            break
        sub = index.get(symbol)
        created = False
        if sub is None:
            # The previous iteration ends on a write that commits, but not
            # every branch does — release before this wait regardless.
            release_before_provider_wait(session)
            info = lookup_symbol(symbol)  # guarded; raises under privacy mode
            kind = info.get("kind") if info else None
            if not isinstance(kind, SecurityKind):
                continue  # unknown ticker -- stays unlinked
            # M154: the RESOLVED spelling is the row's identity, not the
            # probe's. A row that already answers for it is REUSED, never
            # re-INSERTed — that INSERT was the `uq_securities_symbol`
            # violation the old comment here mislabelled a concurrent race
            # (the constraint was always right; the lookup was wrong).
            resolved = str(info["symbol"]).strip().upper()
            sub = index.get(resolved)
            if sub is None:
                sub = Security(
                    user_id=user_id,
                    symbol=str(info["symbol"]),
                    name=str(info["name"]),
                    kind=kind,
                    listing_currency=str(info["listing_currency"]),
                    exchange=str(info["exchange"]),
                )
                session.add(sub)
                try:
                    session.commit()
                    session.refresh(sub)
                except IntegrityError:
                    # A GENUINE race: another task committed this listing
                    # between the map build and our INSERT. Roll back and
                    # re-query by the RESOLVED spelling — the probe's bare
                    # one is exactly what made the old recovery find
                    # nothing and skip the sub-fund silently.
                    session.rollback()
                    index = security_symbol_index(
                        session.exec(
                            select(Security)
                            .where(Security.user_id == user_id)
                            .order_by(Security.id)
                        ).all()
                    )
                    sub = index.get(resolved)
                    if sub is None:
                        continue  # shouldn't happen, but defensive
                else:
                    created = True
            # The probe's spelling now resolves directly — including when
            # the resolver disambiguated a shared stem (`005930` →
            # `005930.KS`) — which is what the backlink below reads.
            index.setdefault(symbol, sub)
            index.setdefault(resolved, sub)
        if sub.kind not in _FUND_KINDS_LOOKUP:
            continue  # individual stock -- created for linking, but no sub-holdings to fetch
        fetched = False
        has_rows = session.exec(
            select(EtfHolding.id).where(EtfHolding.security_id == sub.id).limit(1)
        ).first()
        if has_rows is None:
            # M147: the sub-fund runs the SAME provider chain as the top
            # fund (issuer full CSV first, yfinance top-slice + stored-bare
            # .TO retry second) — one write path, one manual-wins rule, one
            # kind repair. The guard may raise under privacy mode.
            # M153: `has_rows` above autobegan, and the provider chain's
            # FIRST act is a network fetch — release before it. The write
            # path's own delete+insert+commit stays one transaction; this
            # runs before any of it exists.
            release_before_provider_wait(session, sub)
            written = import_fund_composition(session, sub)
            if written is not None:
                fetched = True
        touched_fund_ids.append(sub.id)
        try:
            # `ensure_fundamentals` releases its own read before its fetch
            # (M153); nothing to do here.
            ensure_fundamentals(session, sub, date.today())
        except Exception:  # - guard/network noise degrades silently
            logging.getLogger(__name__).warning(
                "Fundamentals enrichment failed for %s; continuing", sub.symbol
            )
        # Backfill price history for newly created securities
        if created:
            try:
                # M153: whichever branch ran above may have left a read
                # transaction open (`ensure_fundamentals` returns early on a
                # cached row), and `backfill_range` waits before it writes.
                release_before_provider_wait(session, sub)
                backfill_range(session, sub, date.today() - timedelta(days=365), date.today())
            except Exception:  # - network noise degrades silently
                logging.getLogger(__name__).warning(
                    "Price history backfill failed for %s; continuing", sub.symbol
                )
        if created or fetched:
            enriched += 1

    # Backfill holding_security_id on touched funds: after creating sub-fund
    # securities, update holdings where we can now resolve the symbol.
    # M154: the same index, so a BARE holding symbol (`005930`) also finds
    # the suffixed row (`005930.KS`) it never used to match — including the
    # spelling the resolver just disambiguated above.
    for holding in session.exec(
        select(EtfHolding).where(EtfHolding.security_id.in_(touched_fund_ids))
    ).all():
        if holding.holding_security_id is None and holding.holding_symbol:
            sec = index.get(holding.holding_symbol.strip().upper())
            if sec is not None:
                holding.holding_security_id = sec.id
                session.add(holding)
    session.commit()

    return enriched


def fetch_dividend_history(symbol: str) -> list[dict[str, object]] | None:
    """yfinance dividend history: list of {date, amount} sorted newest-first.

    Returns None when the provider cannot establish a public quote; a true,
    quote-verified no-dividend history returns ``[]``. The M141 verdict is
    published on the current context so the refresh writer can preserve cached
    events and advance dead-symbol bookkeeping for a hard Yahoo miss.

    Each amount is the per-share dividend in the security's listing currency.
    """
    _guard_external_fetch()

    clean = symbol.strip().upper()
    if not clean:
        _last_quote_outcome.set(QuoteOutcome.unknown)
        _last_quote_reason.set(None)
        return None
    try:
        divs = _ticker(clean).dividends
    except Exception as exc:  # unofficial API may break arbitrarily
        outcome, reason = _classify_history_error(exc)
        _last_quote_outcome.set(outcome)
        _last_quote_reason.set(reason)
        return None
    if divs is None or divs.empty:
        # Yahoo returns the same empty dividend frame for a listing that paid
        # no dividends and for ``Quote not found``. Only a positive quote
        # probe makes this a destructive, successful empty refresh.
        outcome, reason = probe_quote(clean)
        _last_quote_outcome.set(outcome)
        _last_quote_reason.set(reason)
        return [] if outcome is QuoteOutcome.ok else None
    try:
        history = [
            {
                "date": d.date().isoformat(),
                "amount": str(Decimal(str(v)).quantize(Decimal("0.000001"))),
            }
            for d, v in divs.items()
        ][::-1]  # newest first
    except Exception:  # malformed provider body is unavailable, not empty
        _last_quote_outcome.set(QuoteOutcome.unavailable)
        _last_quote_reason.set(f"Invalid dividend history for {clean}")
        return None
    _last_quote_outcome.set(QuoteOutcome.ok)
    _last_quote_reason.set(None)
    return history

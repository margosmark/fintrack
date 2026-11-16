"""Bank of Canada Valet client + fx_rates cache policy (docs/07).

Discipline:
- Per-date lookups hit the cache first; a TRUE miss (no cached rate on or
  before the target date) triggers ONE Valet series fetch — 10 s timeout,
  3 retries with exponential backoff (0.5 s base).
- Fetched rows are cached forever as source=boc_valet; existing rows of any
  source are never overwritten (manual rows fill gaps only, docs/07).
- Source split (docs/07): boc_valet = fetched authority, permanent;
  manual = user-entered gap-fill via POST /fx, permanent — a later fetch
  never replaces it. Only EMPTY (base, quote, date) dates are ever written
  by either side, and fx_rates itself is NEVER seeded (no demo rows).
- Retries exhausted -> fall back to manual gap-fill rates; failing that, an
  {"code": "fx_unavailable"} error envelope — a missing rate never becomes a
  5xx.
- Range queries backfill the whole observed span in ONE Valet series call.
- Scheduled refresh (M150, docs/07): `services/fx_refresh.py` keeps the book
  current on a timer through `refresh_pair` — the fetch still lives HERE, so
  this module remains the only place that talks to Valet (AGENTS §2).
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from datetime import date, timedelta
from decimal import Decimal

import requests
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.errors import api_error
from app.models.fx_rates import FxRate, FxSource
from app.services.session_release import release_before_provider_wait

TWO_PLACES = Decimal("0.01")


def cached_cad_amount(
    session: Session, currency: str, value_date: date, amount: Decimal
) -> Decimal | None:
    """Convert using only the exact transaction-date FX cache row.

    CAD is already the reporting currency. Missing foreign-currency rows stay
    unresolved; this helper never calls the fetch-on-miss path.
    """
    currency = currency.upper()
    if currency == "CAD":
        return amount.quantize(TWO_PLACES)
    rate = session.exec(
        select(FxRate).where(
            FxRate.base == currency,
            FxRate.quote == "CAD",
            FxRate.date == value_date,
        )
    ).first()
    if rate is None:
        return None
    return (amount * rate.rate).quantize(TWO_PLACES)


# BoC Valet's data endpoint is /valet/observations (live-probed 200 OK
# 2026-09-24); /valet/rates returns 404 — the original W2 URL never worked
# and was masked by the (since-removed) demo seed rows.
VALET_URL = "https://www.bankofcanada.ca/valet/observations/FX{pair}/json"
TIMEOUT_SECONDS = 10
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5
# Weekends carry no BoC observations, so a single-date miss pulls a short span.
FETCH_LOOKBACK_DAYS = 10

# docs/07 "Publication calendar & carry-forward": BoC publishes business days
# only, so an exact-date consumer resolves to the newest published row on or
# before the target — but never more than this many days back. A wider gap is
# genuinely missing data and keeps its documented error, never silent wrong
# math. This is ALSO the staleness bound the rate-book status chip reports
# (M150): past it a rate is no longer carried forward, it is simply missing.
# Canonical home: `tax/acb.py` aliases this name, so the two cannot drift.
CARRY_FORWARD_MAX_DAYS = 7

# Curated currencies tracked by the Currency Tracker board (docs/15).
#
# MEMBERSHIP RULE (M151): a base is here only because Bank of Canada
# publishes a LIVE `FX{base}CAD` daily series for it — not because it is a
# major currency. That rule is what keeps the scheduled refresh honest: a
# base with no series would make `pairs_behind` call a 404 on every tick for
# a row `GET /fx/board` can never show (docs/07 §Curated board membership).
#
# DKK was the 18th base and is deliberately NOT here. LIVE-PROBED 2026-10-11:
#   - `FXDKKCAD` and `FXDKK100CAD` both answer 404 ("Series ... not found"),
#     while `FXEURCAD` answers 200;
#   - `/valet/lists/series/json` (16,170 series) has NO DKK member of the
#     81-strong `FX*CAD` family — the only DKK series BoC ever published are
#     `IEXE0301` (DKK_NOON, last 2017-04-28), `IEXE0301.CL` (DKK_CLOSE,
#     2017-04-28), `IEXM0301` (monthly, 2017-04-01) and `IEXA0301` (annual,
#     2016-01-01) — all discontinued a decade ago;
#   - every one of the 17 bases below answered 200 with a 2026-10-09
#     observation over the trailing 14 days.
# So there is no series to repoint at: dropping the base IS the fix.
#
# DKK cash legs keep working — this constant is only read by the board
# (`GET /fx/board`) and the refresh worker. The pair-explicit paths
# (`GET /fx`, `POST /fx` manual gap-fill, `latest_rate`, `get_rates_between`)
# never consult it, and a tenant whose only foreign money is DKK still gets
# `FX rates missing` from `GET /fx/status` (docs/07 §Rate-book status).
# CAD itself is excluded: its rate would be exactly 1.00.
CURATED_FX_BASES: tuple[str, ...] = (
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "CHF",
    "CNY",
    "AUD",
    "NZD",
    "HKD",
    "SGD",
    "INR",
    "MXN",
    "BRL",
    "ZAR",
    "KRW",
    "NOK",
    "SEK",
)


class ValetUnavailable(Exception):
    """All retries exhausted (docs/07: caller falls back to manual rates)."""


# Privacy lockdown (docs/11 step 6): when the requesting user has
# privacy_mode=true, NO BoC Valet fetch may leave the machine. The flag rides
# the request context (bound per-request in deps.get_current_user), so every
# current and future fetch call site inherits the guard without passing flags.
_privacy_lock: ContextVar[bool] = ContextVar("fx_privacy_lock", default=False)


def bind_privacy_lock(enabled: bool) -> None:
    """Bind the requesting user's privacy_mode to this request context."""
    _privacy_lock.set(enabled)


def privacy_locked() -> bool:
    """True when the active request has privacy_mode on (no BoC fetches)."""
    return _privacy_lock.get()


def _guard_external_fetch() -> None:
    """The single Valet seam guard — cache reads never reach it."""
    if _privacy_lock.get():
        raise api_error(
            403,
            "Privacy mode blocks Bank of Canada Valet fetches; "
            "cached and manual rates keep working",
            "privacy_mode_blocks_external_api",
        )


def _normalize(base: str, quote: str) -> tuple[str, str]:
    return base.strip().upper(), quote.strip().upper()


def _pair(base: str, quote: str) -> str:
    return f"{base}{quote}"


def _parse_observations(payload: dict, series: str) -> dict[date, Decimal]:
    """`series` is the full BoC series name (e.g. FXUSDCAD) — the observation
    key in the payload, which matches the URL series segment (live-probed
    2026-09-24); the bare pair (USDCAD) is NOT a key in the response."""
    rates: dict[date, Decimal] = {}
    for obs in payload.get("observations", []):
        entry = obs.get(series) or {}
        value = entry.get("v")
        if value:
            rates[date.fromisoformat(obs["d"])] = Decimal(str(value))
    return rates


def fetch_valet_series(
    base: str, quote: str, start_date: date, end_date: date
) -> dict[date, Decimal]:
    """ONE Valet call covering [start_date, end_date]; raises when exhausted."""
    _guard_external_fetch()
    base, quote = _normalize(base, quote)
    pair = _pair(base, quote)
    series = f"FX{pair}"  # BoC daily-rates series name = the observation key
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(
                VALET_URL.format(pair=pair),
                params={
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
                timeout=TIMEOUT_SECONDS,
            )
            # Transient statuses retry like network errors (429 is retryable).
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.RequestException(f"HTTP {response.status_code}")
            response.raise_for_status()
            return _parse_observations(response.json(), series)
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE_SECONDS * 2**attempt)
    raise ValetUnavailable(str(last_exc)) from last_exc


def _get_exact(session: Session, base: str, quote: str, day: date) -> FxRate | None:
    return session.exec(
        select(FxRate).where(FxRate.base == base, FxRate.quote == quote, FxRate.date == day)
    ).first()


def _store_fetched(
    session: Session, base: str, quote: str, fetched: dict[date, Decimal], *, commit: bool = True
) -> int:
    """Cache boc_valet rows forever — into EMPTY dates only (docs/07).

    Existing rows of any source are kept: boc_valet stays authoritative and
    manual gap-fills are user data the fetch never clobbers. There is no
    demo/seeded source — nothing but this function and POST /fx writes
    `fx_rates`, and neither ever overwrites a row.

    `commit=False` flushes instead: the ACB seam runs inside the CALLER's
    transaction (docs/02 — `rebuild_user_pools` never commits), so a mid-sweep
    `session.commit()` would commit a half-written trade write path. Read
    callers keep the default so a fetched row survives the request.

    Returns the number of rows added, so the scheduled refresh (M150) can
    report what a tick actually changed — a re-run of an already-cached
    window returns 0 and writes nothing.
    """
    added = 0
    for day, rate in fetched.items():
        if _get_exact(session, base, quote, day) is None:
            session.add(
                FxRate(base=base, quote=quote, rate=rate, date=day, source=FxSource.boc_valet)
            )
            added += 1
    if added:
        if commit:
            session.commit()
        else:
            session.flush()
    return added


def latest_rate(
    session: Session, base: str, quote: str, on_or_before: date, *, allow_fetch: bool = True
) -> FxRate | None:
    """Newest cached rate on or before on_or_before; ONE fetch on a true miss.

    Returns None only when no usable row exists and fetching failed or was
    disabled. This is the market-data seam where fetch-on-miss is allowed
    (w2-contract §BackendMarkets 6) — ingestion stays cache-only (docs/03).
    """
    base, quote = _normalize(base, quote)

    def newest() -> FxRate | None:
        return session.exec(
            select(FxRate)
            .where(FxRate.base == base, FxRate.quote == quote, FxRate.date <= on_or_before)
            .order_by(FxRate.date.desc())
        ).first()

    row = newest()
    if row is not None or not allow_fetch:
        return row
    # M175 (docs/07 §Provider-wait ledger): `newest()` above autobegan, and the
    # next act is a Valet series fetch (10 s timeout, 3 retries) — the exact
    # read-then-wait shape M153 fixed on the yfinance seams. It matters here
    # because this is a PER-POSITION call: `valuation.compute_positions` asks
    # for a rate on every foreign-listing row, so a cold cache used to park the
    # request session once per holding. Every caller is read-only up to this
    # point (valuation, `api/accounts._build_public`, `GET /fx`) and the rows
    # this fetch writes commit themselves in `_store_fetched`, so the release
    # commits a read transaction and nothing of the caller's.
    release_before_provider_wait(session)
    try:
        fetched = fetch_valet_series(
            base, quote, on_or_before - timedelta(days=FETCH_LOOKBACK_DAYS), on_or_before
        )
        _store_fetched(session, base, quote, fetched)
    except ValetUnavailable:
        pass  # degrade to whatever the cache holds (docs/07 discipline)
    return newest()


def get_rate_or_404(session: Session, base: str, quote: str, on_date: date) -> FxRate:
    rate = latest_rate(session, base, quote, on_date)
    if rate is None:
        raise api_error(
            404, f"No {base}/{quote} rate available for {on_date.isoformat()}", "fx_unavailable"
        )
    return rate


def _weekdays(start_date: date, end_date: date) -> list[date]:
    days = []
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() < 5:  # BoC publishes on business days only
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def get_rates_between(
    session: Session,
    base: str,
    quote: str,
    start_date: date,
    end_date: date,
    *,
    allow_fetch: bool = True,
    commit: bool = True,
) -> list[FxRate]:
    """Rates for [start_date, end_date]; misses backfill in ONE bulk call.

    `commit=False` is the in-transaction variant used by the ACB sweep (see
    `_store_fetched`); it flushes the fetched rows into the caller's own
    transaction instead of committing it.
    """

    def between() -> list[FxRate]:
        return list(
            session.exec(
                select(FxRate)
                .where(
                    FxRate.base == base,
                    FxRate.quote == quote,
                    FxRate.date >= start_date,
                    FxRate.date <= end_date,
                )
                .order_by(FxRate.date)
            )
        )

    base, quote = _normalize(base, quote)
    if start_date > end_date:
        raise api_error(422, "start_date must be on or before end_date", "invalid_date_range")
    rows = between()
    if allow_fetch:
        covered = {row.date for row in rows}
        missing = [day for day in _weekdays(start_date, end_date) if day not in covered]
        if missing:
            if commit:
                # M175 (docs/07 §Provider-wait ledger): the `between()` SELECT
                # above autobegan and the fetch is a Valet series call. Released
                # only on the `commit=True` side — the ONE caller that passes
                # `commit=False` is `tax/acb.py::FxRateBook._backfill`, which
                # deliberately runs inside `rebuild_user_pools` (docs/02: that
                # path NEVER commits, so an aborted trade write cannot leave a
                # half-applied sweep behind). Committing there would break that
                # contract, so that caller keeps its transaction.
                release_before_provider_wait(session)
            try:
                fetched = fetch_valet_series(base, quote, start_date, end_date)
                _store_fetched(session, base, quote, fetched, commit=commit)
            except ValetUnavailable:
                pass  # serve whatever the cache + manual gap-fills hold
            rows = between()
    return rows


def newest_cached_date(session: Session, base: str, quote: str) -> date | None:
    """Newest date the cache holds for the pair — a pure read, never fetches.

    This is what tells a caller (and the M150 status chip) how far behind the
    book is. It deliberately does not carry forward: a rate 10 days old IS the
    answer here, not the answer for a target date.
    """
    base, quote = _normalize(base, quote)
    row = session.exec(
        select(FxRate)
        .where(FxRate.base == base, FxRate.quote == quote)
        .order_by(FxRate.date.desc())  # type: ignore[attr-defined]
    ).first()
    return None if row is None else row.date


def refresh_pair(
    session: Session,
    base: str,
    quote: str,
    end_date: date,
    *,
    lookback_days: int = FETCH_LOOKBACK_DAYS,
    commit: bool = True,
) -> int:
    """Fetch ``[end_date - lookback_days, end_date]`` and cache it (M150).

    The scheduled refresh's ONE network call per pair (docs/07 §Scheduled
    refresh). It lives in this module rather than in the worker so Valet stays
    behind the single egress seam AGENTS §2 pins — the worker only decides
    WHICH pair and WHEN.

    Raises ``ValetUnavailable`` when every retry is exhausted (the worker logs
    it and moves to the next pair). Returns the number of rows added; the store
    path writes EMPTY dates only, so re-running an already-cached window adds
    nothing and touches nothing — a manual gap-fill row survives it untouched.
    """
    base, quote = _normalize(base, quote)
    fetched = fetch_valet_series(base, quote, end_date - timedelta(days=lookback_days), end_date)
    return _store_fetched(session, base, quote, fetched, commit=commit)

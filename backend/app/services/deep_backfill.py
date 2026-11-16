"""Full-history price backfill per held security (M144, docs/07 §Backfill depth).

The daily machinery keeps the LAST ~365 days fresh (`BACKFILL_DAYS` on the
securities router + `ensure_recent_price(force=True)`); nothing ever walked
further back, so a position bought before that window had no close to value it
with. One held security without a close makes its whole bucket `valued: false`
(`services/account_value.py`) and the portfolio merge propagates `None`, so a
single unpriceable holding blanks every day it was held — real account 459's
`VUS` (zero cached rows, held 2025-02-27 → 2026-02-27) turned Mark's entire
Jan 1 - Feb 26 YTD table red.

This module is the one-time, re-runnable sweep that closes that hole:

* **start bound = the row's own need**, `min(committed trade date, watchlist
  added_at)` — never a hard-coded decade, so a security held since last month
  costs one window and nothing earlier (docs/07 §Backfill depth);
* **dead symbols are skipped outright** (M141) unless `probe_dead`;
* **privacy mode refuses the fetch** for that owner — the seam guard raises and
  the row is reported, never fetched;
* **idempotent and abortable**: `_cache_closes` inserts only missing
  ``(security, date)`` rows and commits per security, so a run that dies
  halfway leaves the finished rows cached and the rest resumable by re-running.

The one thing this sweep refuses to do is cache a series that contradicts the
user's own ledger. A bare ticker can answer as a DIFFERENT instrument (measured
M144: bare ``VUS`` = *Virtus U.S. Dividend ETF* at $30.23 vs ``VUS.TO`` at
$130.70, against a recorded buy of $79.69 CAD), so every answering candidate is
compared with the row's committed trade prices before one is chosen, and a row
whose trades no candidate agrees with is left alone and reported — an honest
gap beats a fabricated valuation (docs/07 §Series validation).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import or_
from sqlmodel import Session, select

from app.models.accounts import Account
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.securities import Security
from app.models.trades import Trade
from app.models.users import User
from app.models.watchlist import WatchlistItem
from app.services import market_data
from app.services.market_data import CandidateSeries
from app.services.reports import FxCache
from app.services.total_return import _trade_currency
from app.services.trade_filters import committed_trade_conditions

log = logging.getLogger(__name__)

#: How far a fetched close may sit from the ledger's own recorded trade price
#: and still count as the SAME instrument. Broker rows carry commission/spread
#: and the close is a different moment of the day, so the tolerance is wide on
#: purpose: it separates "this listing" from "a different listing" (the M144
#: cases differ by 4x, not 10%), it does not police prices. A candidate must
#: agree on a MAJORITY of the trades it can be compared against.
SERIES_TOLERANCE = Decimal("0.30")

#: The second reading (measured M144, prod VTI/VEA): a trade whose price is
#: UNCONVERTEDly within this of the candidate's own close is reading the
#: listing's currency, not the row's stated one. Rows written before M132 have
#: no `trades.currency` and read in the ACCOUNT's currency, but the broker
#: exported their prices natively — VTI's buy is 329.91 against a $329.91 USD
#: close while the row says CAD — so the fx-converted comparison alone
#: double-converts and refuses a series that plainly matches. Deliberately
#: TIGHT (commission/spread scale, observed ≤0.52% on the prod rows): a loose
#: face-value match could be two currencies' unrelated numbers coinciding, and
#: the wide tolerance above is only safe when the currency premise is sound.
RAW_SERIES_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class TradeEvidence:
    """One recorded trade price — evidence about which listing a row means."""

    date: dt.date
    price: Decimal
    currency: str


@dataclass
class BackfillResult:
    """What one security's sweep did (logged per symbol, docs/07)."""

    security_id: int
    symbol: str
    start: dt.date | None = None
    end: dt.date | None = None
    inserted: int = 0
    #: The listing whose series was cached (`VUS.TO`), when one was chosen.
    series: str | None = None
    #: Machine-readable skip/verdict token; `None` when closes landed.
    skipped: str | None = None
    #: Human detail for a skip (the reason, or the exception message).
    detail: str | None = None
    #: Candidates that answered, in the order they were tried.
    candidates: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.skipped is not None:
            suffix = f" ({self.detail})" if self.detail else ""
            return f"{self.symbol}: skipped {self.skipped}{suffix}"
        return (
            f"{self.symbol}: {self.start} -> {self.end} via {self.series}, "
            f"{self.inserted} close(s) inserted"
        )


def _committed_only() -> list[object]:
    """The docs/03 staging predicate WITHOUT the user half.

    The sweep walks every tenant in one statement, so it cannot pin
    `Trade.user_id`; the committed half is the same expression
    `trade_filters.committed_trade_conditions` builds for a single user.
    """
    return [
        or_(
            ImportBatch.status == ImportBatchStatus.committed,
            Trade.import_batch_id.is_(None),
        )
    ]


def trade_evidence(session: Session, security: Security) -> list[TradeEvidence]:
    """The row's committed trades that actually moved shares.

    Staged rows are excluded (docs/03) and quantity-0 rows are skipped: a
    dividend's `price_native` is the CASH it paid, not a quote for the
    security, so it says nothing about which listing this is.
    """
    rows = session.exec(
        select(Trade)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(
            *committed_trade_conditions(security.user_id),
            Trade.security_id == security.id,
            Trade.quantity != 0,
        )
        .order_by(Trade.date, Trade.id)  # type: ignore[attr-defined]
    ).all()
    currencies = {
        account.id: account.currency
        for account in session.exec(
            select(Account).where(Account.user_id == security.user_id)
        ).all()
    }
    # M132: a row written before the currency column reads in its ACCOUNT's
    # currency, exactly as every consumer does — never `None.upper()`.
    return [
        TradeEvidence(
            row.date,
            row.price_native,
            _trade_currency(row, currencies.get(row.account_id, "CAD")).upper(),
        )
        for row in rows
    ]


def needed_start(session: Session, security: Security) -> dt.date | None:
    """Earliest date this row's closes are NEEDED (docs/07 §Backfill depth).

    The earliest of its committed trades and its watchlist entry — the two
    things that make a security worth pricing. `None` means nothing has ever
    asked for a price and the sweep leaves the row alone.
    """
    first_trade = session.exec(
        select(Trade.date)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(*committed_trade_conditions(security.user_id), Trade.security_id == security.id)
        .order_by(Trade.date)  # type: ignore[attr-defined]
    ).first()
    first_watch = session.exec(
        select(WatchlistItem.created_at)
        .where(WatchlistItem.security_id == security.id)
        .order_by(WatchlistItem.created_at)
    ).first()
    dates = [
        stamp.date() if isinstance(stamp, dt.datetime) else stamp
        for stamp in (first_trade, first_watch)
        if stamp is not None
    ]
    return min(dates) if dates else None


def held_security_ids(session: Session, user_id: int | None = None) -> set[int]:
    """Every security a committed trade or a watchlist row has ever touched."""
    trade_filter = _committed_only() if user_id is None else committed_trade_conditions(user_id)
    trade_ids = session.exec(
        select(Trade.security_id)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(*trade_filter)
    ).all()
    watch = select(WatchlistItem.security_id)
    if user_id is not None:
        watch = watch.where(WatchlistItem.user_id == user_id)
    watch_ids = session.exec(watch).all()
    return {int(sid) for sid in (*trade_ids, *watch_ids) if sid is not None}


def never_held_shares(session: Session, security: Security) -> bool:
    """True for a row whose trades have ALL moved zero shares — the per-user
    `CASH` placeholder (docs/05), which represents cash, not a listed
    instrument. Pricing it fetches a real ticker (`CASH` = Pathward Financial)
    into a row no valuation ever reads; the sweep skips it and says so.
    """
    share_row = session.exec(
        select(Trade.id)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(
            *committed_trade_conditions(security.user_id),
            Trade.security_id == security.id,
            Trade.quantity != 0,
        )
    ).first()
    if share_row is not None:
        return False
    any_row = session.exec(
        select(Trade.id)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(*committed_trade_conditions(security.user_id), Trade.security_id == security.id)
    ).first()
    return any_row is not None


class _FxLookup:
    """Cache-only fx in whichever direction resolves (docs/12; never fetches)."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._caches: dict[str, FxCache] = {}

    def _cache(self, quote: str) -> FxCache:
        cache = self._caches.get(quote)
        if cache is None:
            cache = FxCache(self._session, quote)
            self._caches[quote] = cache
        return cache

    def rate(self, base: str, quote: str, day: dt.date) -> Decimal | None:
        """`base` -> `quote` on-or-before `day`, via either published pair."""
        if base == quote:
            return Decimal("1")
        direct = self._cache(quote).rate_info(base, quote, day)
        if direct is not None:
            return direct[0]
        inverse = self._cache(base).rate_info(quote, base, day)
        if inverse is None or inverse[0] == 0:
            return None
        return Decimal("1") / inverse[0]


def score_series(
    candidate: CandidateSeries,
    evidence: list[TradeEvidence],
    fx: _FxLookup,
) -> tuple[int, int]:
    """`(comparable, agreeing)` — how many trades this series explains.

    A trade is comparable when the candidate has a close ON its date and at
    least one reading of the price can be tested; an uncomparable trade is not
    a vote either way (a weekend-dated buy, a currency the cache never saw).

    Two readings per trade (docs/07 §Series validation):

    * **raw** — the trade price UNCONVERTED against the candidate's close,
      within the tight `RAW_SERIES_TOLERANCE`. This catches the pre-M132 rows
      whose prices are the broker's native export but whose (absent) currency
      makes them read in the account's: measured prod VTI bought at 329.91
      against VTI's $329.91 USD close, while the row itself says CAD — the
      converted reading alone would refuse this plainly-matching series;
    * **converted** — the close fx-converted into the trade's stated currency
      (via either published pair), within the wide `SERIES_TOLERANCE`, which
      is where a same-listing trade carrying commission/spread lands.

    Either reading agreeing counts the trade as explained; the wide tolerance
    is only ever applied AFTER fx, never to a bare number.
    """
    comparable = agreeing = 0
    for ev in evidence:
        close = candidate.closes.get(ev.date)
        if close is None:
            continue
        raw_hit = abs(close - ev.price) <= abs(ev.price) * RAW_SERIES_TOLERANCE
        rate = fx.rate(candidate.currency, ev.currency, ev.date)
        if rate is None and not raw_hit:
            continue
        comparable += 1
        if raw_hit:
            agreeing += 1
            continue
        converted = close * rate  # type: ignore[operator]  # None excluded above
        if abs(converted - ev.price) <= abs(ev.price) * SERIES_TOLERANCE:
            agreeing += 1
    return comparable, agreeing


def choose_series(
    candidates: list[CandidateSeries],
    evidence: list[TradeEvidence],
    fx: _FxLookup,
) -> tuple[CandidateSeries | None, str]:
    """Which listing to cache — or nothing at all (docs/07 §Series validation).

    A candidate that explains a majority of the trades it can be compared
    against WINS, in the documented candidate order. When the row's trades are
    all outside every candidate's data, the first candidate answers (the legacy
    order — nothing contradicts it). When trades ARE comparable and NO listing
    agrees, nothing is cached: the caller reports `no_listing_agrees` and the
    honest gap stays.
    """
    if not candidates:
        return None, "no_candidates"
    if not evidence:
        return candidates[0], "no_evidence"
    comparable_any = False
    for candidate in candidates:
        comparable, agreeing = score_series(candidate, evidence, fx)
        comparable_any = comparable_any or comparable > 0
        if comparable and agreeing * 2 >= comparable:
            return candidate, "verified"
    if comparable_any:
        return None, "no_listing_agrees"
    return candidates[0], "unverified"


def _prefetch(
    session: Session, security: Security, today: dt.date, probe_dead: bool
) -> tuple[BackfillResult | None, dt.date]:
    """Everything BEFORE the network: the skips that need no fetch.

    Returns `(skip, start)` — `skip` is the finished result when the row must
    not be fetched (`start` is then only informative), else `None` and the
    row's own need-date.
    """
    result = BackfillResult(security_id=security.id or -1, symbol=security.symbol)
    result.start = needed_start(session, security)
    result.end = today
    start = result.start
    if start is None:
        result.skipped = "not_held_or_watched"
        return result, today
    if never_held_shares(session, security):
        result.skipped = "cash_placeholder"
        return result, start
    if security.dead_symbol and not probe_dead:
        # M141: the automatic paths never probe a row Yahoo has disowned.
        result.skipped = "dead_symbol"
        result.detail = security.dead_symbol_reason
        return result, start
    return None, start


def backfill_security(
    session: Session,
    security: Security,
    today: dt.date,
    *,
    evidence: list[TradeEvidence] | None = None,
    fx: _FxLookup | None = None,
    probe_dead: bool = False,
    dry_run: bool = False,
) -> BackfillResult:
    """Deep-backfill ONE security, from its own need-date to `today`.

    Fetches through `market_data` (the only network seam, docs/11), chooses the
    series by the check above, and caches insert-only. Never raises: a privacy
    refusal, a dead symbol or a network failure is a REPORTED skip, so a sweep
    over many rows is never aborted by one of them (docs/07 §Backfill depth).

    `dry_run=True` makes the same fetches and writes NOTHING — the operator
    sees exactly which listing the row would be deepened under first.
    """
    skipped, start = _prefetch(session, security, today, probe_dead)
    if skipped is not None:
        return skipped
    if evidence is None:
        evidence = trade_evidence(session, security)
    if fx is None:
        fx = _FxLookup(session)
    outcome = BackfillResult(security_id=security.id or -1, symbol=security.symbol)
    outcome.start = start
    outcome.end = today
    # M175 (docs/07 §Provider-wait ledger): `_prefetch` and `trade_evidence`
    # READ immediately above, so SQLAlchemy autobegan and the pool connection
    # would sit `idle in transaction` for the whole fetch. `deep_backfill_all`
    # is a SWEEP — one session over every held/watched row — so without this
    # the operator's `scripts/deep_price_backfill.py` run (dry run by DEFAULT)
    # carried one open transaction across every provider wait for minutes: the
    # exact M152 `backfill_etf_holdings.py` shape on the next sweep along. It
    # is the last database contact before the wait, and both callers reach it
    # read-only (`deep_backfill_all` reads; `--apply`'s writes happen BELOW, in
    # `record_quote_outcome`/`cache_closes`, each of which commits itself).
    market_data.release_before_provider_wait(session, security)
    # The verdict this function books below must be the one THIS fetch
    # publishes; clear a previous call's first (same rule as `backfill_range`).
    market_data.reset_quote_verdict()
    try:
        candidates = market_data.fetch_history_candidates(security.symbol, start, today)
    except Exception as exc:  # the seam raises its own envelope
        # Privacy mode (403 privacy_mode_blocks_external_api) and a dead socket
        # are both "this row could not be fetched" — never a swept-over failure.
        outcome.skipped = "fetch_refused"
        outcome.detail = str(exc)
        return outcome
    outcome.candidates = [candidate.symbol for candidate in candidates]
    chosen, verdict = choose_series(candidates, evidence, fx)
    if chosen is None:
        outcome.skipped = verdict
        if verdict == "no_listing_agrees":
            outcome.detail = (
                f"{len(evidence)} recorded trade price(s), none matched by "
                f"{', '.join(outcome.candidates) or 'any candidate'}"
            )
        return outcome
    outcome.series = chosen.symbol
    if dry_run:
        outcome.inserted = len(chosen.closes)
        return outcome
    fetch_outcome, reason = market_data.last_quote_verdict()
    market_data.record_quote_outcome(session, security, fetch_outcome, reason)
    # Option A (t_bd5ec95f): the chosen series was fetched by the candidate
    # probe above; its provider-response arrival instant is the legal stamp.
    outcome.inserted = market_data.cache_closes(
        session, security, chosen.closes, fetched_at=market_data.last_fetch_at()
    )
    log.info(
        "deep backfill: %s (%s) %s -> %s via %s, %d close(s), verdict=%s",
        security.symbol,
        security.id,
        start,
        today,
        chosen.symbol,
        outcome.inserted,
        verdict,
    )
    return outcome


def deep_backfill_all(
    session: Session,
    *,
    user_id: int | None = None,
    today: dt.date | None = None,
    probe_dead: bool = False,
    apply: bool = True,
) -> list[BackfillResult]:
    """Every held/watched security (docs/07 §Backfill depth).

    All tenants unless `user_id` narrows it. Each row's owner binds the privacy
    lock before its own fetch, so one privacy-mode tenant never leaks another's
    calls into its context. `apply=False` is the dry run: same fetches, zero
    writes, so an operator sees exactly which listing each row would be
    deepened under before committing to it.
    """
    today = today or dt.date.today()
    wanted = held_security_ids(session, user_id)
    if not wanted:
        return []
    securities = session.exec(
        select(Security).where(Security.id.in_(sorted(wanted))).order_by(Security.id)
    ).all()
    owners: dict[int, User] = {}
    results: list[BackfillResult] = []
    try:
        for security in securities:
            owner = owners.get(security.user_id)
            if owner is None:
                owner = session.get(User, security.user_id)
                if owner is None:  # pragma: no cover — FK guarantees the row
                    continue
                owners[security.user_id] = owner
            market_data.bind_privacy_lock(bool(owner.privacy_mode))
            result = backfill_security(
                session, security, today, probe_dead=probe_dead, dry_run=not apply
            )
            results.append(result)
            log.info("deep backfill: %s", result.describe())
    finally:
        # Never leave the lock bound to a tenant's flag in this context.
        market_data.bind_privacy_lock(False)
    return results

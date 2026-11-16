"""ACB pool engine (docs/06) — the ONLY write path to `acb_pools` (docs/02 invariant).

Every rate comes exclusively from the `fx_rates` table (base = security listing
currency, quote = CAD, date = trade date). `trades.fx_rate_used` is a display
copy and is NEVER read here. All money math is Decimal, quantized to cents
(ROUND_HALF_UP) at each allocation so stored pools match Numeric(18,2).

`cents()` below is THE canonical money-rounding entry point for the whole app
(AGENTS §2): every module imports it rather than defining a private copy, and
the few surfaces whose HALF_EVEN mode is documented pass it explicitly as
`cents(value, rounding=ROUND_HALF_EVEN)`.

FX precedence (docs/07) has two steps, in order:

1. the EXACT trade-date row;
2. carry-forward — the newest published rate within `CARRY_FORWARD_MAX_DAYS`
   days before it. BoC publishes business days only, so weekend/holiday trade
   dates (2025-12-26 is Boxing Day) never get a row of their own; refusing
   them was the bug that bricked any file touching a holiday.

A date neither step answers triggers ONE batched `fx.get_rates_between`
backfill per base for the whole sweep (never one call per date), then both
steps again. Only when a gap wider than `CARRY_FORWARD_MAX_DAYS` survives that
does the docs/06 `fx_rate_missing` 422 surface — genuinely missing data, not a
closed market. The table stays the only source: backfill FILLS the table first,
it never reads around it (docs/06). `FxRateBook` owns that fetch; the
single-date seam `fx_rate_cad` stays cache-only (exact + carry-forward), so a
read path never opens a network call of its own.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlmodel import Session, select

from app.core.errors import APIError, api_error
from app.models.acb_pools import AcbPool
from app.models.accounts import Account
from app.models.fx_rates import FxRate
from app.models.import_batches import ImportBatch
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.services import fx
from app.services.trade_filters import committed_trade_conditions
from app.tax.superficial_loss import SuperficialLossDenial, evaluate_superficial_losses

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
_MAX_REFINEMENT_PASSES = 25

# docs/07 carry-forward bound. BoC publishes business days only, so a
# non-publication day resolves to the newest rate on or before it — but never
# more than this many days back. A wider gap is genuinely missing data and
# keeps the documented 422 (no silent wrong math on stale rates).
# Since M150 the canonical home is `services/fx.CARRY_FORWARD_MAX_DAYS` (this
# module already imports it, and the fx module must never import back this way);
# the name stays here because every consumer of the rule reads it from here.
CARRY_FORWARD_MAX_DAYS = fx.CARRY_FORWARD_MAX_DAYS

# Exactly the `trade.action` values `_replay` reads an fx rate for (see the
# `rates.rate(...)` call sites below). Cash-only actions — dividends,
# distributions, fees — never ask, so their dates must not drag a Valet call
# into the sweep. A registered `sell` reads no rate either (`continue` before
# the conversion), which is what `_needs_rate` encodes.
_RATE_ACTIONS = frozenset(
    {
        TradeAction.buy,
        TradeAction.drip,
        TradeAction.transfer_in,
        TradeAction.return_of_capital,
    }
)


def cents(value: Decimal, *, rounding: str = ROUND_HALF_UP) -> Decimal:
    """THE canonical money-rounding entry point (docs/02, docs/06).

    Every module that rounds money imports THIS — never a private copy. The
    ACB pool math below calls it with the default (`ROUND_HALF_UP`, pinned by
    docs/06 and by `Numeric(18,2)` on `acb_pools`), so its numbers are
    byte-identical to the pre-consolidation helper.

    `rounding` exists so the surfaces whose HALF_EVEN behaviour is DOCUMENTED
    (and must not drift) express their mode through the same one implementation
    instead of keeping a second `quantize` call:

    * `services/valuation.py` and `services/exposure_breakdown.py` — docs/08
      §Portfolio value history (F-M130-1) and docs/16 §4 pin `ROUND_HALF_EVEN`;
      `tests/test_m130_portfolio_value.py::test_f_m130_1_...` hand-computes the
      half-even result and the frontend mirrors the mode in
      `lib/positionGroups.ts` / `lib/impact.ts`.
    * `api/securities.py` overview totals — the Σ of the same per-position
      fields that pass publishes, so it keeps that pass's mode.
    * `services/budgets.py::spent_between` / `carry_in` — `lib/s3Engine.ts`
      documents the half-even client mirror and
      `frontend/tests/fixtures/s3-vectors.json` is a golden captured from
      `spent_between` (docs/33 §S3 parity).

    Nothing else passes `rounding`: money that has no documented mode of its
    own is ROUND_HALF_UP (docs/12 §money, `tests/test_reports_money.py`).
    `backend/tests/test_money_rounding_single_source.py` pins both halves.
    """
    return value.quantize(CENT, rounding=rounding)


def _fx_rate_missing(
    base: str, day: dt.date, symbol: str, *, privacy_blocked: bool = False
) -> APIError:
    """The docs/06 envelope for an unresolved conversion date."""
    if privacy_blocked:
        # The backfill was refused by the docs/11 lockdown (fetch_valet_series
        # raises `privacy_mode_blocks_external_api` before any packet leaves
        # the machine), so the user is told which policy stopped it and what
        # to do instead. Same 422 `fx_rate_missing` envelope as always — only
        # the remedy line changes.
        return api_error(
            422,
            f"No {base}/CAD fx rate for {symbol} on {day}; privacy mode blocks the "
            "backfill (privacy_mode_blocks_external_api) — add a manual rate before "
            "computing tax values",
            "fx_rate_missing",
        )
    return api_error(
        422,
        f"No {base}/CAD fx rate for {symbol} on {day}; "
        "add a manual rate before computing tax values",
        "fx_rate_missing",
    )


def _carry_forward(
    rates: dict[tuple[str, dt.date], Decimal], base: str, day: dt.date
) -> tuple[dt.date, Decimal] | None:
    """(date, rate) of the newest row on or before `day`, ≤ CARRY_FORWARD_MAX_DAYS back.

    Reads the already-loaded span, so at most CARRY_FORWARD_MAX_DAYS + 1 dict
    lookups and never a query. `back == 0` is the exact trade date, so this is
    BOTH steps of docs/07 FX precedence in one lookup: the exact row when BoC
    published one, otherwise the newest published rate before it.
    """
    for back in range(CARRY_FORWARD_MAX_DAYS + 1):
        candidate = day - dt.timedelta(days=back)
        found = rates.get((base, candidate))
        if found is not None:
            return candidate, found
    return None


def _backfill_span(session: Session, base: str, start: dt.date, end: dt.date) -> bool:
    """ONE bulk Valet call covering [start, end]. True when privacy blocked it.

    Fail-soft per symbol, mirroring the price/security backfill in
    `market_data.ensure_enriched` ("failed; continuing"): a failure logs and
    returns so every other base still gets its fetch attempt, and the hard
    `fx_rate_missing` 422 is raised only afterwards by whichever caller still
    cannot resolve its date.

    Always `commit=False` — this runs inside `rebuild_user_pools`, which is
    documented (docs/02) to NEVER commit: the fetched rows ride the caller's
    own transaction, so an aborted trade write never leaves a half-applied
    sweep behind.
    """
    if fx.privacy_locked():
        # docs/11: skip the seam outright rather than raising out of it — the
        # sweep keeps going and the read path reports the missing rate.
        return True
    try:
        fx.get_rates_between(session, base, "CAD", start, end, commit=False)
    except APIError as exc:  # the seam's own privacy guard, belt and braces
        if exc.code == "privacy_mode_blocks_external_api":
            return True
        logger.warning("fx backfill for %s failed (%s); continuing", base, exc.code)
    except Exception:  # - network noise degrades silently
        logger.warning("fx backfill failed for %s; continuing", base)
    return False


def _log_carry(base: str, day: dt.date, used: dt.date, symbol: str) -> None:
    """Record which publication day actually priced a non-publication day."""
    if used != day:
        logger.info(
            "fx carry-forward: %s/CAD for %s on %s uses the %s rate (no BoC "
            "publication on the trade date)",
            base,
            symbol,
            day,
            used,
        )


def fx_rate_cad(session: Session, base: str, day: dt.date, symbol: str) -> Decimal:
    """base→CAD rate on `day`, from the fx_rates table ONLY (docs/06 FX precedence).

    Both steps of docs/07 in one query: the exact trade-date row when BoC
    published one, otherwise the newest published rate within
    CARRY_FORWARD_MAX_DAYS (a Saturday trade reads Friday's rate). This is the
    cache-only single-date seam — the ONE sweep-level backfill lives in
    `FxRateBook`, so a read path never opens a network call of its own.
    """
    if base == "CAD":
        return Decimal("1")  # no conversion; no fx_rates row required for CAD-listed
    carried = session.exec(
        select(FxRate)
        .where(
            FxRate.base == base,
            FxRate.quote == "CAD",
            FxRate.date <= day,
            FxRate.date >= day - dt.timedelta(days=CARRY_FORWARD_MAX_DAYS),
        )
        .order_by(FxRate.date.desc())
        .limit(1)
    ).first()
    if carried is None:
        raise _fx_rate_missing(base, day, symbol, privacy_blocked=fx.privacy_locked())
    _log_carry(base, day, carried.date, symbol)
    return carried.rate


def _needs_rate(trade: Trade, account: Account) -> bool:
    """True when `_replay` will ask `FxRateBook.rate` for this trade's date.

    Mirrors the `rates.rate(...)` call sites exactly — the sweep's up-front
    backfill is scoped to dates it will actually consume, so an unrelated
    cash-only row can never trigger a network call.
    """
    if trade.action in _RATE_ACTIONS:
        return True
    if trade.action == TradeAction.sell:
        return bool(account.taxable)  # registered sells `continue` before any fx read
    return False  # transfer_out, corporate_action_adjustment, cash-only actions


class FxRateBook:
    """Every base→CAD rate one tax-state computation needs, loaded up front.

    docs/34 (M60): the replay called `fx_rate_cad` once per non-CAD trade, so a
    74-trade tenant issued 25 `fx_rates` statements per report — an N+1 that
    scales with trade count. This loads the whole trade-date span up front (plus
    CARRY_FORWARD_MAX_DAYS of history before it, so a non-publication day can
    carry forward) and answers from memory.

    docs/07 backfill: when the span cannot resolve a needed date — neither an
    exact row nor a carry-forward one — ONE `fx.get_rates_between` call per
    base goes out, issued in `__init__` BEFORE the replay starts, so one
    symbol's failure is logged and the others still get their attempt, and the
    `fx_rate_missing` 422 surfaces only when a date remains unresolvable after
    both. A date inside the carry-forward window never fetches: the newest
    published rate ≤ the trade date wins, exactly as BoC discipline intends
    (a Saturday trade reads Friday's rate) — which is also why a holiday-dated
    sweep re-reads the cache instead of re-hitting Valet on every run.

    Deliberately NOT a session-level cache: a `session.info` cache would go
    stale for a caller that writes an fx row and recomputes inside one session,
    which is precisely the `POST /fx` → `POST /acb/rebuild` repair path.
    """

    def __init__(
        self,
        session: Session,
        rows: list[tuple[Trade, Account, Security]],
        *,
        allow_fetch: bool = True,
    ) -> None:
        self._session = session
        self._allow_fetch = allow_fetch
        self._rates: dict[tuple[str, dt.date], Decimal] = {}
        self._bases: list[str] = []
        self._span_start = dt.date.min
        self._span_end = dt.date.max
        self._backfilled: set[str] = set()
        self._privacy_blocked = False
        wanted = [
            (security.listing_currency, trade.date)
            for trade, _account, security in rows
            if security.listing_currency != "CAD"
        ]
        if not wanted:
            return  # all-CAD tenant: the replay needs no fx row at all
        self._bases = sorted({base for base, _day in wanted})
        first, last = min(day for _base, day in wanted), max(day for _base, day in wanted)
        self._span_start = first - dt.timedelta(days=CARRY_FORWARD_MAX_DAYS)
        self._span_end = last
        self._load()
        self._prefetch(rows)

    def _load(self) -> None:
        """(Re)read the trade-date span into memory; existing rows are kept."""
        for row in self._session.exec(
            select(FxRate).where(
                FxRate.base.in_(self._bases),
                FxRate.quote == "CAD",
                FxRate.date >= self._span_start,
                FxRate.date <= self._span_end,
            )
        ).all():
            self._rates.setdefault((row.base, row.date), row.rate)

    def _resolve(self, base: str, day: dt.date) -> tuple[dt.date, Decimal] | None:
        """docs/07 precedence over the loaded span: exact row, else carry-forward."""
        return _carry_forward(self._rates, base, day)

    def _prefetch(self, rows: list[tuple[Trade, Account, Security]]) -> None:
        """The sweep's ONE batched backfill per base, up front and fail-soft.

        Only dates `_replay` will actually ask for count (`_needs_rate`), and
        only when neither step of docs/07 resolves them from the cache — so a
        fully cached sweep makes ZERO network calls, and a cash-only row on a
        date with no rate can never drag a Valet call in.
        """
        needed = {
            (security.listing_currency, trade.date)
            for trade, account, security in rows
            if security.listing_currency != "CAD" and _needs_rate(trade, account)
        }
        for base in sorted({b for b, _day in needed}):
            days = sorted(day for b, day in needed if b == base)
            if all(self._resolve(base, day) is not None for day in days):
                continue  # cache answers every date — zero network, the common case
            # Cover the whole window the sweep may ask about, in one call.
            self._backfill(base, min(days) - dt.timedelta(days=CARRY_FORWARD_MAX_DAYS), max(days))

    def _backfill(self, base: str, start: dt.date, end: dt.date) -> None:
        """Fetch `base`'s missing span at most ONCE per sweep, then reload."""
        if base in self._backfilled:
            return
        self._backfilled.add(base)
        if not self._allow_fetch:
            return  # cache-only caller (docs/03): the seam stays shut
        if _backfill_span(self._session, base, start, end):
            self._privacy_blocked = True
        self._load()

    def rate(self, base: str, day: dt.date, symbol: str) -> Decimal:
        """base→CAD rate on `day`; identical contract to `fx_rate_cad`."""
        if base == "CAD":
            return Decimal("1")
        resolved = self._resolve(base, day)
        if resolved is None:
            # Safety net for a date `_replay` asks for that `_prefetch` did not
            # enumerate (plus the backfill `_prefetch` already issued, which the
            # memo turns into a no-op). Memoized per base, so the sweep still
            # issues at most ONE Valet call per base however many dates miss.
            self._backfill(base, day - dt.timedelta(days=CARRY_FORWARD_MAX_DAYS), day)
            resolved = self._resolve(base, day)
        if resolved is None:
            raise _fx_rate_missing(
                base,
                day,
                symbol,
                privacy_blocked=self._privacy_blocked or fx.privacy_locked(),
            )
        used, value = resolved
        _log_carry(base, day, used, symbol)
        return value


PoolKey = tuple[int, int]  # (account_id, security_id)


@dataclass
class PoolState:
    quantity: Decimal = Decimal("0")
    total_acb_cad: Decimal = Decimal("0")


@dataclass(frozen=True)
class Acquisition:
    """A share acquisition (buy/drip) in ANY account — feeds the superficial-loss rule."""

    trade_id: int
    account_id: int
    security_id: int
    date: dt.date
    quantity: Decimal
    taxable: bool


@dataclass(frozen=True)
class Disposal:
    trade_id: int
    account_id: int
    security_id: int
    symbol: str
    date: dt.date
    quantity_sold: Decimal
    proceeds_cad: Decimal
    acb_cad: Decimal
    outlays_commissions_cad: Decimal
    gain_cad: Decimal  # raw; superficial-loss denials are layered on top


@dataclass(frozen=True)
class RocGainRecord:
    security_id: int
    symbol: str
    account_id: int
    date: dt.date
    excess_gain_cad: Decimal  # portion of the ROC that fell below the zero floor (docs/06)


@dataclass
class TaxState:
    pools: dict[PoolKey, PoolState] = field(default_factory=dict)
    disposals: list[Disposal] = field(default_factory=list)
    roc_gains: list[RocGainRecord] = field(default_factory=list)
    acquisitions: list[Acquisition] = field(default_factory=list)
    denials: list[SuperficialLossDenial] = field(default_factory=list)


def _load_rows(session: Session, user_id: int) -> list[tuple[Trade, Account, Security]]:
    """Committed (or manual) trades only — staged import rows are invisible to
    the tax engine (docs/03 staging model; trades-side twin of
    `services.txn_filters.committed_tx_conditions`). Without this a
    `pending_review` investment CSV moves pools, disposals, ROC and the
    superficial-loss rule before the user commits it."""
    rows = session.exec(
        select(Trade, Account, Security)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .join(Account, Trade.account_id == Account.id)
        .join(Security, Trade.security_id == Security.id)
        .where(*committed_trade_conditions(user_id))
        .order_by(Trade.date, Trade.id)
    ).all()
    return [(trade, account, security) for trade, account, security in rows]


def _replay(
    session: Session,
    rows: list[tuple[Trade, Account, Security]],
    addbacks: dict[int, Decimal],
    rates: FxRateBook,
) -> tuple[dict[PoolKey, PoolState], list[Disposal], list[RocGainRecord], list[Acquisition]]:
    """Chronological replay per (non-registered account, security); docs/06 mechanics.

    Registered accounts never get pools but their trades still register as
    acquisitions / holdings for the superficial-loss rule.
    """
    pools: dict[PoolKey, PoolState] = defaultdict(PoolState)
    # Holding quantity is separate from taxable ACB state. Registered accounts
    # never create a pool, but still need chronology for sell/transfer_out
    # validation and for cross-account superficial-loss evaluation.
    holding_quantities: dict[PoolKey, Decimal] = defaultdict(lambda: Decimal("0"))
    disposals: list[Disposal] = []
    roc_gains: list[RocGainRecord] = []
    acquisitions: list[Acquisition] = []

    for trade, account, security in rows:
        key: PoolKey = (int(account.id), int(security.id))
        taxable = account.taxable
        pool = pools[key]

        if trade.action in (TradeAction.buy, TradeAction.drip, TradeAction.transfer_in):
            quantity = abs(trade.quantity)
            fx = rates.rate(security.listing_currency, trade.date, security.symbol)
            delta = cents(quantity * trade.price_native * fx) + cents(trade.commission * fx)
            if taxable:
                pool.total_acb_cad += delta + addbacks.get(int(trade.id), Decimal("0"))
                pool.quantity += quantity
            holding_quantities[key] += quantity
            acquisitions.append(
                Acquisition(int(trade.id), key[0], key[1], trade.date, quantity, taxable)
            )
        elif trade.action == TradeAction.sell:
            quantity_sold = abs(trade.quantity)
            held_quantity = holding_quantities[key]
            if quantity_sold > held_quantity:
                raise api_error(
                    422,
                    f"Trade #{trade.id} sells {quantity_sold} {security.symbol} but only "
                    f"{held_quantity} held in account {key[0]}",
                    "oversell",
                )
            if not taxable:
                # Registered sells do not enter capital-gains reporting, but the
                # holding chronology above still validates them and feeds the
                # cross-account superficial-loss rule.
                holding_quantities[key] -= quantity_sold
                continue
            fx = rates.rate(security.listing_currency, trade.date, security.symbol)
            proceeds = cents(quantity_sold * trade.price_native * fx)
            allocated = cents(pool.total_acb_cad * quantity_sold / held_quantity)
            outlays = cents(trade.commission * fx)
            gain = proceeds - allocated
            disposals.append(
                Disposal(
                    trade_id=int(trade.id),
                    account_id=key[0],
                    security_id=key[1],
                    symbol=security.symbol,
                    date=trade.date,
                    quantity_sold=quantity_sold,
                    proceeds_cad=proceeds,
                    acb_cad=allocated,
                    outlays_commissions_cad=outlays,
                    gain_cad=gain,
                )
            )
            pool.total_acb_cad -= allocated
            pool.quantity -= quantity_sold
            holding_quantities[key] -= quantity_sold
        elif trade.action == TradeAction.transfer_out:
            # Share disposition: reduce the holding at average cost. A transfer
            # realizes no gain, so no Disposal is recorded (docs/06).
            quantity_out = abs(trade.quantity)
            held_quantity = holding_quantities[key]
            if quantity_out > held_quantity:
                raise api_error(
                    422,
                    f"Trade #{trade.id} transfers out {quantity_out} {security.symbol} but only "
                    f"{held_quantity} held in account {key[0]}",
                    "oversell",
                )
            if not taxable:
                holding_quantities[key] -= quantity_out
                continue
            # quantity_out == 0 is a CASH transfer row (docs/02 input
            # convention: the amount rides in price_native, no shares move —
            # M113 lets a Wealthsimple MoneyMovement row land here). Nothing
            # is allocated, and `held_quantity` may itself be 0, which the
            # division below would raise on (0/0).
            allocated = (
                cents(pool.total_acb_cad * quantity_out / held_quantity)
                if held_quantity
                else Decimal("0")
            )
            pool.total_acb_cad -= allocated
            pool.quantity -= quantity_out
            holding_quantities[key] -= quantity_out
        elif trade.action == TradeAction.return_of_capital:
            fx = rates.rate(security.listing_currency, trade.date, security.symbol)
            roc_cad = cents(trade.price_native * fx)  # gross cash amount (docs/02 convention)
            if not taxable:
                continue
            reduced = pool.total_acb_cad - roc_cad
            if reduced < 0:
                # Floor at zero; the excess is a capital gain in the year realized.
                roc_gains.append(
                    RocGainRecord(key[1], security.symbol, key[0], trade.date, -reduced)
                )
                pool.total_acb_cad = Decimal("0")
            else:
                roc_gains.append(
                    RocGainRecord(key[1], security.symbol, key[0], trade.date, Decimal("0"))
                )
                pool.total_acb_cad = reduced
        elif trade.action == TradeAction.corporate_action_adjustment:
            if trade.quantity > 0:
                # Resulting absolute share count; total ACB preserved (splits/consolidations).
                holding_quantities[key] = trade.quantity
                if taxable:
                    pool.quantity = trade.quantity
            elif trade.quantity == 0:
                # price_native carries a SIGNED CAD ACB delta applied directly to the pool.
                if taxable:
                    pool.total_acb_cad += cents(trade.price_native)
            else:
                raise api_error(
                    422,
                    f"Trade #{trade.id}: corporate_action_adjustment needs quantity>0 "
                    "(resulting share count) or quantity=0 (signed ACB delta)",
                    "invalid_corporate_action",
                )
        # Remaining actions (dividend, distribution, fee, contribution, …) are cash-only
        # events with no pool effect.

    # Zeroed pools (fully disposed positions, registered accounts) are not cached.
    return (
        {k: v for k, v in pools.items() if v.quantity != 0 or v.total_acb_cad != 0},
        disposals,
        roc_gains,
        acquisitions,
    )


def compute_user_tax_state(session: Session, user_id: int, *, allow_fetch: bool = True) -> TaxState:
    """Full-history evaluation over every trade of the user (docs/02 invariant).

    Denial add-backs change later pool math, so replay + denial detection iterate
    to a fixpoint (converges immediately in practice; bounded for safety). Every
    call re-evaluates open 30-day windows — provisional results self-correct on
    the next rebuild/report run.

    `allow_fetch=False` keeps the sweep strictly cache-only (docs/03): the
    docs/07 backfill is skipped and an unresolvable date answers the plain
    `fx_rate_missing` 422, which is what seeding paths rely on.
    """
    rows = _load_rows(session, user_id)
    symbols = {int(s.id): s.symbol for _, _, s in rows}
    # One fx statement for the whole computation (docs/34) — the replay used to
    # ask the table once per non-CAD trade, and it runs up to
    # _MAX_REFINEMENT_PASSES times.
    rates = FxRateBook(session, rows, allow_fetch=allow_fetch)
    addbacks: dict[int, Decimal] = {}
    denials: list[SuperficialLossDenial] = []
    pools: dict[PoolKey, PoolState] = {}
    disposals: list[Disposal] = []
    roc_gains: list[RocGainRecord] = []
    acquisitions: list[Acquisition] = []
    for _ in range(_MAX_REFINEMENT_PASSES):
        pools, disposals, roc_gains, acquisitions = _replay(session, rows, addbacks, rates)
        denials, new_addbacks = evaluate_superficial_losses(disposals, acquisitions, rows, symbols)
        if new_addbacks == addbacks:
            break
        addbacks = new_addbacks
    return TaxState(pools, disposals, roc_gains, acquisitions, denials)


def rebuild_user_pools(
    session: Session, user_id: int, *, allow_fetch: bool = True
) -> dict[str, int]:
    """Rebuild ALL of the user's non-registered pools from trades.

    The single sanctioned write path to `acb_pools` (docs/02 invariant). Runs
    inside the CALLER's transaction — never commits. The trades write path calls
    this before its commit so pools can never drift from history.

    `allow_fetch=False` is the ingestion-side contract (docs/03): demo seeding
    passes it so a boot never opens a network connection, and its fx gaps keep
    deferring on `fx_rate_missing` exactly as docs/07 states.
    """
    state = compute_user_tax_state(session, user_id, allow_fetch=allow_fetch)
    # Flush DELETEs before INSERTing replacements — SQLAlchemy orders inserts first
    # within a flush, which would trip uq_acb_pools_account_security otherwise.
    for existing in session.exec(select(AcbPool).where(AcbPool.user_id == user_id)).all():
        session.delete(existing)
    session.flush()
    inserted = 0
    for (account_id, security_id), pool in sorted(state.pools.items()):
        if pool.quantity == 0 and pool.total_acb_cad == 0:
            continue  # fully disposed — nothing left to cache
        session.add(
            AcbPool(
                user_id=user_id,
                account_id=account_id,
                security_id=security_id,
                quantity=pool.quantity,
                total_acb_cad=pool.total_acb_cad,
            )
        )
        inserted += 1
    row_count = len(_load_rows(session, user_id))
    session.flush()
    return {"pools": inserted, "trades": row_count}

"""Re-cache one security's closes in the ledger's own frame (M149, docs/07 §Price frame).

The defect this exists for, measured live in prod on 2026-10-11: security 86
(``VGT``) had its whole cached year written from Yahoo's SPLIT-ADJUSTED
history, while its trades are the broker's unadjusted export. VGT split 8:1 on
2026-04-21, so Yahoo answered ``91.57`` for 2025-11-18 and the 51 shares bought
that day at ``$732.59`` were valued at 51 x 91.57 ≈ $4.7k instead of $37.4k —
on days the YTD table drew as valued, not red.

The repair is deliberately a RE-FETCH, never a scale-up of the rows already in
the table. Multiplying the cached rows by the ratio would be the obvious move
and it is NOT idempotent: run it twice and the window is worth 64x. Refetching
goes through the same seam every writer uses (``market_data.fetch_history``,
which normalizes to as-traded before returning), so the repaired window is the
window the sweep would have written all along, and running it again produces
exactly the same numbers.

Write rules — identical to every other writer of ``prices`` (docs/07):

- a missing ``(security, date)`` row is INSERTED with ``source=yfinance``;
- an existing ``source=yfinance`` row is UPDATEd to the refetched close
  (that is the whole point — a wrong machine row is not manual data);
- an existing ``source=manual`` row is counted and LEFT ALONE (manual wins);
- an existing row already holding that exact close is counted, not rewritten,
  so a re-run reports ``updated=0``.

The M141 verdict is booked like any other fetch (``record_quote_outcome``),
so a manual repair that genuinely cannot find the symbol arms the same flag a
"Try refresh" would, and one that succeeds clears it.

Privacy mode refuses from inside the fetch seam (``403
privacy_mode_blocks_external_api``) and the transaction is never opened.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlmodel import Session, select

from app.core.errors import api_error
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.services import market_data

#: `prices.price` is `Numeric(20, 8)` (docs/02), so that is the precision the
#: column can hold. The repair compares what it fetched against what is STORED,
#: and compares it at that precision — otherwise the round-trip through the
#: column leaves the last two digits short on every run and a re-report would
#: rewrite the same number forever instead of reporting `unchanged`.
PRICE_SCALE = Decimal("1e-8")


@dataclass
class SplitRepair:
    """What one repair did — asserted by tests, printed by the CLI."""

    security_id: int
    symbol: str
    start: dt.date
    end: dt.date
    #: Closes the refetch produced for the window, in the LEDGER's frame.
    fetched: int = 0
    #: Every split Yahoo reported for the symbol — the events the refetch
    #: un-adjusted the window with. Empty when the symbol has split nowhere.
    splits: list[tuple[dt.date, Decimal]] = field(default_factory=list)
    updated: int = 0
    inserted: int = 0
    unchanged: int = 0
    #: ``source=manual`` rows the repair refused to touch (manual wins).
    skipped_manual: int = 0
    #: Machine-readable refusal token; ``None`` when closes landed.
    refused: str | None = None

    def summary(self) -> str:
        if self.refused is not None:
            return f"NOT REPAIRED {self.symbol} (security {self.security_id}): {self.refused}"
        applied = ", ".join(f"{day.isoformat()} x{ratio}" for day, ratio in self.splits)
        return (
            f"REPAIRED {self.symbol} (security {self.security_id}) "
            f"{self.start} -> {self.end}: splits [{applied or 'none'}], "
            f"{self.updated} updated, {self.inserted} inserted, "
            f"{self.unchanged} unchanged, {self.skipped_manual} manual row(s) kept"
        )


def repair_split_window(
    session: Session,
    security: Security,
    start: dt.date,
    end: dt.date,
    *,
    apply: bool = True,
) -> SplitRepair:
    """Re-cache ``[start, end]`` for one security through the normalizing seam.

    Commit-on-success, like the shared price-cache writer: the seam this
    replaces (``market_data._cache_closes``) has always committed its own rows.
    Refuses with ``422 invalid_date_range`` for an inverted window and reports
    ``no_closes`` — writing nothing — when Yahoo answers nothing for it.

    ``apply=False`` is the CLI's dry run: the same fetch and the same counts,
    then a ROLLBACK. It also skips the M141 bookkeeping, because that writes
    unconditionally and a preview has no verdict to book.
    """
    if start > end:
        raise api_error(
            422,
            "start_date must not be after end_date",
            "invalid_date_range",
        )
    result = SplitRepair(
        security_id=security.id or -1,
        symbol=security.symbol,
        start=start,
        end=end,
    )
    # ONE fetch, through the seam every writer uses. The normalization this
    # repair exists to apply already happened inside it, which is exactly why
    # a re-run is a no-op rather than a second scaling.
    #
    # M175 (docs/07 §Provider-wait ledger): both callers READ first — the CLI
    # does `session.get(Security, id)` and `_window_line`, the route does
    # `owned_or_404` — and everything this function writes happens BELOW this
    # fetch, so nothing is uncommitted here and the release cannot eat the
    # `apply=False` dry run's rollback (that rollback discards the re-cached
    # closes, which do not exist yet at this point).
    market_data.release_before_provider_wait(session, security)
    # Ditto: the verdict read after this fetch is THIS call's, never a
    # previous fetch's (the rule `backfill_range` follows).
    market_data.reset_quote_verdict()
    closes = market_data.fetch_history(security.symbol, start, end)
    result.splits = list(market_data.last_splits())
    verdict, reason = market_data.last_quote_verdict()
    if apply:
        market_data.record_quote_outcome(session, security, verdict, reason)
    if not closes:
        result.refused = "no_closes"
        if not apply:
            session.rollback()
        return result
    result.fetched = len(closes)
    existing = {
        row.date: row
        for row in session.exec(select(Price).where(Price.security_id == security.id)).all()
    }
    for day, fetched in sorted(closes.items()):
        price = fetched.quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
        row = existing.get(day)
        if row is None:
            session.add(
                Price(
                    user_id=security.user_id,
                    security_id=security.id,
                    date=day,
                    price=price,
                    source=PriceSource.yfinance,
                    # Fetch provenance audit (Option A, t_bd5ec95f): the split
                    # repair RE-DERIVES as-traded closes from one max frame —
                    # a computed correction, not a per-day provider quote —
                    # so no fetch stamp is claimed. NULL stays (docs/07).
                    fetched_at=None,
                )
            )
            result.inserted += 1
        elif row.source == PriceSource.manual:
            result.skipped_manual += 1
        elif row.price != price:
            row.price = price
            # The value was RE-DERIVED (as-traded rewrite), not re-quoted, so
            # any older fetch stamp no longer describes this value — clear it
            # rather than let it claim provenance it does not have.
            row.fetched_at = None
            session.add(row)
            result.updated += 1
        else:
            result.unchanged += 1
    if apply:
        session.commit()
    else:
        session.rollback()
    return result

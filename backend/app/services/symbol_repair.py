"""Correct a `securities` row whose symbol was typed as a NAME (M142, docs/07).

The bug this exists for: Mark typed ``APOTEX`` — a company name — into the
add form, so the row stored a symbol no quote will ever answer for, and the
watched security sat in "No prices yet" forever. Yahoo's search endpoint
knows the listing (``APTX.TO`` — APOTEX HEALTH CORP, Toronto, CAD), so the
fix is to correct the ROW, not to throw it away.

A correction, never a delete + re-create: the row keeps its id, so the
watchlist attachment, trades, cached prices, ACB pools, fundamentals and
composition rows pointing at it all stay attached (docs/02 declares no ON
DELETE rules — ``DELETE /securities`` pre-checks every reference and refuses
any in-use row, which is exactly why re-adding loses history).

Three refusals, all reported rather than worked around:

- **no answer / ambiguous answer** — a repair must never pick a listing for
  the user; several matches mean the human chooses one and repairs again
  (the add form's chooser, docs/09).
- **the target key is taken** — ``uq_securities_symbol (user_id, symbol,
  exchange)`` would reject the UPDATE, and silently folding two rows
  together is the M117 dedup's job (``services/security_dedup.py``).
- **privacy mode** — resolution is an external fetch (docs/11 step 6).

The caller owns the transaction (same discipline as ``security_dedup``) —
except that the OPTIONAL backfill commits, because the shared price-cache
writer (``market_data._cache_closes``) has always committed its own rows.

M143 additions:

- **A bare-vs-``.TO`` collision refuses** (``resolve_symbol`` answers
  ``ambiguous`` with no ``pick``): a repair never picks the market for you,
  it surfaces the chooser's message and the operator names the listing.
- **A row whose identity moved drops its cached closes** before the
  backfill refetches them — the old bars were fetched for the listing the
  row just stopped pointing at (security 294 cached a year of *Virtus
  Silvant* bars under a Vanguard symbol). Manual price rows survive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlmodel import Session, select

from app.models.prices import Price, PriceSource
from app.models.securities import Security, SecurityKind
from app.services import market_data

# Same span POST /securities/{id}/refresh backfills (docs/05).
REPAIR_BACKFILL_DAYS = 365


@dataclass
class SymbolRepair:
    """What one repair did — asserted by tests, printed by the operator CLI."""

    security_id: int
    changed: bool
    reason: str
    before: dict[str, str] = field(default_factory=dict)
    after: dict[str, str] = field(default_factory=dict)
    #: Closes the follow-up backfill cached (0 when `backfill=False`).
    prices_backfilled: int = 0
    #: M143: cached closes DROPPED because they belonged to the listing the
    #: row just stopped pointing at (0 when the row's identity did not move).
    prices_deleted: int = 0

    def summary(self) -> str:
        verb = "REPAIRED" if self.changed else "unchanged"
        lines = [f"{verb} security {self.security_id}: {self.reason}"]
        if self.changed:
            lines.append(
                "  "
                + " -> ".join(
                    [
                        f"{self.before.get('symbol')} ({self.before.get('listing_currency')},"
                        f" {self.before.get('name')})",
                        f"{self.after.get('symbol')} ({self.after.get('listing_currency')},"
                        f" {self.after.get('name')})",
                    ]
                )
            )
            lines.append(
                f"  prices backfilled: {self.prices_backfilled} ({REPAIR_BACKFILL_DAYS}-day window)"
            )
            if self.prices_deleted:
                lines.append(f"  prices dropped (previous listing): {self.prices_deleted}")
                if not self.prices_backfilled:
                    lines.append(
                        "  WARNING: the previous listing's closes were dropped and NOTHING "
                        "replaced them — the row has no price history until a refresh lands"
                    )
        return "\n".join(lines)


def _snapshot(security: Security) -> dict[str, str]:
    return {
        "symbol": security.symbol,
        "name": security.name,
        "kind": security.kind.value if hasattr(security.kind, "value") else str(security.kind),
        "listing_currency": security.listing_currency,
        "exchange": security.exchange,
    }


def repair_symbol(
    session: Session,
    security: Security,
    *,
    pick: str | None = None,
    backfill: bool = True,
    backfill_days: int | None = None,
) -> SymbolRepair:
    """Point `security` at the listing its (or ``pick``'s) symbol resolves to.

    Resolution goes through the SAME seam the add form uses
    (``market_data.resolve_symbol``), so ``APOTEX`` becomes the one listing
    Yahoo's search returns. A name matching TWO listings refuses — a repair
    must never pick for the user — and ``pick`` is how the human answers
    that: the operator reads the chooser (or the add form's) and names the
    canonical symbol, which is then resolved and applied. ``pick`` may also
    simply be a better spelling of the stored symbol.

    Only fields market data owns are written (symbol, name, kind, listing
    currency, exchange): a hand-entered price row, a manual fundamentals
    block and manual composition rows are untouched (manual wins, docs/07).

    With ``backfill=True`` the normal refresh machinery runs afterwards
    (``market_data.backfill_range`` — the exact call behind
    ``POST /securities/{id}/refresh``), so closes land in the same operator
    step and the M129 day-delta has a prior session to measure against.
    ``backfill_days`` widens the default :data:`REPAIR_BACKFILL_DAYS` window;
    a repair that DROPPED a longer series always refetches that span, no
    matter which of the two is larger (docs/07 §Symbol repair).
    """
    before = _snapshot(security)
    query = pick.strip().upper() if pick else security.symbol
    # M143: with no `pick` the row is resolved exactly as the add form would
    # resolve it, so a bare-vs-`.TO` collision comes back `ambiguous` and the
    # repair REFUSES with the chooser's message ("pick one and repair again")
    # instead of silently keeping whichever side answered first. An explicit
    # `pick` IS that choice, so it skips the collision check.
    resolution = market_data.resolve_symbol(query, disambiguate=pick is None)
    target = resolution["result"]
    if target is None:
        reason = (
            "no listing resolves for this symbol"
            if resolution["match"] == "none"
            else f"several listings match '{query}' — pick one and repair again"
        )
        return SymbolRepair(security_id=security.id, changed=False, reason=reason)

    new_symbol = str(target["symbol"])
    new_exchange = str(target["exchange"])
    # `str()` round-trip so the value is a real member, not whatever object
    # the resolution payload happens to carry.
    new_kind = SecurityKind(str(target["kind"]))
    new_name = str(target["name"])
    new_currency = str(target["listing_currency"])
    clash = session.exec(
        select(Security).where(
            Security.user_id == security.user_id,
            Security.symbol == new_symbol,
            Security.exchange == new_exchange,
            Security.id != security.id,
        )
    ).first()
    if clash is not None:
        return SymbolRepair(
            security_id=security.id,
            changed=False,
            reason=(
                f"security {clash.id} already owns {new_symbol}/{new_exchange} — "
                "merge the duplicates first (services/security_dedup.py)"
            ),
        )

    target_snapshot = {
        "symbol": new_symbol,
        "name": new_name,
        "kind": new_kind.value,
        "listing_currency": new_currency,
        "exchange": new_exchange,
    }
    if target_snapshot == before:
        return SymbolRepair(
            security_id=security.id,
            changed=False,
            reason=f"already canonical ({new_symbol})",
            before=before,
            after=before,
        )

    security.symbol = new_symbol
    security.name = new_name
    security.kind = new_kind
    security.listing_currency = new_currency
    security.exchange = new_exchange
    # M141: the dead-symbol verdict was about the OLD spelling — a corrected
    # symbol has not been judged yet, so the guard starts over (the backfill
    # below is its first real fetch).
    security.quote_miss_count = 0
    security.dead_symbol = False
    security.dead_symbol_reason = None
    session.add(security)
    session.flush()

    # M143: `fetch_history` keys off the SYMBOL, so a row that changes its
    # symbol must not keep closes fetched under the old one — a year of
    # Virtus bars next to a Vanguard symbol is exactly the "wrong
    # instrument" bug this repair exists to remove. Manual rows are never
    # touched (manual wins, docs/07); the follow-up backfill refetches the
    # dropped span under the corrected symbol.
    #
    # The SYMBOL is deliberately the only trigger. An exchange- or name-only
    # correction is metadata about the SAME instrument: those closes came
    # from the listing the row still points at (a bare symbol that misses
    # always falls through to `.TO`, which is where the bars came from), and
    # dropping them would trade a cosmetic fix for lost history.
    prices_deleted = 0
    dropped_from: date | None = None
    if new_symbol != before["symbol"]:
        stale = session.exec(
            select(Price).where(
                Price.security_id == security.id,
                Price.source != PriceSource.manual,
            )
        ).all()
        for row in stale:
            session.delete(row)
        prices_deleted = len(stale)
        if stale:
            dropped_from = min(row.date for row in stale)
        session.flush()

    repair = SymbolRepair(
        security_id=security.id,
        changed=True,
        reason=f"resolved via {resolution['match']} ({security.symbol})",
        before=before,
        after=_snapshot(security),
        prices_deleted=prices_deleted,
    )
    if backfill:
        window_days = backfill_days or REPAIR_BACKFILL_DAYS
        start = date.today() - timedelta(days=window_days)
        # Dropping bars the refetch would not reach back would be a silent
        # loss of history, so the window opens at the OLDEST dropped close
        # (a 5-year series drops and comes back as a 5-year series).
        if dropped_from is not None and dropped_from < start:
            start = dropped_from
        repair.prices_backfilled = market_data.backfill_range(
            session,
            security,
            start,
            date.today(),
        )
    return repair

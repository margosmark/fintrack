"""Link provider fund legs to the security row their symbol answers for.

The READ side is `market_data.security_symbol_index` (M154, docs/07 §Symbol
matching) — the one index every fund-composition seam matches through: the
EXACT spelling first, then an unambiguous bare stem. This module is the
OPERATOR half of the same rule, so rows written before an index rule (or
before a repair that made a stem unambiguous) get linked without waiting for a
re-import to rewrite them.

`M154` shipped its relink as ad-hoc SQL that nobody could re-run; M164 turned
it into this function plus `scripts/relink_holdings.py` (dry-run by default,
same discipline as `scripts/repair_symbol.py`), because the run it exists for
is not a one-off: `security_dedup`'s venue-alias repair (M164,
docs/07 §Venue aliases) removes rows that were making a stem LOOK ambiguous,
and the legs behind that stem link on the next run of this pass.

What it will never do: guess. A stem two listings share (`005930.KS` +
`005930.KQ`, `VIU` + `VIU.TO`) carries no index entry at all, so its legs stay
NULL exactly as M154 pinned — the deepening loop's `lookup_symbol` picks the
listing there, never a guess. Weights are never touched: linking sets one FK
column (`etf_holdings.holding_security_id`), never a `weight_pct`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models.etf_holdings import EtfHolding
from app.models.securities import Security
from app.services.market_data import security_symbol_index


@dataclass
class RelinkReport:
    """What one pass did — asserted by tests, printed by the script."""

    #: ``(holding_id, its symbol, security_id, that row's symbol)`` per leg
    #: this pass linked (or would link) — both spellings, because the leg's
    #: symbol is frequently the BARE one and the row's the suffixed one.
    linked: list[tuple[int, str, int, str]] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return not self.linked

    def summary(self) -> str:
        lines = [f"linked={len(self.linked)}"]
        for holding_id, holding_symbol, security_id, security_symbol in self.linked:
            lines.append(
                f"  holding {holding_id} ({holding_symbol}) -> "
                f"security {security_id} ({security_symbol})"
            )
        return "\n".join(lines)


def relink_holdings(session: Session, *, apply: bool = False) -> RelinkReport:
    """Link every unlinked leg whose symbol answers through the index.

    Flushes only when ``apply`` — the caller owns the transaction, and a dry
    run must be able to roll everything back (the data-purge rule: rehearse
    with aggregates first).
    """
    report = RelinkReport()
    for user_id, legs in _unlinked_legs_by_user(session).items():
        # Ordered by id so the index's first-wins ties are deterministic —
        # the same discipline every write seam applies.
        index = security_symbol_index(
            session.exec(
                select(Security).where(Security.user_id == user_id).order_by(Security.id)
            ).all()
        )
        for leg in legs:
            symbol = (leg.holding_symbol or "").strip().upper()
            if not symbol:
                continue
            matched = index.get(symbol)
            if matched is None:
                continue
            report.linked.append(
                (leg.id, symbol, matched.id, (matched.symbol or "").strip().upper())
            )
            if apply:
                leg.holding_security_id = matched.id
                session.add(leg)
    if apply and report.linked:
        session.flush()
    return report


def _unlinked_legs_by_user(session: Session) -> dict[int, list[EtfHolding]]:
    """Unlinked provider legs with a symbol, per user, ordered by id."""
    rows = session.exec(
        select(EtfHolding)
        .where(EtfHolding.holding_security_id.is_(None), EtfHolding.holding_symbol.is_not(None))
        .order_by(EtfHolding.id)
    ).all()
    by_user: dict[int, list[EtfHolding]] = defaultdict(list)
    for row in rows:
        by_user[row.user_id].append(row)
    return by_user

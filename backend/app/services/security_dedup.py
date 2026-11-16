"""Repair pass for duplicated `securities` rows (docs/02 uniqueness, M117).

The unique key is ``(user_id, symbol, exchange)``, but nothing used to
normalize either part of it: the investment-CSV import matched and stored the
raw symbol cell, so a file reading ``veqt`` could sit next to the ``VEQT``
row created through ``POST /securities``. Two rows for one listing then split
a holding's trades, prices and ACB pool across a "stale" twin, and valuation
would pick whichever row the query happened to reach first.

Two idempotent passes over the whole table (row counts here are tiny — this
is a repair, not a hot path):

1. **merge** — group rows whose *normalized* ``(user_id, symbol, exchange)``
   collides, keep the row with the most price history (tie: lowest id), move
   every reference onto it, and drop it. A group whose ``acb_pools`` would
   collide is left untouched and reported instead: two pools for one
   ``(account_id, security_id)`` mean cost basis was computed twice, and
   silently choosing a winner would corrupt ACB (docs/06).

   The venue part of that key is the ALIAS TABLE's canonical spelling
   (M164, ``services/venue_codes.py``, docs/07 §Venue aliases): the provider
   spells Shanghai ``SHH`` on one run and ``SHSE`` on the next, and
   ``uq_securities_symbol`` happily keeps both rows for the one listing
   behind them. A survivor of such a group is rewritten to the canonical
   code, so the next provider answer collides instead of duplicating. Codes
   the table does not know pass through untouched — ``VT``/TSX and
   ``VT``/NYSE remain two listings, because the rule is per pair of venue
   codes and never per symbol.

2. **normalize** — store ``symbol``/``exchange`` as ``strip().upper()``.
   Safe only after (1), because merging leaves exactly one row per normalized
   key, so the UPDATE cannot trip ``uq_securities_symbol``. A row whose
   merge was refused (conflicting ACB pools) still shares its key with the
   survivor, so it is reported untouched instead of rewritten into a
   constraint violation.

Every write path normalizes ``symbol``/``exchange`` casing now
(``POST``/``PATCH /securities``, the CSV import), and every PROVIDER payload
is canonicalized for venue spelling (``market_data._normalize_exchange``) —
so this cleans rows written before either and stays re-runnable: a second
call reports nothing to do.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlmodel import Session, select

from app.models.acb_pools import AcbPool
from app.models.etf_holdings import EtfHolding
from app.models.fundamentals import Fundamentals
from app.models.prices import Price
from app.models.securities import Security
from app.models.trades import Trade
from app.models.watchlist import WatchlistItem
from app.services.venue_codes import canonical_exchange


def norm_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


def norm_exchange(exchange: str) -> str:
    return (exchange or "").strip().upper()


@dataclass
class RepairReport:
    """What one repair pass did — asserted by tests, logged by the migration."""

    normalized: int = 0
    #: ``(kept_id, sorted loser ids)`` per merged group.
    merged: list[tuple[int, tuple[int, ...]]] = field(default_factory=list)
    #: Rows whose ``security_id`` moved onto a keeper, per table name.
    repointed: dict[str, int] = field(default_factory=dict)
    #: Rows dropped because the keeper already owned that slot, per table name.
    deleted: dict[str, int] = field(default_factory=dict)
    #: ``(user_id, loser ids, reason)`` for groups deliberately left alone.
    skipped: list[tuple[int, tuple[int, ...], str]] = field(default_factory=list)
    #: ``(user_id, security_id, reason)`` for rows left un-normalized because
    #: another surviving row already owns their normalized key — i.e. a merge
    #: this pass refused. Reported, never silently rewritten into a
    #: constraint violation.
    unnormalized: list[tuple[int, int, str]] = field(default_factory=list)
    #: ``(security_id, the spelling it carried, the canonical one)`` per row
    #: whose venue code was rewritten to the alias table's canonical spelling
    #: because its group merged (M164, docs/07 §Venue aliases). Never applied
    #: to a row the merge did not touch — a user-typed venue code that has no
    #: twin stays exactly as typed.
    canonicalized: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def loser_ids(self) -> list[int]:
        return [loser for _kept, losers in self.merged for loser in losers]

    @property
    def done(self) -> bool:
        return not (
            self.merged
            or self.skipped
            or self.unnormalized
            or self.normalized
            or self.canonicalized
        )

    def summary(self) -> str:
        lines = [
            f"normalized={self.normalized} merged_groups={len(self.merged)} "
            f"merged_away={len(self.loser_ids)}"
        ]
        for kept, losers in self.merged:
            lines.append(f"  kept {kept} <- merged {list(losers)}")
        for security_id, was, canonical in self.canonicalized:
            lines.append(f"  exchange {was} -> {canonical} on {security_id}")
        for table, count in sorted(self.repointed.items()):
            lines.append(f"  repointed {count} {table} row(s)")
        for table, count in sorted(self.deleted.items()):
            lines.append(f"  deleted {count} {table} row(s)")
        for user_id, losers, reason in self.skipped:
            lines.append(f"  SKIPPED user={user_id} ids={list(losers)}: {reason}")
        for user_id, security_id, reason in self.unnormalized:
            lines.append(f"  NOT NORMALIZED user={user_id} security={security_id}: {reason}")
        return "\n".join(lines)


# The ONLY `securities` columns this repair reads or writes — exactly the four
# that existed when migration `0045_merge_dup_securities` shipped (docs/02).
#
# Pinned as an explicit column list on purpose: that migration runs this
# service at a point in the chain where LATER columns do not exist yet. A
# full-entity `select(Security)` expands to every mapped column, so the first
# revision to add one to `securities` broke `alembic upgrade head` from scratch
# with `UndefinedColumn` — M141 hit exactly that adding `quote_miss_count`.
# A data migration must never depend on the current shape of the ORM model
# (AGENTS §6); this list is the frozen contract that makes it independent.
_SEC_KEYS = (Security.id, Security.user_id, Security.symbol, Security.exchange)


@dataclass(frozen=True)
class SecurityRef:
    """A `securities` row as this repair needs it: the four key columns only.

    Deliberately not a `Security`: holding an ORM entity here would drag every
    mapped column back into the SELECT (see `_SEC_KEYS`).
    """

    id: int
    user_id: int
    symbol: str
    exchange: str


def repair_securities(session: Session) -> RepairReport:
    """Run both passes. Flushes only — the caller owns the transaction."""
    report = RepairReport()
    for members in _duplicate_groups(session):
        _merge_group(session, members, report)
    _normalize_all(session, report)
    return report


def _all_rows(session: Session) -> list[SecurityRef]:
    """Every `securities` row, read through the frozen `_SEC_KEYS` column list
    so the shape of the ORM model cannot reach this code path."""
    return [
        SecurityRef(id=r.id, user_id=r.user_id, symbol=r.symbol, exchange=r.exchange)
        for r in session.exec(select(*_SEC_KEYS)).all()
    ]


def _duplicate_groups(session: Session) -> list[list[SecurityRef]]:
    """Groups of rows that are ONE listing: same user, same symbol, same VENUE.

    The venue part is the ALIAS TABLE's canonical spelling, not the stored one
    (M164, docs/07 §Venue aliases) — the provider spells Shanghai `SHH` on one
    run and `SHSE` on the next, and `uq_securities_symbol` then keeps both
    rows, splitting one listing's prices and trades across two ids. A venue the
    table does not know passes through unchanged, so `VT`/TSX and `VT`/NYSE
    stay two listings: the rule is per pair of venue codes, never per symbol.
    """
    groups: dict[tuple[int, str, str], list[SecurityRef]] = defaultdict(list)
    for row in _all_rows(session):
        key = (row.user_id, norm_symbol(row.symbol), canonical_exchange(row.exchange))
        groups[key].append(row)
    return [
        sorted(members, key=lambda m: m.id)  # stable order across runs
        for members in groups.values()
        if len(members) > 1
    ]


def _keeper(session: Session, members: list[SecurityRef]) -> SecurityRef:
    """The survivor: the row holding the most price history — its closes are
    what a position should be valued from. Ties go to the lowest id."""
    counts: dict[int, int] = defaultdict(int)
    ids = [member.id for member in members]
    # Single-column select → SQLModel yields scalars, not Row tuples.
    for security_id in session.exec(
        select(Price.security_id).where(Price.security_id.in_(ids))
    ).all():
        counts[security_id] += 1
    return sorted(members, key=lambda m: (-counts[m.id], m.id))[0]


def _merge_group(session: Session, members: list[SecurityRef], report: RepairReport) -> None:
    keeper = _keeper(session, members)
    losers = [m for m in members if m.id != keeper.id]

    reason = _acb_conflict_reason(session, keeper, losers)
    if reason is not None:
        report.skipped.append((keeper.user_id, tuple(m.id for m in losers), reason))
        return

    _move_trades(session, keeper, losers, report)
    _move_etf_holdings(session, keeper, losers, report)
    _move_watchlist(session, keeper, losers, report)
    _move_prices(session, keeper, losers, report)
    _move_fundamentals(session, keeper, losers, report)

    # Core DELETE, not `session.delete(entity)`: we hold SecurityRef values, so
    # there is no ORM identity to expire — and loading one would re-introduce
    # the full-column SELECT this refactor exists to avoid. Table-level, not
    # ORM-level, so the statement is independent of the mapped columns too.
    # Flush the repoints FIRST: the `_move_*` calls above only staged ORM
    # changes, and a Core DELETE executes immediately — without this the FK
    # updates would still be pending and the DELETE would trip the very
    # foreign keys the moves exist to clear.
    session.flush()
    loser_ids = [m.id for m in losers]
    session.execute(sa.delete(Security.__table__).where(Security.__table__.c.id.in_(loser_ids)))
    # The DELETE bypassed the unit of work, so drop the losers from the
    # identity map by hand — otherwise a caller that still holds one (the
    # tests, or any session that loaded the group) would read a row that no
    # longer exists instead of the None it must see.
    _expunge_deleted(session, loser_ids)
    session.flush()
    report.merged.append((keeper.id, tuple(sorted(m.id for m in losers))))
    _canonicalize_keeper(session, keeper, losers, report)


def _canonicalize_keeper(
    session: Session, keeper: SecurityRef, losers: list[SecurityRef], report: RepairReport
) -> None:
    """Give the survivor the venue code the alias table calls canonical (M164).

    The group only exists because two spellings of ONE venue were stored, so
    the survivor must carry the spelling every future write will produce
    (``market_data._normalize_exchange`` canonicalizes provider payloads) —
    otherwise the twin the next provider run answered with a different code
    would be a fresh duplicate. Only a group that really merged is rewritten: a
    lone row with an unusual venue code is the user's own statement of venue
    and is left exactly as typed.

    Runs AFTER the losers are deleted and flushed, so the UPDATE cannot trip
    ``uq_securities_symbol`` — no other row can own
    ``(user_id, symbol, canonical)`` key, because every row that would is in
    this very group.
    """
    canonical = canonical_exchange(keeper.exchange)
    if canonical == norm_exchange(keeper.exchange):
        return
    session.execute(
        sa.update(Security.__table__)
        .where(Security.__table__.c.id == keeper.id)
        .values(exchange=canonical)
    )
    report.canonicalized.append((keeper.id, norm_exchange(keeper.exchange), canonical))
    for obj in list(session.identity_map.values()):
        if isinstance(obj, Security) and obj.id == keeper.id:
            session.expire(obj, ("exchange",))


def _expunge_deleted(session: Session, ids: list[int]) -> None:
    """Detach any in-memory `Security` whose row a Core DELETE just removed.

    Core statements bypass SQLAlchemy's identity map, so an instance the
    caller loaded before the repair would otherwise keep serving stale
    attributes (or raise `ObjectDeletedError` on the next flush).
    """
    for obj in list(session.identity_map.values()):
        if isinstance(obj, Security) and obj.id in ids:
            session.expunge(obj)


def _acb_conflict_reason(
    session: Session, keeper: SecurityRef, losers: list[SecurityRef]
) -> str | None:
    """Refuse a merge that would put two ACB pools on one account+security.

    ``uq_acb_pools_account_security`` means at most one pool per
    ``(account_id, security_id)``. If the keeper and a loser — or two losers —
    already pool the same account, the quantities were accumulated twice and
    picking either row would silently drop the other's cost basis.
    """
    account_ids = [
        pool.account_id
        for pool in session.exec(
            select(AcbPool).where(AcbPool.security_id.in_([keeper.id, *[m.id for m in losers]]))
        ).all()
    ]
    if len(account_ids) != len(set(account_ids)):
        return "conflicting acb_pools rows — cost basis must be merged by hand (docs/06)"
    return None


def _bump(report: RepairReport, table: str, key: str, count: int) -> None:
    if not count:
        return
    bucket = report.repointed if key == "repointed" else report.deleted
    bucket[table] = bucket.get(table, 0) + count


def _move_trades(
    session: Session, keeper: SecurityRef, losers: list[SecurityRef], report: RepairReport
) -> None:
    ids = [loser.id for loser in losers]
    moved = 0
    for trade in session.exec(select(Trade).where(Trade.security_id.in_(ids))).all():
        trade.security_id = keeper.id
        session.add(trade)
        moved += 1
    _bump(report, "trades", "repointed", moved)


def _move_etf_holdings(
    session: Session, keeper: SecurityRef, losers: list[SecurityRef], report: RepairReport
) -> None:
    ids = [loser.id for loser in losers]
    moved = 0
    for row in session.exec(
        select(EtfHolding).where(
            EtfHolding.security_id.in_(ids) | EtfHolding.holding_security_id.in_(ids)
        )
    ).all():
        if row.security_id in ids:
            row.security_id = keeper.id
        if row.holding_security_id in ids:
            row.holding_security_id = keeper.id
        session.add(row)
        moved += 1
    _bump(report, "etf_holdings", "repointed", moved)


def _move_watchlist(
    session: Session, keeper: SecurityRef, losers: list[SecurityRef], report: RepairReport
) -> None:
    """``uq_watchlist_user_security`` — keeper's entry wins; one loser row may
    be adopted if the keeper has none, the rest are dropped."""
    ids = [loser.id for loser in losers]
    rows = sorted(
        session.exec(select(WatchlistItem).where(WatchlistItem.security_id.in_(ids))).all(),
        key=lambda row: row.id,
    )
    if session.exec(select(WatchlistItem).where(WatchlistItem.security_id == keeper.id)).first():
        for row in rows:
            session.delete(row)
        _bump(report, "watchlist", "deleted", len(rows))
        return
    if not rows:
        return
    survivor, *rest = rows
    survivor.security_id = keeper.id
    session.add(survivor)
    _bump(report, "watchlist", "repointed", 1)
    for row in rest:
        session.delete(row)
    _bump(report, "watchlist", "deleted", len(rest))


def _move_prices(
    session: Session, keeper: SecurityRef, losers: list[SecurityRef], report: RepairReport
) -> None:
    """``uq_prices_security_date`` — the keeper's close for a date wins."""
    ids = [loser.id for loser in losers]
    owned = {
        row.date for row in session.exec(select(Price).where(Price.security_id == keeper.id)).all()
    }
    moved = dropped = 0
    for row in session.exec(select(Price).where(Price.security_id.in_(ids))).all():
        if row.date in owned:
            session.delete(row)
            dropped += 1
            continue
        row.security_id = keeper.id
        session.add(row)
        owned.add(row.date)
        moved += 1
    _bump(report, "prices", "repointed", moved)
    _bump(report, "prices", "deleted", dropped)


def _move_fundamentals(
    session: Session, keeper: SecurityRef, losers: list[SecurityRef], report: RepairReport
) -> None:
    """``uq_fundamentals_security`` — the keeper's fundamentals block wins."""
    ids = [loser.id for loser in losers]
    rows = sorted(
        session.exec(select(Fundamentals).where(Fundamentals.security_id.in_(ids))).all(),
        key=lambda row: row.id,
    )
    if session.exec(select(Fundamentals).where(Fundamentals.security_id == keeper.id)).first():
        for row in rows:
            session.delete(row)
        _bump(report, "fundamentals", "deleted", len(rows))
        return
    if not rows:
        return
    survivor, *rest = rows
    survivor.security_id = keeper.id
    session.add(survivor)
    _bump(report, "fundamentals", "repointed", 1)
    for row in rest:
        session.delete(row)
    _bump(report, "fundamentals", "deleted", len(rest))


def _normalize_all(session: Session, report: RepairReport) -> None:
    """Store every survivor's key normalized, lowest id first.

    Only safe because merging already collapsed each duplicate group to one
    row — except for a group this pass refused (conflicting ACB pools). There
    the loser is still alive and still owns the raw spelling, so rewriting it
    would be exactly the ``uq_securities_symbol`` violation the repair exists
    to prevent. Such rows are left alone and reported.
    """
    # Read through the frozen column list (see `_SEC_KEYS`): at the point in
    # the chain where migration 0045 runs, columns added by LATER revisions do
    # not exist yet, and a full-entity SELECT would ask for them.
    rows = sorted(_all_rows(session), key=lambda row: row.id)
    occupied = {(row.user_id, row.symbol, row.exchange) for row in rows}
    normalized_ids: list[int] = []
    for row in rows:
        current = (row.user_id, row.symbol, row.exchange)
        target = (row.user_id, norm_symbol(row.symbol), norm_exchange(row.exchange))
        if target == current:
            continue
        if target in occupied:
            report.unnormalized.append(
                (
                    row.user_id,
                    row.id,
                    "normalized key already owned by a row this pass did not merge",
                )
            )
            continue
        occupied.discard(current)
        # Core UPDATE for the same reason as the reads: the statement must not
        # depend on which columns the ORM model happens to map today.
        session.execute(
            sa.update(Security.__table__)
            .where(Security.__table__.c.id == row.id)
            .values(symbol=target[1], exchange=target[2])
        )
        occupied.add(target)
        normalized_ids.append(row.id)
        report.normalized += 1
    if normalized_ids:
        # Same identity-map hygiene as the DELETE above: the UPDATE bypassed
        # the unit of work, so any in-memory copy must be refreshed on next
        # read rather than keep serving the raw spelling.
        for obj in list(session.identity_map.values()):
            if isinstance(obj, Security) and obj.id in normalized_ids:
                session.expire(obj, ("symbol", "exchange"))
    session.flush()

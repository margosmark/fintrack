"""provider_fund_registry access — the DB is the registry (M168, docs/07 §Provider registry).

Every coverage lookup and every coverage write goes through here. Three
rules this module exists to keep:

1. **A short session of its own on the network paths.** The provider chain
   READS the registry and then waits on the issuer; if that read ran on the
   caller's session the connection would sit `idle in transaction` for the
   whole fetch — the exact row M148 drained the pool with and M152's prod
   sweep caught (docs/01 § Sessions and the event loop). So ``session=None``
   (what ``services/market_data.py`` always passes) opens a session, does one
   statement, commits and closes it *before* the wait. A caller that already
   holds a session — the breakdown, reading the two sector families — passes
   it instead and never pays a second connection.
2. **One upsert, never a plain insert.** Two enrichment tasks may discover
   the same ticker at the same moment; `ON CONFLICT (family, ticker) DO
   UPDATE` makes the loser's write a no-op instead of a 23P01 crash.
3. **No network.** This module reads and writes rows; the probes that decide
   WHICH rows to write live in ``services/market_data.py``, the only module
   allowed to reach the outside (AGENTS §2). The CLI
   (``fintrackerctl provider-funds``) composes both.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from app.models.provider_registry import (
    ProviderFundFamily,
    ProviderFundRegistry,
    ProviderFundStatus,
)


@contextmanager
def _own_session() -> Iterator[Session]:
    """A session that is closed before the caller can wait on anything.

    Imported lazily: ``app.api.deps`` builds its engine from settings at call
    time, and importing it at module scope would freeze the DATABASE_URL
    before a test could swap it (docs/01, AGENTS §5).
    """
    from app.api.deps import get_engine

    with Session(get_engine(), expire_on_commit=False) as session:
        yield session
        session.commit()


@contextmanager
def _session_or(session: Session | None) -> Iterator[tuple[Session, bool]]:
    if session is not None:
        yield session, False
        return
    with _own_session() as own:
        yield own, True


def get(
    family: ProviderFundFamily | str,
    ticker: str,
    *,
    session: Session | None = None,
) -> ProviderFundRegistry | None:
    """The one row for ``(family, ticker)``, or None. Never a network call."""
    with _session_or(session) as (s, _):
        row = s.exec(
            select(ProviderFundRegistry).where(
                ProviderFundRegistry.family == ProviderFundFamily(family),
                ProviderFundRegistry.ticker == ticker.strip().upper(),
            )
        ).first()
        if row is not None:
            # Detach the attributes the caller needs: an own-session read is
            # committed and closed the moment this context exits, and an
            # expired instance would re-open a transaction to answer a
            # `.status` read later.
            s.expunge(row) if session is None else None
        return row


def load_family(
    family: ProviderFundFamily | str,
    *,
    verified_only: bool = True,
    session: Session | None = None,
) -> dict[str, ProviderFundRegistry]:
    """Every row of one family, keyed by ticker — ONE query, ONE session.

    The breakdown loads ``static_sector`` and ``fund_sector_mix`` this way so
    a 25,000-row exposure walk does dict lookups instead of 25,000
    statements (docs/16 §Sector chain).
    """
    with _session_or(session) as (s, _):
        stmt = select(ProviderFundRegistry).where(
            ProviderFundRegistry.family == ProviderFundFamily(family)
        )
        if verified_only:
            stmt = stmt.where(ProviderFundRegistry.status == ProviderFundStatus.verified)
        rows = s.exec(stmt.order_by(ProviderFundRegistry.id)).all()
        if session is None:
            for row in rows:
                s.expunge(row)
        return {row.ticker: row for row in rows}


def upsert(
    family: ProviderFundFamily | str,
    ticker: str,
    *,
    status: ProviderFundStatus,
    run: str,
    port_id: str | None = None,
    venue_suffix: str | None = None,
    endpoint: str | None = None,
    sector: str | None = None,
    sector_mix: dict | None = None,
    session: Session | None = None,
) -> ProviderFundRegistry:
    """Insert or update one row; the issuer's latest verdict always wins.

    ``verified_at`` is stamped only for a ``verified`` verdict (a miss row's
    freshness lives in ``last_probe_at``), and ``miss_count`` is incremented
    on every miss so a re-verification sweep can tell a ticker that went dark
    from one that was never probed.
    """
    now = datetime.now(UTC)
    # Payload fields are written only when the CALLER supplied one: a re-verify
    # that answers 200 for a fund it never re-resolved must not blank the
    # endpoint it still has, and a `miss` verdict must not erase the portId
    # either (the row stops answering, but the evidence stays). To CLEAR a
    # field, `remove` the row and `set` it again.
    values: dict[str, object] = {
        "status": status,
        "verified_by_run": run,
        "updated_at": now,
    }
    if status is ProviderFundStatus.verified:
        for key, value in (
            ("port_id", port_id),
            ("venue_suffix", venue_suffix),
            ("endpoint", endpoint),
            ("sector", sector),
            ("sector_mix", sector_mix),
        ):
            if value is not None:
                values[key] = value
        values.update({"verified_at": now, "miss_count": 0, "last_probe_at": now})
    else:
        values["last_probe_at"] = now

    stmt = (
        pg_insert(ProviderFundRegistry.__table__)  # type: ignore[attr-defined]
        .values(
            family=ProviderFundFamily(family),
            ticker=ticker.strip().upper(),
            miss_count=0 if status is ProviderFundStatus.verified else 1,
            **{k: v for k, v in values.items() if k not in ("updated_at", "miss_count")},
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["family", "ticker"],
            set_=dict(values)
            | (
                {"miss_count": ProviderFundRegistry.__table__.c.miss_count + 1}
                if status is ProviderFundStatus.miss
                else {"miss_count": 0}
            ),
        )
        .returning(ProviderFundRegistry.__table__.c.id)  # type: ignore[attr-defined]
    )

    with _session_or(session) as (s, owns):
        row_id = s.execute(stmt).scalar_one()
        if owns:
            s.commit()
        row = s.get(ProviderFundRegistry, row_id)
        if row is not None and owns:
            s.expunge(row)
        return row  # type: ignore[return-value]


def list_rows(
    *,
    family: ProviderFundFamily | str | None = None,
    status: ProviderFundStatus | str | None = None,
    session: Session | None = None,
) -> list[ProviderFundRegistry]:
    """Every matching row, ordered (family, ticker) — what the CLI prints."""
    with _session_or(session) as (s, _):
        stmt = select(ProviderFundRegistry)
        if family is not None:
            stmt = stmt.where(ProviderFundRegistry.family == ProviderFundFamily(family))
        if status is not None:
            stmt = stmt.where(
                ProviderFundRegistry.status
                == (
                    status if isinstance(status, ProviderFundStatus) else ProviderFundStatus(status)
                )
            )
        rows = s.exec(stmt.order_by(ProviderFundRegistry.family, ProviderFundRegistry.ticker)).all()
        if session is None:
            for row in rows:
                s.expunge(row)
        return rows


def delete(
    family: ProviderFundFamily | str,
    ticker: str,
    *,
    session: Session | None = None,
) -> bool:
    """Drop one row (the CLI's `remove`, and the tests' cleanup)."""
    with _session_or(session) as (s, owns):
        row = s.exec(
            select(ProviderFundRegistry).where(
                ProviderFundRegistry.family == ProviderFundFamily(family),
                ProviderFundRegistry.ticker == ticker.strip().upper(),
            )
        ).first()
        if row is None:
            return False
        s.delete(row)
        if owns:
            s.commit()
        return True


def stale(
    *,
    older_than_days: int = 180,
    family: ProviderFundFamily | str | None = None,
    session: Session | None = None,
) -> list[ProviderFundRegistry]:
    """``verified`` rows the quarterly sweep should re-probe (docs/07).

    Older than ``older_than_days`` (180 by default) or never verified at all.
    ``miss`` rows are excluded: a miss is a verdict about the ticker, not a
    coverage claim that can go stale.
    """
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    with _session_or(session) as (s, _):
        stmt = select(ProviderFundRegistry).where(
            ProviderFundRegistry.status == ProviderFundStatus.verified
        )
        if family is not None:
            stmt = stmt.where(ProviderFundRegistry.family == ProviderFundFamily(family))
        rows = s.exec(
            stmt.where(
                (ProviderFundRegistry.verified_at.is_(None))
                | (ProviderFundRegistry.verified_at < cutoff)
            ).order_by(ProviderFundRegistry.family, ProviderFundRegistry.ticker)
        ).all()
        if session is None:
            for row in rows:
                s.expunge(row)
        return rows


def census(*, session: Session | None = None) -> dict[str, dict[str, int]]:
    """``{family: {verified: n, miss: n}}`` — what `provider-funds list` prints."""
    with _session_or(session) as (s, _):
        rows = s.exec(select(ProviderFundRegistry)).all()
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = out.setdefault(str(row.family), {"verified": 0, "miss": 0})
        bucket[row.status.value] += 1
    return out

#!/usr/bin/env python3
"""Correct a `securities` row whose symbol was typed as a NAME (M142, docs/07).

Thin CLI over `app.services.symbol_repair.repair_symbol`. The row this exists
for is prod security 293: Mark typed `APOTEX` — a company name — into the add
form, so the watched row stored a symbol no quote answers for. Yahoo's search
knows the listing (`APTX.TO` — APOTEX HEALTH CORP, Toronto, CAD); the repair
corrects the ROW in place, so the watchlist attachment, trades, cached prices
and ACB pools stay with it. Re-adding the symbol instead would drop all of it.

Dry run by default: the report prints what WOULD change, nothing is written.

    cd backend
    uv run python scripts/repair_symbol.py --security-id 293              # report
    uv run python scripts/repair_symbol.py --security-id 293 --apply      # write

`APOTEX` matches TWO listings (APTX.TO + APTX.NE), so that row needs the
human's pick first — a repair never guesses a listing:

    uv run python scripts/repair_symbol.py --security-id 293 --symbol APTX.TO --apply

From outside (the container owns DATABASE_URL and the app package; the host
has no Python env — same reason `scripts/` is COPY'd into the image):

    ssh example.internal 'cd /home/developer/fin_tracker && docker exec fin_tracker-api-1 \
        python scripts/repair_symbol.py --security-id 293 --apply'

Refusals are reported, never worked around (docs/03 §strict beats silent): no
listing resolves, several listings match (the human picks one and repairs
again), or another row already owns the target `(user_id, symbol, exchange)`
key — merging duplicates is `scripts/` + `services/security_dedup.py`'s job.

The follow-up price backfill is the NORMAL refresh machinery
(`market_data.backfill_range`, the call behind `POST /securities/{id}/refresh`)
and it commits, so `--apply` writes the correction and the closes together.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# `python scripts/repair_symbol.py` puts scripts/, not backend/, at
# sys.path[0] — same guard the other scripts in this directory use.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# SQLModel's Session, not sqlalchemy's: the whole app is written against
# `.exec(...)`, which plain SQLAlchemy Session does not have.
from sqlmodel import Session, select

from app.api.deps import get_engine
from app.models.prices import Price
from app.models.securities import Security
from app.models.watchlist import WatchlistItem
from app.services.symbol_repair import repair_symbol


def _row_line(session: Session, security: Security) -> str:
    """One line per row — the before/after evidence an operator reads back."""
    prices = session.exec(
        select(Price).where(Price.security_id == security.id).order_by(Price.date)
    ).all()
    watched = session.exec(
        select(WatchlistItem).where(WatchlistItem.security_id == security.id)
    ).all()
    newest = prices[-1] if prices else None
    return (
        f"  security {security.id} (user {security.user_id}): {security.symbol} — "
        f"{security.name} [{security.kind.value}] {security.listing_currency} "
        f"@ {security.exchange} | prices {len(prices)}"
        f"{f', newest {newest.date} @ {newest.price}' if newest is not None else ''}"
        f" | watchlist rows {len(watched)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Correct a securities row whose symbol was typed as a name"
    )
    parser.add_argument("--security-id", type=int, required=True, help="row to repair")
    parser.add_argument(
        "--symbol",
        dest="pick",
        default=None,
        help=(
            "canonical ticker to repair TO (e.g. APTX.TO) — required when the "
            "stored value is a NAME that matches several listings, and when "
            "the stored ticker COLLIDES across markets (bare VGRO answers "
            "NYSEArca while VGRO.TO answers the TSX), since a repair never "
            "picks the market for you"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the correction + backfill closes (default: dry run)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help=(
            "backfill window in days (default 365). A repair that dropped a "
            "longer series refetches the dropped span automatically."
        ),
    )
    args = parser.parse_args(argv)

    with Session(get_engine(), expire_on_commit=False) as session:
        security = session.get(Security, args.security_id)
        if security is None:
            print(f"no security with id {args.security_id}")
            return 1
        print("before:")
        print(_row_line(session, security))
        repair = repair_symbol(
            session, security, pick=args.pick, backfill=args.apply, backfill_days=args.days
        )
        print(repair.summary())
        print("after:")
        print(_row_line(session, security))
        if not args.apply:
            session.rollback()
            print("dry run — nothing written (re-run with --apply)")
            return 0
        if repair.changed:
            session.commit()
            print("committed")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

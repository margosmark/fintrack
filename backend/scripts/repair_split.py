#!/usr/bin/env python3
"""Re-cache one security's closes for a window in the ledger's own frame (M149).

Thin CLI over `app.services.split_repair.repair_split_window` — the operator
half of `POST /securities/{id}/split-repair` for a run that has to be scripted
or audited from outside the stack. The window this exists for is prod security
86 `VGT`: cached from Yahoo's split-ADJUSTED history while its trades are the
broker's unadjusted export, so 51 shares bought at $732.59 on 2025-11-18 were
valued at 51 x 91.57 ≈ $4.7k instead of $37.4k (docs/07 §Price frame).

Dry run by default: the same fetch and the same counts, nothing written.

    cd backend
    uv run python scripts/repair_split.py --security-id 86 \
        --start 2025-10-09 --end 2026-10-09              # report
    uv run python scripts/repair_split.py --security-id 86 \
        --start 2025-10-09 --end 2026-10-09 --apply      # write

From outside the stack (the image COPYs scripts/, the container owns the app
package and DATABASE_URL):

    ssh example.internal 'cd /home/developer/fin_tracker && docker exec fin_tracker-api-1 \
        python scripts/repair_split.py --security-id 86 \
        --start 2025-10-09 --end 2026-10-09 --apply'

Re-derives the window rather than scaling the stored rows, so running it again
reports `updated= 0` instead of double-scaling the price. `source=manual` rows
are counted and never written. Pair it with `scripts/split_probe.py` to see
the window's trade-date ratios fall back to ~1.0 afterwards.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# `python scripts/repair_split.py` puts scripts/, not backend/, at sys.path[0].
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select

from app.api.deps import get_engine
from app.models.prices import Price
from app.models.securities import Security
from app.services.split_repair import repair_split_window


def _window_line(session: Session, security: Security, start: date, end: date) -> str:
    """The yfinance rows the repair would touch, so a run can be read back."""
    rows = session.exec(
        select(Price).where(
            Price.security_id == security.id,
            Price.date >= start,
            Price.date <= end,
        )
    ).all()
    manual = [row for row in rows if row.source.value == "manual"]
    first = rows[0] if rows else None
    last = rows[-1] if rows else None
    return (
        f"  {len(rows)} cached row(s) in window, {len(manual)} manual"
        f"{f', first {first.date} @ {first.price}' if first is not None else ''}"
        f"{f', last {last.date} @ {last.price}' if last is not None else ''}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-cache one security's closes in the ledger's as-traded frame"
    )
    parser.add_argument("--security-id", type=int, required=True, help="row to repair")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="write the repaired window (default: dry run)"
    )
    args = parser.parse_args(argv)

    with Session(get_engine(), expire_on_commit=False) as session:
        security = session.get(Security, args.security_id)
        if security is None:
            print(f"no security with id {args.security_id}")
            return 1
        print("before:")
        print(
            f"  security {security.id} (user {security.user_id}): {security.symbol} "
            f"[{security.kind.value}] {security.listing_currency}"
        )
        print(_window_line(session, security, args.start, args.end))
        result = repair_split_window(session, security, args.start, args.end, apply=args.apply)
        print(result.summary())
        if args.apply:
            print("after:")
            print(_window_line(session, security, args.start, args.end))
            print("committed")
        else:
            print("dry run — nothing written (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

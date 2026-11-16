#!/usr/bin/env python3
"""Deep price backfill per HELD security — full available history (M144, docs/07).

The daily sweep keeps the last ~365 days fresh and nothing else; a position
bought before that window has no close to value it with, and ONE held security
without a close blanks every bucket it was held in (the portfolio merge
propagates `None`). This is the one-time, re-runnable sweep that walks each
security from its OWN need-date — `min(committed trade date, watchlist added
date)` — to today, dead symbols and privacy-mode tenants skipped, insert-only
so a re-run adds nothing.

Each row's listing is chosen by comparing the answering candidates against the
security's own recorded trade prices (docs/07 §Series validation): a bare
ticker can answer as a different instrument (`VUS` = Virtus U.S. Dividend ETF
on NYSEArca vs `VUS.TO` on the TSX), and caching that would fabricate a
valuation. A row no candidate agrees with is left alone and reported.

Dry run by default — it prints every row's verdict, nothing is written.

    cd backend
    uv run python scripts/deep_price_backfill.py                      # report
    uv run python scripts/deep_price_backfill.py --user-id 343        # one tenant
    uv run python scripts/deep_price_backfill.py --apply              # write

From outside the stack the same run is:

    docker exec -i fin_tracker-api-1 python scripts/deep_price_backfill.py --apply

(the image COPYs `scripts/`, the container owns the app package, the settings
and `DATABASE_URL`).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

# `python scripts/deep_price_backfill.py` puts scripts/, not backend/, at
# sys.path[0] — same guard the other scripts in this directory use.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session

from app.api.deps import get_engine
from app.services.deep_backfill import deep_backfill_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deep price backfill per held security (M144, docs/07)"
    )
    parser.add_argument("--user-id", type=int, help="limit the sweep to one tenant")
    parser.add_argument("--apply", action="store_true", help="write the closes (default: dry run)")
    parser.add_argument(
        "--probe-dead",
        action="store_true",
        help="also probe rows flagged dead_symbol (the M141 escape hatch)",
    )
    args = parser.parse_args(argv)

    with Session(get_engine(), expire_on_commit=False) as session:
        results = deep_backfill_all(
            session,
            user_id=args.user_id,
            probe_dead=args.probe_dead,
            apply=args.apply,
        )

    if not results:
        print("no held or watched securities — nothing to do")
        return 0

    for result in results:
        print(f"  {result.describe()}")

    inserted = sum(result.inserted for result in results if result.skipped is None)
    by_skip = Counter(result.skipped for result in results if result.skipped is not None)
    mode = "WROTE" if args.apply else "would write (dry run)"
    print(
        f"{len(results)} securities: {inserted} close(s) {mode}; "
        + (
            ", ".join(f"{count}x {reason}" for reason, count in sorted(by_skip.items()))
            if by_skip
            else "nothing skipped"
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Benchmark price series backfill — the four vs-Benchmark picks (M173, docs/28).

The panel reads ``benchmark_prices`` and never fetches, so the series has to be
written by someone: the scheduled worker does it a day at a time
(``services/benchmark_refresh.py``), and this script does the one-time deep
walk — from the earliest COMMITTED trade in the ledger to today, so every
tenant's window is covered from its own start.

The four picks and their LISTING VENUES live in one place
(``services/benchmarks.py::BENCHMARKS``): ``SPY`` on NYSE Arca (USD) and
``XIU.TO`` / ``XBB.TO`` / ``XAW.TO`` on the TSX (CAD). A benchmark is cached
against its BENCHMARK symbol, never a user ``securities`` row, so no tenant's
picker grows a fund no trade explains (docs/28 decision 5).

Insert-only: a re-run adds nothing and never rewrites a cached close.

Dry run by default — it prints every symbol's verdict, nothing is written.

    cd backend
    uv run python scripts/backfill_benchmarks.py            # report
    uv run python scripts/backfill_benchmarks.py --apply    # write

From outside the stack the same run is:

    docker exec -i fin_tracker-api-1 python scripts/backfill_benchmarks.py --apply

(the image COPYs `scripts/`, the container owns the app package, the settings
and `DATABASE_URL`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# `python scripts/backfill_benchmarks.py` puts scripts/, not backend/, at
# sys.path[0] — same guard the other scripts in this directory use.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session

from app.api.deps import get_engine
from app.services import benchmarks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark price series backfill (M173, docs/28)")
    parser.add_argument("--apply", action="store_true", help="write the closes (default: dry run)")
    parser.add_argument("--start", help="override the window start (YYYY-MM-DD)")
    parser.add_argument(
        "--ignore-privacy",
        action="store_true",
        help="fetch even while a tenant has privacy_mode on (docs/11)",
    )
    args = parser.parse_args(argv)

    start = None
    if args.start:
        import datetime as dt

        try:
            start = dt.date.fromisoformat(args.start)
        except ValueError:
            raise SystemExit(f"--start {args.start!r} is not a YYYY-MM-DD date") from None

    with Session(get_engine(), expire_on_commit=False) as session:
        results = benchmarks.backfill_all(
            session,
            start=start,
            dry_run=not args.apply,
            respect_privacy=not args.ignore_privacy,
        )
        coverage = {
            result.symbol: benchmarks.series_bounds(session, result.symbol) for result in results
        }

    if results and all(result.skipped == "privacy_blocked" for result in results):
        print("privacy_mode is on for a tenant — no benchmark fetch was attempted (docs/11)")
        return 2

    for result in results:
        first, last = coverage[result.symbol]
        covered = "no series" if first is None else f"{first} -> {last}"
        print(f"  {result.describe()}   [cached: {covered}]")

    inserted = sum(result.inserted for result in results if result.skipped is None)
    mode = "WROTE" if args.apply else "would write (dry run)"
    skipped = [result.symbol for result in results if result.skipped is not None]
    print(
        f"{len(results)} benchmarks: {inserted} close(s) {mode}; "
        + (f"skipped: {', '.join(skipped)}" if skipped else "nothing skipped")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

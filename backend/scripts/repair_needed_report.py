#!/usr/bin/env python3
"""Which owned `securities` rows still point at the wrong listing? (M155, docs/07)

Thin CLI over `app.services.repair_report.scan` — the standing replacement for
the one-off repairs M141/M142 needed by hand (prod 293 = `APOTEX` stored as a
symbol, 294 = *Virtus Silvant* under a Vanguard holding, 296 = `VFV, USD`).
Each of those was found because a human happened to look; this asks every
owned row the two questions that would have caught it — *do two listings
answer for your symbol?* (bare collision) and *does what the listing says NOW
match what you store?* (live-symbol heuristics) — and prints the rows that
disagree with the ledger.

READ-ONLY: it never writes a row. Every `repair` finding carries the exact
`scripts/repair_symbol.py` command that would fix it, and a human applies it,
because a repair never picks a listing (docs/07 §Symbol repair).

    cd backend
    uv run python scripts/repair_needed_report.py                     # every tenant
    uv run python scripts/repair_needed_report.py --user-id 343       # one tenant
    uv run python scripts/repair_needed_report.py --user-id 343 --json

From outside the stack (the image COPYs `scripts/`, the container owns the app
package, the settings and `DATABASE_URL`):

    ssh example.internal \
      'docker exec fin_tracker-api-1 python scripts/repair_needed_report.py --user-id 343'

Exit codes: `0` nothing to do, `1` at least one row needs a repair, `2` the
scan itself could not run (the canary probe missed — market data unreachable,
so no row was judged rather than a wall of false `no listing` findings).

**Periodic.** A report that must be remembered is a report that rots, so the
scheduled run is part of the deliverable: `~/.hermes/profiles/soft-dev/scripts/
fintrack_repair_report.sh` runs the command above weekly and delivers the
output (docs/07 §Repair-needed report — same host-level scheduler the M148
autoheal watchdog runs on, because AGENTS §2 keeps fetching OUT of endpoints).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# `python scripts/repair_needed_report.py` puts scripts/, not backend/, at
# sys.path[0] — same guard the other scripts in this directory use.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.deps import get_engine
from app.services.repair_report import CANARY_SYMBOL, scan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report owned securities rows whose stored identity disagrees with the market"
    )
    parser.add_argument(
        "--user-id",
        type=int,
        help="limit the scan to one tenant (default: every tenant)",
    )
    parser.add_argument(
        "--canary",
        default=CANARY_SYMBOL,
        help=f"symbol that must answer before any row is judged (default {CANARY_SYMBOL})",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="seconds to pause between row probes — Yahoo pacing for a large tenant",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "keep yfinance's own ERROR lines on stderr. Off by default: "
            "probing a bare ticker's `.TO` sibling 404s on purpose, and those "
            "expected misses would otherwise be interleaved with the report "
            "(the canary, not the log, is what reports a real outage)."
        ),
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="machine-readable output instead of the human report",
    )
    args = parser.parse_args(argv)
    if not args.verbose:
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    report = scan(
        get_engine(),
        user_id=args.user_id,
        canary=args.canary,
        sleep=args.sleep,
    )
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render())
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Link provider fund legs whose symbol now answers through the M154 index.

Thin CLI over `app.services.holding_relink.relink_holdings`. The legs this
exists for are the ones a shared stem kept NULL: `security_dedup`'s
venue-alias repair (M164, docs/07 §Venue aliases) merges the duplicate
`securities` rows that made the stem look ambiguous, and this pass links the
legs that were waiting behind it — without waiting for a re-import to rewrite
every fund.

Matching is `market_data.security_symbol_index`, the SAME index the write
seams match through: the exact spelling first, then an unambiguous bare stem.
A stem two listings share is never guessed at — those legs stay NULL and the
deepening loop's `lookup_symbol` picks the listing (docs/07 §Symbol
matching). `weight_pct` is never touched: linking sets one FK column.

Dry run by default: the report prints what WOULD change, nothing is written.

    cd backend
    uv run python scripts/relink_holdings.py            # report
    uv run python scripts/relink_holdings.py --apply    # write

From outside (the container owns DATABASE_URL and the app package; the host
has no Python env — same reason `scripts/` is COPY'd into the image):

    ssh example.internal 'cd /home/developer/fin_tracker && docker exec fin_tracker-api-1 \
        python scripts/relink_holdings.py --apply'

Idempotent by construction: a linked leg is no longer unlinked, so a second
run reports `linked=0`. Run the dedup MIGRATION first when the blocker was a
duplicate row — this pass only matches what is in the table.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# `python scripts/relink_holdings.py` puts scripts/, not backend/, at
# sys.path[0] — same guard the other scripts in this directory use.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# SQLModel's Session, not sqlalchemy's: the whole app is written against
# `.exec(...)`, which plain SQLAlchemy Session does not have.
from sqlmodel import Session

from app.api.deps import get_engine
from app.services.holding_relink import relink_holdings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write the links (default: report only)"
    )
    args = parser.parse_args()

    with Session(get_engine(), expire_on_commit=False) as session:
        report = relink_holdings(session, apply=args.apply)
        if args.apply and report.linked:
            session.commit()
    verb = "Linked" if args.apply else "Would link"
    print(f"{verb} {len(report.linked)} leg(s):")
    for line in report.summary().splitlines()[1:]:
        print(line)


if __name__ == "__main__":
    main()

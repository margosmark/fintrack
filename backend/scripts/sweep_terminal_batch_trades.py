#!/usr/bin/env python3
"""Repair batches that reached `discarded`/`reverted` while rows were still linked.

Thin CLI over `app.services.batch_hygiene.sweep_terminal_batch_trades`
(docs/03 §Staged-review flow; M112). Those orphans come from the pre-M112
discard/revert, which deleted a batch's `transactions` but not its `trades` —
prod batch 176 (`reverted`) kept 71 trades and 177 (`discarded`) kept 70.

Dry run by default: the report prints what WOULD go, nothing is written.

    cd backend
    uv run python scripts/sweep_terminal_batch_trades.py                 # report
    uv run python scripts/sweep_terminal_batch_trades.py --apply         # delete
    uv run python scripts/sweep_terminal_batch_trades.py --apply --batch-id 176 --batch-id 177

The database comes from `DATABASE_URL` (settings default otherwise). The
delete is transactional: `--apply` commits the report and the row deletions
together, including the `acb_pools` rebuilds, so a failure leaves the database
exactly as it found it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# `python scripts/sweep_terminal_batch_trades.py` puts scripts/, not backend/,
# at sys.path[0] — same guard the other scripts in this directory use.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# SQLModel's Session, not sqlalchemy's: the whole app (and `acb_pools`'s
# `rebuild_user_pools`) is written against `.exec(...)`, which plain SQLAlchemy
# Session does not have.
from sqlmodel import Session

from app.api.deps import get_engine
from app.services.batch_hygiene import sweep_terminal_batch_trades


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repair trades/transactions left linked to discarded or reverted batches"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete (default: dry run, report only)",
    )
    parser.add_argument(
        "--batch-id",
        dest="batch_ids",
        type=int,
        action="append",
        help="restrict to these batch ids (repeatable); default: every orphan",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="restrict to one tenant (repeatable use: per-account repairs)",
    )
    args = parser.parse_args(argv)

    with Session(get_engine(), expire_on_commit=False) as db:
        report = sweep_terminal_batch_trades(
            db, user_id=args.user_id, batch_ids=args.batch_ids, apply=args.apply
        )
        if args.apply:
            db.commit()
        else:
            db.rollback()  # belt and braces: a dry run cannot write anything

    print(json.dumps(report, indent=2, default=str))
    if not args.apply and report["batches"]:
        print(
            "DRY RUN — nothing was deleted. Re-run with --apply.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

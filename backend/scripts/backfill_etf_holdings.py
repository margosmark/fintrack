#!/usr/bin/env python3
"""One-time fund-composition seed (M147, docs/05 §Auto-enrichment).

The exposure engine decomposes funds through ``etf_holdings`` rows that
nobody ever fetched — every pie showed ~85% Unclassified. This script is
the backfill: it walks every traded fund-shaped security (all users, or
one with ``--user-id``), asks the M147 provider chain for its
composition, and — with ``--apply`` — writes it through the SAME service
function the manual import endpoint uses (``market_data.
import_fund_composition``), then deepens into sub-funds (M19). Dry run by
default: it fetches and REPORTS the per-fund row counts, writes nothing.

    cd backend
    uv run python scripts/backfill_etf_holdings.py            # report
    uv run python scripts/backfill_etf_holdings.py --apply    # write
    uv run python scripts/backfill_etf_holdings.py --user-id 343 --apply

Notes:

* candidates are securities with at least one committed trade and ZERO
  composition rows — a fund that already has rows (manual or provider)
  is never auto-touched (manual-first convention, docs/02);
* `kind=stock` candidates are included on purpose: import-created fund
  rows often carry the stock default (trades._resolve_security), and the
  provider answer is what proves a ticker is a fund — the write path
  repairs the kind when it does;
* the provider chain is issuer-CSV-first, Vanguard full holdings second,
  yfinance top-holdings last
  (docs/07); a ticker no provider covers reports `no provider answer`
  and stays unclassified with the UI's honesty copy naming it;
* this runs with the operator's privileges and no request context, so
  the per-user privacy lock is not bound here — same stance as the other
  admin scripts. A privacy-mode USER's rows are still never fetched in
  the request path; the background auto-enrichment (the real long-term
  mechanism) honors the lock per user;
* a run lasts MINUTES (one provider fetch per fund, plus --sleep), so no
  session may outlive the statement that opened it: the candidate query
  and the per-fund row count each get a session of their own that closes
  before the next fetch (docs/01 § "Sessions and the event loop"). A
  session held across the loop is what a prod `pg_stat_activity` sweep
  reported as an M148-class leak (M152).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# `python scripts/backfill_etf_holdings.py` puts scripts/, not backend/, at
# sys.path[0] — same guard the other scripts in this directory use.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import exists
from sqlmodel import Session, select

from app.api.deps import get_engine
from app.models.etf_holdings import EtfHolding
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade
from app.services import market_data
from app.services.fund_enrichment import _run_enrichment

_FUND_KINDS = (SecurityKind.etf.value, SecurityKind.mutual_fund.value)


def _candidates(session: Session, user_id: int | None, *, refresh: bool) -> list[Security]:
    """Traded securities with zero composition rows, funds first.

    ``refresh`` widens the set to EVERY fund-kind security — including
    sub-funds the M19 deepening created with a top-slice-only import —
    so a second pass upgrades them through the full provider chain
    (import_fund_composition only ever replaces non-manual rows, so this
    is safe to re-run).
    """
    no_rows = ~exists().where(EtfHolding.security_id == Security.id)
    traded = exists().where(Trade.security_id == Security.id)
    if refresh:
        query = select(Security).where(Security.kind.in_(_FUND_KINDS))
    else:
        # Funds first (False < True in a boolean sort), so the highest-
        # impact candidates run before any single-stock probes.
        query = (
            select(Security).where(traded, no_rows).order_by(Security.kind.in_(_FUND_KINDS).desc())
        )
    query = query.order_by(Security.user_id, Security.id)
    if user_id is not None:
        query = query.where(Security.user_id == user_id)
    return list(session.exec(query).all())


def _written_row_count(security_id: int) -> int:
    """Composition rows the fund has NOW, read in a session of its own.

    Scoped to this one read and CLOSED before the next provider fetch
    (docs/01 § "Sessions and the event loop"): nothing here outlives the
    call, so the loop below can wait on the network with no transaction
    open anywhere (M152).
    """
    with Session(get_engine(), expire_on_commit=False) as session:
        rows = session.exec(select(EtfHolding).where(EtfHolding.security_id == security_id)).all()
        return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill etf_holdings composition for every traded fund (M147)"
    )
    parser.add_argument("--user-id", type=int, default=None, help="restrict to one user")
    parser.add_argument(
        "--max-funds", type=int, default=500, help="stop after N candidates (safety)"
    )
    parser.add_argument("--sleep", type=float, default=0.5, help="seconds between provider fetches")
    parser.add_argument("--apply", action="store_true", help="write the rows (default: dry run)")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "re-import EVERY fund-kind security (including sub-funds the "
            "first pass created top-slice-only) through the current chain"
        ),
    )
    args = parser.parse_args(argv)

    with Session(get_engine(), expire_on_commit=False) as session:
        # Snapshot to plain values INSIDE the block: a closed session's
        # instances are detached, so `.kind.value` after the fact would
        # raise rather than quietly re-open a transaction.
        candidates: list[tuple[int, int, str, str]] = []
        for security in _candidates(session, args.user_id, refresh=args.refresh)[: args.max_funds]:
            if security.id is None:
                raise RuntimeError("candidate security has no id (not flushed?)")
            candidates.append((security.id, security.user_id, security.symbol, security.kind.value))
    # The session is CLOSED here on purpose — it must not outlive the one
    # statement that opened it. Everything below waits on a provider (and
    # sleeps), and a session held across that wait is what a prod
    # `pg_stat_activity` sweep read as `idle in transaction` on this very
    # query for 8 minutes straight (M152, docs/01 § "Sessions and the
    # event loop").
    scope = "fund securities (refresh)" if args.refresh else "with zero composition rows"
    print(f"{len(candidates)} candidate securities {scope}")
    written_funds = 0
    total_rows = 0
    for index, (security_id, owner_id, symbol, kind_value) in enumerate(candidates, start=1):
        prefix = (
            f"[{index}/{len(candidates)}] user {owner_id} "
            f"security {security_id} {symbol} [{kind_value}]"
        )
        if args.apply:
            # Same background-task body the auto-enrichment uses: import
            # the fund's own rows, then deepen sub-funds + fundamentals.
            _run_enrichment(security_id)
            rows_written = _written_row_count(security_id)
            if rows_written:
                written_funds += 1
                total_rows += rows_written
                print(f"{prefix}: {rows_written} rows written")
            else:
                print(f"{prefix}: no provider answer (stays unclassified)")
        else:
            composition = market_data.fetch_fund_composition(symbol)
            if composition is None:
                print(f"{prefix}: no provider answer (dry run, nothing written)")
            else:
                source, entries = composition
                print(f"{prefix}: WOULD write {len(entries)} rows from {source.value}")
        if args.sleep and index < len(candidates):
            time.sleep(args.sleep)
    if args.apply:
        print(f"applied: {written_funds} funds enriched, {total_rows} rows written")
    else:
        print("dry run — re-run with --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

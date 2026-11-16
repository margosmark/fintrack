#!/usr/bin/env python3
"""Set `trades.currency` from the broker file that produced them (M132, docs/02).

The M132 migration deliberately backfills NOTHING: `securities.listing_currency`
is not a sound proxy for a row's currency (real account 731's VIU rows are CAD
while the security says USD), so copying it would fabricate per-row currency for
financial data. The SOURCE FILE is the authority, and this script re-reads it.

It is also the only place account 731's two `AdministrativePayment /
TRANSFER_FEE_REIMBURSEMENT` credit rows get repaired. M131 stored a POSITIVE
credit as a NEGATIVE `fee`, so "CAD credited" +145.75 was counted as 145.75
LEAVING the account — 583.00 of the -582.73-vs-WS-0.27 gap is exactly those two
rows (proved row-by-row against the file; docs/05 §Portfolio value cash leg).
The fixed parser now classifies a positive credit as a cash `transfer_in`, and
this script brings the committed rows in line with it.

Dry run by default — it prints every row it would touch, nothing is written.

    cd backend
    uv run python scripts/backfill_trade_currency.py \
        --account-id 731 --file /path/to/tfsa.csv            # report
    uv run python scripts/backfill_trade_currency.py \
        --account-id 731 --file /path/to/tfsa.csv --apply    # write

Safety rails (AGENTS §3 — strict beats silent for financial data):

* the parse and the stored rows must line up 1:1 on DATE and SYMBOL, or the
  script refuses — it never guesses which file row a trade came from;
* only COMMITTED trades are touched (staged rows earn their values at commit);
* a row whose `quantity != 0` never has its action/sign changed — only its
  currency — because re-signing a share row would move ACB pools;
* a currency that is not a 3-letter code never reaches the table (the
  `ck_trades_currency_iso_code` CHECK would reject it anyway).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

# `python scripts/backfill_trade_currency.py` puts scripts/, not backend/,
# at sys.path[0] — same guard the other scripts in this directory use.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select

from app.api.deps import get_engine
from app.importers.investment_profile import parse_investment_csv
from app.models.accounts import Account
from app.models.import_batches import ImportBatch
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.services.trade_filters import committed_trade_conditions


class AlignmentError(RuntimeError):
    """The file and the stored rows cannot be lined up — do not write."""


@dataclass
class Change:
    trade: Trade
    before: dict[str, object] = field(default_factory=dict)
    after: dict[str, object] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.after)

    def describe(self) -> str:
        bits = [f"trade {self.trade.id} {self.trade.date}"]
        for name in self.after:
            bits.append(f"{name} {self.before[name]!r} -> {self.after[name]!r}")
        return "  ".join(bits)


def _iso(currency: str) -> str:
    code = currency.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError(f"not an ISO-4217 code: {currency!r}")
    return code


def plan(
    account: Account,
    raw: bytes,
    rows: list[Trade],
    symbols: dict[int, str],
) -> list[Change]:
    """Row-by-row what would change. Raises AlignmentError on any doubt."""
    parsed, unmapped = parse_investment_csv(raw, account_currency=account.currency)
    if unmapped:
        lines = ", ".join(str(u.line) for u in unmapped[:10])
        raise AlignmentError(f"{len(unmapped)} file row(s) did not parse (lines {lines})")
    if len(parsed) != len(rows):
        raise AlignmentError(
            f"file parsed {len(parsed)} row(s) but account {account.id} has "
            f"{len(rows)} committed trade(s) — refusing to guess the pairing"
        )

    changes: list[Change] = []
    for src, dst in zip(parsed, rows, strict=True):
        if src.date != dst.date:
            raise AlignmentError(
                f"row order diverges at trade {dst.id}: file {src.date} vs stored {dst.date}"
            )
        stored_symbol = symbols.get(dst.security_id or -1, "").strip().upper()
        file_symbol = src.symbol.strip().upper()
        # A symbol-less cash row lands on the per-user `CASH` security
        # (`_cash_security`), so an empty file cell must pair with CASH and
        # with nothing else — the pairing stays exact either way.
        expected_symbol = "CASH" if not file_symbol else file_symbol
        if expected_symbol != stored_symbol:
            raise AlignmentError(
                f"row order diverges at trade {dst.id} ({dst.date}): file symbol "
                f"{file_symbol!r} (expected {expected_symbol!r}) vs stored "
                f"{stored_symbol!r}"
            )

        change = Change(trade=dst)
        wanted = _iso(src.currency)
        if dst.currency != wanted:
            change.before["currency"] = dst.currency
            change.after["currency"] = wanted
        if dst.action != src.action or dst.price_native != src.price_native:
            if dst.quantity != 0:
                raise AlignmentError(
                    f"trade {dst.id} ({dst.date}) wants an action/sign change but moves "
                    f"{dst.quantity} shares — refusing (it would move ACB pools)"
                )
            change.before["action"] = dst.action
            change.before["price_native"] = dst.price_native
            change.after["action"] = TradeAction(src.action)
            change.after["price_native"] = src.price_native
        if change.changed:
            changes.append(change)
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set trades.currency from the broker file (M132 backfill)"
    )
    parser.add_argument("--account-id", type=int, required=True, help="account to repair")
    parser.add_argument(
        "--file", required=True, help="the broker CSV that produced the account's trades"
    )
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        parser.error(f"no such file: {path}")
    raw = path.read_bytes()

    with Session(get_engine(), expire_on_commit=False) as session:
        account = session.get(Account, args.account_id)
        if account is None:
            parser.error(f"no account {args.account_id}")
        rows = list(
            session.exec(
                select(Trade)
                .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
                .where(
                    *committed_trade_conditions(account.user_id),
                    Trade.account_id == account.id,
                )
                .order_by(Trade.date, Trade.id)  # type: ignore[attr-defined]
            ).all()
        )
        securities = {
            sec.id: sec.symbol
            for sec in session.exec(
                select(Security).where(
                    Security.id.in_({r.security_id for r in rows if r.security_id})
                )
            ).all()
        }

        try:
            changes = plan(account, raw, rows, securities)
        except (AlignmentError, ValueError) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1

        if not changes:
            print(f"account {account.id}: {len(rows)} committed trade(s), nothing to do")
            return 0

        for change in changes:
            print("  " + change.describe())
        print(
            f"account {account.id}: {len(changes)} of {len(rows)} committed trade(s) "
            f"would change{' (WROTE)' if args.apply else ' (dry run)'}"
        )
        if not args.apply:
            return 0

        for change in changes:
            for name, value in change.after.items():
                setattr(change.trade, name, value)
            session.add(change.trade)
        session.commit()
        print(f"wrote {len(changes)} trade(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

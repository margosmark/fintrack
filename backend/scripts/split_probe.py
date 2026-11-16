#!/usr/bin/env python3
"""Cache-vs-ledger probe: find a security whose cached closes are in the wrong frame (M149).

The split signature this exists to catch, measured live in prod on 2026-10-11:
Yahoo only serves SPLIT-ADJUSTED closes, so once a symbol splits, every cached
close before the split date reads `1/ratio` of what was actually traded. The
security's own trades are the ledger's ground truth, and comparing them is
enough to see the mismatch without a second data source:

    ratio(d) = (cached close(d) x fx(listing currency -> trade currency, d))
               ÷ trade price(d)

tested in BOTH readings the row can be read in — the trade price UNCONVERTED
and the close fx-converted — because docs/07 §Series validation already proved
a row's stated currency and the number the broker exported can disagree (the
pre-M132 rows). A row EITHER reading puts within 1% of 1 is explained and does
not vote; only a row NEITHER reading explains may. Without that second reading
a stale `listing_currency` reports a ~1.37 "split" that is really the USD/CAD
rate, which is exactly the noise the M144 gate learned to ignore.

A CONSTANT ratio well away from 1 across at least two dates is the signature:
a commission moves a price by under 1% (prod's explained rows measured
1.0002-1.0032), a split moves it by the whole ratio — and an UNDER-valued
position reads the RECIPROCAL, which is how security 86 `VGT` reported
0.1250-0.1244 across all three of its trade dates for the 8:1 split of
2026-04-21. Rows whose trade price or fx rate cannot be compared at all are
counted `unclear`, never as a hit — a probe must not invent a verdict out of
missing data.

Cache-only and read-only: no yfinance call, no write.

    cd backend
    uv run python scripts/split_probe.py                    # every held security
    uv run python scripts/split_probe.py --user-id 343      # one tenant
    uv run python scripts/split_probe.py --security-id 86   # one row

From outside the stack (the image COPYs scripts/, the container owns the app
package and DATABASE_URL):

    ssh example.internal 'cd /home/developer/fin_tracker && docker exec fin_tracker-api-1 \
        python scripts/split_probe.py'

Exit code 2 when a split signature is found, 0 otherwise — so a re-probe after
`POST /securities/{id}/split-repair` can be scripted.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

# `python scripts/split_probe.py` puts scripts/, not backend/, at sys.path[0].
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select

from app.api.deps import get_engine
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.services.deep_backfill import _FxLookup, trade_evidence

#: A ratio past this is not commission/spread. Prod's clean rows measured
#: 1.0002-1.0032; the split case measured 0.1244-0.1252 (the 8:1 RECIPROCAL).
MATERIAL = Decimal("1.02")
#: A ratio inside this band of 1 EXPLAINS the row: commission/spread scale.
#: Mirrors docs/07 §Series validation's two readings — a row whose stated
#: currency disagrees with the number the broker exported (the pre-M132 rows)
#: has a converted ratio of ~1.37 and a raw ratio of ~1.00, and that is a
#: currency-frame defect, not a split. Only a row NEITHER reading explains
#: may vote.
EXPLAINS = Decimal("0.01")
#: The signature is a CONSTANT ratio: its spread across the compared dates has
#: to stay inside 2%, which separates a split from ordinary price noise.
SPREAD = Decimal("0.02")
#: Two dates is the minimum evidence for "constant" — one date is an anecdote.
MIN_DATES = 2


def _explained(ratio: Decimal) -> bool:
    """True when this reading puts the close where the trade says it was."""
    return abs(ratio - Decimal("1")) <= EXPLAINS


def probe_security(session: Session, security: Security) -> tuple[list[Decimal], int]:
    """`(unexplained ratios, trade rows with no comparable close)` for one row.

    The close is read from the cache (never fetched) and tested against the
    trade in BOTH currencies the row can be read in — the trade price
    UNCONVERTED, and the close fx-converted into the trade's currency. A CAD
    trade against a USD listing is then not mistaken for a 1.37x "split", and
    neither is a row whose `listing_currency` is stale.
    """
    evidence = trade_evidence(session, security)
    if not evidence:
        return [], 0
    closes = {
        row.date: row.price
        for row in session.exec(
            select(Price).where(
                Price.security_id == security.id, Price.source == PriceSource.yfinance
            )
        ).all()
    }
    fx = _FxLookup(session)
    listing = (security.listing_currency or "USD").upper()
    ratios: list[Decimal] = []
    unclear = 0
    for ev in evidence:
        close = closes.get(ev.date)
        if close is None or ev.price == 0:
            unclear += 1
            continue
        raw_ratio = close / ev.price
        rate = fx.rate(listing, ev.currency, ev.date)
        converted = (close * rate) / ev.price if rate is not None else None
        if _explained(raw_ratio) or (converted is not None and _explained(converted)):
            continue
        ratio = converted if converted is not None else raw_ratio
        if ratio <= 0:
            # A zero close is not evidence about a frame; it is a broken row.
            unclear += 1
            continue
        ratios.append(ratio)
    return ratios, unclear


def verdict_for(ratios: list[Decimal]) -> str:
    """`split signature` only for a constant ratio well away from 1."""
    if len(ratios) < MIN_DATES:
        return "unclear"
    low, high = min(ratios), max(ratios)
    spread = (high - low) / low
    constant = spread <= SPREAD
    material = high > MATERIAL or high < Decimal("1") / MATERIAL
    return "SPLIT SIGNATURE" if constant and material else "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cache-vs-ledger probe for a split-adjusted price cache"
    )
    parser.add_argument("--user-id", type=int, default=None, help="narrow to one tenant")
    parser.add_argument("--security-id", type=int, default=None, help="narrow to one row")
    args = parser.parse_args(argv)

    hits = 0
    with Session(get_engine(), expire_on_commit=False) as session:
        stmt = (
            select(Security)
            .join(
                Price,
                Price.security_id == Security.id,
            )
            .distinct()
            .order_by(Security.id)
        )
        if args.user_id is not None:
            stmt = stmt.where(Security.user_id == args.user_id)
        if args.security_id is not None:
            stmt = stmt.where(Security.id == args.security_id)
        securities = list(session.exec(stmt).all())
        if not securities:
            print("no security with a cached close matched those filters")
            return 0
        for security in securities:
            ratios, unclear = probe_security(session, security)
            verdict = verdict_for(ratios)
            if verdict == "SPLIT SIGNATURE":
                hits += 1
            shown = ", ".join(f"{ratio:.4f}" for ratio in ratios) or "-"
            print(
                f"security {security.id} {security.symbol} "
                f"(user {security.user_id}, {security.listing_currency}): "
                f"{len(ratios)} compared [{shown}], {unclear} unclear -> {verdict}"
            )
    print(f"\n{len(securities)} row(s) probed, {hits} split signature(s)")
    return 2 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())

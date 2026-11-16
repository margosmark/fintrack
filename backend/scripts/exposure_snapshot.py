"""Byte-identical exposure snapshot for one tenant (M168 acceptance).

Dumps `services.exposure_breakdown.breakdown` for ALL FOUR dimensions as
canonical JSON, so two runs over the same database state can be `diff`ed as
text. This is the card's "the exposure pipeline's outputs are BYTE-IDENTICAL
for Mark's tenant before/after the data move" test: take one snapshot on the
deployed tree that still has the five literals, deploy the tree where they
are rows, take a second — the two files must be equal.

    docker exec fin_tracker-api-1 python scripts/exposure_snapshot.py \\
        --user-id 343 --out /tmp/after.json
    docker cp fin_tracker-api-1:/tmp/after.json ./after.json

`sort_keys=True` canonicalises the KEY order of every object; the `items`
arrays keep their natural order (value descending), which is exactly the part
that must not move. Decimals are written as their string form — the same
serialization the API hands the UI. Run it while markets are closed, or two
price refreshes will differ for reasons that have nothing to do with M168.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session

from app.api.deps import get_engine
from app.services.exposure_breakdown import breakdown

DIMENSIONS = ("sector", "geography", "asset_class", "company")


def _encode(value: object) -> str:
    """Decimals as the API serializes them; everything else must already fit."""
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with Session(get_engine()) as session:
        payload = {
            dimension: breakdown(session, args.user_id, dimension) for dimension in DIMENSIONS
        }
    Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True, default=_encode) + "\n")
    for dimension, block in payload.items():
        items = block["items"]
        top = ", ".join(f"{i['key']} {i['weight_pct']}%" for i in items[:3])
        print(
            f"{dimension:<13} total={block['total_cad']} "
            f"unclassified={block['unclassified_cad']} "
            f"estimated={block['estimated_cad']}  top: {top}"
        )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

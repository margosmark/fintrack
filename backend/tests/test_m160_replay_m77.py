"""M160 server-route replay pins — the M77 slice (docs/33 §Server-first rule 5).

The M77 client engines (`lib/sankeyEngine.ts` / `lib/totalReturnEngine.ts`,
with `scripts/parity/engines/m77.ts` diffing them against the live server)
retired their comparison half with M160, and `tests/m77Parity.test.ts` went
with them. The 6 goldens this fixture froze (cash-flow Sankey + total-return
for three ranges: `full`, `q2` and the no-activity `empty`) become
server-route pins here: seed the captured accounts / transactions / trades /
categories / securities / prices / fx into a throwaway tenant, replay the
captured requests against the mounted routes and byte-diff the answers.

Two facts about this fixture that shape the module:

* **It is a keyed dict, not a `cases[]` harness.** `sankey` and
  `total_return` are both keyed `"full"` / `"q2"` / `"empty"`, and the
  `ranges` dict maps each key to its `{from, to}` — so the key plus `ranges`
  IS the request.
* **The Sankey bakes captured CATEGORY ids into its node ids** —
  `src:cat:1`, `dst:cat:5`, `dst:cat:null` — and those ids do not survive a
  re-seed. `m160_replay_common.translate_body` translates by *key name*, and
  `id`/`source`/`target` are far too generic to hand it (they would also hit
  `total:in` and every `*_trade_*` node). So the sankey golden gets its own
  string-level translator below: only the `cat:<n>` suffix is rewritten,
  through the categories IdMap, and a miss raises rather than passing.
  Node ORDER is unaffected because the sort key is
  ``(-amount, label, id)`` and every (amount, label) pair in the fixture is
  unique — the id only ever breaks ties that do not occur.

Neither route reads a clock (`cashflow_sankey` and `total_return_report` are
pure `from`/`to` window math), so unlike s2b/s2c no clock is injected here.

Run: cd backend && uv run pytest tests/test_m160_replay_m77.py -q
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.m160_replay_common import (
    IdMap,
    assert_golden,
    auth,
    load_fixture,
    make_user,
    seed_accounts,
    seed_categories,
    seed_fx,
    seed_prices,
    seed_securities,
    seed_trades,
    seed_transactions,
    translate_body,
)

M77 = load_fixture("m77-vectors.json")

#: `src:cat:1` / `dst:cat:5` — the only node ids that carry a captured id.
#: `dst:cat:null` (the uncategorized bucket) has no digits and so survives.
_CAT_ID_RE = re.compile(r"^(src|dst):cat:(\d+)$")


def _fx_by_base(rows: list[dict]) -> dict[str, list[dict]]:
    """`seed_fx` takes `{base: rows}`; m77 froze the flat `GET /fx` item list."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["base"], []).append(row)
    return grouped


def _translate_sankey_ids(value: Any, categories: IdMap) -> Any:
    """Rewrite the captured category ids the Sankey bakes into node ids.

    Only the `cat:<digits>` suffix of a `src:`/`dst:` node id is touched, so
    `total:in`, `src:drawn` and every `*_trade_*` node pass through
    untouched, and an id the seeder never produced raises a KeyError instead
    of silently matching.
    """
    if isinstance(value, dict):
        return {key: _translate_sankey_ids(item, categories) for key, item in value.items()}
    if isinstance(value, list):
        return [_translate_sankey_ids(item, categories) for item in value]
    if isinstance(value, str):
        match = _CAT_ID_RE.match(value)
        if match:
            slot, old = match.group(1), int(match.group(2))
            return f"{slot}:cat:{categories[old]}"
    return value


def _seed_demo(session: Session) -> tuple[Any, dict[str, IdMap]]:
    """The captured M77 dataset, re-seeded into one throwaway tenant."""
    user = make_user(session, "m77")
    accounts = seed_accounts(session, user, M77["accounts"])
    categories = seed_categories(session, user, M77["categories"])
    securities = seed_securities(session, user, M77["securities"])
    seed_transactions(session, user, M77["transactions"], accounts, categories)
    seed_trades(session, user, M77["trades"], accounts, securities)
    seed_prices(session, user, M77["prices"], securities)
    seed_fx(session, _fx_by_base(M77["fx"]))
    # Neither golden prints an account, transaction or security id — the
    # Sankey's only embedded ids are category ids, handled by the translator.
    return user, {"category_id": categories}


SANKEY_CASES = [
    {
        "id": name,
        "path": f"/reports/cashflow-sankey?from={rng['from']}&to={rng['to']}",
        "expected": M77["sankey"][name],
    }
    for name, rng in M77["ranges"].items()
]

TOTAL_RETURN_CASES = [
    {
        "id": name,
        "path": f"/reports/total-return?from={rng['from']}&to={rng['to']}",
        "expected": M77["total_return"][name],
    }
    for name, rng in M77["ranges"].items()
]


@pytest.mark.parametrize("case", SANKEY_CASES, ids=[c["id"] for c in SANKEY_CASES])
def test_m77_cashflow_sankey_golden_replays(
    client: TestClient, session: Session, case: dict[str, Any]
) -> None:
    """The 3 Sankey goldens, replayed at the mounted route.

    Three columns — sources, one `total:in`, destinations — over the same
    committed population and signs as `GET /reports/cashflow`, with per-row
    cent quantization so the graph conserves to the cent (docs/12 §Sankey).
    The captured category ids inside the node ids are translated through the
    categories IdMap; everything else compares byte for byte.
    """
    user, maps = _seed_demo(session)
    response = client.get(f"/api/v1{case['path']}", headers=auth(user))
    assert response.status_code == 200, response.text
    # The RESPONSE already carries the suite's ids; the GOLDEN carries the
    # capture's, so only the golden is translated — the same direction
    # `translate_body` runs in every other replay module here.
    assert_golden(
        response.json(),
        _translate_sankey_ids(case["expected"], maps["category_id"]),
        context=f"cashflow-sankey {case['id']}",
    )


@pytest.mark.parametrize("case", TOTAL_RETURN_CASES, ids=[c["id"] for c in TOTAL_RETURN_CASES])
def test_m77_total_return_golden_replays(
    client: TestClient, session: Session, case: dict[str, Any]
) -> None:
    """The 3 total-return goldens, replayed at the mounted route.

    TWR is daily chain-linked, MWR is Modified Dietz over the same window,
    both PERIOD returns rounded once to a fixed 8-decimal plain string — the
    arithmetic M160 retired the browser port of, now pinned against the
    server's own frozen answer. These goldens carry no ids at all, so the
    ordinary key-directed translator suffices.
    """
    user, maps = _seed_demo(session)
    response = client.get(f"/api/v1{case['path']}", headers=auth(user))
    assert response.status_code == 200, response.text
    assert_golden(
        response.json(),
        translate_body(case["expected"], maps),
        context=f"total-return {case['id']}",
    )

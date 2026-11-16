"""M160 server-route replay pins — the s4 slice (docs/33 §Server-first rule 5).

The s4 client mirrors (`lib/reportEngine.ts::capitalGainsReport` /
`computeUserTaxState` + `scripts/parity/engines/s4.ts`) retired with M158
Decision 2 / M160. The s4 goldens (13 `capital-gains.<year>` cases + the
`acb.pools` page, captured from the live stack) become server-route pins
here: seed the captured trades / securities / prices / fx into a throwaway
tenant, rebuild the ACB pools through the single sanctioned write path
(`app.tax.acb.rebuild_user_pools`, docs/02), and replay each request.

The `acb.pools` case drops the two columns no re-seed can reproduce —
`id` (serial) and `updated_at` (the rebuild's clock) — exactly as the s4
fixture's own note documents for the client engine that used to consume it;
every computed field (`quantity`, `total_acb_cad`) and the join order
(security symbol, then id) is compared byte for byte.

Run: cd backend && uv run pytest tests/test_m160_replay_s4.py -q
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.tax.acb import rebuild_user_pools
from tests.m160_replay_common import (
    IdMap,
    assert_golden,
    auth,
    cases_of,
    dataset_of,
    ids_of,
    load_fixture,
    make_user,
    replay_path,
    seed_accounts,
    seed_fx,
    seed_prices,
    seed_securities,
    seed_trades,
    translate_body,
)

S4 = load_fixture("s4-vectors.json")


def _seed_demo(session: Session) -> tuple[Any, dict[str, IdMap]]:
    dataset = dataset_of(S4, case={"dataset": "demo"})
    user = make_user(session, "s4-demo")
    accounts = seed_accounts(session, user, dataset["accounts"])
    securities = seed_securities(session, user, dataset["securities"])
    seed_trades(session, user, dataset["trades"], accounts, securities)
    seed_prices(session, user, dataset["prices"], securities)
    seed_fx(session, dataset.get("fx"))
    # The capture's pools were built by the trades write path; a direct row
    # seed must rebuild through the same sanctioned path (docs/02 invariant).
    rebuild_user_pools(session, user.id, allow_fetch=False)
    session.commit()
    return user, {"account_id": accounts, "security_id": securities}


def _strip_db_assigned(body: Any) -> Any:
    """Drop `id`/`updated_at` from an acb page (serial + rebuild clock)."""
    if isinstance(body, dict):
        return {
            key: _strip_db_assigned(value)
            for key, value in body.items()
            if key not in ("id", "updated_at")
        }
    if isinstance(body, list):
        return [_strip_db_assigned(item) for item in body]
    return body


S4_CASES = cases_of(S4)


@pytest.mark.parametrize("case", S4_CASES, ids=ids_of(S4_CASES))
def test_s4_golden_replays(client: TestClient, session: Session, case: dict[str, Any]) -> None:
    """The 14 s4 goldens (13 tax years + the ACB pool page)."""
    user, maps = _seed_demo(session)
    path = replay_path(case["request"]["path"], maps)
    response = client.get(f"/api/v1{path}", headers=auth(user))
    expected = case["expected"]
    assert response.status_code == expected["status"], response.text
    # The maps run captured-id -> suite-id, so the GOLDEN is translated.
    actual = response.json()
    want = translate_body(expected["body"], maps)
    if case["id"] == "acb.pools":
        actual = _strip_db_assigned(actual)
        want = _strip_db_assigned(want)
    assert_golden(actual, want, context=case["id"])

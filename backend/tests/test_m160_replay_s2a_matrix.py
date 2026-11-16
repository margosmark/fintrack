"""M160 server-route replay pins — the s2a + matrix slices (docs/33 §Server-first rule 5).

The client mirrors these slices used to compare (`lib/reportEngine.ts::
spendingReport` / `categoryBreakdownReport` / `cashflowReport` /
`categoryTagMatrixReport` + `scripts/parity/engines/{s2a,matrix}.ts`) retired
with M158 Decision 2 / M160. The fixtures they were gated against
(`s2a-vectors.json` 29 cases, `matrix-vectors.json` 6 cases — captured from
the live stack by `capture.ts --slice s2a|matrix`) are kept as SERVER-ROUTE
regression pins: this module seeds each case's captured dataset into a
throwaway tenant, replays the captured request against the mounted route and
diffs the answer against the frozen golden, byte for byte.

Run: cd backend && uv run pytest tests/test_m160_replay_s2a_matrix.py -q
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models.securities import Security
from tests.m160_replay_common import (
    IdMap,
    assert_golden,
    auth,
    cases_of,
    collect_tags,
    dataset_of,
    ensure_accounts,
    ids_of,
    load_fixture,
    make_user,
    replay_path,
    seed_categories,
    seed_fx,
    seed_tags,
    seed_trades,
    seed_transactions,
    translate_body,
)

S2A = load_fixture("s2a-vectors.json")
MATRIX = load_fixture("matrix-vectors.json")


def _seed_tenant(
    session: Session, fixture: dict[str, Any], dataset_name: str
) -> tuple[Any, dict[str, IdMap]]:
    dataset = dataset_of(fixture, case={"dataset": dataset_name})
    user = make_user(session, f"{fixture['meta']['slice']}-{dataset_name}")
    categories = seed_categories(session, user, dataset["categories"])
    accounts = ensure_accounts(session, user, dataset)
    tag_rows = collect_tags(dataset)
    tags_by_name = seed_tags(session, user, tag_rows) if tag_rows else {}
    tag_map = IdMap()
    for row in tag_rows:
        tag_map.add(row["id"], tags_by_name[row["name"]].id)
    seed_transactions(
        session, user, dataset["transactions"], accounts, categories, tags_by_name or None
    )
    if dataset.get("trades"):
        # The cashflow route reads trade CASH legs only; security_id is an
        # unread foreign key here (s2a captured no securities), so one
        # stand-in row carries them all.
        stub = Security(
            user_id=user.id,
            symbol="REPLAY-STUB",
            name="replay stub",
            kind="stock",
            listing_currency="CAD",
            exchange="ARCA",
        )
        session.add(stub)
        session.flush()
        session.commit()
        securities = IdMap()
        for trade in dataset["trades"]:
            securities.add(trade["security_id"], stub.id)
        seed_trades(session, user, dataset["trades"], accounts, securities)
    seed_fx(session, dataset.get("fx"))
    return user, {"category_id": categories, "tag_id": tag_map}


def _replay(
    client: TestClient, session: Session, fixture: dict[str, Any], case: dict[str, Any]
) -> None:
    user, maps = _seed_tenant(session, fixture, case["dataset"])
    path = replay_path(case["request"]["path"], maps)
    response = client.get(f"/api/v1{path}", headers=auth(user))
    expected = case["expected"]
    assert response.status_code == expected["status"], response.text
    # The maps run captured-id -> suite-id, so the GOLDEN is translated into
    # this tenant's id space and the live response is compared untouched.
    assert_golden(
        response.json(),
        translate_body(expected["body"], maps),
        context=f"{fixture['meta']['slice']} {case['id']}",
    )


S2A_CASES = cases_of(S2A)
MATRIX_CASES = cases_of(MATRIX)


@pytest.mark.parametrize("case", S2A_CASES, ids=ids_of(S2A_CASES))
def test_s2a_golden_replays(client: TestClient, session: Session, case: dict[str, Any]) -> None:
    """The 29 s2a goldens, answered again by the mounted routes."""
    _replay(client, session, S2A, case)


@pytest.mark.parametrize("case", MATRIX_CASES, ids=ids_of(MATRIX_CASES))
def test_matrix_golden_replays(client: TestClient, session: Session, case: dict[str, Any]) -> None:
    """The 6 category x tag matrix goldens, answered again by the route."""
    _replay(client, session, MATRIX, case)

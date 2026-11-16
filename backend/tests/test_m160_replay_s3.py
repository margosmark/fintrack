"""M160 server-route replay pins — the s3 slice (docs/33 §Server-first rule 5).

The s3 client mirrors (`lib/s3Engine.ts::budgetsProgress` / `previewRule` /
`subscriptionsReport` + `scripts/parity/engines/s3.ts`) retired with M158
Decision 2 / M160 — except `budgetsProgress`, which survives as the audited
client-only compute for the PARTNER view (`lib/budgets.ts`, Ruling 2 on
t_4fea992a; its lock-invariance stays pinned in
`frontend/tests/lockedVaultDegradation.test.ts` → "A13"). The s3 goldens
themselves become server-route pins:

* `budgets.progress` + `subscriptions.detect` — the demo tenant's own
  answers, replayed with the clock PINNED to the capture's `today_et()`
  (`meta.captured_at_et`): both windows are defined relative to "today"
  (docs/29 F1d), and a golden captured on 2026-09-27 must be answered by the
  same 2026-09-27 ledger, not by whatever day the suite runs.
* the 14 `rules.preview.*` cases — the throwaway tenant's previews (the demo
  tenant is method-blocked on POST, docs/31), replayed over the captured
  scratch ledger with the request bodies verbatim from the fixture.

Run: cd backend && uv run pytest tests/test_m160_replay_s3.py -q
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.m160_replay_common import (
    IdMap,
    assert_golden,
    auth,
    cases_of,
    dataset_of,
    ensure_accounts,
    ids_of,
    load_fixture,
    make_user,
    replay_path,
    seed_budgets,
    seed_categories,
    seed_fx,
    seed_transactions,
    translate_body,
)

S3 = load_fixture("s3-vectors.json")
CAPTURED_TODAY = date.fromisoformat(S3["meta"]["captured_at_et"])


def _seed_dataset(
    session: Session, dataset_name: str
) -> tuple[Any, dict[str, IdMap], dict[str, IdMap]]:
    """`(user, response-id-maps, query-id-maps)` for one captured dataset.

    The response map covers the keys the goldens print (`id` for budgets and
    preview items, `user_id`, `category_id`); the query map covers the drill
    filter (`category_id`), which no s3 case actually carries — kept for the
    shared replay_path contract.
    """
    dataset = dataset_of(S3, case={"dataset": dataset_name})
    user = make_user(session, f"s3-{dataset_name}")
    categories = seed_categories(session, user, dataset["categories"])
    accounts = ensure_accounts(session, user, dataset)
    txns = seed_transactions(session, user, dataset["transactions"], accounts, categories)
    seed_fx(session, dataset.get("fx"))
    user_map = IdMap()
    user_map.add(1, user.id)
    # `id` is a budget id in the budgets golden and a TRANSACTION id in the
    # preview items — one tenant never answers both shapes in one case, so a
    # single slot keyed per case is enough (budgets overwrite it below).
    maps = {"id": txns, "user_id": user_map, "category_id": categories}
    return user, maps, {"category_id": categories}


def _seed_demo_with_budgets(session: Session) -> tuple[Any, dict[str, IdMap]]:
    dataset = dataset_of(S3, case={"dataset": "demo"})
    user, maps, _ = _seed_dataset(session, "demo")
    budgets = seed_budgets(session, user, dataset["budgets"], maps["category_id"])
    maps["id"] = budgets
    return user, maps


def _pin_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze every window the two GET goldens derive from `today_et()`."""
    for module in (
        "app.services.budgets",
        "app.services.subscriptions",
        "app.services.txn_filters",
    ):
        monkeypatch.setattr(f"{module}.today_et", lambda: CAPTURED_TODAY)


GET_CASES = [c for c in cases_of(S3, methods=("GET",))]
PREVIEW_CASES = [c for c in cases_of(S3, methods=("POST",))]


@pytest.mark.parametrize("case", GET_CASES, ids=ids_of(GET_CASES))
def test_s3_demo_golden_replays(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]
) -> None:
    """`budgets.progress` and `subscriptions.detect`, answered again at the
    captured clock."""
    _pin_clock(monkeypatch)
    if case["id"] == "budgets.progress":
        user, maps = _seed_demo_with_budgets(session)
    else:
        user, maps, _ = _seed_dataset(session, "demo")
    path = replay_path(case["request"]["path"], {"category_id": maps["category_id"]})
    response = client.get(f"/api/v1{path}", headers=auth(user))
    expected = case["expected"]
    assert response.status_code == expected["status"], response.text
    assert_golden(response.json(), translate_body(expected["body"], maps), context=case["id"])


@pytest.mark.parametrize("case", PREVIEW_CASES, ids=ids_of(PREVIEW_CASES))
def test_s3_preview_golden_replays(
    client: TestClient, session: Session, case: dict[str, Any]
) -> None:
    """The 14 rule-preview goldens over the captured scratch ledger."""
    user, maps, _ = _seed_dataset(session, case["dataset"])
    response = client.post(
        "/api/v1/rules/preview", json=case["request"]["body"], headers=auth(user)
    )
    expected = case["expected"]
    assert response.status_code == expected["status"], response.text
    assert_golden(response.json(), translate_body(expected["body"], maps), context=case["id"])

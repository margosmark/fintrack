"""M160 server-route replay pins — the S2b slice (docs/33 §Server-first rule 5).

The S2b client mirrors (`lib/reportEngine.ts::netWorthReport` /
`investmentPerformanceReport` + `scripts/parity/engines/s2b.ts`) retired with
M158 Decision 2 / M160, and `tests/s2bReports.test.ts` went with them. The 11
goldens this fixture froze (7 net-worth ranges + 4 investment-performance
ranges, captured from the live stack on 2026-09-27) become server-route pins
here: seed the captured accounts / transactions / trades / prices /
securities / fx into a throwaway tenant, replay the captured request against
the mounted route and byte-diff the answer.

Three facts about this fixture that shape the module:

* **It is a keyed dict, not a `cases[]` harness.** `networth` is keyed
  `"<from>|<to>|<granularity>"` and `performance` `"<from>|<to>"`, so the
  key IS the request — this module maps each one to a query string rather
  than reading a captured path.
* **It froze no `categories[]`**, but `transactions.category_id` is a real
  foreign key. Neither report reads a category (net-worth sums accounts +
  trades; performance sums trades + ACB pools), so one stub per referenced
  id satisfies the FK without being able to move a golden — the same bargain
  `m160_replay_common.ensure_accounts` strikes for the account-less slices.
* **`performance` values positions at `dt.date.today()`** (docs/12 §5 — the
  closing price and the closing fx rate). The clock is injected at the
  capture's `captured_at` for the same reason `s2bReports.test.ts` passed
  `captured_at` as its `today`: the newest cached price (2026-09-21) and
  newest fx rate (2026-09-24) both predate the capture, so as of 2026-10-12
  an un-pinned replay still lands on the same rows and the pin is
  future-proofing rather than load-bearing today — but `PriceCache` and
  `FxCache` fall back to *whatever is newest ≤ today*, so the window in which
  a later rate can shadow the captured one only closes when today is pinned.
  `net_worth_report` reads no clock at all (every bucket is derived from
  `from`/`to`), so the pin is inert for those 7 cases.

Run: cd backend && uv run pytest tests/test_m160_replay_s2b.py -q
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from m160_replay_clock import freeze_module_clock
from sqlmodel import Session

from app.services import reports as reports_service
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

S2B = load_fixture("s2b-vectors.json")

#: The capture's own clock (`s2bReports.test.ts` used this as its `today`).
CAPTURED_TODAY = S2B["captured_at"][:10]


def _stub_categories(session: Session, user: Any, transactions: list[dict]) -> IdMap:
    """One stub category per id the captured ledger references.

    The capture never froze a `categories[]` dataset, so there is nothing to
    re-seed — but `transactions.category_id` still has to resolve. Neither S2b
    report reads a category row, so the stubs exist purely to satisfy the FK.
    """
    referenced = sorted(
        {row["category_id"] for row in transactions if row.get("category_id") is not None}
    )
    rows = [{"id": old, "name": f"replay-cat-{old}", "parent_id": None} for old in referenced]
    return seed_categories(session, user, rows)


def _seed_demo(session: Session) -> tuple[Any, dict[str, IdMap]]:
    """The captured S2b dataset, re-seeded into one throwaway tenant."""
    user = make_user(session, "s2b")
    accounts = seed_accounts(session, user, S2B["accounts"])
    categories = _stub_categories(session, user, S2B["transactions"])
    securities = seed_securities(session, user, S2B["securities"])
    seed_transactions(session, user, S2B["transactions"], accounts, categories)
    seed_trades(session, user, S2B["trades"], accounts, securities)
    seed_prices(session, user, S2B["prices"], securities)
    seed_fx(session, S2B["fx"])
    # Neither golden prints an account id; performance prints `security_id`.
    return user, {"security_id": securities}


# --- the fixture's keyed dicts become the parametrization --------------------

NETWORTH_CASES = [
    {
        "id": key,
        "path": f"/reports/net-worth?from={parts[0]}&to={parts[1]}&granularity={parts[2]}",
        "expected": value,
    }
    for key, value in S2B["networth"].items()
    for parts in [key.split("|")]
]

PERFORMANCE_CASES = [
    {
        "id": key,
        "path": f"/reports/investment-performance?from={parts[0]}&to={parts[1]}",
        "expected": value,
    }
    for key, value in S2B["performance"].items()
    for parts in [key.split("|")]
]


@pytest.fixture(name="capture_clock")
def capture_clock_fixture(monkeypatch: pytest.MonkeyPatch) -> str:
    """Answer every case as the capture's own day (docs/12 §5 reads today)."""
    from datetime import date

    freeze_module_clock(monkeypatch, reports_service, date.fromisoformat(CAPTURED_TODAY))
    return CAPTURED_TODAY


@pytest.mark.parametrize("case", NETWORTH_CASES, ids=[c["id"] for c in NETWORTH_CASES])
def test_s2b_net_worth_golden_replays(
    client: TestClient, session: Session, case: dict[str, Any]
) -> None:
    """The 7 net-worth ranges, replayed at the mounted route.

    `net_worth_report` derives every bucket from `from`/`to`/`granularity`
    alone — no clock — so these cases run on the real calendar.
    """
    user, maps = _seed_demo(session)
    response = client.get(f"/api/v1{case['path']}", headers=auth(user))
    assert response.status_code == 200, response.text
    assert_golden(
        response.json(),
        translate_body(case["expected"], maps),
        context=f"net-worth {case['id']}",
    )


@pytest.mark.parametrize("case", PERFORMANCE_CASES, ids=[c["id"] for c in PERFORMANCE_CASES])
def test_s2b_investment_performance_golden_replays(
    client: TestClient,
    session: Session,
    capture_clock: str,
    case: dict[str, Any],
) -> None:
    """The 4 investment-performance ranges, answered at the captured clock.

    `market_value_cad` prices the closing position at `today`'s newest cached
    price and fx rate (docs/12 §5), which is the clock `s2bReports.test.ts`
    passed as its `today` parameter. Pinned to `captured_at` so the golden is
    answered by the capture's day rather than whatever day the suite runs —
    and so a later fx/price row can never shadow the captured one through the
    caches' "newest ≤ today" fallback.
    """
    user, maps = _seed_demo(session)
    response = client.get(f"/api/v1{case['path']}", headers=auth(user))
    assert response.status_code == 200, response.text
    assert_golden(
        response.json(),
        translate_body(case["expected"], maps),
        context=f"investment-performance {case['id']}",
    )

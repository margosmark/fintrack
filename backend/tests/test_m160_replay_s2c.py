"""M160 server-route replay pins — the S2c slice (docs/33 §Server-first rule 5).

The S2c client mirror (`lib/reportEngine.ts::dividendReport` +
`scripts/parity/engines/s2c.ts`) retired with M158 Decision 2 / M160, and
`tests/s2cDividends.test.ts` went with them. The 4 dividends goldens this
fixture froze (the default year, both available years, and an empty year —
captured from the live demo stack on 2026-09-27) become server-route pins
here: seed the captured income trades / securities / fx into a throwaway
tenant, replay the captured request against the mounted route and byte-diff
the answer.

Three facts about this fixture that shape the module:

* **It is a keyed dict, not a `cases[]` harness.** `goldens` is keyed
  `"default"` / `"2026"` / `"2025"` / `"2018"`, so the key IS the request —
  `"default"` maps to `GET /reports/dividends` with no `year` at all, the
  other three to `?year=<n>`.
* **It froze no `accounts[]` and no `categories[]`,** but its 21 trades
  reference `account_id` 4 and 5. `services/dividends.py` joins no `Account`
  and reads no account column at all — verified, the module does not contain
  the string "account" — so account `type`/`taxable` cannot move a golden
  here. The rows are still seeded faithfully rather than invented: 4 and 5
  are exactly the two accounts this same demo tenant's sibling capture
  (`s2b-vectors.json`, same securities table id-for-id, same capture day)
  froze, so if an account-scoped filter ever lands on this route the seed
  already carries the real TFSA / non-registered-USD-margin shapes instead of
  a guess.
* **The TTM window reads `dt.date.today()`** (docs/12 §Dividends), so the
  clock is injected at the capture's `captured_at` exactly the way
  `s2cDividends.test.ts` passed it to its engine: `ttm_total_cad` is a
  trailing-365-day sum, so the golden is stable only while the fixture's
  rows stay inside that window. Measured on 2026-10-12 the pin is *not yet*
  load-bearing — the five income rows dated 2025-10-15/20 still clear the
  floor — but they drop out of the window once the real clock passes
  **2026-10-15**, at which point the un-pinned replay would diverge on
  `ttm_total_cad` for all four cases. Pinning now is what keeps that from
  being a silent red three days out.

The `default` golden resolves its `year` from the seeded income rows
(`available_years[0]`), not from the clock, so it stays stable as long as
the fixture carries no newer year.

Run: cd backend && uv run pytest tests/test_m160_replay_s2c.py -q
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from m160_replay_clock import freeze_module_clock
from sqlmodel import Session

from app.services import dividends as dividends_service
from tests.m160_replay_common import (
    IdMap,
    assert_golden,
    auth,
    load_fixture,
    make_user,
    seed_accounts,
    seed_fx,
    seed_securities,
    seed_trades,
    translate_body,
)

S2C = load_fixture("s2c-vectors.json")
#: The same demo tenant, captured the same day — the only frozen record of
#: what the accounts behind `account_id` 4 and 5 actually were.
S2B = load_fixture("s2b-vectors.json")

#: The capture's own clock — the TTM window is `today - 365d < d <= today`,
#: so `ttm_total_cad` is only reproducible at this date.
CAPTURED_TODAY = date.fromisoformat(S2C["captured_at"])


def _referenced_accounts(session: Session, user: Any) -> IdMap:
    """The accounts the captured trades point at, seeded from frozen rows.

    s2c froze no `accounts[]`, so the ids are filled from the sibling s2b
    capture of the same demo tenant (same securities table, id for id; same
    capture day) rather than invented. `dividend_report` reads no account
    column, so this is belt-and-braces — but a faithful seed costs nothing
    and cannot silently rot into a wrong type if a filter lands later.
    """
    referenced = sorted({row["account_id"] for row in S2C["trades"]})
    rows = [row for row in S2B["accounts"] if row["id"] in referenced]
    seeded = {row["id"] for row in rows}
    missing = [old for old in referenced if old not in seeded]
    assert not missing, (
        f"s2c-vectors.json references account_id {missing}, which the sibling "
        "s2b capture does not freeze — add the row there (or a stub here) "
        "rather than letting the replay seed a guess"
    )
    return seed_accounts(session, user, rows)


def _seed_demo(session: Session) -> tuple[Any, dict[str, IdMap]]:
    """The captured S2c dataset, re-seeded into one throwaway tenant."""
    user = make_user(session, "s2c")
    _accounts = _referenced_accounts(session, user)
    securities = seed_securities(session, user, S2C["securities"])
    seed_trades(session, user, S2C["trades"], _accounts, securities)
    seed_fx(session, S2C["fx"])
    return user, {"security_id": securities}


#: The fixture's keyed goldens become the parametrization: `"default"` is the
#: route's own no-`year` default (the latest year present in the ledger),
#: everything else pins an explicit year.
CASES = [
    {
        "id": "default",
        "path": "/reports/dividends",
        "expected": S2C["goldens"]["default"],
    },
    *[
        {"id": year, "path": f"/reports/dividends?year={year}", "expected": body}
        for year, body in S2C["goldens"].items()
        if year != "default"
    ],
]


@pytest.fixture(name="capture_clock")
def capture_clock_fixture(monkeypatch: pytest.MonkeyPatch) -> date:
    """Answer every case as the capture's own day (the TTM window reads today)."""
    freeze_module_clock(monkeypatch, dividends_service, CAPTURED_TODAY)
    return CAPTURED_TODAY


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_s2c_dividends_golden_replays(
    client: TestClient,
    session: Session,
    capture_clock: date,
    case: dict[str, Any],
) -> None:
    """The 4 dividends goldens, replayed at the mounted route.

    `dividend_report` accumulates per-security buckets over the committed
    income actions with per-row HALF_UP cents on the cache-only trade-date
    rate (docs/12 §Dividends) — the arithmetic M160 retired the browser port
    of, now pinned against the server's own frozen answer.
    """
    user, maps = _seed_demo(session)
    response = client.get(f"/api/v1{case['path']}", headers=auth(user))
    assert response.status_code == 200, response.text
    actual = response.json()
    expected = translate_body(case["expected"], maps)
    # docs/07 §Dividend-view provenance is an ADDITIVE top-level key this
    # 2026-09-27 capture predates. Its whole contract — shape, severity
    # (missing > stale > ok), oldest-timestamp rule and per-source detail —
    # is pinned by tests/test_dividend_provenance.py, so the golden still does
    # what it was frozen for (byte-diff the captured arithmetic) while this
    # assertion makes the delta itself explicit: `provenance` is the ONLY key
    # the route may add, and a second one fails here instead of hiding inside
    # a stripped comparison.
    assert set(actual) - set(expected) == {"provenance"}, sorted(set(actual) ^ set(expected))
    assert_golden(
        {key: value for key, value in actual.items() if key != "provenance"},
        expected,
        context=f"dividends {case['id']}",
    )

"""M160 server-route replay pins — the household slice (docs/33, M159).

`GET /reports/spending?scope=household` (and its categories twin) is
M159's server-first route; the golden vectors (`household-vectors.json`, 12
cases captured from a real throwaway owner+partner partnership) were the
client-comparison gate for the pre-M159 browser engines
(`scripts/parity/engines/household.ts`, retired with M160). This module
re-homes them as server-route pins: two tenants seeded from the fixture's
own ledgers, linked through the REAL invite flow on ONE shared account, and
every captured request replayed against the mounted route.

The last describe block keeps M159's independent anchors: the seeded
household's money is recomputed here from the frozen rows by hand, so a
capture that froze the same wrong number on both sides still fails — the
property `frontend/tests/householdParity.test.ts` used to carry, moved to
the backend because its engine half retired.

Run: cd backend && uv run pytest tests/test_m160_replay_household.py -q
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models.accounts import Account
from tests.m160_replay_common import (
    IdMap,
    assert_golden,
    auth,
    ids_of,
    load_fixture,
    make_user,
    replay_path,
    seed_categories,
    seed_transactions,
    translate_body,
)

HOUSEHOLD = load_fixture("household-vectors.json")
RANGE = {"from": "2026-05-01", "to": "2026-05-31"}


def _seed_household(session: Session, client: TestClient) -> tuple[Any, Any, dict[str, IdMap]]:
    """`(owner, partner, {category_id: combined-map})` from the frozen rows."""
    partner_ds = HOUSEHOLD["datasets"]["partner"]
    owner_ds = HOUSEHOLD["datasets"]["owner"]

    owner = make_user(session, "household-owner")
    partner = make_user(session, "household-partner")

    # The two tenants deliberately created the SAME category names (the
    # fixture's own note): map each tenant's captured ids through its own
    # tree; the id spaces are disjoint in the capture, so one combined map
    # is safe.
    owner_cats = seed_categories(session, owner, partner_ds["ownerCategories"])
    partner_cats = seed_categories(session, partner, partner_ds["ownCategories"])
    cat_map = IdMap()
    cat_map.merge(owner_cats)
    cat_map.merge(partner_cats)
    combined = {"category_id": cat_map}

    # Accounts: the owner's rows name two (shared + hidden); the shared one
    # is exactly the account the partner's projection carries.
    shared_old = {row["account_id"] for row in partner_ds["projectionTransactions"]}
    assert len(shared_old) == 1, shared_old
    owner_accounts = IdMap()
    for old in sorted({row["account_id"] for row in owner_ds["ownTransactions"]}):
        account = Account(
            user_id=owner.id,
            name="m159-owner-shared" if old in shared_old else "m159-owner-hidden",
            type="chequing",
            currency="CAD",
        )
        session.add(account)
        session.flush()
        owner_accounts.add(old, account.id)
    partner_accounts = IdMap()
    for old in sorted({row["account_id"] for row in partner_ds["ownTransactions"]}):
        account = Account(
            user_id=partner.id, name="m159-partner-own", type="chequing", currency="CAD"
        )
        session.add(account)
        session.flush()
        partner_accounts.add(old, account.id)
    session.commit()

    seed_transactions(session, owner, owner_ds["ownTransactions"], owner_accounts, owner_cats)
    seed_transactions(
        session, partner, partner_ds["ownTransactions"], partner_accounts, partner_cats
    )

    # The link itself, through the real invite flow on the shared account.
    invite = client.post(
        "/api/v1/sharing/invites",
        json={"email": partner.email, "account_ids": [owner_accounts[next(iter(shared_old))]]},
        headers=auth(owner),
    )
    assert invite.status_code == 201, invite.text
    accept = client.post(
        "/api/v1/sharing/invites/accept",
        json={"token": invite.json()["token"]},
        headers=auth(partner),
    )
    assert accept.status_code == 200, accept.text
    return owner, partner, combined


def _seat_for(case_id: str) -> str:
    if case_id.startswith("owner-seat."):
        return "owner"
    if case_id.startswith("privacy."):
        return "partner-privacy"
    return "partner"


HOUSEHOLD_CASES = HOUSEHOLD["cases"]


@pytest.mark.parametrize("case", HOUSEHOLD_CASES, ids=ids_of(HOUSEHOLD_CASES))
def test_household_golden_replays(
    client: TestClient, session: Session, case: dict[str, Any]
) -> None:
    """The 12 household goldens: both seats, the narrow-scope contrasts, the
    degenerate owner seat and the privacy-mode mask."""
    owner, partner, maps = _seed_household(session, client)
    seat = _seat_for(case["id"])
    if seat == "owner":
        actor = owner
    else:
        actor = partner
        if seat == "partner-privacy":
            flip = client.patch(
                "/api/v1/users/me", json={"privacy_mode": True}, headers=auth(owner)
            )
            assert flip.status_code == 200, flip.text
    path = replay_path(case["request"]["path"], maps)
    response = client.get(f"/api/v1{path}", headers=auth(actor))
    expected = case["expected"]
    assert response.status_code == expected["status"], response.text
    # The maps run captured-id -> suite-id, so the GOLDEN is translated.
    assert_golden(response.json(), translate_body(expected["body"], maps), context=case["id"])


# ---------------------------------------------------------------------------
# Independent anchors: the seeded household's money, recomputed here from the
# frozen rows rather than read back out of a golden (moved verbatim from
# frontend/tests/householdParity.test.ts — the capture can no longer freeze
# the same wrong number on both sides and pass).
# ---------------------------------------------------------------------------


def _spend(rows: list[dict]) -> float:
    """Everything the spending readers keep: negative cad_amount (a Decimal
    string in the fixture), in range."""
    return sum(
        float(row["cad_amount"])
        for row in rows
        if row["cad_amount"] is not None and float(row["cad_amount"]) < 0
    )


def _golden(case_id: str) -> dict[str, Any]:
    for case in HOUSEHOLD_CASES:
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


def test_household_total_is_own_plus_shared_and_never_the_hidden_account() -> None:
    partner = HOUSEHOLD["datasets"]["partner"]
    own = -_spend(partner["ownTransactions"])
    shared = -_spend(partner["projectionTransactions"])
    assert round(own, 2) == 101.1
    assert round(shared, 2) == 177.75

    body = _golden("household.spending.month.full")["expected"]["body"]
    assert body["spend_total_cad"] == "278.85"
    assert round(float(body["total_cad"]), 2) == round(own + shared, 2)
    # 97.99 sits on the owner's HIDDEN account (docs/43 §2 deny-by-default):
    # if it ever appears here, the allowlist leaked.
    assert body["spend_total_cad"] != "376.84"


def test_the_narrow_scope_is_the_partners_own_rows_alone() -> None:
    body = _golden("own.spending.month.full")["expected"]["body"]
    assert body["spend_total_cad"] == "101.10"


def test_the_owner_seats_household_is_its_own_rows_one_directional_grant() -> None:
    degenerate = _golden("owner-seat.spending.month.household")["expected"]["body"]
    own = _golden("owner-seat.spending.month.own")["expected"]["body"]
    assert degenerate == own
    # 177.75 shared + 97.99 hidden — the owner sees ALL of their own rows,
    # because the partner writes nothing to project back (docs/43 §12).
    assert own["spend_total_cad"] == "275.74"


def test_a_privacy_mode_owner_projects_nothing_into_the_household() -> None:
    body = _golden("privacy.spending.month.household")["expected"]["body"]
    assert body["spend_total_cad"] == "101.10"


def test_the_child_drill_rolls_through_the_owners_tree() -> None:
    body = _golden("household.spending.drill.owner-child")["expected"]["body"]
    assert body["spend_total_cad"] == "45.50"


def test_the_uncategorized_drill_counts_both_tenants_uncategorized_rows() -> None:
    # 12.25 (owner, shared account) + 7.77 (partner) — and NOT the owner's
    # 9.99 on the hidden account.
    body = _golden("household.spending.uncategorized-drill")["expected"]["body"]
    assert body["spend_total_cad"] == "20.02"

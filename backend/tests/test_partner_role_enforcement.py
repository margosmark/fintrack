"""Kilter adoption wave 1 — partner sharing role enforcement (t_09492311).

docs/43 §1 pins the role pair the card's "full vs view-only" scope maps onto:
the **owner** holds the full seat (invite, scope, revoke, audit) and the
**partner** is view-only — the projection reads, plus exactly its own two
writes (`POST /sharing/invites/accept`, `POST /sharing/leave`, both on the
sharing router). The M90 suite pinned that the partner *router* writes
nothing (`test_partner_router_is_get_only`,
`test_partner_surface_writes_no_domain_row`); the card's AC asks for the
other half, against the surfaces a view-only partner can actually reach with
a valid session:

* `test_view_only_partner_is_refused_by_every_mutation_surface` — the
  partner presenting the OWNER's row ids to every domain write route, and to
  the owner's sharing-seat routes, is refused (never a 2xx), the owner's
  data is byte-identical afterwards, and the link itself survives;
* `test_two_households_never_leak_into_each_other` — two complete
  households (owner + partner each) never serve a row, id, amount or name
  from the other, in either direction, and a cross-household row id is
  refused on the write surface too.

No product-code changes here: these are pins for behaviour docs/43 already
specifies (card AC: role enforcement tests, no cross-household data leakage
tests).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session, func, select
from test_household_sharing import (
    API,
    OWNER_EMAIL,
    PARTNER,
    PARTNER_EMAIL,
    PARTNER_ROUTES,
    SHARING,
    THIRD_EMAIL,
    _accept,
    _account,
    _headers,
    _invite,
    _register,
    _seed_shared_and_hidden,
    _tx,
    _user_id,
)

HOUSE1_OWNER = "house-one-owner@example.com"
HOUSE1_PARTNER = "house-one-partner@example.com"
HOUSE2_OWNER = "house-two-owner@example.com"
HOUSE2_PARTNER = "house-two-partner@example.com"


def _count(session: Session, model) -> int:
    return int(session.exec(select(func.count()).select_from(model)).one())  # type: ignore[arg-type]


# --- 1. role enforcement (card AC: view-only cannot mutate) ------------------


def test_view_only_partner_is_refused_by_every_mutation_surface(
    client: TestClient, session: Session
) -> None:
    """A view-only partner aiming at the OWNER's rows through every mutation
    surface gets a refusal — sharing grants a read projection, never a write
    path, not even to the rows inside the allowlist."""
    from app.models.accounts import Account
    from app.models.budgets import Budget
    from app.models.categories import Category
    from app.models.sharing import SharingLink, SharingLinkStatus
    from app.models.transactions import Transaction

    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    account_a, account_b, tx_a, _ = _seed_shared_and_hidden(client, owner_token)

    category = client.post(
        f"{API}/categories", headers=_headers(owner_token), json={"name": "OWNER-CATEGORY"}
    )
    assert category.status_code == 201, category.text
    category_id = category.json()["id"]
    budget = client.post(
        f"{API}/budgets",
        headers=_headers(owner_token),
        json={"category_id": category_id, "amount": "500.00", "period": "monthly"},
    )
    assert budget.status_code == 201, budget.text
    budget_id = budget.json()["id"]

    # Two pending invites: the partner's own (accepted below) and one aimed at
    # a third address — the latter is the owner's row the partner will try to
    # cancel through the sharing seat.
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    stray_invite = _invite(client, owner_token, THIRD_EMAIL, [account_a])
    assert _accept(client, invite["token"], partner_token).status_code == 200

    owner_id = _user_id(client, owner_token)
    partner_id = _user_id(client, partner_token)
    assert owner_id != partner_id

    # Owner baseline: the byte-exact views the partner must leave untouched.
    owner_routes = ("transactions", "accounts", "budgets", "categories")
    before = {
        route: client.get(f"{API}/{route}", headers=_headers(owner_token)).text
        for route in owner_routes
    }
    before["sharing"] = client.get(f"{SHARING}", headers=_headers(owner_token)).text

    domain_tables = (Transaction, Account, Budget, Category)
    counts_before = {table.__name__: _count(session, table) for table in domain_tables}
    links_before = _count(session, SharingLink)

    # (method, path, body) — every one of these must answer 4xx, never 2xx.
    attempts: list[tuple[str, str, dict | None]] = [
        # domain rows the owner shared (A) and hid (B) alike
        ("PATCH", f"{API}/transactions/{tx_a['id']}", {"raw_description": "PARTNER-HACKED"}),
        ("DELETE", f"{API}/transactions/{tx_a['id']}", None),
        ("PATCH", f"{API}/accounts/{account_a}", {"name": "PARTNER-HACKED"}),
        ("DELETE", f"{API}/accounts/{account_a}", None),
        ("PATCH", f"{API}/accounts/{account_b}", {"name": "PARTNER-HACKED"}),
        ("DELETE", f"{API}/accounts/{account_b}", None),
        ("PATCH", f"{API}/budgets/{budget_id}", {"amount": "9999.99"}),
        ("DELETE", f"{API}/budgets/{budget_id}", None),
        ("PATCH", f"{API}/categories/{category_id}", {"name": "PARTNER-HACKED"}),
        ("DELETE", f"{API}/categories/{category_id}", None),
        # creates addressed at the OWNER's rows
        (
            "POST",
            f"{API}/transactions",
            {
                "account_id": account_a,
                "date": "2026-05-10",
                "raw_description": "PARTNER-INJECTED",
                "amount_native": "-1.00",
                "currency": "CAD",
            },
        ),
        (
            "POST",
            f"{API}/budgets",
            {"category_id": category_id, "amount": "1.00", "period": "monthly"},
        ),
        # the OWNER's sharing seat: scope, revoke and a pending invite's cancel
        ("PATCH", f"{SHARING}/scope", {"account_ids": [account_b]}),
        ("POST", f"{SHARING}/revoke", None),
        ("DELETE", f"{SHARING}/invites/{stray_invite['id']}", None),
    ]

    for method, path, body in attempts:
        response = client.request(method, path, headers=_headers(partner_token), json=body)
        assert response.status_code >= 400, (
            f"view-only partner got {response.status_code} from {method} {path}: "
            f"{response.text[:400]}"
        )
        # every refusal is the error envelope, never a bare body
        payload = response.json()
        assert "detail" in payload and "code" in payload, payload

    # Exact codes for the sharing-seat refusals (docs/43 §5/§7).
    scope_response = client.patch(
        f"{SHARING}/scope", headers=_headers(partner_token), json={"account_ids": [account_b]}
    )
    assert (scope_response.status_code, scope_response.json()["code"]) == (
        404,
        "sharing_not_linked",
    )
    revoke_response = client.post(f"{SHARING}/revoke", headers=_headers(partner_token))
    assert (revoke_response.status_code, revoke_response.json()["code"]) == (
        404,
        "sharing_not_linked",
    )
    cancel_response = client.delete(
        f"{SHARING}/invites/{stray_invite['id']}", headers=_headers(partner_token)
    )
    assert (cancel_response.status_code, cancel_response.json()["code"]) == (
        404,
        "sharing_missing",
    )

    # Nothing moved: owner bytes, domain row counts and the link row itself.
    after = {
        route: client.get(f"{API}/{route}", headers=_headers(owner_token)).text
        for route in owner_routes
    }
    after["sharing"] = client.get(f"{SHARING}", headers=_headers(owner_token)).text
    assert after == before, "owner data changed under a view-only partner"
    assert {t.__name__: _count(session, t) for t in domain_tables} == counts_before
    assert _count(session, SharingLink) == links_before

    # the owner holds one row per invite — resolve the ACTIVE seat by state
    # (docs/43 §5: rows are addressed by state, never by recency).
    active_links = session.exec(
        select(SharingLink).where(
            SharingLink.user_id == owner_id, SharingLink.status == SharingLinkStatus.active
        )
    ).all()
    assert len(active_links) == 1, f"expected exactly one active link, got {len(active_links)}"
    link = active_links[0]
    assert link.status == SharingLinkStatus.active and link.partner_user_id == partner_id

    # The partner's OWN read surface is still live (refusals above were not a logout).
    state = client.get(f"{PARTNER}/state", headers=_headers(partner_token))
    assert state.status_code == 200 and state.json()["linked"] is True


# --- 2. cross-household isolation (card AC: no leakage) ---------------------


def test_two_households_never_leak_into_each_other(client: TestClient) -> None:
    """Two complete households, each with their own shared account: neither
    partner, nor either owner, ever receives a row, id, amount or name from
    the other household — and a cross-household id aimed at the write surface
    is refused like any foreign row."""
    # Household one
    owner1 = _register(client, HOUSE1_OWNER)["access_token"]
    partner1 = _register(client, HOUSE1_PARTNER)["access_token"]
    account1 = _account(client, owner1, "HOUSEHOLD-ONE-VAULT")
    tx1 = _tx(client, owner1, account1, "-1111.11", "HOUSEHOLD-ONE-MERCHANT")
    invite1 = _invite(client, owner1, HOUSE1_PARTNER, [account1])
    assert _accept(client, invite1["token"], partner1).status_code == 200

    # Household two
    owner2 = _register(client, HOUSE2_OWNER)["access_token"]
    partner2 = _register(client, HOUSE2_PARTNER)["access_token"]
    account2 = _account(client, owner2, "HOUSEHOLD-TWO-VAULT")
    tx2 = _tx(client, owner2, account2, "-2222.22", "HOUSEHOLD-TWO-MERCHANT")
    invite2 = _invite(client, owner2, HOUSE2_PARTNER, [account2])
    assert _accept(client, invite2["token"], partner2).status_code == 200

    # Partner one's OWN private tenant data — it must not surface in
    # household two either (the projection never mixes in the caller's rows).
    private_account = _account(client, partner1, "PARTNER-ONE-PRIVATE-CACHE")
    _tx(client, partner1, private_account, "-3333.33", "PARTNER-ONE-PRIVATE-MERCHANT")

    def partner_sweep(token: str) -> str:
        texts = []
        for route in PARTNER_ROUTES:
            response = client.get(f"{PARTNER}/{route}", headers=_headers(token))
            assert response.status_code == 200, f"{route}: {response.text}"
            texts.append(response.text)
        return "".join(texts)

    house2_markers = ("HOUSEHOLD-TWO", "2222.22")
    house1_markers = ("HOUSEHOLD-ONE", "1111.11")
    private_markers = ("PARTNER-ONE-PRIVATE", "3333.33")

    sweep1 = partner_sweep(partner1)
    sweep2 = partner_sweep(partner2)
    for marker in house2_markers:
        assert marker not in sweep1, f"household two leaked into partner one: {marker}"
    for marker in house1_markers + private_markers:
        assert marker not in sweep2, f"household one leaked into partner two: {marker}"

    # Id-level isolation: the allowlist and the tenant both hold.
    p1_accounts = client.get(f"{PARTNER}/accounts", headers=_headers(partner1)).json()["items"]
    assert [row["id"] for row in p1_accounts] == [account1]
    p2_accounts = client.get(f"{PARTNER}/accounts", headers=_headers(partner2)).json()["items"]
    assert [row["id"] for row in p2_accounts] == [account2]
    p1_txs = client.get(f"{PARTNER}/transactions", headers=_headers(partner1)).json()["items"]
    assert {row["id"] for row in p1_txs} == {tx1["id"]}

    # Each partner's link names their own owner — never the other household's.
    state1 = client.get(f"{PARTNER}/state", headers=_headers(partner1)).json()
    state2 = client.get(f"{PARTNER}/state", headers=_headers(partner2)).json()
    assert state1["owner"]["user_id"] == _user_id(client, owner1)
    assert state2["owner"]["user_id"] == _user_id(client, owner2)

    # Owners see only their own households.
    owner1_view = client.get(f"{API}/transactions", headers=_headers(owner1)).text
    owner2_view = client.get(f"{API}/transactions", headers=_headers(owner2)).text
    for marker in house2_markers:
        assert marker not in owner1_view
    for marker in house1_markers:
        assert marker not in owner2_view

    # Cross-household row ids aimed at the write surface are refused, and the
    # target row survives byte-identical.
    patch_response = client.patch(
        f"{API}/transactions/{tx2['id']}",
        headers=_headers(partner1),
        json={"raw_description": "CROSS-HOUSEHOLD-HACK"},
    )
    assert patch_response.status_code == 404, patch_response.text
    delete_response = client.delete(f"{API}/accounts/{account2}", headers=_headers(partner1))
    assert delete_response.status_code == 404, delete_response.text
    # The target row survives, byte-identical, in its own owner's list view
    # (there is no owner GET /transactions/{id}; the list is the read surface).
    owner2_after = client.get(f"{API}/transactions", headers=_headers(owner2))
    assert owner2_after.status_code == 200
    assert "HOUSEHOLD-TWO-MERCHANT" in owner2_after.text
    assert "CROSS-HOUSEHOLD-HACK" not in owner2_after.text

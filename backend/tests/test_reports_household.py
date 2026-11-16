"""M159 + M179 — the household scope on the transaction-population report
routes (docs/12 §Owner dimension, docs/08 §Report routes, docs/43 §6).

The route shape is M158 Decision 1's ruling, verbatim: a `scope=household`
param on `GET /reports/spending` and `GET /reports/spending/categories` that
unions the caller's own rows with the rows their own ACTIVE `sharing_links`
row projects, then computes the SAME aggregations the existing routes run.
**M179 extends that scope to `GET /reports/category-tag-matrix`** — the
Categories tab's second panel, which is drawn from the same population as the
donut above it, so leaving it owner-only put two populations on one screen.
Everything this file pins falls out of that:

* tenancy — the projection half is reached only through the caller's own
  link; a non-joined user's id (there is no such input, and no way to name
  one) contributes nothing, and the owner's UN-shared account never reaches
  the household population;
* the one-directional grant — the link lets the PARTNER seat read the OWNER's
  allowlisted accounts (docs/43 §2, default DENY), so an OWNER-seat caller's
  household scope is their own rows, exactly as an unlinked caller's is;
* the merged namespace — a projected row carries the OWNER's category/tag ids
  (docs/43 §6), so both trees are loaded and the counterpart's entries name a
  bare-id conflict; two tenants' `Groceries` never mislabel each other;
* P1-5 — the same answer under `encryption_required` [False, True]: the guard
  changes which WRITES are accepted, never what an aggregate returns.

Not staged here: committed-only and the LEDGER split rule under the widened
population. Both ride the SAME predicate as `scope=own`
(`services/reports.py::_committed_transactions`), which
`tests/test_reports_spending_cashflow.py` already pins for the narrow scope —
one loader, one contract, nothing to drift.

Run: cd backend && uv run pytest tests/test_reports_household.py -q
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models.accounts import Account
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transactions import Transaction
from app.models.users import User

API = "/api/v1"
SHARING = f"{API}/sharing"
PASSWORD = "correct-horse-battery"
FROM = "2026-05-01"
TO = "2026-05-31"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client: TestClient, tag: str, role: str) -> dict[str, Any]:
    email = f"m159-{tag}-{role}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    response = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"token": response.json()["access_token"], "email": email}


def _user_id(client: TestClient, token: str) -> int:
    response = client.get(f"{API}/users/me", headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _account(client: TestClient, token: str, name: str) -> int:
    response = client.post(
        f"{API}/accounts",
        headers=_headers(token),
        json={"name": name, "type": "chequing", "currency": "CAD"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _category(client: TestClient, token: str, name: str) -> int:
    response = client.post(f"{API}/categories", headers=_headers(token), json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _tx(
    client: TestClient,
    token: str,
    account_id: int,
    amount: str,
    description: str,
    *,
    category_id: int | None = None,
    day: str = "2026-05-10",
) -> int:
    payload: dict[str, Any] = {
        "account_id": account_id,
        "date": day,
        "raw_description": description,
        "amount_native": amount,
        "currency": "CAD",
    }
    if category_id is not None:
        payload["category_id"] = category_id
    response = client.post(f"{API}/transactions", headers=_headers(token), json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _tag(client: TestClient, token: str, name: str) -> int:
    response = client.post(f"{API}/tags", headers=_headers(token), json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _tag_tx(client: TestClient, token: str, transaction_id: int, tag_ids: list[int]) -> None:
    response = client.put(
        f"{API}/transactions/{transaction_id}/tags",
        headers=_headers(token),
        json={"tag_ids": tag_ids},
    )
    assert response.status_code == 200, response.text


def _matrix(client: TestClient, token: str, **params: str) -> dict[str, Any]:
    response = client.get(
        f"{API}/reports/category-tag-matrix", headers=_headers(token), params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


def _link(
    client: TestClient, owner: dict[str, Any], partner: dict[str, Any], account_ids: list[int]
) -> None:
    invite = client.post(
        f"{SHARING}/invites",
        headers=_headers(owner["token"]),
        json={"email": partner["email"], "account_ids": account_ids},
    )
    assert invite.status_code == 201, invite.text
    accept = client.post(
        f"{SHARING}/invites/accept",
        headers=_headers(partner["token"]),
        json={"token": invite.json()["token"]},
    )
    assert accept.status_code == 200, accept.text


def _spending(client: TestClient, token: str, **params: str) -> dict[str, Any]:
    response = client.get(f"{API}/reports/spending", headers=_headers(token), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _breakdown(client: TestClient, token: str, **params: str) -> dict[str, Any]:
    response = client.get(
        f"{API}/reports/spending/categories", headers=_headers(token), params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


class HouseHold:
    """Owner (shared + hidden account), partner (own account), linked on the
    shared account only. Amounts chosen so every population is a distinct sum:
    partner own-only 30.00, owner shared 100.00, owner hidden 55.00, owner
    total 155.00, household 130.00.

    `day` dates the three rows (default: the M159/M179 window). A route whose
    window is the CURRENT month — `GET /reports/insights` — needs rows inside
    it, so that test passes `frozen_today`'s month with the `frozen_today`
    fixture applied.
    """

    def __init__(self, client: TestClient, tag: str, *, day: str = "2026-05-10") -> None:
        self.owner = _register(client, tag, "owner")
        self.partner = _register(client, tag, "partner")
        self.owner_id = _user_id(client, self.owner["token"])
        self.partner_id = _user_id(client, self.partner["token"])
        self.shared = _account(client, self.owner["token"], "M159-SHARED")
        self.hidden = _account(client, self.owner["token"], "M159-HIDDEN")
        self.own = _account(client, self.partner["token"], "M159-PARTNER-OWN")
        self.owner_category = _category(client, self.owner["token"], "OwnerGroceries")
        self.partner_category = _category(client, self.partner["token"], "PartnerGroceries")
        self.owner_shared_tx = _tx(
            client,
            self.owner["token"],
            self.shared,
            "-100.00",
            "M159-OWNER-SHARED",
            category_id=self.owner_category,
            day=day,
        )
        self.owner_hidden_tx = _tx(
            client,
            self.owner["token"],
            self.hidden,
            "-55.00",
            "M159-OWNER-HIDDEN",
            category_id=self.owner_category,
            day=day,
        )
        self.partner_tx = _tx(
            client,
            self.partner["token"],
            self.own,
            "-30.00",
            "M159-PARTNER-OWN",
            category_id=self.partner_category,
            day=day,
        )
        _link(client, self.owner, self.partner, [self.shared])


# --- the ruled shape: the union, and nothing outside it ----------------------


def test_household_scope_unions_own_and_projection_rows(client: TestClient) -> None:
    """The partner's household = their own 30.00 + the owner's SHARED 100.00."""
    house = HouseHold(client, "union")
    report = _spending(
        client,
        house.partner["token"],
        **{"from": FROM, "to": TO, "granularity": "month", "scope": "household"},
    )
    assert report["spend_total_cad"] == "130.00"
    assert report["total_cad"] == "130.00"


def test_default_scope_stays_the_callers_own_rows(client: TestClient) -> None:
    """`scope` is additive: with it absent (and at `own`) every caller keeps
    the pre-M159 answer byte-for-byte — the seven flipped routes' contract."""
    house = HouseHold(client, "default")
    for params in ({}, {"scope": "own"}):
        partner = _spending(
            client,
            house.partner["token"],
            **{"from": FROM, "to": TO, "granularity": "month", **params},
        )
        assert partner["spend_total_cad"] == "30.00", params
        owner = _spending(
            client,
            house.owner["token"],
            **{"from": FROM, "to": TO, "granularity": "month", **params},
        )
        assert owner["spend_total_cad"] == "155.00", params


def test_the_owners_unshared_account_never_reaches_the_household(client: TestClient) -> None:
    """Scope is the ALLOWLIST (docs/43 §2, deny by default): the owner's
    hidden 55.00 is in the owner's own answer and in NO partner answer."""
    house = HouseHold(client, "hidden")
    household = _spending(
        client,
        house.partner["token"],
        **{"from": FROM, "to": TO, "granularity": "day", "scope": "household"},
    )
    assert household["spend_total_cad"] == "130.00"
    totals = [item["total_cad"] for item in household["items"]]
    assert "55.00" not in totals, totals
    assert household["spend_total_cad"] != "185.00"


def test_an_empty_allowlist_projects_nothing(client: TestClient) -> None:
    """A link that shares no accounts still links — the household then holds
    exactly the caller's own rows (deny by default, docs/43 §2)."""
    house = HouseHold(client, "empty-scope")
    # One active link per seat (I2): end the first partnership, then re-link
    # the SAME pair with an empty allowlist.
    revoke = client.post(f"{SHARING}/revoke", headers=_headers(house.owner["token"]))
    assert revoke.status_code in (200, 204), revoke.text
    _link(client, house.owner, house.partner, [])
    report = _spending(
        client,
        house.partner["token"],
        **{"from": FROM, "to": TO, "granularity": "month", "scope": "household"},
    )
    assert report["spend_total_cad"] == "30.00", report


# --- tenancy: the caller's OWN link is the only way in ----------------------


def test_a_non_joined_caller_gets_zero_partner_rows(client: TestClient) -> None:
    """The explicit tripwire M158's ruling asks for: a user who joined
    nothing has no projection, so `scope=household` cannot deliver a single
    row that is not their own — no caller-supplied id exists to widen it."""
    # A linked pair EXISTS in this database — the stranger is simply not in
    # it, which is what makes the zero-partner-rows answer a tenancy fact.
    HouseHold(client, "non-joined")
    stranger = _register(client, "non-joined", "stranger")
    stranger_account = _account(client, stranger["token"], "M159-STRANGER")
    _tx(client, stranger["token"], stranger_account, "-10.00", "M159-STRANGER-ROW")

    household = _spending(
        client,
        stranger["token"],
        **{"from": FROM, "to": TO, "granularity": "month", "scope": "household"},
    )
    own = _spending(
        client,
        stranger["token"],
        **{"from": FROM, "to": TO, "granularity": "month"},
    )
    assert household == own, "an unlinked caller's household scope is their own rows"
    assert household["spend_total_cad"] == "10.00"
    # And the linked pair's money is nowhere in the stranger's answer.
    assert household["spend_total_cad"] not in {"30.00", "100.00", "130.00", "165.00"}


def test_the_owner_seats_household_is_the_owner_own_rows(client: TestClient) -> None:
    """M90's grant is one-directional (docs/43 §2): the partner reads the
    owner's allowlisted accounts, never the reverse — so an owner-seat caller
    gains nothing from `scope=household`, and the response proves it."""
    house = HouseHold(client, "owner-seat")
    household = _spending(
        client,
        house.owner["token"],
        **{"from": FROM, "to": TO, "granularity": "month", "scope": "household"},
    )
    own = _spending(
        client,
        house.owner["token"],
        **{"from": FROM, "to": TO, "granularity": "month"},
    )
    assert household == own
    assert household["spend_total_cad"] == "155.00"


def test_revocation_takes_the_projection_away_immediately(client: TestClient) -> None:
    """Scope is re-read per request (docs/43 §6) — no cache, no stale window."""
    house = HouseHold(client, "revoke")
    revoke = client.post(f"{SHARING}/revoke", headers=_headers(house.owner["token"]))
    assert revoke.status_code in (200, 204), revoke.text
    household = _spending(
        client,
        house.partner["token"],
        **{"from": FROM, "to": TO, "granularity": "month", "scope": "household"},
    )
    assert household["spend_total_cad"] == "30.00"


def test_a_privacy_mode_owner_projects_nothing_into_the_household(client: TestClient) -> None:
    """docs/43 §9 (D4): the owner's privacy posture governs what the PARTNER
    may read, and a household total is the owner's money exactly as much as a
    row's amount is. The projection serializers null every monetary field under
    this flag; an aggregate has no field to null, so the mask is
    population-level — the partner's household is their own rows."""
    house = HouseHold(client, "privacy")
    flip = client.patch(
        f"{API}/users/me",
        headers=_headers(house.owner["token"]),
        json={"privacy_mode": True},
    )
    assert flip.status_code == 200, flip.text
    assert flip.json()["privacy_mode"] is True

    household = _spending(
        client,
        house.partner["token"],
        **{"from": FROM, "to": TO, "granularity": "month", "scope": "household"},
    )
    assert household["spend_total_cad"] == "30.00", household
    # And the owner's own view is untouched — the mask hides the figures from
    # the PARTNER, never from the account holder (docs/43 §9).
    owner = _spending(
        client,
        house.owner["token"],
        **{"from": FROM, "to": TO, "granularity": "month", "scope": "household"},
    )
    assert owner["spend_total_cad"] == "155.00"

    # Turning it back on re-opens the projection on the next request — the
    # flag is read per request, never cached (docs/43 §6).
    unflip = client.patch(
        f"{API}/users/me",
        headers=_headers(house.owner["token"]),
        json={"privacy_mode": False},
    )
    assert unflip.status_code == 200, unflip.text
    reopened = _spending(
        client,
        house.partner["token"],
        **{"from": FROM, "to": TO, "granularity": "month", "scope": "household"},
    )
    assert reopened["spend_total_cad"] == "130.00", reopened


# --- the merged namespace: two tenants' ids must not mislabel each other ----


def test_household_names_resolve_from_both_trees(client: TestClient) -> None:
    """The owner's `OwnerGroceries` (id X) and the partner's
    `PartnerGroceries` (id Y) are DIFFERENT categories in different tenants.
    The household breakdown carries both names against their own money — no
    cross-tenant mislabel, and no row silently folded into `Uncategorized`."""
    house = HouseHold(client, "names")
    report = _breakdown(
        client,
        house.partner["token"],
        **{"from": FROM, "to": TO, "scope": "household"},
    )
    names = {item["name"]: item["cad_total"] for item in report["items"]}
    assert names.get("OwnerGroceries") == "100.00", names
    assert names.get("PartnerGroceries") == "30.00", names
    assert report["spend_total_cad"] == "130.00"
    assert report["total_cad"] == "130.00"

    # The narrow scope keeps its own labels: the partner's tree alone.
    own = _breakdown(client, house.partner["token"], **{"from": FROM, "to": TO})
    assert {item["name"] for item in own["items"]} == {"PartnerGroceries"}


def test_household_items_roll_up_through_the_owners_tree(client: TestClient) -> None:
    """The donut rolls children to their ROOT through the tree that owns the
    id — a projection row under an owner child category lands on the owner's
    root, not on the partner's same-numbered category."""
    house = HouseHold(client, "rollup")
    root = _category(client, house.owner["token"], "M159-OwnerRoot")
    child = client.post(
        f"{API}/categories",
        headers=_headers(house.owner["token"]),
        json={"name": "M159-OwnerChild", "parent_id": root},
    )
    assert child.status_code == 201, child.text
    child_id = child.json()["id"]
    _tx(
        client,
        house.owner["token"],
        house.shared,
        "-20.00",
        "M159-OWNER-CHILD",
        category_id=child_id,
    )
    report = _breakdown(
        client,
        house.partner["token"],
        **{"from": FROM, "to": TO, "scope": "household"},
    )
    names = {item["name"]: item["cad_total"] for item in report["items"]}
    assert names.get("M159-OwnerRoot") == "20.00", names
    assert "M159-OwnerChild" not in names, names


# --- P1-5: the write guard never moves a read number ------------------------


def _set_encryption_required(client: TestClient, token: str, value: bool) -> None:
    response = client.patch(
        f"{API}/users/me", headers=_headers(token), json={"encryption_required": value}
    )
    assert response.status_code == 200, response.text
    assert response.json()["encryption_required"] is value


@pytest.mark.parametrize("encryption_required", [False, True])
def test_household_answers_are_indifferent_to_encryption_required(
    client: TestClient, encryption_required: bool
) -> None:
    """P1-5: `encryption_required` is a WRITE guard. The household aggregate
    reads the mandatory plaintext money columns (docs/33 envelope table), so
    both routes answer identically under either value."""
    house = HouseHold(client, f"p15-{encryption_required}")
    token = house.partner["token"]
    _set_encryption_required(client, token, encryption_required)
    under_flag = _spending(
        client,
        token,
        **{"from": FROM, "to": TO, "granularity": "month", "scope": "household"},
    )
    under_flag_breakdown = _breakdown(
        client, token, **{"from": FROM, "to": TO, "scope": "household"}
    )

    _set_encryption_required(client, token, not encryption_required)
    under_other = _spending(
        client,
        token,
        **{"from": FROM, "to": TO, "granularity": "month", "scope": "household"},
    )
    under_other_breakdown = _breakdown(
        client, token, **{"from": FROM, "to": TO, "scope": "household"}
    )
    assert under_flag == under_other, "the guard must not move a household number"
    assert under_flag_breakdown == under_other_breakdown
    assert under_flag["spend_total_cad"] == "130.00"


# --- the CSV obligation (docs/08) and the parameter contract -----------------


def test_household_csv_carries_the_same_rows_as_the_json(client: TestClient) -> None:
    """`format=csv` re-runs the SAME computation over the SAME scope — the
    header follows the JSON field names, so a household CSV cannot describe a
    population the JSON does not have."""
    house = HouseHold(client, "csv")
    token = house.partner["token"]
    response = client.get(
        f"{API}/reports/spending",
        headers=_headers(token),
        params={
            "from": FROM,
            "to": TO,
            "granularity": "month",
            "scope": "household",
            "format": "csv",
        },
    )
    assert response.status_code == 200, response.text
    lines = [line for line in response.text.strip().splitlines() if line]
    assert lines[0] == "bucket,category_id,category_name,total_cad"
    body = lines[1:]
    assert len(body) == 2, body
    assert sum(float(line.rsplit(",", 1)[1]) for line in body) == 130.0

    json_body = _spending(
        client,
        token,
        **{"from": FROM, "to": TO, "granularity": "month", "scope": "household"},
    )
    assert [float(item["total_cad"]) for item in json_body["items"]] == [
        float(line.rsplit(",", 1)[1]) for line in body
    ]


def test_an_unknown_scope_is_a_422_not_a_silent_narrowing(client: TestClient) -> None:
    house = HouseHold(client, "bad-scope")
    response = client.get(
        f"{API}/reports/spending",
        headers=_headers(house.partner["token"]),
        params={"from": FROM, "to": TO, "scope": "everyone"},
    )
    assert response.status_code == 422, response.text


def test_the_household_scope_composes_with_the_drill_filter(client: TestClient) -> None:
    """`category_id` narrows the WIDENED population exactly as it narrows the
    narrow one — the drill is a row filter, the scope is a population."""
    house = HouseHold(client, "drill")
    drill = _spending(
        client,
        house.partner["token"],
        **{
            "from": FROM,
            "to": TO,
            "granularity": "month",
            "scope": "household",
            "category_id": str(house.partner_category),
        },
    )
    assert drill["spend_total_cad"] == "30.00"
    owner_drill = _spending(
        client,
        house.partner["token"],
        **{
            "from": FROM,
            "to": TO,
            "granularity": "month",
            "scope": "household",
            "category_id": str(house.owner_category),
        },
    )
    assert owner_drill["spend_total_cad"] == "100.00"


def test_the_widened_population_keeps_the_committed_only_rule(
    client: TestClient, session: Session
) -> None:
    """A row staged in a `pending_review` batch is invisible to the owner's
    own reports (docs/03) — the household half rides the SAME predicate, so a
    staged owner row must not surface in the partner's household either."""
    house = HouseHold(client, "staged")
    owner = session.get(User, house.owner_id)
    shared = session.get(Account, house.shared)
    assert owner is not None and shared is not None
    batch = ImportBatch(
        user_id=owner.id,
        account_id=shared.id,
        institution="M159",
        file_name="m159.csv",
        status=ImportBatchStatus.pending_review,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    session.add(
        Transaction(
            user_id=owner.id,
            account_id=shared.id,
            date=date(2026, 5, 11),
            raw_description="M159-STAGED-ROW",
            normalized_payee="M159-STAGED",
            amount_native=Decimal("-777.00"),
            currency="CAD",
            cad_amount=Decimal("-777.00"),
            import_batch_id=batch.id,
            dedup_hash=f"m159-staged-{house.shared}",
        )
    )
    session.commit()

    owner_own = _spending(
        client,
        house.owner["token"],
        **{"from": FROM, "to": TO, "granularity": "month"},
    )
    assert owner_own["spend_total_cad"] == "155.00", owner_own
    household = _spending(
        client,
        house.partner["token"],
        **{"from": FROM, "to": TO, "granularity": "month", "scope": "household"},
    )
    assert household["spend_total_cad"] == "130.00", household


# --- M179: the Category x Tag matrix is the same population as the donut -----


def test_matrix_household_unions_own_and_projection_rows(client: TestClient) -> None:
    """The Categories tab's second panel over the SAME population as the donut
    above it: the partner's household matrix is their own 30.00 + the owner's
    SHARED 100.00, and the owner's HIDDEN 55.00 is in neither answer."""
    house = HouseHold(client, "matrix-union")
    own = _matrix(client, house.partner["token"], **{"from": FROM, "to": TO})
    assert own["total_cad"] == "30.00", own
    household = _matrix(
        client, house.partner["token"], **{"from": FROM, "to": TO, "scope": "household"}
    )
    assert household["total_cad"] == "130.00", household
    # Rows are ranked by spend, largest first — the owner's shared category
    # leads, and the owner's HIDDEN 55.00 is in neither answer.
    assert [row["category_name"] for row in household["rows"]] == [
        "OwnerGroceries",
        "PartnerGroceries",
    ], household
    assert sorted(cell["cad_total"] for cell in household["items"]) == ["100.00", "30.00"], (
        household
    )
    assert "55.00" not in {cell["cad_total"] for cell in household["items"]}


def test_matrix_default_scope_stays_the_callers_own_rows(client: TestClient) -> None:
    """`scope` is additive here too: absent (and at `own`) the matrix is
    byte-identical to the pre-M179 shape for both seats."""
    house = HouseHold(client, "matrix-default")
    for params in ({}, {"scope": "own"}):
        partner = _matrix(client, house.partner["token"], **{"from": FROM, "to": TO, **params})
        assert partner["total_cad"] == "30.00", params
        owner = _matrix(client, house.owner["token"], **{"from": FROM, "to": TO, **params})
        assert owner["total_cad"] == "155.00", params


def test_matrix_household_names_resolve_from_both_trees(client: TestClient) -> None:
    """A projected row carries the OWNER's category ids (docs/43 §6), so the
    household matrix's rows are named from BOTH trees — the owner's row is not
    labelled with the partner's same-numbered category, and nothing foreign
    falls back to `Uncategorized`."""
    house = HouseHold(client, "matrix-names")
    household = _matrix(
        client, house.partner["token"], **{"from": FROM, "to": TO, "scope": "household"}
    )
    named = {row["category_name"]: row["category_id"] for row in household["rows"]}
    assert set(named) == {"OwnerGroceries", "PartnerGroceries"}, named
    assert named["OwnerGroceries"] == house.owner_category
    assert named["PartnerGroceries"] == house.partner_category
    assert "Uncategorized" not in named

    own = _matrix(client, house.partner["token"], **{"from": FROM, "to": TO})
    assert {row["category_name"] for row in own["rows"]} == {"PartnerGroceries"}


def test_matrix_household_tag_columns_resolve_from_both_tenants(client: TestClient) -> None:
    """The COLUMN axis is the counterpart's tag namespace too: a tag only the
    owner attached must appear by name (a bare foreign tag id would render an
    id-less column), and the cell it names must carry the owner's money."""
    house = HouseHold(client, "matrix-tags")
    owner_tag = _tag(client, house.owner["token"], "M179-OwnerTag")
    partner_tag = _tag(client, house.partner["token"], "M179-PartnerTag")
    _tag_tx(client, house.owner["token"], house.owner_shared_tx, [owner_tag])
    _tag_tx(client, house.partner["token"], house.partner_tx, [partner_tag])

    household = _matrix(
        client, house.partner["token"], **{"from": FROM, "to": TO, "scope": "household"}
    )
    columns = {column["tag_name"]: column["tag_id"] for column in household["columns"]}
    assert columns.get("M179-OwnerTag") == owner_tag, columns
    assert columns.get("M179-PartnerTag") == partner_tag, columns
    cells = {
        (cell["category_name"], cell["tag_name"]): cell["cad_total"] for cell in household["items"]
    }
    assert cells[("OwnerGroceries", "M179-OwnerTag")] == "100.00", cells
    assert cells[("PartnerGroceries", "M179-PartnerTag")] == "30.00", cells


def test_matrix_household_csv_carries_the_same_rows_as_the_json(client: TestClient) -> None:
    """The docs/08 CSV-parity obligation, on the matrix: `format=csv` under
    `scope=household` re-runs the same computation over the same scope, so the
    CSV rows and the JSON `items` are one population."""
    house = HouseHold(client, "matrix-csv")
    response = client.get(
        f"{API}/reports/category-tag-matrix",
        headers=_headers(house.partner["token"]),
        params={"from": FROM, "to": TO, "scope": "household", "format": "csv"},
    )
    assert response.status_code == 200, response.text
    lines = [line for line in response.text.strip().splitlines() if line]
    assert lines[0] == "category_id,category_name,tag_id,tag_name,cad_total"
    body = lines[1:]
    assert sum(float(line.rsplit(",", 1)[1]) for line in body) == 130.0

    json_body = _matrix(
        client, house.partner["token"], **{"from": FROM, "to": TO, "scope": "household"}
    )
    assert [float(cell["cad_total"]) for cell in json_body["items"]] == [
        float(line.rsplit(",", 1)[1]) for line in body
    ]


def test_matrix_unknown_scope_is_a_422(client: TestClient) -> None:
    house = HouseHold(client, "matrix-bad-scope")
    response = client.get(
        f"{API}/reports/category-tag-matrix",
        headers=_headers(house.partner["token"]),
        params={"from": FROM, "to": TO, "scope": "everyone"},
    )
    assert response.status_code == 422, response.text


def test_matrix_non_joined_caller_gets_zero_partner_rows(client: TestClient) -> None:
    """The same tripwire M158's ruling asks for, on the M179 route: a user who
    joined nothing has no projection, so `scope=household` cannot deliver a
    single row that is not their own."""
    HouseHold(client, "matrix-non-joined")
    stranger = _register(client, "matrix-non-joined", "stranger")
    stranger_account = _account(client, stranger["token"], "M179-STRANGER")
    _tx(client, stranger["token"], stranger_account, "-10.00", "M179-STRANGER-ROW")

    household = _matrix(client, stranger["token"], **{"from": FROM, "to": TO, "scope": "household"})
    own = _matrix(client, stranger["token"], **{"from": FROM, "to": TO})
    assert household == own, "an unlinked caller's household scope is their own rows"
    assert household["total_cad"] == "10.00"


def test_matrix_owner_seat_household_is_the_owner_own_rows(client: TestClient) -> None:
    """M90's grant is one-directional (docs/43 §2) — on the matrix as on the
    donut: an owner-seat caller gains nothing from `scope=household`."""
    house = HouseHold(client, "matrix-owner-seat")
    household = _matrix(
        client, house.owner["token"], **{"from": FROM, "to": TO, "scope": "household"}
    )
    own = _matrix(client, house.owner["token"], **{"from": FROM, "to": TO})
    assert household == own
    assert household["total_cad"] == "155.00"


def test_matrix_privacy_mode_owner_projects_nothing(client: TestClient) -> None:
    """docs/43 §9 (D4) is a population-level mask, and it covers the matrix:
    while the owner's privacy mode is on the partner's household matrix is
    their own rows, and the owner's own matrix is untouched."""
    house = HouseHold(client, "matrix-privacy")
    flip = client.patch(
        f"{API}/users/me",
        headers=_headers(house.owner["token"]),
        json={"privacy_mode": True},
    )
    assert flip.status_code == 200, flip.text

    masked = _matrix(
        client, house.partner["token"], **{"from": FROM, "to": TO, "scope": "household"}
    )
    assert masked["total_cad"] == "30.00", masked
    owner = _matrix(client, house.owner["token"], **{"from": FROM, "to": TO, "scope": "household"})
    assert owner["total_cad"] == "155.00", owner


# --- M179 scope population contract across report/export surfaces ---
#
# The Owner lens serves household scope on the three tab routes; the expansion
# adds the two TRANSACTION-ONLY routes whose whole population the M93
# projection delivers (docs/43 §6) — `/reports/insights` and
# `/reports/spending-comparison`. The other ten report GETs declare an own-only
# scope. This sweep classifies the published report surface and tests its
# refusal boundary alongside exports and budgets:
#
#   * scope is intentionally declared on all ten own-only routes as a
#     singleton `own`; unsupported household, unknown and empty values are
#     rejected at `query.scope`. The tests pin the new positive contract rather
#     than the prior silently-ignored parameter.
#   * the supported household controls: the five widened routes still union
#     the caller's own and allowlisted rows, and their CSV twins preserve that
#     same population (M159/M179 + the Owner-lens expansion).
#   * NO successful own-only report response carries the other tenant's rows:
#     not the ten own-only routes, not their CSV/HTML twins, not `/export/*`,
#     not `/budgets`. Refusal bodies are asserted separately as query-validation
#     errors, never used as the tenant-fence proof.
#   * `/export/*` is the caller's own tenant snapshot by construction: the
#     owner's shared account is readable through `/partner/transactions` and a
#     `404` through `/export/ofx`.

LENS_ROUTES: dict[str, dict[str, str]] = {
    "/api/v1/reports/spending": {"from": FROM, "to": TO, "granularity": "month"},
    "/api/v1/reports/spending/categories": {"from": FROM, "to": TO},
    "/api/v1/reports/category-tag-matrix": {"from": FROM, "to": TO},
}

#: The Owner-lens EXPANSION: routes whose entire population is committed
#: transactions — exactly what the M93 projection delivers — and which
#: therefore answer `scope=household` honestly (docs/12 §Owner dimension,
#: docs/08). Same union, same merged category namespace, same math as
#: `LENS_ROUTES`; no UI lens control reads them yet, so the widening is an API
#: contract, not a screen change.
EXPANDED_ROUTES: dict[str, dict[str, str]] = {
    "/api/v1/reports/insights": {},
    # The comparison takes an explicit window, so it is pinned to the SAME
    # frozen month the insights window covers (tests seed one fixture there).
    "/api/v1/reports/spending-comparison": {
        "current_start": "2026-06-01",
        "current_end": "2026-06-15",
    },
}

#: EVERY `/reports/*` GET route that accepts `scope=own|household`.
HOUSEHOLD_ROUTES: dict[str, dict[str, str]] = {**LENS_ROUTES, **EXPANDED_ROUTES}

#: Every OTHER `/reports/*` GET route, with the minimum it needs to answer.
#: This dictionary IS the classification: a `/reports/*` route added later fails
#: `test_the_report_surface_is_fully_classified_by_the_m179_disposition` until
#: docs/12's disposition table and this sweep both name it. These routes' whole
#: population is NOT reachable through the projection (trade cash actions,
#: balances, positions, price rows), so `household` is refused at `query.scope`
#: rather than silently answered with a narrower population.
OWN_ONLY_ROUTES: dict[str, dict[str, str]] = {
    "/api/v1/reports/cashflow": {"from": FROM, "to": TO, "granularity": "month"},
    "/api/v1/reports/cashflow-calendar": {"month": "2026-05"},
    "/api/v1/reports/cashflow-sankey": {"from": FROM, "to": TO},
    "/api/v1/reports/total-return": {"from": FROM, "to": TO},
    "/api/v1/reports/net-worth": {"from": FROM, "to": TO, "granularity": "month"},
    "/api/v1/reports/networth-snapshot": {"target_date": TO},
    "/api/v1/reports/investment-performance": {"from": FROM, "to": TO},
    "/api/v1/reports/capital-gains": {"year": "2026"},
    "/api/v1/reports/dividends": {"year": "2026"},
    "/api/v1/reports/dividend-calendar": {},
}

#: The server-side twins of the unextended routes (`format`): a CSV or an HTML
#: report is a surface too, and it must answer what its JSON twin answers.
OWN_ONLY_TWINS: dict[str, tuple[str, ...]] = {
    "/api/v1/reports/cashflow": ("csv",),
    "/api/v1/reports/net-worth": ("csv",),
    "/api/v1/reports/investment-performance": ("csv",),
    "/api/v1/reports/capital-gains": ("csv", "html"),
    "/api/v1/reports/dividends": ("csv",),
}

#: Strings that exist ONLY in the owner's tenant of the HouseHold fixture: their
#: presence in a partner-seat answer is a leak, whatever the route.
OWNER_MARKERS = (
    "M159-OWNER-SHARED",
    "M159-OWNER-HIDDEN",
    "M159-SHARED",
    "OwnerGroceries",
    "100.00",
    "130.00",
    "155.00",
)

#: The one exact figure each money-carrying unextended route must print for the
#: partner seat: their own rows, with the owner's 100.00 nowhere near them.
OWN_ONLY_FIGURES: dict[str, str] = {
    "/api/v1/reports/cashflow": "30.00",
    "/api/v1/reports/net-worth": "-30.00",
}


def _published_get_paths(prefix: str) -> dict[str, dict[str, Any]]:
    """Every GET path under `prefix`, as PUBLISHED (`app.openapi()`)."""
    from app.main import app

    return {
        path: ops["get"]
        for path, ops in app.openapi()["paths"].items()
        if path.startswith(prefix) and "get" in ops
    }


def _scope_param(operation: dict[str, Any]) -> dict[str, Any] | None:
    for param in operation.get("parameters", []):
        if param["name"] == "scope":
            return cast(dict[str, Any], param)
    return None


def _get(client: TestClient, token: str, path: str, params: dict[str, str] | None = None) -> Any:
    """GET a PUBLISHED path (the sweep's dictionaries are `app.openapi()` keys,
    so they carry the `/api/v1` prefix themselves)."""
    assert path.startswith(API), path
    return client.get(path, headers=_headers(token), params=params or {})


def _assert_no_owner_rows(path: str, text: str) -> None:
    found = [marker for marker in OWNER_MARKERS if marker in text]
    assert not found, f"{path} leaks the owner's tenant to the partner seat: {found}"


def test_the_report_surface_is_fully_classified_by_the_m179_disposition() -> None:
    """Every `/reports/*` GET route is either household-capable or own-only, and
    the two dictionaries above ARE that classification: a route added later fails
    here until the disposition table (docs/12 §Owner dimension) and this sweep
    both name it."""
    published = set(_published_get_paths(f"{API}/reports"))
    unclassified = sorted(published - (set(HOUSEHOLD_ROUTES) | set(OWN_ONLY_ROUTES)))
    assert not unclassified, f"unclassified /reports route(s): {unclassified}"
    gone = sorted((set(HOUSEHOLD_ROUTES) | set(OWN_ONLY_ROUTES)) - published)
    assert not gone, f"classified route(s) no longer published: {gone}"


def test_scope_is_declared_with_the_correct_population_on_every_report_route() -> None:
    """OpenAPI publishes a defaulted singleton on own-only routes and the
    existing two-value allowlist on every household-capable route."""
    published = _published_get_paths(f"{API}/reports")
    for path, operation in sorted(published.items()):
        param = _scope_param(operation)
        if path in HOUSEHOLD_ROUTES:
            assert param is not None, path
            assert param["schema"].get("enum") == ["own", "household"], (path, param)
            assert param.get("schema", {}).get("default") == "own", (path, param)
        else:
            assert param is not None, f"{path} must refuse unsupported population requests"
            assert param["schema"].get("const") == "own", (path, param)
            assert param.get("schema", {}).get("default") == "own", (path, param)


def test_own_only_reports_preserve_bare_and_explicit_own_for_all_seats(
    client: TestClient,
) -> None:
    """Omitted and scope=own remain byte-identical on every own-only route;
    linked owner, linked partner and an unlinked caller all get their own data."""
    house = HouseHold(client, "audit-own-only")
    stranger = _register(client, "audit-own-only", "stranger")
    stranger_account = _account(client, stranger["token"], "M179-STRANGER-OWN")
    _tx(client, stranger["token"], stranger_account, "-10.00", "M179-STRANGER-ROW")

    for path, params in OWN_ONLY_ROUTES.items():
        for seat, token in (
            ("owner", house.owner["token"]),
            ("partner", house.partner["token"]),
            ("unlinked", stranger["token"]),
        ):
            bare = _get(client, token, path, params)
            explicit_own = _get(client, token, path, {**params, "scope": "own"})
            assert bare.status_code == explicit_own.status_code == 200, (path, seat)
            assert explicit_own.content == bare.content, (path, seat)
            if seat == "partner":
                _assert_no_owner_rows(path, bare.text)
                figure = OWN_ONLY_FIGURES.get(path)
                if figure is not None:
                    assert figure in bare.text, f"{path} omitted partner's own {figure}"
            if seat == "unlinked":
                _assert_no_owner_rows(path, bare.text)


def test_unsupported_scope_values_are_query_validation_422_on_all_routes_and_twins(
    client: TestClient,
) -> None:
    house = HouseHold(client, "audit-refusal")
    token = house.partner["token"]
    values = ("household", "everyone", "")
    for path, params in OWN_ONLY_ROUTES.items():
        surfaces = [(path, params)]
        surfaces.extend((path, {**params, "format": fmt}) for fmt in OWN_ONLY_TWINS.get(path, ()))
        for surface, base_params in surfaces:
            for value in values:
                response = _get(client, token, surface, {**base_params, "scope": value})
                assert response.status_code == 422, (surface, value, response.text)
                errors = response.json()
                assert errors["code"] == "validation_error", (surface, value, errors)
                assert any(
                    error.get("loc") == ["query", "scope"]
                    for error in errors["detail"]
                    if isinstance(error, dict)
                ), (surface, value, errors)


def test_the_csv_and_html_twins_inherit_their_json_twin_answer(client: TestClient) -> None:
    """Implemented format twins enforce the same own-only scope contract;
    the widened CSV control remains household-capable."""
    house = HouseHold(client, "audit-twins")
    token = house.partner["token"]
    for path, formats in OWN_ONLY_TWINS.items():
        params = OWN_ONLY_ROUTES[path]
        for fmt in formats:
            served = _get(client, token, path, {**params, "format": fmt})
            asked = _get(client, token, path, {**params, "format": fmt, "scope": "own"})
            assert served.status_code == asked.status_code == 200, (path, fmt, asked.text[:200])
            assert asked.content == served.content, (path, fmt)
            _assert_no_owner_rows(f"{path}?format={fmt}", served.text)

    # CONTROL — the three widened routes' CSV twins DO carry the population
    # (M179's CSV-parity obligation): the parameter is not inert everywhere.
    csv = _get(
        client,
        token,
        f"{API}/reports/spending",
        {"from": FROM, "to": TO, "granularity": "month", "format": "csv", "scope": "household"},
    )
    assert csv.status_code == 200, csv.text
    body = [line for line in csv.text.strip().splitlines() if line]
    assert sum(float(line.rsplit(",", 1)[1]) for line in body[1:]) == 130.0, body


def _zip_entries(payload: bytes) -> dict[str, bytes]:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist() if name != "manifest.json"}


def test_the_export_surface_is_the_callers_own_tenant(client: TestClient) -> None:
    """`/export/*` is not a report population: an export is the CALLER's own
    tenant snapshot (docs/11 §One-click export), and the M93 projection is not an
    export source. The partner's three exports carry their own tenant and nothing
    of the owner's — and no `scope` value can make it otherwise."""
    house = HouseHold(client, "audit-export")
    token = house.partner["token"]

    full = _get(client, token, f"{API}/export/full")
    assert full.status_code == 200, full.text[:200]
    snapshot = full.json()
    assert {row["name"] for row in snapshot["accounts"]} == {"M159-PARTNER-OWN"}
    assert {row["raw_description"] for row in snapshot["transactions"]} == {"M159-PARTNER-OWN"}
    _assert_no_owner_rows("/export/full", full.text)
    scoped = _get(client, token, f"{API}/export/full", {"scope": "household"})
    assert scoped.content == full.content, "an export has no population to widen"

    one_click = _get(client, token, f"{API}/export/zip")
    assert one_click.status_code == 200, one_click.text[:200]
    entries = _zip_entries(one_click.content)
    joined = b"\n".join(entries.values())
    assert b"M159-PARTNER-OWN" in joined
    assert b"M159-OWNER" not in joined and b"OwnerGroceries" not in joined
    # The scheduled export (M75, docs/11 §Scheduled export) delivers THIS payload
    # from a client-side runner, so it inherits the same property.
    scheduled = _get(client, token, f"{API}/export/zip", {"scope": "household"})
    assert _zip_entries(scheduled.content) == entries

    own_ofx = _get(client, token, f"{API}/export/ofx", {"account_id": str(house.own)})
    assert own_ofx.status_code == 200, own_ofx.text[:200]
    assert "M159-PARTNER-OWN" in own_ofx.text
    _assert_no_owner_rows("/export/ofx", own_ofx.text)

    # The owner's shared account is readable THROUGH the projection
    # (`/partner/transactions`, docs/43 §6) and never through an export: 404.
    foreign_ofx = _get(client, token, f"{API}/export/ofx", {"account_id": str(house.shared)})
    assert foreign_ofx.status_code == 404, foreign_ofx.text[:200]


def test_budgets_are_not_a_household_population(client: TestClient) -> None:
    """docs/43 §12: merged household BUDGET MODELS are an explicit anti-scope
    ("v2+ if ever"), a MODEL-level ruling rather than a report population — so
    there is no population to select here and the caller's list stays the
    caller's, `scope` or no `scope`."""

    def _budget(token: str, category_id: int, amount: str) -> None:
        response = client.post(
            f"{API}/budgets",
            headers=_headers(token),
            json={"category_id": category_id, "amount": amount},
        )
        assert response.status_code == 201, response.text

    house = HouseHold(client, "audit-budgets")
    _budget(house.owner["token"], house.owner_category, "500.00")
    _budget(house.partner["token"], house.partner_category, "50.00")

    bare = _get(client, house.partner["token"], f"{API}/budgets")
    assert bare.status_code == 200, bare.text[:200]
    assert [row["category_id"] for row in bare.json()] == [house.partner_category]
    _assert_no_owner_rows("/budgets", bare.text)
    scoped = _get(client, house.partner["token"], f"{API}/budgets", {"scope": "household"})
    assert scoped.content == bare.content


# --- Owner-lens expansion: the two transaction-only routes -------------------
#
# `/reports/insights` and `/reports/spending-comparison` sum COMMITTED
# TRANSACTIONS only — the one data set the M93 projection delivers (docs/43 §6)
# — so their whole population is reachable under `scope=household` and they
# answer it with the same union, the same merged category namespace and the
# same math as the three tab routes (docs/12 §Owner dimension, docs/08).
#
# What the expansion must NOT cost is pinned here as tightly as the M179
# refusal: bare and `scope=own` stay byte-identical for every seat, unknown and
# empty values are still 422 at `query.scope` (a widened route refuses what it
# does not support instead of silently ignoring it), and the owner's UN-shared
# account stays out of the widened answer.

EXPANDED_DAY = "2026-06-10"  # inside the frozen current month (FROZEN_TODAY)


def _expanded_unshared_markers_absent(path: str, text: str) -> None:
    """The owner's HIDDEN account is outside the allowlist: never in an answer."""
    assert "M159-OWNER-HIDDEN" not in text, f"{path} reached the owner's unshared account"
    assert "55.00" not in text, f"{path} summed the owner's unshared 55.00"


def test_the_expanded_routes_keep_bare_and_own_byte_identical_for_every_seat(
    client: TestClient,
) -> None:
    """The widening is additive: omitted `scope` and `scope=own` answer exactly
    what they answered before, for linked owner, linked partner and a stranger."""
    house = HouseHold(client, "expand-parity", day=EXPANDED_DAY)
    stranger = _register(client, "expand-parity", "stranger")
    _account(client, stranger["token"], "M179-STRANGER-OWN")

    for path, params in EXPANDED_ROUTES.items():
        for seat, token in (
            ("owner", house.owner["token"]),
            ("partner", house.partner["token"]),
            ("unlinked", stranger["token"]),
        ):
            bare = _get(client, token, path, params)
            explicit_own = _get(client, token, path, {**params, "scope": "own"})
            assert bare.status_code == explicit_own.status_code == 200, (path, seat, bare.text)
            assert explicit_own.content == bare.content, (path, seat)
            if seat != "owner":
                _assert_no_owner_rows(path, bare.text)


def test_unknown_and_empty_scope_still_refuse_on_the_expanded_routes(
    client: TestClient,
) -> None:
    """A route that now ANSWERS `household` still refuses everything else at
    `query.scope` — widening the allowlist must never widen it to silence."""
    house = HouseHold(client, "expand-refusal", day=EXPANDED_DAY)
    token = house.partner["token"]
    for path, params in EXPANDED_ROUTES.items():
        for value in ("everyone", "", "shared_household"):
            response = _get(client, token, path, {**params, "scope": value})
            assert response.status_code == 422, (path, value, response.text)
            errors = response.json()
            assert errors["code"] == "validation_error", (path, value, errors)
            assert any(
                error.get("loc") == ["query", "scope"]
                for error in errors["detail"]
                if isinstance(error, dict)
            ), (path, value, errors)
        # and the value it DOES support still answers 200
        supported = _get(client, token, path, {**params, "scope": "household"})
        assert supported.status_code == 200, (path, supported.text[:300])


def test_spending_comparison_household_unions_own_and_projection_rows(
    client: TestClient,
) -> None:
    """Same union, same merged namespace, same math as `GET /reports/spending`
    over the same fixture: partner own 30.00, + owner shared 100.00 = 130.00;
    the owner's hidden 55.00 stays out; an owner seat reads its own 155.00."""
    house = HouseHold(client, "expand-compare", day=EXPANDED_DAY)
    params = EXPANDED_ROUTES[f"{API}/reports/spending-comparison"]

    partner = _get(client, house.partner["token"], f"{API}/reports/spending-comparison", params)
    assert partner.status_code == 200, partner.text
    assert Decimal(str(partner.json()["current_period"]["total"])) == Decimal("30.00")
    _assert_no_owner_rows("/reports/spending-comparison", partner.text)

    household = _get(
        client,
        house.partner["token"],
        f"{API}/reports/spending-comparison",
        {**params, "scope": "household"},
    )
    assert household.status_code == 200, household.text
    assert Decimal(str(household.json()["current_period"]["total"])) == Decimal("130.00")
    _expanded_unshared_markers_absent("/reports/spending-comparison", household.text)
    names = {
        row["category_name"]: Decimal(str(row["current"])) for row in household.json()["deltas"]
    }
    # the owner's projected rows carry the OWNER's category id, so the merged
    # tree names it — `Uncategorized` here would be the namespace bug docs/12
    # and docs/43 §6 forbid under this scope
    assert names.get("OwnerGroceries") == Decimal("100.00"), names
    assert names.get("PartnerGroceries") == Decimal("30.00"), names

    owner = _get(
        client,
        house.owner["token"],
        f"{API}/reports/spending-comparison",
        {**params, "scope": "household"},
    )
    assert owner.status_code == 200, owner.text
    # one-directional grant: the owner's household is the owner's own rows
    assert Decimal(str(owner.json()["current_period"]["total"])) == Decimal("155.00")


def test_insights_household_unions_own_and_projection_rows(
    client: TestClient,
    frozen_today: date,
) -> None:
    """`GET /reports/insights` (current-month window) answers the household
    population too: 30.00 own → 130.00 under the lens, with the counterpart's
    category named in `category_deltas` and the unshared 55.00 nowhere."""
    assert EXPANDED_DAY[:7] == frozen_today.strftime("%Y-%m"), frozen_today
    house = HouseHold(client, "expand-insights", day=EXPANDED_DAY)

    partner = _get(client, house.partner["token"], f"{API}/reports/insights")
    assert partner.status_code == 200, partner.text
    assert Decimal(str(partner.json()["current_month_spending"])) == Decimal("30.00")
    _assert_no_owner_rows("/reports/insights", partner.text)

    household = _get(
        client, house.partner["token"], f"{API}/reports/insights", {"scope": "household"}
    )
    assert household.status_code == 200, household.text
    assert Decimal(str(household.json()["current_month_spending"])) == Decimal("130.00")
    _expanded_unshared_markers_absent("/reports/insights", household.text)
    deltas = {
        row["category_name"]: Decimal(str(row["current"]))
        for row in household.json()["category_deltas"]
    }
    assert deltas.get("OwnerGroceries") == Decimal("100.00"), deltas
    assert deltas.get("PartnerGroceries") == Decimal("30.00"), deltas

    owner = _get(client, house.owner["token"], f"{API}/reports/insights", {"scope": "household"})
    assert owner.status_code == 200, owner.text
    assert Decimal(str(owner.json()["current_month_spending"])) == Decimal("155.00")


def test_the_expanded_routes_obey_the_privacy_mask_and_the_deny_default(
    client: TestClient,
    frozen_today: date,
) -> None:
    """docs/43 §9 D4 (population-level mask) and §2 (default DENY) reach the
    expansion unchanged: a privacy-mode owner projects NOTHING, and a link that
    shares no account widens to exactly the caller's own rows."""
    house = HouseHold(client, "expand-privacy", day=EXPANDED_DAY)
    partner = house.partner["token"]
    owner = house.owner["token"]

    # D4 — privacy mode is a POPULATION mask: the household collapses to the
    # partner's own rows, asserted against the same figures the lens test pins.
    flip = client.patch(f"{API}/users/me", headers=_headers(owner), json={"privacy_mode": True})
    assert flip.status_code == 200, flip.text
    for path, params in EXPANDED_ROUTES.items():
        masked = _get(client, partner, path, {**params, "scope": "household"})
        assert masked.status_code == 200, (path, masked.text[:300])
        _assert_no_owner_rows(path, masked.text)
        own = _get(client, partner, path, params)
        assert masked.content == own.content, path
    unflip = client.patch(f"{API}/users/me", headers=_headers(owner), json={"privacy_mode": False})
    assert unflip.status_code == 200, unflip.text

    # deny-by-default — one active link per seat (I2): end this partnership,
    # then re-link the SAME pair with an EMPTY allowlist (docs/43 §2).
    revoke = client.post(f"{SHARING}/revoke", headers=_headers(owner))
    assert revoke.status_code in (200, 204), revoke.text
    _link(client, house.owner, house.partner, [])
    for path, params in EXPANDED_ROUTES.items():
        widened = _get(client, partner, path, {**params, "scope": "household"})
        assert widened.status_code == 200, (path, widened.text[:300])
        own = _get(client, partner, path, params)
        assert widened.content == own.content, path
        _assert_no_owner_rows(path, widened.text)

"""M90 household sharing — docs/43 §Verification 1-2.

The acceptance set, straight from the spec's named-tests table:

* the full invite → accept → scoped projection → deny-by-default → revoke
  walkthrough (§Verification 1), ending with `GET /partner/state` at
  `200 {linked: false}` and the four data routes at `401 sharing_not_linked`
  while the partner's own session still works (M92);
* `test_partner_state_contract` — the amended `/partner/state` contract
  (unauth → 401 `invalid_token`; authenticated + unlinked → `200
  {linked: false}` alone; linked → the pre-M92 payload + `linked: true`;
  revoked → back to `{linked: false}` while the data routes still 401);
* `test_pre_handler_refusals_write_no_receipt` — docs/43 §3's handler-layer
  boundary: 422 / 429 / demo-guard 403 leave no `sharing_audit` row;
* `test_every_sharing_query_is_scoped_by_the_join` — the SQL-sweep
  escalation tripwire (`before_cursor_execute`): a failure here is an
  ESCALATION to the parent card, never a design choice (docs/43 §6);
* the named cases: demo tenant both directions, privacy-mode honesty (D4),
  GET-only partner router, no key material, no hidden/own rows, single-use
  token + address privacy, the DB-decided accept race, the `mcp_audit`
  receipt shape, rate limits + invite cap, export/demo-reset classification;
* the tenancy regression pin: the owner's list endpoints never touch the
  sharing tables (the byte-identical pre-M90 baseline itself is re-probed
  across trees at gate time — see the M90 handoff).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, func, select

import app.api.sharing as sharing_api
from app.api.deps import get_engine
from app.core.ratelimit import SHARING_INVITE_LIMIT, SHARING_MUTATE_LIMIT, limiter
from app.core.security import create_access_token
from app.models.mcp_audit import McpAudit
from app.models.sharing import SharingAudit, SharingLink, SharingLinkStatus
from app.models.users import User
from app.services.demo_seed import DEMO_EMAIL

API = "/api/v1"
SHARING = f"{API}/sharing"
PARTNER = f"{API}/partner"
PASSWORD = "correct-horse-battery"
OWNER_EMAIL = "owner@example.com"
PARTNER_EMAIL = "partner@example.com"
THIRD_EMAIL = "third@example.com"

# The five partner GETs, in sweep order (docs/43 §6).
PARTNER_ROUTES = ("state", "accounts", "transactions", "categories", "budgets")


# --- fixtures / helpers -----------------------------------------------------


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client: TestClient, email: str) -> dict:
    client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    response = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


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


def _tx(
    client: TestClient,
    token: str,
    account_id: int,
    amount: str,
    description: str,
    *,
    extra: dict | None = None,
) -> dict:
    payload = {
        "account_id": account_id,
        "date": "2026-05-10",
        "raw_description": description,
        "amount_native": amount,
        "currency": "CAD",
    }
    payload.update(extra or {})
    response = client.post(f"{API}/transactions", headers=_headers(token), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _invite(client: TestClient, owner_token: str, email: str, account_ids=None) -> dict:
    body: dict = {"email": email}
    if account_ids is not None:
        body["account_ids"] = account_ids
    response = client.post(f"{SHARING}/invites", headers=_headers(owner_token), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _accept(client: TestClient, token_value: str, partner_token: str):
    return client.post(
        f"{SHARING}/invites/accept",
        headers=_headers(partner_token),
        json={"token": token_value},
    )


def _leave(client: TestClient, partner_token: str):
    """POST /sharing/leave — the partner's own withdrawal (M90 D6 follow-up)."""
    return client.post(f"{SHARING}/leave", headers=_headers(partner_token))


def _sweep(client: TestClient, token: str) -> dict[str, object]:
    """One GET per partner route; returns the parsed JSON (assert 200 first)."""
    out: dict[str, object] = {}
    for route in PARTNER_ROUTES:
        response = client.get(f"{PARTNER}/{route}", headers=_headers(token))
        assert response.status_code == 200, f"{route}: {response.status_code} {response.text}"
        out[route] = response
    return out


def _budget(client: TestClient, token: str, *, amount: str, category_name: str) -> int:
    """Owner-side budget creation (category first) — returns the budget id."""
    category = client.post(
        f"{API}/categories", headers=_headers(token), json={"name": category_name}
    )
    assert category.status_code == 201, category.text
    budget = client.post(
        f"{API}/budgets",
        headers=_headers(token),
        json={"category_id": category.json()["id"], "amount": amount, "period": "monthly"},
    )
    assert budget.status_code == 201, budget.text
    return budget.json()["id"]


def _seed_shared_and_hidden(client: TestClient, owner_token: str) -> tuple[int, int, dict, dict]:
    """Accounts A (shared) and B (hidden), one distinctive transaction each."""
    account_a = _account(client, owner_token, "SHARED-CHEQUING")
    account_b = _account(client, owner_token, "HIDDEN-RESERVE")
    tx_a = _tx(client, owner_token, account_a, "-1234.56", "VISIBLE-GROCERY-RUN")
    tx_b = _tx(client, owner_token, account_b, "-9876.54", "SECRET-WIRE-TRANSFER")
    return account_a, account_b, tx_a, tx_b


# --- 1. the E2E walkthrough (docs/43 §Verification 1) -----------------------


def test_full_walkthrough_invite_accept_scope_revoke(client: TestClient, session: Session) -> None:
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    owner_id = _user_id(client, owner_token)
    partner_id = _user_id(client, partner_token)
    account_a, account_b, _, tx_b = _seed_shared_and_hidden(client, owner_token)

    # 1. Invite — uppercase address on the wire, normalized at rest; token
    #    shown ONCE; expires_at = created_at + 7d (DB clock vs app clock can
    #    differ by the insert latency, hence the 2s tolerance).
    invite = _invite(client, owner_token, PARTNER_EMAIL.upper(), [account_a])
    assert invite["status"] == "pending"
    assert invite["email"] == PARTNER_EMAIL
    assert invite["account_ids"] == [account_a]
    assert isinstance(invite["token"], str) and len(invite["token"]) >= 40
    assert invite["token"] in invite["accept_url"]
    assert invite["accept_url"].endswith(f"?token={invite['token']}")
    assert invite["email_sent"] is False  # SMTP unconfigured in tests
    created = datetime.fromisoformat(invite["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(invite["expires_at"].replace("Z", "+00:00"))
    # Invariant: expires_at = created_at + 7d (docs/43 §3) — EXACT in prod
    # where app and DB share one clock; in tests the DB sits on another
    # machine (~1s NTP skew observed), so the TTL is pinned to ±60s.
    assert abs((expires - created) - timedelta(days=7)) <= timedelta(seconds=60)
    # Owner state now reads pending, with no partner yet.
    state = client.get(SHARING, headers=_headers(owner_token)).json()
    assert state["status"] == "pending" and state["partner"] is None

    # 2. Accept as the partner (their own session).
    accepted = _accept(client, invite["token"], partner_token)
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["status"] == "active"
    assert body["partner"]["user_id"] == partner_id
    assert body["partner"]["email"] == PARTNER_EMAIL
    assert body["accepted_at"] is not None

    # 3. Scoped projection: exactly [A]; B contributes nothing — its id,
    #    name and transaction never appear in any partner payload.
    accounts = client.get(f"{PARTNER}/accounts", headers=_headers(partner_token))
    assert accounts.status_code == 200
    assert [item["id"] for item in accounts.json()["items"]] == [account_a]
    assert "HIDDEN-RESERVE" not in accounts.text

    # 4. Transactions: A's rows only, committed-only; B's account id is a
    #    404 (the client asked for a row it may not see), not a silent ignore.
    feed = client.get(f"{PARTNER}/transactions", headers=_headers(partner_token))
    assert feed.status_code == 200
    assert {row["account_id"] for row in feed.json()["items"]} == {account_a}
    assert "SECRET-WIRE-TRANSFER" not in feed.text
    assert tx_b["id"] not in {row["id"] for row in feed.json()["items"]}
    blocked = client.get(
        f"{PARTNER}/transactions",
        headers=_headers(partner_token),
        params={"account_id": account_b},
    )
    assert blocked.status_code == 404, blocked.text
    assert blocked.json() == {
        "detail": "Account not found",
        "code": "sharing_account_missing",
    }

    # 5. Deny-by-default proven live: scope [] ⇒ partner sees nothing,
    #    on the very next request (no cache, no logout).
    scoped_out = client.patch(
        f"{SHARING}/scope",
        headers=_headers(owner_token),
        json={"account_ids": []},
    )
    assert scoped_out.status_code == 200 and scoped_out.json()["account_ids"] == []
    empty = client.get(f"{PARTNER}/accounts", headers=_headers(partner_token))
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}

    # 6. Scope back to [A] ⇒ accounts return.
    rescoped = client.patch(
        f"{SHARING}/scope",
        headers=_headers(owner_token),
        json={"account_ids": [account_a]},
    )
    assert rescoped.status_code == 200
    back = client.get(f"{PARTNER}/accounts", headers=_headers(partner_token))
    assert [item["id"] for item in back.json()["items"]] == [account_a]

    # 7. Revoke — idempotent (M79 semantics): 200, then 200 again.
    revoked = client.post(f"{SHARING}/revoke", headers=_headers(owner_token))
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["revoked_at"] is not None
    again = client.post(f"{SHARING}/revoke", headers=_headers(owner_token))
    assert again.status_code == 200 and again.json()["status"] == "revoked"

    # 8. The partner's NEXT request: /partner/state is now 200 {linked:false}
    #    (M92 — a gone link is an answer, not an auth failure) while the data
    #    routes still answer one clean 401, and their own session still works
    #    (they are not bounced to /login).
    cut = client.get(f"{PARTNER}/state", headers=_headers(partner_token))
    assert cut.status_code == 200, cut.text
    assert cut.json() == {"linked": False}
    refused = client.get(f"{PARTNER}/accounts", headers=_headers(partner_token))
    assert refused.status_code == 401
    assert refused.json() == {
        "detail": "No shared household is linked to this account",
        "code": "sharing_not_linked",
    }
    still_mine = client.get(f"{API}/users/me", headers=_headers(partner_token))
    assert still_mine.status_code == 200 and still_mine.json()["id"] == partner_id

    # 9. The audit trail, newest first; actor is the owner except on accept.
    audit = client.get(f"{SHARING}/audit", headers=_headers(owner_token))
    assert audit.status_code == 200
    receipts = audit.json()["items"]
    assert [(row["event"], row["response_code"]) for row in receipts] == [
        ("revoke", 200),
        ("revoke", 200),
        ("scope_update", 200),
        ("scope_update", 200),
        ("invite_accept", 200),
        ("invite_create", 201),
    ]
    assert all(
        row["actor_user_id"] == owner_id for row in receipts if row["event"] != "invite_accept"
    )
    accept_receipts = [row for row in receipts if row["event"] == "invite_accept"]
    assert accept_receipts and accept_receipts[0]["actor_user_id"] == partner_id
    assert all(re.fullmatch(r"[0-9a-f]{64}", row["args_hash"]) for row in receipts)
    # The invite body's email was hashed, never stored: no receipt carries it.
    assert PARTNER_EMAIL not in audit.text


def test_partner_state_contract(client: TestClient) -> None:
    """M92 (docs/43 §6) — the amended `GET /partner/state` contract.

    * no session -> **401 `invalid_token`** (raised by `get_current_user`,
      before the resolver runs; this is the only 401 the route can produce);
    * authenticated, never linked -> **200 `{linked: false}`** and NO other
      key — a normal first-page state, not an auth failure;
    * linked -> **200 `{linked: true, ...}`** — the pre-M92 payload plus the
      discriminator, byte-for-byte everything else unchanged;
    * revoked -> back to **200 `{linked: false}`**, while the four DATA routes
      still answer **401 `sharing_not_linked`**.
    """
    # 1. Unauthenticated.
    anon = client.get(f"{PARTNER}/state")
    assert anon.status_code == 401, anon.text
    assert anon.json() == {"detail": "Not authenticated", "code": "invalid_token"}

    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]

    # 2. Authenticated + never linked (both seats: the invitee AND the owner,
    #    who has no incoming link of their own).
    for label, token in (("partner", partner_token), ("owner", owner_token)):
        response = client.get(f"{PARTNER}/state", headers=_headers(token))
        assert response.status_code == 200, f"{label}: {response.text}"
        assert response.json() == {"linked": False}, label

    # 3. Linked.
    account_id = _account(client, owner_token, "STATE-ACCOUNT")
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_id])
    accepted = _accept(client, invite["token"], partner_token)
    assert accepted.status_code == 200, accepted.text

    linked = client.get(f"{PARTNER}/state", headers=_headers(partner_token))
    assert linked.status_code == 200, linked.text
    body = linked.json()
    assert body["linked"] is True
    # The discriminator is the ONLY added key: the pre-M92 shape is intact.
    assert set(body) == {
        "linked",
        "owner",
        "account_ids",
        "privacy_mode",
        "link_id",
        "accepted_at",
    }
    assert body["owner"]["user_id"] == _user_id(client, owner_token)
    assert body["owner"]["email"] == OWNER_EMAIL
    assert body["account_ids"] == [account_id]
    assert isinstance(body["privacy_mode"], bool)
    assert isinstance(body["link_id"], int) and body["link_id"] > 0
    assert body["accepted_at"] is not None

    # 4. Revoked: the state route flips to {linked:false} on the next read,
    #    the data routes keep their 401, and the session stays valid.
    revoked = client.post(f"{SHARING}/revoke", headers=_headers(owner_token))
    assert revoked.status_code == 200, revoked.text
    after = client.get(f"{PARTNER}/state", headers=_headers(partner_token))
    assert after.status_code == 200 and after.json() == {"linked": False}
    for route in ("accounts", "transactions", "categories", "budgets"):
        refused = client.get(f"{PARTNER}/{route}", headers=_headers(partner_token))
        assert refused.status_code == 401, route
        assert refused.json()["code"] == "sharing_not_linked"
    still_mine = client.get(f"{API}/users/me", headers=_headers(partner_token))
    assert still_mine.status_code == 200


# --- 2. the escalation tripwire (docs/43 §6) --------------------------------


class _SqlRecorder:
    """Records every SELECT the sweep executes: (statement, parameters)."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            self.statements.append((statement, parameters))


def _param_dict(parameters: object) -> dict:
    # psycopg (pyformat) hands SQLAlchemy a named-parameter dict; anything
    # else would silently disable the value assertions below, so it fails loud.
    assert isinstance(parameters, dict), (
        f"expected named pyformat parameters, got {type(parameters).__name__}: {parameters!r}"
    )
    return parameters


def _in_clause_texts(statement: str, column: str) -> list[str]:
    """Inner text of every `<column> IN (...)` clause — paren-BALANCED, so a
    psycopg named bind `%(account_id_1)s` (whose `)` sits inside the list)
    is not cut short the way a `[^)]*` regex would."""
    out: list[str] = []
    for match in re.finditer(rf"\b{column} IN \(", statement):
        index = match.end()
        depth = 1
        while index < len(statement) and depth:
            if statement[index] == "(":
                depth += 1
            elif statement[index] == ")":
                depth -= 1
            index += 1
        assert depth == 0, f"unbalanced IN clause in: {statement}"
        out.append(statement[match.end() : index - 1])
    return out


def _resolved_in_values(clause_text: str, parameters: dict) -> list[int]:
    """Bound values of one IN clause: named binds resolved through the
    parameter dict (flattening a single tuple parameter, which psycopg may
    adapt to `((1, 2))`), or plain integer literals parsed directly."""
    names = re.findall(r"%\((\w+)\)s", clause_text)
    if not names:
        return [int(piece) for piece in re.findall(r"-?\d+", clause_text)]
    values: list[int] = []
    for name in names:
        bound = parameters[name]
        if isinstance(bound, (list, tuple)):
            values.extend(int(item) for item in bound)
        else:
            values.append(int(bound))
    return values


def test_every_sharing_query_is_scoped_by_the_join(client: TestClient, session: Session) -> None:
    """The tripwire: partner SELECTs must reach the owner's rows ONLY through
    `SharingLink.partner_user_id == me` and deliver ONLY the allowlist.

    Rules asserted over every SELECT of a five-route sweep:
      R1  every `user_id = :p` binds the OWNER's id — never the partner's;
      R2  the link table itself is always read via `partner_user_id = <me>`
          (the JOIN path — never an unscoped read of `sharing_links`);
      R3  every statement touching a `user_id` table carries a `user_id =`
          predicate (ownership is expressed, never omitted);
      R4  every statement touching an account-scoped table carries an
          `account_id IN (<allowlist>)` clause whose bound values are a
          subset of the scope — including the batch/valuation reads.

    Exemptions, each justified: `prices` (shared — `uq_prices_security_date`
    is global and no OWNER query filters it either) and `fx_rates` (the
    shared cache, no `user_id` column) are not tenant tables; joined-only
    tables are reached by FK from a scoped driving table (their own lack of
    a predicate is covered by R1: no statement may bind the partner's id).
    """
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    account_a, account_b, _, _ = _seed_shared_and_hidden(client, owner_token)
    owner_id = _user_id(client, owner_token)
    partner_id = _user_id(client, partner_token)
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert _accept(client, invite["token"], partner_token).status_code == 200

    engine = get_engine()
    recorder = _SqlRecorder()
    event.listen(engine, "before_cursor_execute", recorder)
    try:
        responses = _sweep(client, partner_token)
    finally:
        event.remove(engine, "before_cursor_execute", recorder)

    # Liveness: a listener that recorded nothing proves nothing.
    assert len(recorder.statements) >= 5, "sweep recorded too little SQL to be a tripwire"

    metadata = SQLModel.metadata
    tenant_tables = {
        name
        for name, table in metadata.tables.items()
        if "user_id" in table.c and name not in {"prices"}  # prices: shared by constraint
    }
    account_tables = {name for name, table in metadata.tables.items() if "account_id" in table.c}
    scope = {account_a}

    for statement, parameters in recorder.statements:
        params = _param_dict(parameters)

        # R1 — ownership predicates bind the OWNER, never the partner.
        for match in re.finditer(r"\buser_id = %\((\w+)\)s", statement):
            assert params[match.group(1)] == owner_id, (
                f"a user_id predicate bound {params[match.group(1)]!r}, "
                f"expected owner {owner_id}:\n{statement}"
            )

        # R2 — the link table is reached only via the JOIN path.
        if re.search(r"\bFROM sharing_links\b", statement):
            partner_binds = re.findall(r"\bpartner_user_id = %\((\w+)\)s", statement)
            assert partner_binds, f"sharing_links read without partner_user_id:\n{statement}"
            assert all(params[name] == partner_id for name in partner_binds), (
                f"partner_user_id bound to {partner_id} incorrectly:\n{statement}"
            )

        # R3 — every tenant table in the statement is behind a predicate.
        touched_tenant = [
            name for name in tenant_tables if re.search(rf"\b(?:FROM|JOIN) {name}\b", statement)
        ]
        # Only DRIVING tables must carry the predicate: `FROM <tenant>` —
        # every subquery counts, `FROM (SELECT ...)` does not name a table.
        # A JOINed tenant table is reached through its parent row (the
        # export's VIA_PARENT pattern; the tags decorator drives
        # transaction_tags and joins tags through the scoped page ids), and
        # sharing_links is R2's business (partner_user_id, never a bare
        # user_id — see the lookbehind below).
        driving = set(re.findall(r"\bFROM ([a-z_][a-z0-9_]*)", statement))
        ownership_scoped = sorted(
            name for name in touched_tenant if name in driving and name != "sharing_links"
        )
        if ownership_scoped:
            assert re.search(r"(?<!partner_)user_id =", statement), (
                f"SELECT drives {ownership_scoped} with no user_id predicate:\n{statement}"
            )

        # R4 — account-scoped reads carry the allowlist.
        touched_account = [
            name for name in account_tables if re.search(rf"\b(?:FROM|JOIN) {name}\b", statement)
        ]
        if touched_account:
            clauses = _in_clause_texts(statement, "account_id")
            assert clauses, (
                f"SELECT touches {touched_account} with no account_id IN clause:\n{statement}"
            )
            for clause_text in clauses:
                values = _resolved_in_values(clause_text, params)
                assert values, f"empty account_id IN clause:\n{statement}"
                assert set(values) <= scope, (
                    f"account_id IN {values} escapes the allowlist {scope}:\n{statement}"
                )

    # The sweep itself is functional (not just its SQL): A visible, B absent.
    assert [item["id"] for item in responses["accounts"].json()["items"]] == [account_a]  # type: ignore[union-attr]
    assert "HIDDEN-RESERVE" not in responses["accounts"].text  # type: ignore[union-attr]
    assert account_b not in [row["account_id"] for row in responses["transactions"].json()["items"]]  # type: ignore[union-attr]


def test_partner_router_is_get_only() -> None:
    """docs/43 §6: ALL five partner routes are GET — a pinned invariant."""
    from app.api.partner import router as partner_router

    methods = set()
    for route in partner_router.routes:
        methods |= set(getattr(route, "methods", set()) or set())
    assert methods == {"GET"}, f"partner router exposes {sorted(methods)}"


def test_partner_surface_writes_no_domain_row(client: TestClient, session: Session) -> None:
    """Sweeping every partner route leaves EVERY table's row count unchanged
    (the accept mutation lives on the sharing router; partner = read-only)."""
    from sqlmodel import SQLModel

    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    account_a, _, _, _ = _seed_shared_and_hidden(client, owner_token)
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert _accept(client, invite["token"], partner_token).status_code == 200

    def counts() -> dict[str, int]:
        out = {}
        for name, table in SQLModel.metadata.tables.items():
            out[name] = int(
                session.exec(select(func.count()).select_from(table)).one()  # type: ignore[arg-type]
            )
        return out

    before = counts()
    _sweep(client, partner_token)
    assert counts() == before


# --- 3. the named cases (docs/43 §Verification 2) ---------------------------


def test_demo_tenant_cannot_invite_and_cannot_be_invited(
    client: TestClient, session: Session
) -> None:
    """Both directions of the demo boundary (docs/43 §10)."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]

    # The demo tenant, minted directly: conftest disables the demo seed, and
    # the boundary under test is the WRITE GUARD, not the seeder.
    demo = User(email=DEMO_EMAIL, hashed_password="not-a-real-hash")
    session.add(demo)
    session.commit()
    assert demo.id is not None
    demo_id: int = demo.id
    demo_token = create_access_token(demo_id)

    # A demo session can never mutate (guard) — invite, scope, revoke, leave, accept.
    for method, path, body in (
        ("post", f"{SHARING}/invites", {"email": THIRD_EMAIL}),
        ("patch", f"{SHARING}/scope", {"account_ids": []}),
        ("post", f"{SHARING}/revoke", None),
        ("post", f"{SHARING}/leave", None),
        ("post", f"{SHARING}/invites/accept", {"token": "anything"}),
    ):
        response = getattr(client, method)(
            path, headers=_headers(demo_token), json=body if body is not None else None
        )
        assert response.status_code == 403, (path, response.status_code, response.text)
        assert response.json()["code"] == "demo_account_restricted"

    # The demo TENANT cannot be invited either (the guard does not cover it:
    # the CALLER here is not the demo).
    refused = client.post(
        f"{SHARING}/invites",
        headers=_headers(owner_token),
        json={"email": DEMO_EMAIL},
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "demo_account_restricted"

    # Neither seat ever holds a row, and the demo reads 401 like anyone else.
    demo_rows = session.exec(
        select(SharingLink).where(
            (SharingLink.user_id == demo_id)
            | (SharingLink.partner_user_id == demo_id)
            | (SharingLink.email == DEMO_EMAIL)
        )
    ).all()
    assert demo_rows == []
    for route in PARTNER_ROUTES:
        response = client.get(f"{PARTNER}/{route}", headers=_headers(demo_token))
        if route == "state":
            # M92: state is a normal answer for an authenticated caller.
            assert response.status_code == 200, route
            assert response.json() == {"linked": False}
        else:
            assert response.status_code == 401, route
            assert response.json()["code"] == "sharing_not_linked"


def _set_privacy(client: TestClient, token: str, enabled: bool) -> None:
    response = client.patch(
        f"{API}/users/me", headers=_headers(token), json={"privacy_mode": enabled}
    )
    assert response.status_code == 200, response.text


def test_owner_privacy_mode_seals_invite_email_and_partner_egress(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4 / docs/43 §9: the OWNER's posture governs — invite mail suppressed
    and zero egress across a full partner sweep — and since M90 D4/A it also
    governs the amounts: the three money-carrying routes differ from a
    privacy-off sweep exactly where money crosses the seam (JSON `null`),
    while `categories` (no money) and everything but the flag in `state`
    stay byte-identical."""
    from tests.test_privacy_export import _forbid_network

    # The two AGENTS §2 network seams explode if touched (fx `requests.get`
    # + yfinance); the mail seam is spied separately below.
    _forbid_network(monkeypatch)
    mailer_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        sharing_api,
        "send_invite_email",
        lambda target, url, settings: mailer_calls.append((target, url)) or False,
    )

    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    account_a, _, _, _ = _seed_shared_and_hidden(client, owner_token)
    # A budget too: budgets carry one figure, so it must show the mask diff.
    _budget(client, owner_token, amount="500.00", category_name="EGRESS-GROCERIES")

    _set_privacy(client, owner_token, True)
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert invite["email_sent"] is False
    assert mailer_calls == [], "owner privacy mode must suppress the invite mail"
    assert _accept(client, invite["token"], partner_token).status_code == 200

    # Zero outbound HTTP across the sweep: `_forbid_network` raises inside
    # fx/yfinance if any read reaches out, and this sweep is all 200s (also
    # pinned by _sweep itself).
    private = _sweep(client, partner_token)

    _set_privacy(client, owner_token, False)
    public = _sweep(client, partner_token)

    # The money routes differ across privacy on/off — the mask is the diff —
    # and the seeded figure is gone from the privacy-on bytes. A route with
    # no money at all (categories) must still be byte-identical.
    for route in ("accounts", "transactions", "budgets"):
        assert private[route].content != public[route].content, route  # type: ignore[union-attr]
        assert "-1234.56" not in private[route].text, route  # type: ignore[union-attr]
    assert private["categories"].content == public["categories"].content  # type: ignore[union-attr]
    left = dict(private["state"].json())  # type: ignore[union-attr]
    right = dict(public["state"].json())  # type: ignore[union-attr]
    assert left.pop("privacy_mode") is True
    assert right.pop("privacy_mode") is False
    assert left == right


def test_privacy_mode_masks_every_partner_money_field(client: TestClient) -> None:
    """docs/43 §9 (M90 D4/A "Reading A", replacing the old Reading B pin):
    the OWNER's privacy mode hides the figures from the PARTNER.

    Every monetary field on all three money-carrying partner routes comes
    back JSON `null`; identity, dates, payees, category names and budget
    period do not move; the owner's OWN routes keep real numbers (privacy
    mode is the account holder's posture, not a global eraser), and the
    seeded amount never appears in any partner payload at all — the
    byte-scan is the assertion the card asked for.
    """
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    account_a, _, _, _ = _seed_shared_and_hidden(client, owner_token)
    budget_id = _budget(client, owner_token, amount="500.00", category_name="GROCERIES")
    _set_privacy(client, owner_token, True)
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert _accept(client, invite["token"], partner_token).status_code == 200

    sweep = _sweep(client, partner_token)

    # 1. Byte-scan: neither the shared transaction's amount nor the budget's
    #    target is present anywhere in the partner's bytes.
    for route, response in sweep.items():
        assert "-1234.56" not in response.text, route
        assert "500.00" not in response.text, route

    # 2. Accounts: rows listed, all four money fields null.
    accounts = sweep["accounts"].json()["items"]  # type: ignore[union-attr]
    assert [row["id"] for row in accounts] == [account_a]
    for row in accounts:
        for field in ("cash_balance", "investment_value", "total_value", "starting_balance"):
            assert row[field] is None, field

    # 3. Transactions: rows listed, money null, non-money untouched.
    feed = sweep["transactions"].json()["items"]  # type: ignore[union-attr]
    assert [row["raw_description"] for row in feed] == ["VISIBLE-GROCERY-RUN"]
    for row in feed:
        assert row["amount_native"] is None
        assert row["cad_amount"] is None

    # 4. Budgets: the definition survives (D3 shape), the figure does not.
    budgets = sweep["budgets"].json()  # type: ignore[union-attr]
    assert [row["id"] for row in budgets] == [budget_id]
    assert budgets[0]["category_name"] == "GROCERIES"
    assert budgets[0]["period"] == "monthly"
    assert budgets[0]["amount"] is None

    # 5. `state` still mirrors the posture the banner renders from.
    assert sweep["state"].json()["privacy_mode"] is True  # type: ignore[union-attr]

    # 6. The OWNER's own view is unaffected — same numbers as always.
    own_accounts = client.get(f"{API}/accounts", headers=_headers(owner_token)).json()["items"]
    assert [row["cash_balance"] for row in own_accounts if row["id"] == account_a] == ["-1234.56"]
    own_feed = client.get(f"{API}/transactions", headers=_headers(owner_token)).json()["items"]
    assert "-1234.56" in [row["amount_native"] for row in own_feed]
    own_budgets = client.get(f"{API}/budgets", headers=_headers(owner_token)).json()
    assert [row["amount"] for row in own_budgets if row["id"] == budget_id] == ["500.00"]

    # 7. The mask is conditional: privacy OFF puts every figure back.
    _set_privacy(client, owner_token, False)
    open_sweep = _sweep(client, partner_token)
    assert "-1234.56" in open_sweep["transactions"].text  # type: ignore[union-attr]
    assert "-1234.56" in open_sweep["accounts"].text  # type: ignore[union-attr]
    assert open_sweep["budgets"].json()[0]["amount"] == "500.00"  # type: ignore[union-attr]


def test_partner_sees_the_locked_shape_for_cipher_rows(
    client: TestClient, session: Session
) -> None:
    """docs/43 §8: a cipher-bearing row serializes IDENTICALLY for owner and
    partner (same TransactionPublic), and the partner's client resolves with
    `dek = null` — the frontend half lives in
    `frontend/tests/householdSharing.test.ts`."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    account_a = _account(client, owner_token, "SHARED-CHEQUING")
    _tx(
        client,
        owner_token,
        account_a,
        "-50.00",
        "PLAINTEXT-ROW",
        extra={"payee_cipher": "c29tZS1jaXBoZXJ0ZXh0", "amount_cipher": "bW9yZS1jaXBoZXI="},
    )
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert _accept(client, invite["token"], partner_token).status_code == 200

    owner_rows = client.get(f"{API}/transactions", headers=_headers(owner_token)).json()["items"]
    partner_rows = client.get(f"{PARTNER}/transactions", headers=_headers(partner_token)).json()[
        "items"
    ]
    assert len(owner_rows) == len(partner_rows) == 1
    # Byte-for-byte the same serializer output, ciphers included.
    assert owner_rows[0] == partner_rows[0]
    assert partner_rows[0]["payee_cipher"] == "c29tZS1jaXBoZXJ0ZXh0"
    assert partner_rows[0]["amount_cipher"] == "bW9yZS1jaXBoZXI="


def test_no_key_material_in_any_partner_payload(client: TestClient) -> None:
    """docs/43 §8.3: no /partner/* body contains vault material or a
    key-shaped field, and the partner's own bearer can only ever see THEIR
    own vault rows (a valid session is not a 401 here — scoping is the
    invariant being pinned)."""
    forbidden_keys = re.compile(r"dek|kek|wrapped|passphrase|index_key", re.IGNORECASE)
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    account_a = _account(client, owner_token, "SHARED-CHEQUING")
    _tx(client, owner_token, account_a, "-10.00", "VISIBLE-ROW")
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert _accept(client, invite["token"], partner_token).status_code == 200

    # Owner enrolls a vault device with a wrapped blob.
    device = "ab" * 16
    created = client.post(
        f"{API}/vault/envelopes",
        headers=_headers(owner_token),
        json={"device_id": device, "wrapped_blob": "SECRET-WRAPPED-BLOB"},
    )
    assert created.status_code == 201, created.text
    envelope_id = created.json()["id"]

    responses = _sweep(client, partner_token)
    for route, response in responses.items():
        assert "wrapped_blob" not in response.text, route
        assert "vault_envelopes" not in response.text, route
        payload = response.json()
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    assert not forbidden_keys.search(key), f"{route}: key {key!r}"
                    stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)

    # The partner's OWN (valid) bearer resolves only THEIR vault rows.
    partner_envelopes = client.get(
        f"{API}/vault/envelopes",
        headers=_headers(partner_token),
        params={"device_id": device},
    )
    assert partner_envelopes.status_code == 200
    body = partner_envelopes.json()
    assert body["items"] == []
    assert "SECRET-WRAPPED-BLOB" not in partner_envelopes.text
    # The owner's row still exists — scoping hid it, nothing deleted it.
    owner_envelopes = client.get(
        f"{API}/vault/envelopes",
        headers=_headers(owner_token),
        params={"device_id": device},
    )
    assert [row["id"] for row in owner_envelopes.json()["items"]] == [envelope_id]


def test_partner_payload_contains_no_hidden_and_no_own_rows(client: TestClient) -> None:
    """Allowlist + tenant isolation: the partner sees the owner's shared
    account and NOTHING else — no hidden-owner row, no partner-own row."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    partner_id = _user_id(client, partner_token)
    account_a, account_b, _, _ = _seed_shared_and_hidden(client, owner_token)
    # The partner's OWN tenant: account C + a row of their own.
    account_c = _account(client, partner_token, "PARTNER-OWNED-ACCOUNT")
    _tx(client, partner_token, account_c, "-555.55", "PARTNER-PRIVATE-ROW")

    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert _accept(client, invite["token"], partner_token).status_code == 200

    responses = _sweep(client, partner_token)
    combined = "".join(r.text for r in responses.values())  # type: ignore[union-attr]
    for marker in (
        "HIDDEN-RESERVE",
        "SECRET-WIRE-TRANSFER",
        "-9876.54",
        "PARTNER-OWNED-ACCOUNT",
        "PARTNER-PRIVATE-ROW",
        "-555.55",
    ):
        assert marker not in combined, f"partner payload leaked {marker!r}"

    accounts = responses["accounts"].json()  # type: ignore[union-attr]
    assert [item["id"] for item in accounts["items"]] == [account_a]
    assert all(item["id"] != account_c for item in accounts["items"])
    assert account_b != account_a
    feed = responses["transactions"].json()  # type: ignore[union-attr]
    assert {row["account_id"] for row in feed["items"]} == {account_a}
    # The projection is never the caller's own tenant.
    state = responses["state"].json()  # type: ignore[union-attr]
    assert state["owner"]["user_id"] != partner_id


def test_only_the_sharing_modules_import_the_link_table() -> None:
    """Static scan of `backend/app/**`: proof that NO pre-existing route
    gained a cross-tenant path (docs/43 §Verification 2).

    Allowlist (PM-reviewed): the two modules that define the table, the two
    routers that read it, `models/__init__.py` (the spec registers the
    models there so alembic/export see them), `api/users.py` — which
    names the classes ONLY inside `_USER_OWNED_MODELS`, the delete/demo-reset
    classification (a delete path, never a read) — and, since M159,
    `services/reports.py`, the ONE service that reads the link table to widen
    a report population (`_household_projection`). That read is the shape
    M158 Decision 1 ruled: it resolves the caller's OWN active link by the
    same indexed read `GET /partner/transactions` uses, extends only a
    PARTNER-seat caller (docs/43 §2 is one-directional), and yields an empty
    scope for a privacy-mode owner (docs/43 §9 D4) — each pinned by
    `test_reports_household.py`, so the escalation this tripwire exists to
    catch (an UNreviewed cross-tenant path) cannot slip in behind it.
    Anything else referencing `SharingLink` fails.
    """
    root = Path(__file__).resolve().parents[1] / "app"
    allowed = {
        "models/sharing.py",
        "models/__init__.py",
        "schemas/sharing.py",
        "api/sharing.py",
        "api/partner.py",
        "api/users.py",
        # M159 (see the docstring above): the household report scope.
        "services/reports.py",
    }
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative in allowed:
            continue
        if re.search(r"\bSharingLink\b", path.read_text(encoding="utf-8")):
            offenders.append(relative)
    assert offenders == [], f"SharingLink referenced outside the sharing modules: {offenders}"


def test_invite_rate_limit_and_pending_cap(client: TestClient) -> None:
    """docs/43 §11: 6th invite inside a minute → 429 (limiter ON, the
    test_import_dos.py pattern); a 6th outstanding pending → 400 cap."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    limit_per_minute = int(SHARING_INVITE_LIMIT.split("/")[0])
    assert limit_per_minute == 5

    limiter.reset()
    limiter.enabled = True
    try:
        attempts = []
        for index in range(limit_per_minute + 1):
            response = client.post(
                f"{SHARING}/invites",
                headers=_headers(owner_token),
                json={"email": f"rate-{index}@example.com"},
            )
            attempts.append((response.status_code, response.json()))
        assert all(status == 201 for status, _ in attempts[:limit_per_minute])
        last_status, last_body = attempts[-1]
        assert last_status == 429
        assert last_body == {"detail": "Too many attempts", "code": "rate_limited"}
    finally:
        limiter.enabled = False
        limiter.reset()

    # Cap surface, limiter off: 5 outstanding pending, then a 6th address → 400.
    owner2_token = _register(client, "cap-owner@example.com")["access_token"]
    for index in range(5):
        response = client.post(
            f"{SHARING}/invites",
            headers=_headers(owner2_token),
            json={"email": f"cap-{index}@example.com"},
        )
        assert response.status_code == 201, response.text
    capped = client.post(
        f"{SHARING}/invites",
        headers=_headers(owner2_token),
        json={"email": "cap-6@example.com"},
    )
    assert capped.status_code == 400
    assert capped.json() == {
        "detail": "At most 5 pending invites per account",
        "code": "sharing_invite_cap",
    }


def test_pre_handler_refusals_write_no_receipt(client: TestClient, session: Session) -> None:
    """docs/43 §3 (M92 F-M91-6): receipts are written at the HANDLER layer.

    The three pre-handler refusals — body validation `422`, rate limiter
    `429`, demo write guard `403` — are answered before a handler exists to
    attribute a row to, so none of them leaves a `sharing_audit` row.
    A handler-level refusal DOES: self-invite's `403 sharing_self_invite` is
    pinned by `test_sharing_audit_matches_the_mcp_audit_shape`.
    """
    owner_token = _register(client, OWNER_EMAIL)["access_token"]

    def codes() -> list[int]:
        rows = session.exec(select(SharingAudit)).all()
        return sorted(row.response_code for row in rows)

    # (a) Body validation — FastAPI answers 422 before the endpoint runs.
    bad = client.post(
        f"{SHARING}/invites",
        headers=_headers(owner_token),
        json={"email": "valid@example.com", "unexpected": 1},
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["code"] == "validation_error"
    assert codes() == []

    # (b) The rate limiter — its decorator answers 429 before the endpoint runs.
    limiter.reset()
    limiter.enabled = True
    try:
        statuses = [
            client.post(
                f"{SHARING}/invites",
                headers=_headers(owner_token),
                json={"email": f"pre-handler-{index}@example.com"},
            ).status_code
            for index in range(6)
        ]
    finally:
        limiter.enabled = False
        limiter.reset()
    assert statuses == [201, 201, 201, 201, 201, 429], statuses
    # Five invites reached the handler and were receipted; the 429 was not.
    assert codes() == [201, 201, 201, 201, 201]

    # (c) The demo write guard — a dependency, answered before the handler.
    demo = User(email=DEMO_EMAIL, hashed_password="not-a-real-hash")
    session.add(demo)
    session.commit()
    assert demo.id is not None
    demo_token = create_access_token(demo.id)
    blocked = client.post(
        f"{SHARING}/invites",
        headers=_headers(demo_token),
        json={"email": "someone@example.com"},
    )
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["code"] == "demo_account_restricted"
    # Still exactly the five handler-reached receipts: the 403 left no row.
    assert codes() == [201, 201, 201, 201, 201]


def test_single_use_token_and_address_privacy(client: TestClient) -> None:
    """Replay → 409 used; a token for ANOTHER address, presented by a third
    account, answers the IDENTICAL body as an unknown token; the invite list
    never carries token/accept_url."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    third_token = _register(client, THIRD_EMAIL)["access_token"]

    invite_partner = _invite(client, owner_token, PARTNER_EMAIL)
    invite_third = _invite(client, owner_token, THIRD_EMAIL)

    # Wrong account for this token == unknown token, byte-identical bodies.
    wrong = _accept(client, invite_partner["token"], third_token)
    unknown = _accept(client, "definitely-not-a-real-token", third_token)
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["code"] == "sharing_invite_invalid"

    # The right account accepts; the same token can never be used again.
    assert _accept(client, invite_partner["token"], partner_token).status_code == 200
    replay = _accept(client, invite_partner["token"], partner_token)
    assert replay.status_code == 409
    assert replay.json()["code"] == "sharing_invite_used"

    # The invite list carries no capability.
    listing = client.get(f"{SHARING}/invites", headers=_headers(owner_token))
    assert listing.status_code == 200
    for item in listing.json()["items"]:
        assert "token" not in item and "accept_url" not in item
    assert invite_partner["token"] not in listing.text
    assert invite_third["token"] not in listing.text

    # The owner's OTHER pending invite is NOT acceptable now that the owner
    # holds an active link — the create check (5) mirrored on accept.
    blocked_again = _accept(client, invite_third["token"], third_token)
    assert blocked_again.status_code == 409, blocked_again.text
    assert blocked_again.json()["code"] == "sharing_partner_exists"


def test_accept_race_is_decided_by_the_database(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two owners' invites, one accepter: exactly one 200, one
    409 sharing_partner_occupied, exactly one active row — and the loser is
    refused by the PARTIAL UNIQUE INDEX, not by an application pre-check
    (the pre-check is bypassed to reproduce the true interleaving)."""
    owner1_token = _register(client, "race-owner1@example.com")["access_token"]
    owner2_token = _register(client, "race-owner2@example.com")["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    partner_id = _user_id(client, partner_token)

    invite1 = _invite(client, owner1_token, PARTNER_EMAIL)
    invite2 = _invite(client, owner2_token, PARTNER_EMAIL)

    first = _accept(client, invite1["token"], partner_token)
    assert first.status_code == 200, first.text

    # (a) The index exists: a second active row for one partner is refused
    #     by the database itself, on this very session.
    clash = SharingLink(
        user_id=_user_id(client, owner2_token),
        partner_user_id=partner_id,
        email=PARTNER_EMAIL,
        token_hash="f" * 64,
        status=SharingLinkStatus.active,
        account_ids=[],
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    session.add(clash)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()

    # (b) Through HTTP, with the pre-check bypassed — both accepts "pass"
    #     their checks, so the insert race reaches the index and the loser
    #     gets the pinned 409, never a 500.
    monkeypatch.setattr(sharing_api, "_partner_occupied", lambda *a, **k: False)
    loser = _accept(client, invite2["token"], partner_token)
    monkeypatch.undo()
    assert loser.status_code == 409, loser.text
    assert loser.json()["code"] == "sharing_partner_occupied"

    # (c) The sequential path answers the same way, via the pre-check.
    sequential = _accept(client, invite2["token"], partner_token)
    assert sequential.status_code == 409
    assert sequential.json()["code"] == "sharing_partner_occupied"

    active = session.exec(
        select(SharingLink).where(
            SharingLink.partner_user_id == partner_id,
            SharingLink.status == SharingLinkStatus.active,
        )
    ).all()
    assert len(active) == 1, f"expected exactly one active row, got {len(active)}"
    # invite1's owner holds the active link; invite2's invite is untouched.
    assert active[0].user_id == _user_id(client, owner1_token)
    assert active[0].token_hash == sharing_api.hash_invite_token(invite1["token"])


def test_sharing_audit_matches_the_mcp_audit_shape(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Column-for-column parity with `mcp_audit` where the shapes overlap,
    refusals included, and best-effort: a forced receipt failure still
    returns the invite."""
    sharing_cols = {c.name: c for c in sa_inspect(SharingAudit).columns}
    mcp_cols = {c.name: c for c in sa_inspect(McpAudit).columns}
    shared = {"user_id", "args_hash", "response_code", "duration_ms", "created_at", "updated_at"}
    assert shared <= set(sharing_cols) and shared <= set(mcp_cols)
    for name in shared:
        left, right = sharing_cols[name].type, mcp_cols[name].type
        assert type(left) is type(right), f"{name}: {type(left)} != {type(right)}"
        assert getattr(left, "length", None) == getattr(right, "length", None), name
        assert getattr(left, "timezone", None) == getattr(right, "timezone", None), name
    # D7: the actor column is deliberately FK-less (and so is invite_id).
    assert sharing_cols["actor_user_id"].foreign_keys == set()
    assert sharing_cols["invite_id"].foreign_keys == set()

    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    # A REFUSAL still leaves a receipt: self-invite is refused by the handler.
    refused = client.post(
        f"{SHARING}/invites",
        headers=_headers(owner_token),
        json={"email": OWNER_EMAIL},
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "sharing_self_invite"
    receipts = session.exec(
        select(SharingAudit).where(SharingAudit.user_id == _user_id(client, owner_token))
    ).all()
    assert [(r.event, r.response_code) for r in receipts] == [("invite_create", 403)]
    assert re.fullmatch(r"[0-9a-f]{64}", receipts[0].args_hash)
    # The invite address is hashed, never stored: `sharing_audit` has no
    # column that could carry it (the shape is pinned, not just the content).
    audit_columns = {column.name for column in sa_inspect(SharingAudit).columns}
    assert "email" not in audit_columns and "token" not in audit_columns
    assert OWNER_EMAIL not in json.dumps([r.args_hash for r in receipts])

    # Best-effort: a receipt that cannot even be constructed must not fail
    # the user's invite (docs/43 §3).

    class _Boom:
        def __init__(self, *a, **k) -> None:
            raise RuntimeError("receipt sink unreachable")

    monkeypatch.setattr(sharing_api, "SharingAudit", _Boom)
    still_works = client.post(
        f"{SHARING}/invites",
        headers=_headers(owner_token),
        json={"email": "best-effort@example.com"},
    )
    assert still_works.status_code == 201, still_works.text
    monkeypatch.undo()
    # And the invite really exists afterwards.
    listing = client.get(f"{SHARING}/invites", headers=_headers(owner_token)).json()
    assert any(item["email"] == "best-effort@example.com" for item in listing["items"])


def test_export_registry_picks_up_both_tables(client: TestClient) -> None:
    """docs/43 §10: both tables ride SQLModel.metadata, so `GET /export/full`
    includes them with no registry edit, and neither carries a secret."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    account_a = _account(client, owner_token, "SHARED-CHEQUING")
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])

    response = client.get(f"{API}/export/full", headers=_headers(owner_token))
    assert response.status_code == 200
    snapshot = response.json()
    assert "sharing_links" in snapshot, "sharing_links missing from the export registry"
    assert "sharing_audit" in snapshot, "sharing_audit missing from the export registry"
    link_rows = snapshot["sharing_links"]
    assert len(link_rows) == 1
    # docs/43 §10 pins what ships: the plaintext address (the owner's own
    # record) and the sha256 capability digest; the RAW token and its
    # `accept_url` exist only in the 201 body and the email — never here.
    assert PARTNER_EMAIL in json.dumps(link_rows)
    assert "token_hash" in link_rows[0]
    assert len(link_rows[0]["token_hash"]) == 64
    assert "accept_url" not in response.text
    assert invite["token"] not in response.text


def test_dangling_account_ids_are_skipped_at_read(client: TestClient, session: Session) -> None:
    """I6 (docs/43 §3): ids validated at write time may dangle later — they
    are skipped at READ time, never staged into (the sync_agreements rule)."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    account_a = _account(client, owner_token, "SHARED-CHEQUING")
    account_b = _account(client, owner_token, "SOON-DELETED")
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a, account_b])
    assert _accept(client, invite["token"], partner_token).status_code == 200

    deleted = client.delete(f"{API}/accounts/{account_b}", headers=_headers(owner_token))
    assert deleted.status_code in (204, 200), deleted.text

    state = client.get(f"{PARTNER}/state", headers=_headers(partner_token)).json()
    assert state["account_ids"] == [account_a]
    accounts = client.get(f"{PARTNER}/accounts", headers=_headers(partner_token)).json()
    assert [item["id"] for item in accounts["items"]] == [account_a]


# --- 4. tenancy regression (docs/43 §Verification 2) ------------------------


def test_owner_list_endpoints_never_touch_the_sharing_tables(
    client: TestClient, session: Session
) -> None:
    """The pre-M90 byte-identical claim, pinned in-suite: with a live
    sharing link in the tenant, the owner's three list endpoints emit ZERO
    statements against the sharing tables — the sharing work added no
    predicate to any existing query (the full byte-comparison against the
    pre-M90 tree is re-run across worktrees at gate time)."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    account_a, _, _, _ = _seed_shared_and_hidden(client, owner_token)
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert _accept(client, invite["token"], partner_token).status_code == 200

    engine = get_engine()
    recorder = _SqlRecorder()
    event.listen(engine, "before_cursor_execute", recorder)
    try:
        for path in (f"{API}/transactions", f"{API}/accounts", f"{API}/budgets"):
            response = client.get(path, headers=_headers(owner_token))
            assert response.status_code == 200, response.text
    finally:
        event.remove(engine, "before_cursor_execute", recorder)

    assert len(recorder.statements) >= 3, "listener recorded too little SQL"
    leaking = [
        statement
        for statement, _ in recorder.statements
        if re.search(r"\b(?:FROM|JOIN) sharing_(?:links|audit)\b", statement)
    ]
    assert leaking == [], "owner list endpoints touched the sharing tables:\n" + "\n".join(leaking)


# --- 5. the partner's own leave route (M90 D6 follow-up, docs/43 §5) --------


def test_partner_can_leave_and_both_sides_render_the_ended_state(
    client: TestClient, session: Session
) -> None:
    """D6: consent withdrawal exists on the ACCEPT side too.

    `POST /sharing/leave` → 200 with the row ending in exactly the state
    `revoke` writes (`revoked` + `revoked_at`), so both sides render today's
    ended-link path with no second UI; the receipt carries `actor_user_id =
    partner` under the owner's tenant; the repeat is idempotent (M79,
    docs/39 §8 — `200` again, a second receipt, no 409); and the partner is
    NOT logged out by leaving (their own session keeps working)."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    owner_id = _user_id(client, owner_token)
    partner_id = _user_id(client, partner_token)
    account_a = _account(client, owner_token, "SHARED-CHEQUING")
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert _accept(client, invite["token"], partner_token).status_code == 200

    left = _leave(client, partner_token)
    assert left.status_code == 200, left.text
    body = left.json()
    assert body["status"] == "revoked"
    assert body["revoked_at"] is not None
    assert body["link_id"] == invite["id"]
    assert body["partner"]["user_id"] == partner_id
    assert body["account_ids"] == [account_a]

    # The row: the SAME terminal state `revoke` writes, partner seat intact.
    row = session.get(SharingLink, invite["id"])
    assert row is not None
    assert row.status is SharingLinkStatus.revoked
    assert row.revoked_at is not None
    assert row.partner_user_id == partner_id
    assert row.account_ids == [account_a]

    # The receipt: `leave`, actor = the partner, attributed to the owner.
    receipts = session.exec(select(SharingAudit).where(SharingAudit.user_id == owner_id)).all()
    leaves = [r for r in receipts if r.event == "leave"]
    assert [(r.response_code, r.actor_user_id, r.invite_id) for r in leaves] == [
        (200, partner_id, invite["id"])
    ]
    assert re.fullmatch(r"[0-9a-f]{64}", leaves[0].args_hash)

    # The owner's NEXT load reads the ended state (docs/43 §13 renders
    # `revoked` as "Nobody has access").
    owner_state = client.get(SHARING, headers=_headers(owner_token))
    assert owner_state.status_code == 200, owner_state.text
    assert owner_state.json()["status"] == "revoked"

    # The partner's next projection read: `GET /partner/state` answers the
    # M92 `200 {linked: false}` (data, not an error — that is what makes the
    # Settings row render nothing), a DATA route answers the uniform 401, and
    # their own session still works — leaving is not being logged out.
    cut = client.get(f"{PARTNER}/state", headers=_headers(partner_token))
    assert cut.status_code == 200, cut.text
    assert cut.json() == {"linked": False}
    data_cut = client.get(f"{PARTNER}/accounts", headers=_headers(partner_token))
    assert data_cut.status_code == 401
    assert data_cut.json() == {
        "detail": "No shared household is linked to this account",
        "code": "sharing_not_linked",
    }
    still_mine = client.get(f"{API}/users/me", headers=_headers(partner_token))
    assert still_mine.status_code == 200 and still_mine.json()["id"] == partner_id

    # Idempotent: 200 → 200, each with its own receipt (M79 semantics).
    again = _leave(client, partner_token)
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "revoked"
    after = session.exec(select(SharingAudit).where(SharingAudit.user_id == owner_id)).all()
    assert sum(1 for r in after if r.event == "leave") == 2


def test_leave_by_a_non_partner_answers_the_uniform_not_linked_envelope(
    client: TestClient, session: Session
) -> None:
    """A caller with NO partner row answers `404 sharing_not_linked` whose
    BODY is byte-identical to the partner read's envelope — one uniform
    answer for never-linked / already-removed, so the response never
    distinguishes "you were revoked" from "you never had access". The status
    is 404, not 401: a 401 from a mutating path would run the client's
    refresh → /login dance on a perfectly valid session (docs/43 §7). A
    refusal with no row to attribute it to writes NO receipt."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    third_token = _register(client, THIRD_EMAIL)["access_token"]
    account_a = _account(client, owner_token, "SHARED-CHEQUING")
    pending_token = _register(client, "pending-leave@example.com")["access_token"]
    # Two PENDING invites exist before either is accepted — create refuses a
    # new row while one is `active` (check 5), so the never-accepted addressee
    # has to be invited before the household goes live.
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    pending_invite = _invite(client, owner_token, "pending-leave@example.com", [account_a])
    assert pending_invite["status"] == "pending"
    assert _accept(client, invite["token"], partner_token).status_code == 200

    # The reference envelope, captured from a partner DATA read itself —
    # `/state` deliberately does NOT produce it any more (M92: `200 {linked:
    # false}`), so the byte-identity is pinned against the route that still
    # owns the uniform 401.
    reference = client.get(f"{PARTNER}/accounts", headers=_headers(third_token))
    assert reference.status_code == 401, reference.text
    assert reference.json()["code"] == "sharing_not_linked"

    def receipts() -> list[SharingAudit]:
        return list(
            session.exec(
                select(SharingAudit).where(SharingAudit.user_id == _user_id(client, owner_token))
            ).all()
        )

    before = [(r.event, r.response_code) for r in receipts()]

    # (a) a stranger — never linked to this household.
    stranger = _leave(client, third_token)
    assert stranger.status_code == 404, stranger.text
    assert stranger.content == reference.content  # byte-identical body
    assert stranger.json() == {
        "detail": "No shared household is linked to this account",
        "code": "sharing_not_linked",
    }

    # (b) the invite's own addressee, never accepted: an invite is not a
    #     partnership — `partner_user_id` is still NULL (I4).
    never_accepted = _leave(client, pending_token)
    assert never_accepted.status_code == 404
    assert never_accepted.content == reference.content

    # Neither refusal left a receipt (no owner to attribute it to), and the
    # live household is untouched.
    assert [(r.event, r.response_code) for r in receipts()] == before
    still_linked = client.get(f"{PARTNER}/state", headers=_headers(partner_token))
    assert still_linked.status_code == 200, still_linked.text
    row = session.get(SharingLink, invite["id"])
    assert row is not None and row.status is SharingLinkStatus.active


def test_leave_mutates_only_the_partner_row_and_reads_distinctly_from_revoke(
    client: TestClient, session: Session
) -> None:
    """Two things the card pins, in one walk:

    * `leave` mutates ONLY the row where `partner_user_id = me` — a user who
      is simultaneously an OWNER keeps their own active link, and the third
      tenant riding it re-verifies unaffected (AGENTS §2 / docs/43 §1);
    * `leave` ≠ `revoke`: both receipts end up on the owner's log with
      DISTINCT actors (`leave` = partner, `revoke` = owner), while both rows
      finish in the same terminal state so the UI needs no second path."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    third_token = _register(client, THIRD_EMAIL)["access_token"]
    owner_id = _user_id(client, owner_token)
    partner_id = _user_id(client, partner_token)
    account_a = _account(client, owner_token, "SHARED-CHEQUING")

    # A → P (P is the partner seat).
    invite_ap = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert _accept(client, invite_ap["token"], partner_token).status_code == 200
    # P → T (P is the OWNER seat at the same time — two different rows).
    invite_pt = _invite(client, partner_token, THIRD_EMAIL)
    assert _accept(client, invite_pt["token"], third_token).status_code == 200

    left = _leave(client, partner_token)
    assert left.status_code == 200, left.text

    # Only the partner row moved; P's own household is untouched.
    ap_row = session.get(SharingLink, invite_ap["id"])
    pt_row = session.get(SharingLink, invite_pt["id"])
    assert ap_row is not None and ap_row.status is SharingLinkStatus.revoked
    assert ap_row.revoked_at is not None
    assert pt_row is not None and pt_row.status is SharingLinkStatus.active
    assert pt_row.revoked_at is None
    assert pt_row.user_id == partner_id and pt_row.partner_user_id == _user_id(client, third_token)

    # P still owns an active household…
    assert client.get(SHARING, headers=_headers(partner_token)).json()["status"] == "active"
    # …and the OTHER tenant re-verifies unaffected, on the very next request.
    reverify = client.get(f"{PARTNER}/state", headers=_headers(third_token))
    assert reverify.status_code == 200, reverify.text
    assert reverify.json()["owner"]["user_id"] == partner_id

    # The mirror act on a fresh relationship: A re-invites P, then REVOKE.
    invite_again = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert _accept(client, invite_again["token"], partner_token).status_code == 200
    revoked = client.post(f"{SHARING}/revoke", headers=_headers(owner_token))
    assert revoked.status_code == 200, revoked.text

    # Both receipts on the owner's log, distinct actors, distinct events.
    rows = session.exec(select(SharingAudit).where(SharingAudit.user_id == owner_id)).all()
    leaves = [r for r in rows if r.event == "leave"]
    revokes = [r for r in rows if r.event == "revoke"]
    assert [(r.response_code, r.actor_user_id) for r in leaves] == [(200, partner_id)]
    assert [(r.response_code, r.actor_user_id) for r in revokes] == [(200, owner_id)]
    assert leaves[0].id != revokes[0].id
    assert leaves[0].invite_id == invite_ap["id"]
    assert revokes[0].invite_id == invite_again["id"]
    # The row shapes differ exactly where the actor does: who ended it.
    left_row = session.get(SharingLink, invite_ap["id"])
    revoked_row = session.get(SharingLink, invite_again["id"])
    assert left_row is not None and revoked_row is not None
    assert left_row.status is revoked_row.status is SharingLinkStatus.revoked
    assert left_row.revoked_at is not None and revoked_row.revoked_at is not None


def test_leave_shares_the_mutation_rate_limiter(client: TestClient) -> None:
    """Rate parity with `POST /sharing/invites`'s peers: the same 10/min
    `SHARING_MUTATE_LIMIT` bucket the owner's mutations use — the partner's
    first mutation is not a fresh avenue for request floods."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    account_a = _account(client, owner_token, "SHARED-CHEQUING")
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert _accept(client, invite["token"], partner_token).status_code == 200

    limit_per_minute = int(SHARING_MUTATE_LIMIT.split("/")[0])
    assert limit_per_minute == 10

    limiter.reset()
    limiter.enabled = True
    try:
        attempts = [_leave(client, partner_token) for _ in range(limit_per_minute + 1)]
        assert [r.status_code for r in attempts[:limit_per_minute]] == [200] * limit_per_minute
        last = attempts[-1]
        assert last.status_code == 429, last.text
        assert last.json() == {"detail": "Too many attempts", "code": "rate_limited"}
    finally:
        limiter.enabled = False
        limiter.reset()


# --- 6. state-based row targeting (M98 F-1 / F-2 / F-6) ----------------------
#
# The M94 review's escalations, all three of them the same bug: an owner- or
# partner-act resolved "the sharing row" NEWEST-BY-ID, and a pending invite
# created before the accept keeps a higher `id` forever — so it hid the live
# partner from `GET /sharing`, absorbed `PATCH /scope`, made `revoke` refuse
# 409 permanently (even after that row was cancelled) and let `leave` answer
# 200 as a no-op while the link survived. Rows are addressed by STATE now:
# `active` first, a pending invite only by its OWN id.


def _not_linked_reference(client: TestClient, token: str) -> bytes:
    """The pinned byte-identity reference (docs/43 §7): a partner DATA read's
    401 body — the envelope every "no link FOR YOU" answer must join."""
    response = client.get(f"{PARTNER}/accounts", headers=_headers(token))
    assert response.status_code == 401, response.text
    assert response.json() == {
        "detail": "No shared household is linked to this account",
        "code": "sharing_not_linked",
    }
    return response.content


def _receipts_for(session: Session, user_id: int, event: str) -> list[SharingAudit]:
    return list(
        session.exec(
            select(SharingAudit)
            .where(SharingAudit.user_id == user_id, SharingAudit.event == event)
            .order_by(SharingAudit.id)
        ).all()
    )


def test_owner_state_scope_and_revoke_target_the_active_row_not_the_newest(
    client: TestClient, session: Session
) -> None:
    """F-1 (P1): two invites created before the accept, the OLDER one
    accepted → every owner act must land on that active row, never on the
    newer pending one (the reviewer's live repro, 3/3)."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    owner_id = _user_id(client, owner_token)
    partner_id = _user_id(client, partner_token)
    account_a, account_b, _, _ = _seed_shared_and_hidden(client, owner_token)

    older = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    newer = _invite(client, owner_token, "second-choice@example.com", [account_b])
    assert newer["id"] > older["id"], "the fixture needs the pending row to sort NEWER"
    assert _accept(client, older["token"], partner_token).status_code == 200

    # (1) `GET /sharing` reads the LIVE partner, not the stray invite — it
    #     used to answer {"status": "pending", "link_id": newer} and the
    #     partner vanished from the owner's canonical state view.
    state = client.get(SHARING, headers=_headers(owner_token))
    assert state.status_code == 200, state.text
    assert state.json()["status"] == "active"
    assert state.json()["link_id"] == older["id"]
    assert state.json()["partner"]["user_id"] == partner_id

    # (2) A scope PATCH lands on the ACTIVE row — asserted through the
    #     partner's own view, because the 200 alone used to be a lie.
    scoped = client.patch(
        f"{SHARING}/scope",
        headers=_headers(owner_token),
        json={"account_ids": [account_b]},
    )
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["link_id"] == older["id"]
    assert scoped.json()["account_ids"] == [account_b]
    partner_view = client.get(f"{PARTNER}/accounts", headers=_headers(partner_token))
    assert partner_view.status_code == 200, partner_view.text
    assert [row["id"] for row in partner_view.json()["items"]] == [account_b]

    # (3) Revoke succeeds — NOT the permanent 409 — and it ends the LIVE
    #     link: the partner's own read flips on the very next request.
    revoked = client.post(f"{SHARING}/revoke", headers=_headers(owner_token))
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["link_id"] == older["id"]
    assert revoked.json()["revoked_at"] is not None
    assert client.get(f"{PARTNER}/state", headers=_headers(partner_token)).json() == {
        "linked": False
    }

    # (4) M79 idempotency is untouched: the repeat answers 200 on the SAME
    #     row, not 404 and not the stray invite.
    again = client.post(f"{SHARING}/revoke", headers=_headers(owner_token))
    assert again.status_code == 200, again.text
    assert again.json()["link_id"] == older["id"]

    # (5) The stray pending invite stays addressable by ITS OWN id — the
    #     only way a pending row is ever targeted (docs/43 §5).
    cancelled = client.delete(f"{SHARING}/invites/{newer['id']}", headers=_headers(owner_token))
    assert cancelled.status_code == 204, cancelled.text

    rows = session.exec(
        select(SharingLink).where(SharingLink.user_id == owner_id).order_by(SharingLink.id)
    ).all()
    assert [(row.id, row.status) for row in rows] == [
        (older["id"], SharingLinkStatus.revoked),
        (newer["id"], SharingLinkStatus.cancelled),
    ]
    # Every revoke receipt names the row that was actually revoked.
    assert [row.invite_id for row in _receipts_for(session, owner_id, "revoke")] == [
        older["id"],
        older["id"],
    ]


def test_cancel_pending_then_revoke_active_both_succeed_in_sequence(
    client: TestClient, session: Session
) -> None:
    """F-1 check 6: cancelling the newer row used to leave `revoke` at 409
    FOREVER — the cancelled row was still the newest, so there was no API
    path to withdraw consent at all. Cancel is by invite id; revoke is by
    state, so the sequence works in either order."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    account_a = _account(client, owner_token, "SHARED-CHEQUING")
    live = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    stray = _invite(client, owner_token, "stray@example.com", [account_a])
    assert stray["id"] > live["id"]
    assert _accept(client, live["token"], partner_token).status_code == 200

    cancelled = client.delete(f"{SHARING}/invites/{stray['id']}", headers=_headers(owner_token))
    assert cancelled.status_code == 204, cancelled.text

    # The state view still reads the ACTIVE link after the cancel (it used
    # to read the cancelled row), so the UI keeps showing the partner.
    state = client.get(SHARING, headers=_headers(owner_token))
    assert state.status_code == 200, state.text
    assert state.json()["status"] == "active"
    assert state.json()["link_id"] == live["id"]

    revoked = client.post(f"{SHARING}/revoke", headers=_headers(owner_token))
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["link_id"] == live["id"]
    assert client.get(f"{PARTNER}/state", headers=_headers(partner_token)).json() == {
        "linked": False
    }
    row = session.get(SharingLink, live["id"])
    assert row is not None and row.status is SharingLinkStatus.revoked
    stray_row = session.get(SharingLink, stray["id"])
    assert stray_row is not None and stray_row.status is SharingLinkStatus.cancelled


def test_owner_scope_and_revoke_without_a_live_link_join_the_not_linked_family(
    client: TestClient, session: Session
) -> None:
    """M98 ruling 2: no active row → the byte-identical `404
    sharing_not_linked` envelope. Never a `200` no-op, and never the `409`
    a cancel could not clear (F-1's permanent refusal)."""
    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    stranger_token = _register(client, THIRD_EMAIL)["access_token"]
    owner_id = _user_id(client, owner_token)
    reference = _not_linked_reference(client, partner_token)

    # A pending invite is not a household: nothing to scope, nothing to revoke.
    _invite(client, owner_token, PARTNER_EMAIL)
    for refusal in (
        client.patch(f"{SHARING}/scope", headers=_headers(owner_token), json={"account_ids": []}),
        client.post(f"{SHARING}/revoke", headers=_headers(owner_token)),
    ):
        assert refusal.status_code == 404, refusal.text
        assert refusal.content == reference

    # An owner with NO row at all answers the same bytes (docs/43 §7 — the
    # code that used to be `404 sharing_missing` on these two routes).
    stranger_scope = client.patch(
        f"{SHARING}/scope", headers=_headers(stranger_token), json={"account_ids": []}
    )
    stranger_revoke = client.post(f"{SHARING}/revoke", headers=_headers(stranger_token))
    for refusal in (stranger_scope, stranger_revoke):
        assert refusal.status_code == 404, refusal.text
        assert refusal.content == reference

    # Handler-level refusals still write their receipt (docs/43 §3): the
    # owner's two 404s, each with no link id to name.
    receipts = _receipts_for(session, owner_id, "scope_update") + _receipts_for(
        session, owner_id, "revoke"
    )
    assert [(row.response_code, row.invite_id) for row in receipts] == [(404, None), (404, None)]


def test_partner_leave_targets_the_active_row_not_the_newest(
    client: TestClient, session: Session
) -> None:
    """F-2 (P2): a partner row that sorts NEWER than the live one used to win
    newest-first, so `leave` answered 200 as a no-op, the active link
    survived, `/partner/state` stayed `linked: true` and every receipt named
    the stale row. Leave resolves `partner_user_id = me AND status='active'`
    — the row that GRANTS access."""
    owner_a_token = _register(client, OWNER_EMAIL)["access_token"]
    owner_b_token = _register(client, "other-owner@example.com")["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    owner_a_id = _user_id(client, owner_a_token)
    owner_b_id = _user_id(client, owner_b_token)

    older = _invite(client, owner_b_token, PARTNER_EMAIL)  # lower id, accepted SECOND
    newer = _invite(client, owner_a_token, PARTNER_EMAIL)  # higher id, accepted FIRST
    assert newer["id"] > older["id"]

    first = _accept(client, newer["token"], partner_token)
    assert first.status_code == 200, first.text
    first_leave = _leave(client, partner_token)
    assert first_leave.status_code == 200, first_leave.text
    assert first_leave.json()["link_id"] == newer["id"]

    # The reviewer's setup: accept the EARLIER-created, still-pending invite —
    # now the LIVE row is the older one and the newest row is terminal.
    assert _accept(client, older["token"], partner_token).status_code == 200
    linked = client.get(f"{PARTNER}/state", headers=_headers(partner_token))
    assert linked.status_code == 200 and linked.json()["linked"] is True

    left = _leave(client, partner_token)
    assert left.status_code == 200, left.text
    assert left.json()["status"] == "revoked"
    assert left.json()["link_id"] == older["id"], "leave must name the row it actually ended"
    assert left.json()["revoked_at"] is not None

    # The live link is really gone — the old build answered 200 while
    # `linked: true` persisted — and the row the partner read resolves is
    # the one that was written.
    assert client.get(f"{PARTNER}/state", headers=_headers(partner_token)).json() == {
        "linked": False
    }
    row = session.get(SharingLink, older["id"])
    assert row is not None and row.status is SharingLinkStatus.revoked
    assert row.revoked_at is not None

    # Receipts land on the OWNING tenant of the row they concern: `newer`'s
    # leave under owner A, `older`'s under owner B (zero before this act).
    assert [row.invite_id for row in _receipts_for(session, owner_a_id, "leave")] == [newer["id"]]
    assert [row.invite_id for row in _receipts_for(session, owner_b_id, "leave")] == [older["id"]]

    # M79 repeat: 200 again, on the partnership that ended LAST — never on
    # the stale row that ended earlier.
    repeat = _leave(client, partner_token)
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["link_id"] == older["id"]
    assert [row.invite_id for row in _receipts_for(session, owner_b_id, "leave")] == [
        older["id"],
        older["id"],
    ]


def test_owner_side_accept_race_loser_gets_the_owner_attributed_code(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-6: the loser of an OWNER-side accept race — two invites of ONE
    owner, accepted concurrently — used to be handed the partner-side code
    `sharing_partner_occupied`, blaming the wrong party. The index that
    refuses it is `uq_sharing_links_active_owner`, so the answer is the
    owner-attributed `sharing_invite_used`; the receipt still names the
    invitee who acted (D7), and the PARTNER-side arm keeps its partner code
    (`test_accept_race_is_decided_by_the_database`)."""
    owner_token = _register(client, "race-owner@example.com")["access_token"]
    winner_token = _register(client, "race-winner@example.com")["access_token"]
    loser_token = _register(client, "race-loser@example.com")["access_token"]
    owner_id = _user_id(client, owner_token)
    winner_id = _user_id(client, winner_token)
    loser_id = _user_id(client, loser_token)

    winner_invite = _invite(client, owner_token, "race-winner@example.com")
    loser_invite = _invite(client, owner_token, "race-loser@example.com")
    assert loser_invite["id"] > winner_invite["id"]

    # The winner takes the seat first (through HTTP, no pre-check bypassed).
    accepted = _accept(client, winner_invite["token"], winner_token)
    assert accepted.status_code == 200, accepted.text

    # (a) The OWNER-side index exists on this very session: a second active
    #     row for one owner is refused by the database itself.
    clash = SharingLink(
        user_id=owner_id,
        partner_user_id=loser_id,
        email="race-loser@example.com",
        token_hash="e" * 64,
        status=SharingLinkStatus.active,
        account_ids=[],
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    session.add(clash)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()

    # (b) Through HTTP with the OWNER pre-check bypassed — both accepts
    #     "pass" their checks, so the insert race reaches the index and the
    #     loser gets the OWNER-attributed pinned code: never a 500, never
    #     `sharing_partner_occupied`.
    monkeypatch.setattr(sharing_api, "_owner_has_active", lambda *a, **k: False)
    loser = _accept(client, loser_invite["token"], loser_token)
    monkeypatch.undo()
    assert loser.status_code == 409, loser.text
    assert loser.json()["code"] == "sharing_invite_used"
    assert loser.json()["detail"] == "This invitation has already been used"

    # The receipt: the losing attempt, under the OWNER's tenant, actor = the
    # invitee who acted, response 409, naming the invite it addressed.
    assert [
        (row.actor_user_id, row.invite_id, row.response_code)
        for row in _receipts_for(session, owner_id, "invite_accept")
    ] == [
        (winner_id, winner_invite["id"], 200),
        (loser_id, loser_invite["id"], 409),
    ]

    # Exactly one active row — the index's own verdict, unaffected.
    active = session.exec(
        select(SharingLink).where(
            SharingLink.user_id == owner_id,
            SharingLink.status == SharingLinkStatus.active,
        )
    ).all()
    assert [row.id for row in active] == [winner_invite["id"]]

    # (c) The sequential arm of the same condition stays owner-attributed
    #     (create check (5) mirrored) — the partner-side code never appears
    #     on an owner-side conflict.
    sequential = _accept(client, loser_invite["token"], loser_token)
    assert sequential.status_code == 409, sequential.text
    assert sequential.json()["code"] == "sharing_partner_exists"


def test_partner_carried_in_is_money_and_follows_the_privacy_mask(
    client: TestClient, session: Session
) -> None:
    """M110 / docs/43 §9 — the rollover's `carried_in` on the PARTNER seam.

    It rides ALONGSIDE the definition as its own field (visible wherever a
    budget definition is visible), it is produced by the same one producer the
    owner's route uses, and it is MONEY: under the owner's privacy mode it
    serializes to JSON `null` exactly like the target `amount` — the M93
    honesty pattern extended, byte-scan included.
    """
    from datetime import date as _date

    from app.models.budgets import Budget as _Budget
    from app.services.txn_filters import today_et

    owner_token = _register(client, OWNER_EMAIL)["access_token"]
    partner_token = _register(client, PARTNER_EMAIL)["access_token"]
    account_a, _, _, _ = _seed_shared_and_hidden(client, owner_token)

    category = client.post(
        f"{API}/categories", headers=_headers(owner_token), json={"name": "CARRY-GROCERIES"}
    )
    assert category.status_code == 201, category.text
    category_id = category.json()["id"]
    budget = client.post(
        f"{API}/budgets",
        headers=_headers(owner_token),
        json={
            "category_id": category_id,
            "amount": 500.0,
            "period": "monthly",
            "carry": "forward",
        },
    )
    assert budget.status_code == 201, budget.text
    budget_id = budget.json()["id"]

    today: _date = today_et()
    source_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    # In force before the source month (docs/09), else nothing may carry.
    stored = session.get(_Budget, budget_id)
    assert stored is not None
    stored.created_at = datetime.combine(
        source_start - timedelta(days=1), datetime.min.time(), tzinfo=UTC
    )
    session.add(stored)
    session.commit()

    # $500 budgeted, $537.50 spent in the source month -> a $37.50 DEBT to carry.
    _tx(
        client,
        owner_token,
        account_a,
        "-537.50",
        "CARRY-SOURCE-SPEND",
        extra={"date": source_start.replace(day=10).isoformat(), "category_id": category_id},
    )

    _set_privacy(client, owner_token, True)
    invite = _invite(client, owner_token, PARTNER_EMAIL, [account_a])
    assert _accept(client, invite["token"], partner_token).status_code == 200

    # Privacy ON: the row, its flag and its identity survive; both figures do not.
    sweep = _sweep(client, partner_token)
    masked = [row for row in sweep["budgets"].json() if row["id"] == budget_id]  # type: ignore[union-attr]
    assert len(masked) == 1, sweep["budgets"].text  # type: ignore[union-attr]
    assert masked[0]["carry"] == "forward"
    assert masked[0]["amount"] is None
    assert masked[0]["carried_in"] is None
    assert "500.00" not in sweep["budgets"].text  # type: ignore[union-attr]
    assert "37.50" not in sweep["budgets"].text  # type: ignore[union-attr]

    # Privacy OFF: the frozen snapshot comes back — same producer, same number.
    _set_privacy(client, owner_token, False)
    open_budgets = client.get(f"{PARTNER}/budgets", headers=_headers(partner_token))
    assert open_budgets.status_code == 200, open_budgets.text
    visible = [row for row in open_budgets.json() if row["id"] == budget_id]
    assert visible[0]["amount"] == "500.00"
    assert visible[0]["carried_in"] == "-37.50"

    own = client.get(f"{API}/budgets", headers=_headers(owner_token))
    assert own.status_code == 200, own.text
    assert [row["carried_in"] for row in own.json() if row["id"] == budget_id] == ["-37.50"]

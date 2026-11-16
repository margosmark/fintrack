"""M107 — split transactions: parent/child ledger rows, two population rules, merge-back.

The 22 backend names below are the contract (docs/44 §"Named tests"), M90/M93
style: the test NAMES are pinned by the spec, so a future change that renames
one has to explain itself in the same commit.

Specs pinned here: docs/44 (the whole contract), plus the reconciliation rows
docs/02 §transactions, docs/08, docs/09, docs/03, docs/11, docs/12, docs/19,
docs/33, docs/34, docs/38, docs/43 §6.

Three properties are asserted over and over on purpose:

* **a split moves no money** — native AND CAD totals are byte-identical
  before/after, in every consumer (T8/T10/T11);
* **the two population rules never leak into each other** — LEDGER counts
  children and drops parents, IDENTITY sees parents and never a child
  (T12-T17);
* **merge is an exact inverse** — the parent's pre-split row comes back
  byte-identical, because nothing about it was ever written (T9).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session
from sqlmodel import func, select

from app.api.deps import get_engine
from app.importers.base import RawTransaction, build_dedup_hash
from app.models.reconciliation import ReconciliationItem
from app.models.trades import Trade
from app.models.transaction_events import TransactionEvent
from app.models.transaction_tags import TransactionTag
from app.models.transactions import Transaction
from app.models.users import User
from app.services.import_dedup import RowKey, find_near_duplicates
from tests.test_household_sharing import _accept, _invite

API = "/api/v1"
PASSWORD = "split-pass-123"
SPLIT_URL = f"{API}/transactions/{{transaction_id}}/split"
EXPORT_FULL = f"{API}/export/full"
RESTORE_URL = f"{API}/backup/restore"

# Columns compared by the exact-inverse test: every persisted field except the
# three the DB owns (`id`, the two timestamps) and the two the split itself
# is allowed to flip (`is_split`, `split_checksum`).
INVERSE_FIELDS = [
    name
    for name in Transaction.__table__.columns.keys()
    if name not in {"id", "created_at", "updated_at", "is_split", "split_checksum"}
]


# --- helpers -----------------------------------------------------------------


def _register(client: TestClient) -> tuple[dict[str, str], str, str]:
    """(headers, email, bearer token) — a fresh tenant per test."""
    email = f"split-{uuid.uuid4().hex[:12]}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    body = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert body.status_code == 200, body.text
    token = body.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, email, token


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def _account(client: TestClient, headers: dict[str, str], name: str, type_: str) -> int:
    response = client.post(
        f"{API}/accounts",
        headers=headers,
        json={"name": name, "type": type_, "currency": "CAD"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _category(client: TestClient, headers: dict[str, str], name: str | None = None) -> int:
    name = name or _uniq("cat")
    response = client.post(f"{API}/categories", headers=headers, json={"name": name})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _tag(client: TestClient, headers: dict[str, str]) -> int:
    response = client.post(f"{API}/tags", headers=headers, json={"name": _uniq("tag")})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _tx(
    client: TestClient,
    headers: dict[str, str],
    account_id: int,
    amount: str,
    description: str,
    *,
    day: str = "2026-06-15",
    **extra: Any,
) -> dict:
    payload: dict[str, Any] = {
        "account_id": account_id,
        "date": day,
        "raw_description": description,
        "amount_native": amount,
        "currency": "CAD",
    }
    payload.update(extra)
    response = client.post(f"{API}/transactions", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _split(
    client: TestClient,
    headers: dict[str, str],
    transaction_id: int,
    children: list[dict] | None = None,
    method: str = "POST",
):
    url = f"{API}/transactions/{transaction_id}/split"
    if method == "GET":
        return client.get(url, headers=headers)
    if method == "DELETE":
        return client.delete(url, headers=headers)
    body = {"children": children or []}
    if method == "PUT":
        return client.put(url, headers=headers, json=body)
    return client.post(url, headers=headers, json=body)


def _legs(*amounts: str) -> list[dict]:
    return [{"amount_native": amount} for amount in amounts]


def _code(response) -> str:
    return response.json().get("code", "")


def _reload(session: Session, transaction_id: int) -> Transaction:
    row = session.get(Transaction, transaction_id)
    assert row is not None, f"transaction {transaction_id} vanished"
    session.refresh(row)
    return row


def _children_of(session: Session, parent_id: int) -> list[Transaction]:
    rows = session.exec(
        select(Transaction).where(Transaction.parent_split_id == parent_id).order_by(Transaction.id)
    ).all()
    return list(rows)


def _count_children(session: Session, parent_id: int) -> int:
    return int(
        session.exec(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.parent_split_id == parent_id)
        ).one()
    )


def _user(session: Session, email: str) -> User:
    return session.exec(select(User).where(User.email == email)).one()


def _seed(
    session: Session,
    *,
    user_id: int,
    account_id: int,
    amount: str,
    description: str,
    day: date = date(2026, 6, 15),
    cad_amount: str | None = ...,
    category_id: int | None = None,
) -> Transaction:
    """Row straight into the table — for the cases a handler refuses to make
    (a zero-amount parent, a CAD mirror that is not `amount x 1`).

    `cad_amount=None` seeds an UNRESOLVED CAD mirror; the default (`...`)
    mirrors the native amount the way a CAD-denominated row normally lands.
    """
    value = Decimal(amount)
    resolved = value if cad_amount is ... else (None if cad_amount is None else Decimal(cad_amount))
    row = Transaction(
        user_id=user_id,
        account_id=account_id,
        date=day,
        raw_description=description,
        normalized_payee=description,
        amount_native=value,
        currency="CAD",
        cad_amount=resolved,
        category_id=category_id,
        dedup_hash=build_dedup_hash(
            account_id,
            RawTransaction(date=day, amount_native=value, raw_description=description),
        ),
    )
    session.add(row)
    session.flush()
    assert row.id is not None
    return row


# `credits_positive=True` is the file convention the app already uses: debits
# negative, credits positive, so no sign is inverted (csv_profile.py:262).
CSV_PROFILE = {
    "columns": {"date": "Date", "amount_native": "Amount", "raw_description": "Description"},
    "date_format": "%Y-%m-%d",
    "credits_positive": True,
}


def _upload_csv(
    client: TestClient, headers: dict[str, str], account_id: int, csv: str, name: str = "s.csv"
) -> dict:
    response = client.post(
        f"{API}/imports/upload",
        headers=headers,
        data={"account_id": str(account_id), "profile_json": json.dumps(CSV_PROFILE)},
        files={"file": (name, csv.encode(), "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- §3 money math -----------------------------------------------------------


def test_split_children_sum_exactly_to_the_parent(client: TestClient, session: Session) -> None:
    """docs/44 D3 — 3 legs on `-187.42`: exact sum, one checksum, `sum_ok`."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    groceries = _category(client, headers)
    parent = _tx(client, headers, account, "-187.42", "COSTCO #123")

    response = _split(
        client,
        headers,
        parent["id"],
        [
            {"amount_native": "-120.00", "category_id": groceries},
            {"amount_native": "-40.00"},
            {"amount_native": "-27.42"},
        ],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"parent", "children", "split_checksum", "sum_ok"}

    assert body["sum_ok"] is True
    assert body["split_checksum"] == "-187.42"
    assert len(body["children"]) == 3
    total = sum((Decimal(child["amount_native"]) for child in body["children"]), Decimal("0"))
    assert total == Decimal("-187.42")

    assert body["parent"]["is_split"] is True
    assert body["parent"]["id"] == parent["id"]
    assert body["parent"]["child_count"] == 3

    row = _reload(session, parent["id"])
    assert row.is_split is True
    assert row.split_checksum == row.amount_native == Decimal("-187.42")

    drilldown = _split(client, headers, parent["id"], method="GET")
    assert drilldown.status_code == 200, drilldown.text
    assert drilldown.json()["sum_ok"] is True
    assert [child["id"] for child in drilldown.json()["children"]] == [
        child["id"] for child in body["children"]
    ], "GET returns children ordered by id ASC (entry order)"


def test_split_sum_mismatch_is_refused_and_writes_nothing(
    client: TestClient, session: Session
) -> None:
    """docs/44 §3.1 — 422 with the residual in `detail`, ZERO rows written."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    parent = _tx(client, headers, account, "-187.42", "COSTCO #123")
    before = _reload(session, parent["id"])

    response = _split(client, headers, parent["id"], _legs("-120.00", "-40.00", "-17.42"))
    assert response.status_code == 422, response.text
    assert _code(response) == "split_sum_mismatch"
    detail = response.json()["detail"]
    assert "Children must sum to -187.42" in detail
    assert "they sum to -177.42" in detail
    assert "(residual -10.00)" in detail

    assert _count_children(session, parent["id"]) == 0
    after = _reload(session, parent["id"])
    assert after.is_split is False and after.split_checksum is None
    assert after.amount_native == before.amount_native
    assert after.category_id == before.category_id
    assert after.dedup_hash == before.dedup_hash


def test_split_leg_sign_zero_and_count_rules(client: TestClient, session: Session) -> None:
    """docs/44 §3.2 + §4.2 steps 8-10, one case per code."""
    headers, email, _ = _register(client)
    user_id = _user(session, email).id
    assert user_id is not None
    account = _account(client, headers, _uniq("acct"), "chequing")
    parent = _tx(client, headers, account, "-187.42", "COSTCO #123")

    wrong_sign = _split(client, headers, parent["id"], _legs("-120.00", "+67.42"))
    assert wrong_sign.status_code == 422 and _code(wrong_sign) == "split_leg_sign"

    zero_leg = _split(client, headers, parent["id"], _legs("0.00", "-187.42"))
    assert zero_leg.status_code == 422 and _code(zero_leg) == "split_leg_zero"

    too_few = _split(client, headers, parent["id"], _legs("-187.42"))
    assert too_few.status_code == 422 and _code(too_few) == "split_too_few_legs"

    too_many = _split(client, headers, parent["id"], _legs(*(["-10.00"] * 21)))
    assert too_many.status_code == 422 and _code(too_many) == "split_too_many_legs"

    # 20 is the documented ceiling: it passes the count rule and is refused
    # for the SUM instead — proving step 8 does not fire at the boundary.
    at_the_ceiling = _split(client, headers, parent["id"], _legs(*(["-10.00"] * 20)))
    assert at_the_ceiling.status_code == 422, at_the_ceiling.text
    assert _code(at_the_ceiling) == "split_sum_mismatch", at_the_ceiling.text

    zero_parent = _seed(
        session,
        user_id=user_id,
        account_id=account,
        amount="0.00",
        description="ZERO ROW",
    )
    session.commit()
    zero = _split(client, headers, zero_parent.id, _legs("-0.00", "-0.00"))
    assert zero.status_code == 422 and _code(zero) == "split_zero_parent"

    assert _count_children(session, parent["id"]) == 0


def test_split_validation_order_is_deterministic(client: TestClient, session: Session) -> None:
    """docs/44 §4.2 — one request, several rules tripped: the FIRST one wins.

    The body is parsed INSIDE the handler (after steps 1-5) precisely so a
    malformed body aimed at a child still answers `split_nested`, not
    `validation_error`.
    """
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    parent = _tx(client, headers, account, "-187.42", "COSTCO #123")
    created = _split(client, headers, parent["id"], _legs("-120.00", "-67.42"))
    assert created.status_code == 201, created.text
    child_id = created.json()["children"][0]["id"]

    # step 3 (child) beats step 8 (too few legs) and step 6 (extra key).
    nested = client.post(
        f"{API}/transactions/{child_id}/split",
        headers=headers,
        json={"children": _legs("-1.00"), "bogus": True},
    )
    assert nested.status_code == 422 and _code(nested) == "split_nested"

    # step 5 (already split) beats step 8 (too few legs).
    again = _split(client, headers, parent["id"], _legs("-1.00"))
    assert again.status_code == 409 and _code(again) == "already_split"

    # Steps 8/10/12 need a row that is NOT already split — otherwise step 5
    # (`already_split`) wins, which is exactly what the case above proves.
    fresh = _tx(client, headers, account, "-187.42", "FRESH ROW")
    # step 8 (count) beats step 10 (sign) beats step 12 (sum).
    order = _split(client, headers, fresh["id"], _legs("+1.00"))
    assert order.status_code == 422, order.text
    assert _code(order) == "split_too_few_legs", order.text

    # step 10 (sign) beats step 12 (sum) when the count is legal.
    sign_then_sum = _split(client, headers, fresh["id"], _legs("-100.00", "+1.00"))
    assert sign_then_sum.status_code == 422, sign_then_sum.text
    assert _code(sign_then_sum) == "split_leg_sign", sign_then_sum.text

    # …and step 5 wins over all of them on a row that IS split (above), while
    # step 3 wins over step 6 on a child (above).

    # step 6 (shape) beats step 12 (sum) — a body that is not a body at all,
    # on the still-unsplit row (on a split one, step 5 would win instead).
    wrong_shape = client.post(
        f"{API}/transactions/{fresh['id']}/split",
        headers=headers,
        json={"children": "not-a-list"},
    )
    assert wrong_shape.status_code == 422 and _code(wrong_shape) == "validation_error"
    assert isinstance(wrong_shape.json()["detail"], list), "house validation_error envelope"

    # steps 1 and 2 beat everything: an unknown id is a 404 even with a
    # body that trips every other rule.
    missing = client.post(
        f"{API}/transactions/999999/split",
        headers=headers,
        json={"children": _legs("+1.00")},
    )
    assert missing.status_code == 404 and _code(missing) == "not_found"

    assert _count_children(session, parent["id"]) == 2, "refusals write nothing"


def test_investment_accounts_are_excluded_from_splitting(
    client: TestClient, session: Session
) -> None:
    """docs/44 §13 — 422 before any write; NO child, trade or acb_pool row."""
    headers, email, _ = _register(client)
    user = _user(session, email)
    for type_ in ("brokerage_non_registered", "tfsa", "rrsp"):
        account = _account(client, headers, _uniq(type_), type_)
        parent = _tx(client, headers, account, "-1000.00", "BUY 10 XYZ")
        response = _split(client, headers, parent["id"], _legs("-500.00", "-500.00"))
        assert response.status_code == 422, (type_, response.text)
        assert _code(response) == "split_account_not_allowed"
        assert type_ in response.json()["detail"], response.json()["detail"]
        assert _count_children(session, parent["id"]) == 0
        assert _reload(session, parent["id"]).is_split is False

    assert (
        session.exec(select(func.count()).select_from(Trade).where(Trade.user_id == user.id)).one()
        == 0
    ), "the refused call must not invent a trade"
    # `acb_pools` is rebuilt from `trades` alone (docs/06), so an empty trade
    # set must mean an empty pool set — this is the ACB invariant itself.
    from app.models.acb_pools import AcbPool

    assert (
        session.exec(
            select(func.count()).select_from(AcbPool).where(AcbPool.user_id == user.id)
        ).one()
        == 0
    ), "the refused call must not invent an acb_pools row"

    for type_ in ("chequing", "credit_card"):
        account = _account(client, headers, _uniq(type_), type_)
        parent = _tx(client, headers, account, "-187.42", "COSTCO #123")
        response = _split(client, headers, parent["id"], _legs("-120.00", "-67.42"))
        assert response.status_code == 201, (type_, response.text)


def test_nested_split_and_split_state_codes(client: TestClient, session: Session) -> None:
    """docs/44 §4.3 — `split_nested` / `already_split` / `not_split`."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    unsplit = _tx(client, headers, account, "-50.00", "ORDINARY ROW")

    for method in ("GET", "DELETE", "PUT"):
        response = _split(client, headers, unsplit["id"], _legs("-25.00", "-25.00"), method)
        assert response.status_code == 409, (method, response.text)
        assert _code(response) == "not_split", method

    parent = _tx(client, headers, account, "-187.42", "COSTCO #123")
    created = _split(client, headers, parent["id"], _legs("-120.00", "-67.42"))
    assert created.status_code == 201, created.text
    child_id = created.json()["children"][0]["id"]

    nested = _split(client, headers, child_id, _legs("-60.00", "-60.00"))
    assert nested.status_code == 422 and _code(nested) == "split_nested"
    assert _count_children(session, child_id) == 0, "a child can never itself be split"

    second = _split(client, headers, parent["id"], _legs("-120.00", "-67.42"))
    assert second.status_code == 409 and _code(second) == "already_split"


def test_parent_mutation_is_refused_while_split(client: TestClient, session: Session) -> None:
    """docs/44 §5 — the parent admits exactly ONE edit: merge."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    groceries = _category(client, headers)
    tag_id = _tag(client, headers)
    parent = _tx(client, headers, account, "-187.42", "COSTCO #123", category_id=groceries)
    created = _split(client, headers, parent["id"], _legs("-120.00", "-67.42"))
    assert created.status_code == 201, created.text
    parent_id = parent["id"]

    ledger_patches: list[dict] = [
        {"category_id": groceries},
        {"amount_native": "-200.00"},
        {"date": "2026-07-01"},
        {"raw_description": "RENAMED"},
        {"currency": "USD"},
        {"note": "a note"},
        {"account_id": account},
        {"posting_date": "2026-06-16"},
        {"normalized_payee": "Costco"},
    ]
    for payload in ledger_patches:
        response = client.patch(f"{API}/transactions/{parent_id}", headers=headers, json=payload)
        assert response.status_code == 409, (payload, response.text)
        assert _code(response) == "transaction_is_split", payload
        assert "Merge its parts first" in response.json()["detail"]

    deleted = client.delete(f"{API}/transactions/{parent_id}", headers=headers)
    assert deleted.status_code == 409 and _code(deleted) == "transaction_is_split"

    for method in ("POST", "PUT"):
        attach = client.request(
            method,
            f"{API}/transactions/{parent_id}/tags",
            headers=headers,
            json={"tag_ids": [tag_id]} if method == "PUT" else {"tag_id": tag_id},
        )
        assert attach.status_code == 409, (method, attach.text)
        assert _code(attach) == "transaction_is_split", method

    # Transfer routes are pinned in
    # `test_transfer_and_reconciliation_refuse_split_rows`; review and
    # recurring accept/suppress are identity-row properties and stay open.

    # The E2EE backfill (docs/33 A17) MUST keep working: cipher/index fields
    # only → 200, and the ledger fields are untouched.
    cipher_only = client.patch(
        f"{API}/transactions/{parent_id}",
        headers=headers,
        json={"amount_cipher": "v1.ciphertext", "payee_token": "tok-index"},
    )
    assert cipher_only.status_code == 200, cipher_only.text
    assert cipher_only.json()["amount_cipher"] == "v1.ciphertext"

    row = _reload(session, parent_id)
    assert row.is_split is True
    assert row.amount_native == Decimal("-187.42")
    assert row.category_id == groceries


def test_child_edit_matrix(client: TestClient, session: Session) -> None:
    """docs/44 D9 — category/note edit, identity/money frozen, no self-delete."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    groceries = _category(client, headers)
    apparel = _category(client, headers)
    parent = _tx(client, headers, account, "-187.42", "COSTCO #123")
    created = _split(
        client,
        headers,
        parent["id"],
        [
            {"amount_native": "-120.00", "category_id": groceries},
            {"amount_native": "-67.42"},
        ],
    )
    assert created.status_code == 201, created.text
    child = created.json()["children"][0]
    child_id = child["id"]

    recat = client.patch(
        f"{API}/transactions/{child_id}", headers=headers, json={"category_id": apparel}
    )
    assert recat.status_code == 200, recat.text
    assert recat.json()["category_id"] == apparel

    noted = client.patch(
        f"{API}/transactions/{child_id}", headers=headers, json={"note": "kids clothes"}
    )
    assert noted.status_code == 200, noted.text
    assert noted.json()["note"] == "kids clothes"

    for payload in (
        {"amount_native": "-99.00"},
        {"date": "2026-07-04"},
        {"account_id": account},
        {"raw_description": "RENAMED PART"},
        {"normalized_payee": "Costco West"},
        {"currency": "USD"},
        {"posting_date": "2026-06-16"},
    ):
        response = client.patch(f"{API}/transactions/{child_id}", headers=headers, json=payload)
        assert response.status_code == 409, (payload, response.text)
        assert _code(response) == "split_leg_immutable", payload

    removed = client.delete(f"{API}/transactions/{child_id}", headers=headers)
    assert removed.status_code == 409 and _code(removed) == "split_leg_immutable"
    assert _count_children(session, parent["id"]) == 2, "a child can never be deleted alone"

    row = _reload(session, child_id)
    assert row.amount_native == Decimal("-120.00")
    assert row.note == "kids clothes"
    assert row.category_id == apparel


def test_merge_is_an_exact_inverse(client: TestClient, session: Session) -> None:
    """docs/44 D5 + T9 — split then merge, every field byte-identical."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    groceries = _category(client, headers)
    tag_id = _tag(client, headers)
    parent = _tx(
        client,
        headers,
        account,
        "-187.42",
        "COSTCO #123",
        category_id=groceries,
        payee_cipher="v1.payee",
        amount_cipher="v1.amount",
        payee_token="tok-payee",
    )
    parent_id = parent["id"]
    attach = client.post(
        f"{API}/transactions/{parent_id}/tags", headers=headers, json={"tag_id": tag_id}
    )
    assert attach.status_code == 200, attach.text
    noted = client.patch(
        f"{API}/transactions/{parent_id}", headers=headers, json={"note": "pre-split note"}
    )
    assert noted.status_code == 200, noted.text

    before = {name: getattr(_reload(session, parent_id), name) for name in INVERSE_FIELDS}
    before_tags = {
        row.tag_id
        for row in session.exec(
            select(TransactionTag).where(TransactionTag.transaction_id == parent_id)
        ).all()
    }

    created = _split(
        client,
        headers,
        parent_id,
        [
            {"amount_native": "-120.00", "category_id": groceries},
            {"amount_native": "-40.00"},
            {"amount_native": "-27.42"},
        ],
    )
    assert created.status_code == 201, created.text
    assert _count_children(session, parent_id) == 3
    assert _reload(session, parent_id).is_split is True

    merged = _split(client, headers, parent_id, method="DELETE")
    assert merged.status_code == 200, merged.text

    after = {name: getattr(_reload(session, parent_id), name) for name in INVERSE_FIELDS}
    assert after == before, {
        name: (before[name], after[name]) for name in INVERSE_FIELDS if before[name] != after[name]
    }
    assert after_tags(session, parent_id) == before_tags, (
        "the parent's tags were never touched by the split"
    )
    assert _count_children(session, parent_id) == 0, "every child is gone"
    row = _reload(session, parent_id)
    assert row.is_split is False and row.split_checksum is None


def after_tags(session: Session, transaction_id: int) -> set[int]:
    return {
        row.tag_id
        for row in session.exec(
            select(TransactionTag).where(TransactionTag.transaction_id == transaction_id)
        ).all()
    }


def test_cad_allocation_never_moves_a_cad_total(client: TestClient, session: Session) -> None:
    """docs/44 §3.3 — largest-remainder allocation off the parent's CACHED CAD."""
    headers, email, _ = _register(client)
    user = _user(session, email)
    account = _account(client, headers, _uniq("acct"), "chequing")

    # CAD ≠ native: this is the case where re-deriving from FX would drift.
    parent = _seed(
        session,
        user_id=user.id,
        account_id=account,
        amount="-187.42",
        cad_amount="-192.33",
        description="FX ROW",
    )
    session.commit()

    response = _split(client, headers, parent.id, _legs("-120.00", "-40.00", "-27.42"))
    assert response.status_code == 201, response.text

    children = _children_of(session, parent.id)
    assert len(children) == 3
    shares = [child.cad_amount for child in children]
    assert all(share is not None for share in shares)
    assert all(share == share.quantize(Decimal("0.01")) for share in shares)  # type: ignore[union-attr]
    total = sum((share for share in shares if share is not None), Decimal("0"))
    assert total == Decimal("-192.33"), "Σ child cad_amount == parent cad_amount, to the cent"

    # Residual lands on the LARGEST |amount| — transcribed from §3.3, not from
    # the implementation.
    amounts = [Decimal("-120.00"), Decimal("-40.00"), Decimal("-27.42")]
    cad = Decimal("-192.33")
    native = Decimal("-187.42")
    cent = Decimal("0.01")
    expected = [
        (cad * amount / native).quantize(cent)
        for amount in amounts  # HALF_UP is the default
    ]
    residual = cad - sum(expected, Decimal("0"))
    biggest = max(range(len(amounts)), key=lambda i: abs(amounts[i]))
    expected[biggest] += residual
    assert [child.cad_amount for child in children] == expected

    # Unresolved stays unresolved.
    plain = _seed(
        session,
        user_id=user.id,
        account_id=account,
        amount="-80.00",
        cad_amount=None,
        description="NO CAD ROW",
    )
    session.commit()
    none_ok = _split(client, headers, plain.id, _legs("-50.00", "-30.00"))
    assert none_ok.status_code == 201, none_ok.text
    assert [child.cad_amount for child in _children_of(session, plain.id)] == [None, None]


# --- §2 the two population rules ---------------------------------------------


def _money_snapshot(client: TestClient, headers: dict[str, str], account_id: int) -> dict:
    """Every total the spec says a split must not move (docs/44 T8/T11)."""
    today = date.today()
    span = {"from": str(today.replace(day=1)), "to": str(today)}
    spending = client.get(f"{API}/reports/spending/categories", headers=headers, params=span)
    cashflow = client.get(f"{API}/reports/cashflow", headers=headers, params=span)
    networth = client.get(
        f"{API}/reports/net-worth",
        headers=headers,
        params={**span, "granularity": "month"},
    )
    accounts = client.get(f"{API}/accounts", headers=headers)
    for response in (spending, cashflow, networth, accounts):
        assert response.status_code == 200, response.text
    return {
        "spending_total": json.dumps(spending.json()["total_cad"]),
        "spending_items": json.dumps(spending.json()["items"], sort_keys=True),
        "cashflow": json.dumps(cashflow.json()["buckets"], sort_keys=True),
        "networth": json.dumps(networth.json()["buckets"], sort_keys=True),
        "accounts": json.dumps(
            [(row["id"], row["cash_balance"]) for row in accounts.json()["items"]],
            sort_keys=True,
        ),
    }


def test_ledger_population_counts_children_and_never_parents(
    client: TestClient, session: Session
) -> None:
    """docs/44 T8 — totals byte-identical; only the CATEGORY moves."""
    headers, email, _ = _register(client)
    user = _user(session, email)
    account = _account(client, headers, _uniq("acct"), "chequing")
    groceries = _category(client, headers)
    apparel = _category(client, headers)
    health = _category(client, headers)
    today = date.today()

    # Three ordinary rows + the one that will be split, all in the CURRENT
    # period so `budget_spent`'s bounded-below window sees them.
    ordinary = [
        _tx(
            client,
            headers,
            account,
            amount,
            _uniq("row"),
            day=str(today),
            category_id=groceries,
        )
        for amount in ("-10.00", "-20.00", "-30.00")
    ]
    split_me = _tx(
        client,
        headers,
        account,
        "-60.00",
        _uniq("splitme"),
        day=str(today),
        category_id=groceries,
    )

    from app.core.config import get_settings
    from app.services.budgets import budget_spent
    from app.services.reports import FxCache

    reporting = get_settings().reporting_currency
    fx = FxCache(session, reporting)

    def spent(category_id: int) -> str:
        return str(budget_spent(session, user.id, category_id, "monthly", reporting, fx))

    before_money = _money_snapshot(client, headers, account)
    before_budgets = {groceries: spent(groceries), apparel: spent(apparel), health: spent(health)}
    assert before_budgets[groceries] != "0", "the fixture must actually spend"

    created = _split(
        client,
        headers,
        split_me["id"],
        [
            {"amount_native": "-40.00", "category_id": groceries},
            {"amount_native": "-15.00", "category_id": apparel},
            {"amount_native": "-5.00", "category_id": health},
        ],
    )
    assert created.status_code == 201, created.text

    after_money = _money_snapshot(client, headers, account)
    # The headline invariant: every CAD total is BYTE-identical.
    assert after_money["spending_total"] == before_money["spending_total"]
    assert after_money["cashflow"] == before_money["cashflow"]
    assert after_money["networth"] == before_money["networth"]
    assert after_money["accounts"] == before_money["accounts"]

    # `budget_spent` answers in the magnitude of NET DEBITS, so a row that
    # leaves a category subtracts its whole amount and a row that enters one
    # adds its own. The numbers are pinned because the sign convention is
    # exactly the thing a split could silently break.
    assert Decimal(before_budgets[groceries]) == Decimal("120.00"), (
        "fixture: 10 + 20 + 30 + 60 of debits, all in Groceries"
    )
    after_budgets = {groceries: spent(groceries), apparel: spent(apparel), health: spent(health)}
    assert Decimal(after_budgets[groceries]) == Decimal("100.00"), (
        "Groceries keeps only the -40 part of the split it used to own whole"
    )
    assert Decimal(after_budgets[apparel]) == Decimal("15.00")
    assert Decimal(after_budgets[health]) == Decimal("5.00")
    # …and the three budgets' SUM did not move a cent — the split moved
    # CATEGORY, never MONEY (docs/44 D3).
    assert (
        sum(map(Decimal, after_budgets.values()))
        == sum(map(Decimal, before_budgets.values()))
        == Decimal("120.00")
    )

    # Category breakdown: the money moved bucket, the total did not.
    spending = client.get(
        f"{API}/reports/spending/categories",
        headers=headers,
        params={"from": str(today.replace(day=1)), "to": str(today)},
    ).json()
    assert json.dumps(spending["total_cad"]) == before_money["spending_total"]
    by_id = {int(item["category_id"]): item for item in spending["items"] if item["category_id"]}
    assert json.dumps(by_id[apparel]["cad_total"]) != "0"

    # The rows themselves: parents out, children in, ordinary rows untouched.
    page = client.get(f"{API}/transactions", headers=headers).json()
    ids = {row["id"] for row in page["items"]}
    assert split_me["id"] not in ids, "a split parent never appears in LEDGER items"
    child_ids = {child["id"] for child in created.json()["children"]}
    assert child_ids <= ids
    for row in ordinary:
        assert row["id"] in ids
    assert page["total"] == len(
        [
            row
            for row in session.exec(
                select(Transaction).where(
                    Transaction.user_id == user.id, Transaction.is_split.is_(False)
                )
            ).all()
        ]
    )


def test_list_returns_ledger_rows_plus_split_parents(client: TestClient, session: Session) -> None:
    """docs/44 D11 — items = children + ordinary, never a split parent."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    ordinary = _tx(client, headers, account, "-5.00", "COFFEE")
    parent = _tx(client, headers, account, "-187.42", "COSTCO #123")
    created = _split(client, headers, parent["id"], _legs("-120.00", "-40.00", "-27.42"))
    assert created.status_code == 201, created.text
    children = created.json()["children"]

    page = client.get(f"{API}/transactions?limit=50", headers=headers).json()
    assert set(page) == {"items", "total", "limit", "offset", "split_parents", "withheld"}
    item_ids = {row["id"] for row in page["items"]}
    assert parent["id"] not in item_ids, "the parent is never a LEDGER item"
    assert {child["id"] for child in children} <= item_ids
    assert ordinary["id"] in item_ids
    assert page["total"] == len(page["items"]), "total counts matching LEDGER rows"

    assert len(page["split_parents"]) == 1
    header = page["split_parents"][0]
    assert header["id"] == parent["id"]
    assert header["child_count"] == 3
    assert header["is_split"] is True
    assert header["amount_native"] == "-187.42", "the header carries the TRUE total"
    assert set(header) >= {
        "id",
        "account_id",
        "date",
        "amount_native",
        "parent_split_id",
        "is_split",
        "note",
        "child_count",
    }


def test_list_filters_use_parent_identity_and_child_content(
    client: TestClient, session: Session
) -> None:
    """docs/44 §6.2 — content filters hit children, amount filters hit the statement."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    groceries = _category(client, headers)
    apparel = _category(client, headers)

    # POSITIVE statement: the spec's literal `amount_min=100` shape.
    positive = _tx(
        client, headers, account, "+187.42", "REFUND REVERSAL", payee_token="tok-positive"
    )
    created_pos = _split(
        client,
        headers,
        positive["id"],
        [
            {"amount_native": "+120.00", "category_id": groceries},
            {"amount_native": "+67.42", "category_id": apparel},
        ],
    )
    assert created_pos.status_code == 201, created_pos.text

    # A child's own category is the filter splitting exists for.
    by_category = client.get(f"{API}/transactions?category_id={apparel}", headers=headers).json()
    assert [row["id"] for row in by_category["items"]] == [created_pos.json()["children"][1]["id"]]
    assert [row["id"] for row in by_category["split_parents"]] == [positive["id"]]
    assert by_category["split_parents"][0]["child_count"] == 2
    assert by_category["split_parents"][0]["amount_native"] == "187.42", (
        "the header still reports the whole statement even though only one child matched"
    )

    # Amount filter → the STATEMENT, so the whole group comes back.
    by_amount = client.get(f"{API}/transactions?amount_min=100", headers=headers).json()
    assert {row["id"] for row in by_amount["items"]} == {
        child["id"] for child in created_pos.json()["children"]
    }
    assert [row["id"] for row in by_amount["split_parents"]] == [positive["id"]]

    # Negative statement: `amount_max=-100` finds the -187.42 line, and every
    # part of it — the two smaller parts would not match on their own.
    negative = _tx(client, headers, account, "-187.42", "COSTCO #123")
    created_neg = _split(client, headers, negative["id"], _legs("-120.00", "-40.00", "-27.42"))
    assert created_neg.status_code == 201, created_neg.text
    negative_group = client.get(f"{API}/transactions?amount_max=-100", headers=headers).json()
    assert {row["id"] for row in negative_group["items"]} == {
        child["id"] for child in created_neg.json()["children"]
    }

    # `direction` is the parent's sign, so every part of a paid statement is paid.
    paid = client.get(f"{API}/transactions?direction=paid", headers=headers).json()
    assert {row["id"] for row in paid["items"]} >= {
        child["id"] for child in created_neg.json()["children"]
    }

    # `payee_token` → EVERY child (they inherit the parent's blind index).
    by_token = client.get(f"{API}/transactions?payee_token=tok-positive", headers=headers).json()
    assert {row["id"] for row in by_token["items"]} == {
        child["id"] for child in created_pos.json()["children"]
    }


# --- §7 dedup / import -------------------------------------------------------


def test_import_dedup_matches_the_parent_and_never_a_child(
    client: TestClient, session: Session
) -> None:
    """docs/44 §7 — a re-upload dedups to the PARENT; no child is reachable."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    day, amount, description = "2026-06-15", "-187.42", "COSTCO #123"
    parent = _tx(client, headers, account, amount, description, day=day)
    created = _split(client, headers, parent["id"], _legs("-120.00", "-40.00", "-27.42"))
    assert created.status_code == 201, created.text
    children = _children_of(session, parent["id"])
    assert len(children) == 3

    canonical = build_dedup_hash(
        account,
        RawTransaction(
            date=date.fromisoformat(day),
            amount_native=Decimal(amount),
            raw_description=description,
        ),
    )
    # The parent still owns the CANONICAL hash — the split never rewrites its
    # identity, which is what makes it a stable dedup anchor (docs/44 §7).
    assert _reload(session, parent["id"]).dedup_hash == canonical

    csv = f"Date,Amount,Description\n{day},{amount},{description}\n"
    batch = _upload_csv(client, headers, account, csv, name="reupload.csv")
    parsed = batch["preview"][0]
    assert str(parsed["date"]) == day, parsed
    assert parsed["raw_description"] == description, parsed
    assert Decimal(parsed["amount_native"]) == Decimal(amount), parsed
    assert batch["imported"] == 0, batch
    assert batch["duplicates_skipped"] == 1, "the PARENT is the duplicate, exactly once"
    assert all(row["duplicate"] for row in batch["preview"]), batch["preview"]

    for child in children:
        # The salted `canonical:uuid` shape (import_staging.py) — an importer
        # computes a bare sha256 hex, so a hash containing ":" is unreachable
        # by construction and can never be produced by re-hashing a statement.
        prefix, sep, salt = child.dedup_hash.partition(":")
        assert sep == ":" and len(prefix) == 64 and len(salt) == 32, child.dedup_hash
        assert child.dedup_hash != canonical, "a child never owns the canonical hash"
    assert (
        session.exec(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.account_id == account, Transaction.dedup_hash == canonical)
        ).one()
        == 1
    ), "exactly one row owns the canonical hash — the parent"

    # Near-duplicate preview: children are not even candidates. The probe row
    # shares the -40.00 child's date AND amount with a similar payee, so if a
    # child were in the candidate set this WOULD match.
    probe_date = date.fromisoformat(day)
    near = find_near_duplicates(
        session,
        account,
        [
            RowKey(
                index=0,
                date=probe_date,
                amount_native=Decimal("-40.00"),
                currency="CAD",
                payee=f"{description} EAST",
            )
        ],
    )
    matched_ids = {
        match.transaction_id for match in near.values() if match.transaction_id is not None
    }
    assert matched_ids.isdisjoint({child.id for child in children}), matched_ids


def test_import_never_auto_splits(client: TestClient, session: Session) -> None:
    """docs/44 D6/§7 — a staged row lands as ONE ordinary row."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    csv = "Date,Amount,Description\n2026-06-20,-42.50,GROCERY MART #12\n"
    batch = _upload_csv(client, headers, account, csv, name="fresh.csv")
    assert batch["imported"] == 1 and batch["duplicates_skipped"] == 0, batch

    commit = client.post(f"{API}/imports/{batch['id']}/commit", headers=headers)
    assert commit.status_code == 200, commit.text

    rows = session.exec(
        select(Transaction).where(
            Transaction.account_id == account,
            Transaction.raw_description == "GROCERY MART #12",
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].is_split is False
    assert rows[0].parent_split_id is None
    assert rows[0].split_checksum is None


def test_ofx_export_is_identity_and_round_trips(client: TestClient, session: Session) -> None:
    """docs/44 §7 — the statement keeps its LINE COUNT; re-import = 0 net-new."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    parent = _tx(client, headers, account, "-42.50", "GROCERY MART #12", day="2026-06-10")
    created = _split(client, headers, parent["id"], _legs("-30.00", "-12.50"))
    assert created.status_code == 201, created.text
    children = _children_of(session, parent["id"])

    ofx = client.get(f"{API}/export/ofx", headers=headers, params={"account_id": account})
    assert ofx.status_code == 200, ofx.text
    text = ofx.content.decode("utf-8", errors="replace")
    assert "GROCERY MART #12" in text
    assert text.count("TRNAMT") == 1, "IDENTITY: one statement line, never the parts"

    upload = client.post(
        f"{API}/imports/upload",
        headers=headers,
        data={"account_id": str(account)},
        files={"file": ("statement.ofx", ofx.content, "application/x-ofx")},
    )
    assert upload.status_code == 201, upload.text
    body = upload.json()
    assert body["imported"] == 0, body
    assert body["duplicates_skipped"] == 1, body
    assert all(row["duplicate"] for row in body["preview"]), body["preview"]
    commit = client.post(f"{API}/imports/{body['id']}/commit", headers=headers)
    assert commit.status_code == 200, commit.text
    assert session.exec(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account)
    ).one() == 1 + len(children), "0 net-new — the split's rows are all still there"


def test_search_and_recurring_are_identity(client: TestClient, session: Session) -> None:
    """docs/44 §2 — payee search and the recurring scan see PARENTS only."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")

    parent = _tx(client, headers, account, "-187.42", "COSTCO #123", payee_token="tok-costco")
    created = _split(client, headers, parent["id"], _legs("-120.00", "-40.00", "-27.42"))
    assert created.status_code == 201, created.text
    child_ids = {child["id"] for child in created.json()["children"]}

    hits = client.get(
        f"{API}/search",
        headers=headers,
        params={"q": "costco", "payee_token": "tok-costco", "limit": 10},
    ).json()
    txn_ids = {row["id"] for row in hits["transactions"]}
    assert txn_ids == {parent["id"]}, (
        f"IDENTITY: one hit (the statement), not {len(child_ids)} children — {txn_ids}"
    )

    # Recurring: four monthly occurrences of the same merchant — the 15th of
    # each of the last four months, ANCHORED to the clock: recurring.py only
    # looks back 400 days, and the hard-coded 2026 dates this replaced slid
    # out of that window as the calendar advanced (gone by 2027-08-20).
    # The badge is a statement-line property, so no child is ever a candidate.
    today = date.today()
    last_month = today.year * 12 + today.month - 1

    def _month_15(months_back: int) -> str:
        absolute = last_month - months_back
        return date(absolute // 12, absolute % 12 + 1, 15).isoformat()

    recurring_ids = set()
    for back in (2, 1, 0):
        row = _tx(
            client,
            headers,
            account,
            "-12.99",
            "STREAMING SERVICE",
            day=_month_15(back),
        )
        recurring_ids.add(row["id"])
    split_one = _tx(client, headers, account, "-12.99", "STREAMING SERVICE", day=_month_15(3))
    split_done = _split(client, headers, split_one["id"], _legs("-10.00", "-2.99"))
    assert split_done.status_code == 201, split_done.text
    recurring_children = {child["id"] for child in split_done.json()["children"]}

    candidates = client.get(f"{API}/transactions/recurring/candidates", headers=headers).json()
    seen = {tx_id for group in candidates["groups"] for tx_id in group["transaction_ids"]}
    assert seen.isdisjoint(child_ids | recurring_children), (
        "a split child is never a recurring candidate"
    )
    assert seen & recurring_ids, "the fixture must actually form a group"


# --- §10 export / restore ----------------------------------------------------


def test_export_restore_roundtrip_reconstructs_relationships(
    client: TestClient, session: Session
) -> None:
    """docs/44 D14 — round trip, then again with children BEFORE their parent."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    parent = _tx(client, headers, account, "-187.42", "COSTCO #123")
    created = _split(client, headers, parent["id"], _legs("-120.00", "-40.00", "-27.42"))
    assert created.status_code == 201, created.text

    before = client.get(EXPORT_FULL, headers=headers)
    assert before.status_code == 200, before.text
    snapshot = before.json()
    relationships = sorted(
        (row["id"], row["parent_split_id"], row["is_split"], row["amount_native"])
        for row in snapshot["transactions"]
    )
    assert any(link is not None for _, link, _, _ in relationships), "the snapshot carries the FK"

    # Reorder: every child BEFORE the row it hangs off. DEFERRABLE INITIALLY
    # DEFERRED is what makes this legal (docs/44 §10).
    children_first = [row for row in snapshot["transactions"] if row["parent_split_id"]]
    others = [row for row in snapshot["transactions"] if not row["parent_split_id"]]
    assert children_first, "the fixture must have children to reorder"
    reordered = dict(snapshot)
    reordered["transactions"] = children_first + others
    assert reordered["transactions"] != snapshot["transactions"]

    restore = client.post(
        RESTORE_URL,
        headers=headers,
        json={
            "confirm": "restore",
            "payload": {
                "v": 1,
                "kind": "fintrack-backup",
                "period": "monthly",
                "exported_at": datetime.now(UTC).isoformat(),
                "snapshot": reordered,
            },
        },
    )
    assert restore.status_code == 200, restore.text
    assert restore.json()["restored"]["transactions"] == len(reordered["transactions"])

    after = client.get(EXPORT_FULL, headers=headers)
    assert after.status_code == 200, after.text
    snapshot2 = after.json()
    assert len(snapshot2["transactions"]) == len(snapshot["transactions"])
    assert (
        sorted(
            (row["id"], row["parent_split_id"], row["is_split"], row["amount_native"])
            for row in snapshot2["transactions"]
        )
        == relationships
    ), "relationships identical after a children-first restore"
    assert snapshot2["transactions"] == snapshot["transactions"], (
        "GET /export/full is semantically equal across the round trip"
    )


# --- §9 partner projection ---------------------------------------------------


def test_partner_sees_children_with_their_parent_and_masks_them(
    client: TestClient, session: Session
) -> None:
    """docs/44 D13 — scope by construction, privacy mode masks the header."""
    owner_headers, owner_email, owner_token = _register(client)
    partner_headers, partner_email, _ = _register(client)

    shared_account = _account(client, owner_headers, _uniq("shared"), "chequing")
    hidden_account = _account(client, owner_headers, _uniq("hidden"), "chequing")

    shared_parent = _tx(client, owner_headers, shared_account, "-187.42", "COSTCO #123")
    hidden_parent = _tx(client, owner_headers, hidden_account, "-99.99", "HIDDEN ROW")
    shared_split = _split(client, owner_headers, shared_parent["id"], _legs("-120.00", "-67.42"))
    hidden_split = _split(client, owner_headers, hidden_parent["id"], _legs("-50.00", "-49.99"))
    assert shared_split.status_code == 201, shared_split.text
    assert hidden_split.status_code == 201, hidden_split.text

    invite = _invite(client, owner_token, partner_email, [shared_account])
    accepted = _accept(client, invite["token"], partner_token_of(partner_headers))
    assert accepted.status_code == 200, accepted.text

    page = client.get(f"{API}/partner/transactions?limit=50", headers=partner_headers)
    assert page.status_code == 200, page.text
    body = page.json()
    item_ids = {row["id"] for row in body["items"]}
    child_ids = {child["id"] for child in shared_split.json()["children"]}
    hidden_ids = {child["id"] for child in hidden_split.json()["children"]}

    assert child_ids <= item_ids, "shared account → parent's children are visible"
    assert shared_parent["id"] not in item_ids, "…and the parent is NOT a LEDGER item"
    assert hidden_ids.isdisjoint(item_ids), "unshared account → neither side"
    assert hidden_parent["id"] not in item_ids
    assert {row["id"] for row in body["split_parents"]} == {shared_parent["id"]}
    header = body["split_parents"][0]
    assert header["child_count"] == 2
    assert header["amount_native"] == "-187.42"

    # Privacy mode: the header's money goes null, and no seeded amount survives
    # anywhere in the payload.
    owner = _user(session, owner_email)
    owner.privacy_mode = True
    session.add(owner)
    session.commit()

    masked = client.get(f"{API}/partner/transactions?limit=50", headers=partner_headers)
    assert masked.status_code == 200, masked.text
    masked_body = masked.json()
    masked_header = masked_body["split_parents"][0]
    assert masked_header["amount_native"] is None, "a split header must render 🔒, not a total"
    assert masked_header["cad_amount"] is None
    assert masked_header["child_count"] == 2, "structure survives; only money is masked"
    assert masked_header["date"] == header["date"], "dates are not money (docs/43 §9)"
    wire = json.dumps(masked_body)
    assert "-187.42" not in wire
    assert "-120.00" not in wire
    assert "-67.42" not in wire


def partner_token_of(headers: dict[str, str]) -> str:
    return headers["Authorization"].split()[1]


# --- §11 the event log -------------------------------------------------------


def _events(client: TestClient, headers: dict[str, str], tx_id: int) -> list[dict]:
    response = client.get(f"{API}/transactions/{tx_id}/events", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _sha(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_split_and_merge_write_exactly_one_event_each(client: TestClient, session: Session) -> None:
    """docs/44 §11/D15 — one receipt per mutation; children write none."""
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    groceries = _category(client, headers)
    parent = _tx(client, headers, account, "-187.42", "COSTCO #123")
    parent_id = parent["id"]

    created = _split(client, headers, parent_id, _legs("-120.00", "-67.42"))
    assert created.status_code == 201, created.text
    child_ids = [child["id"] for child in created.json()["children"]]

    events = _events(client, headers, parent_id)
    # Newest first: the split receipt, then the row's OWN creation receipt
    # from `POST /transactions` — which M107 adds to, never replaces.
    assert [event["event_kind"] for event in events] == ["split", "created"], (
        "exactly ONE split receipt for the split itself"
    )
    assert events[0]["payload"]["op"] == "create"
    assert events[0]["payload"]["child_ids"] == child_ids
    assert events[0]["payload"]["amounts"] == ["-120.00", "-67.42"]
    # Hand-recomputed, sorted-keys canonical JSON — the M106 receipt property.
    assert events[0]["payload_hash"] == _sha(events[0]["payload"])
    assert events[0]["summary"] == "you split this transaction"

    for child_id in child_ids:
        assert _events(client, headers, child_id) == [], (
            "children write no `created` receipt — the parent's is the record"
        )

    replaced = _split(client, headers, parent_id, _legs("-90.00", "-97.42"), method="PUT")
    assert replaced.status_code == 200, replaced.text
    new_child_ids = [child["id"] for child in replaced.json()["children"]]
    assert set(new_child_ids).isdisjoint(child_ids), "PUT replaces the whole group"

    events = _events(client, headers, parent_id)
    assert [event["event_kind"] for event in events] == ["split", "split", "created"]
    replace_event = events[0]
    assert replace_event["payload"]["op"] == "replace"
    assert replace_event["payload"]["before"] == child_ids
    assert replace_event["payload"]["after"] == new_child_ids
    assert replace_event["payload_hash"] == _sha(replace_event["payload"])
    assert replace_event["summary"] == "you re-split this transaction"

    for old_id in child_ids:
        assert session.get(Transaction, old_id) is None, "the replaced children are gone"
        assert (
            session.exec(
                select(func.count())
                .select_from(TransactionEvent)
                .where(TransactionEvent.tx_id == old_id)
            ).one()
            == 0
        ), "no `deleted` receipt for a child either"

    merged = _split(client, headers, parent_id, method="DELETE")
    assert merged.status_code == 200, merged.text
    events = _events(client, headers, parent_id)
    assert [event["event_kind"] for event in events] == ["merge", "split", "split", "created"]
    merge_event = events[0]
    assert merge_event["payload"] == {
        "op": "merge",
        "parent_id": parent_id,
        "child_ids": new_child_ids,
    }
    assert merge_event["payload_hash"] == _sha(merge_event["payload"])
    assert merge_event["summary"] == "you merged this transaction back"

    # A later edit to a child writes an ordinary receipt keyed by the CHILD —
    # but only while the child exists; after merge there is nothing to edit.
    created2 = _split(client, headers, parent_id, _legs("-120.00", "-67.42"))
    child_id = created2.json()["children"][0]["id"]
    recat = client.patch(
        f"{API}/transactions/{child_id}", headers=headers, json={"category_id": groceries}
    )
    assert recat.status_code == 200, recat.text
    child_events = _events(client, headers, child_id)
    assert [event["event_kind"] for event in child_events] == ["categorized"]
    assert child_events[0]["tx_id"] == child_id
    assert child_events[0]["payload_hash"] == _sha(child_events[0]["payload"])
    # The parent's log is its OWN history only — the child's `categorized`
    # receipt is keyed by the child and never appears here.
    assert [event["event_kind"] for event in _events(client, headers, parent_id)] == [
        "split",
        "merge",
        "split",
        "split",
        "created",
    ]


def test_transfer_and_reconciliation_refuse_split_rows(
    client: TestClient, session: Session
) -> None:
    """docs/44 §5 — pairs are statement facts; parts can never be one."""
    headers, email, _ = _register(client)
    owner_user = _user(session, email)
    owner_user_id = owner_user.id
    assert owner_user_id is not None
    account = _account(client, headers, _uniq("acct"), "chequing")
    other = _account(client, headers, _uniq("other"), "chequing")

    # A child whose amount WOULD pair, if children were ever candidates.
    parent = _tx(client, headers, account, "-100.00", "SPLIT ME", day="2026-06-15")
    created = _split(client, headers, parent["id"], _legs("-60.00", "-40.00"))
    assert created.status_code == 201, created.text
    child_ids = {child["id"] for child in created.json()["children"]}

    # +60 would pair with the -60 CHILD if children were ever candidates —
    # that is exactly what must not happen.
    counterpart_60 = _tx(client, headers, other, "60.00", "COUNTERPART 60", day="2026-06-15")
    counterpart_100 = _tx(client, headers, other, "100.00", "COUNTERPART 100", day="2026-06-15")

    for target, expected in (
        (parent["id"], "transaction_is_split"),
        (next(iter(child_ids)), "split_leg_immutable"),
    ):
        detect = client.post(f"{API}/transactions/{target}/detect-transfer", headers=headers)
        assert detect.status_code == 409, detect.text
        assert _code(detect) == expected, target

        link = client.post(
            f"{API}/transactions/{target}/link-transfer",
            headers=headers,
            json={"pair_id": counterpart_100["id"]},
        )
        assert link.status_code == 409, link.text
        assert _code(link) == expected, target

        unlink = client.post(f"{API}/transactions/{target}/unlink-transfer", headers=headers)
        assert unlink.status_code == 409, unlink.text
        assert _code(unlink) == expected, target

    # Suggestion lists are IDENTITY: the parent may appear, a child never does.
    suggestions = client.get(f"{API}/transactions/transfer-suggestions", headers=headers).json()
    assert suggestions["items"], "the fixture must produce at least one real suggestion"
    listed = {
        suggestion[key]
        for suggestion in suggestions["items"]
        for key in ("source_id", "candidate_id")
    }
    assert listed.isdisjoint(child_ids), listed
    assert parent["id"] in listed and counterpart_100["id"] in listed, (
        "IDENTITY: the statement line still pairs with its counterpart"
    )
    assert counterpart_60["id"] not in listed, (
        "the +60 row found no partner — its only possible match is a child"
    )

    # Reconciliation: the PARENT is a legal link target, a child never is.
    item_parent = ReconciliationItem(
        user_id=owner_user_id,
        account_id=account,
        statement_date=date(2026, 6, 15),
        statement_amount=Decimal("-100.00"),
        statement_description="SPLIT ME",
    )
    item_child = ReconciliationItem(
        user_id=owner_user_id,
        account_id=account,
        statement_date=date(2026, 6, 15),
        statement_amount=Decimal("-60.00"),
        statement_description="SPLIT ME",
    )
    session.add_all([item_parent, item_child])
    session.flush()

    ok = client.post(
        f"{API}/accounts/{account}/reconciliation/match",
        headers=headers,
        params={"item_id": item_parent.id, "transaction_id": parent["id"]},
    )
    assert ok.status_code == 200, ok.text

    refused = client.post(
        f"{API}/accounts/{account}/reconciliation/match",
        headers=headers,
        params={"item_id": item_child.id, "transaction_id": next(iter(child_ids))},
    )
    assert refused.status_code == 409, refused.text
    assert _code(refused) == "split_leg_immutable"


def test_perf_list_query_count_unchanged_with_splits(client: TestClient, session: Session) -> None:
    """docs/44 §6.4 — ONE conditional statement, never one per group.

    The docs/34 budget for `GET /transactions?limit=50` is 5 statements
    (M108's receipt-flag fetch is constant per request); a page containing
    children adds exactly ONE — the `LEFT JOIN … GROUP BY` that fetches
    `split_parents` — and no statement may start repeating per row.
    """
    headers, _, _ = _register(client)
    account = _account(client, headers, _uniq("acct"), "chequing")
    for index in range(25):
        _tx(client, headers, account, f"-{index + 1}.00", f"ROW {index}", day="2026-06-15")

    def measure() -> tuple[int, Counter]:
        engine = get_engine()
        statements: list[str] = []

        def before(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()))

        sa_event.listen(engine, "before_cursor_execute", before)
        try:
            for _ in range(2):  # warm-up: the first call pays for session setup
                statements.clear()
                response = client.get(f"{API}/transactions?limit=50", headers=headers)
                assert response.status_code == 200, response.text
        finally:
            sa_event.remove(engine, "before_cursor_execute", before)
        return len(statements), Counter(statements)

    plain_count, plain_stmts = measure()

    split_target = session.exec(
        select(Transaction)
        .where(Transaction.account_id == account)
        .order_by(Transaction.id)
        .limit(1)
    ).one()
    created = _split(client, headers, split_target.id, _legs("-0.30", "-0.40", "-0.30"))
    assert created.status_code == 201, created.text

    split_count, split_stmts = measure()

    assert split_count == plain_count + 1, (
        f"a page with children issued {split_count} statements, without them "
        f"{plain_count} — the split_parents fetch must be ONE conditional "
        f"statement, not a per-group loop\n"
        + "\n".join(f"  {n}x {s[:160]}" for s, n in (split_stmts - plain_stmts).items())
    )
    assert plain_count + 1 <= 6, (
        f"docs/34 ceilings: 5 for the unsplit page + 1 conditional = 6, got {split_count}"
    )
    assert max(split_stmts.values()) <= max(3, max(plain_stmts.values())), (
        "an N+1 shows up as one statement repeating per row"
    )

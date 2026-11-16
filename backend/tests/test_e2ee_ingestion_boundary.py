"""M181 (docs/33 § "OUTSIDE THE ENVELOPE (M79)") — the E2EE ingestion write boundary.

Before this change an `encryption_required` tenant could still obtain plaintext
rows from every server-side ingestion path: OFX/QFX/CSV upload and its commit,
the brokerage trade import and its commit, `POST /trades/record-dividend`, a
sync pull, and manual `POST`/`PATCH /trades` (trades have no cipher columns at
all). Only YNAB's migration upload refused.

This file pins the boundary per path, BOTH ways — the ordinary (non-opted-in)
tenant is exercised in the same parametrized test, so an over-broad guard fails
here as loudly as a missing one:

* the refusal is the established `{detail, code}` envelope with
  `code == "encryption_required"`, raised BEFORE the first mutation;
* **zero** rows appear in `import_batches` / `transactions` / `trades` /
  `securities` / `transaction_events` / `acb_pools` for the refusing tenant;
* the copy names the capability that is unavailable and why — and never offers
  "unlock the vault" as the remedy, because unlocking cannot enable a
  server-side import (docs/33 §Server-first computing rule 7d);
* the documented exceptions stay open: zero-write previews, `discard` /
  `revert`, sync consent + `revoke`, and `DELETE`.

Run: cd backend && uv run pytest -q tests/test_e2ee_ingestion_boundary.py
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session

from app.models.securities import Security
from app.services.sync.provider import register_provider
from app.services.sync.stub import default_stub

API = "/api/v1"
FIXTURES = Path(__file__).parent / "fixtures"
BROKERAGE = FIXTURES / "brokerage"
PASSWORD = "correct-horse-battery"

# The statement fixture's signs already follow the canonical convention, so
# `credits_positive: True` means "pass through" (docs/03 sign conventions) —
# the same profile `tests/test_bank_sync.py` uploads with.
CSV_PROFILE = json.dumps(
    {
        "columns": {
            "date": "Date",
            "amount_native": "Amount",
            "raw_description": "Description",
        },
        "date_format": "%Y-%m-%d",
        "credits_positive": True,
    }
)

#: Every table a refusal must leave untouched, counted per tenant. The shared
#: `fx_rates` cache (docs/02) is deliberately not one of them.
GUARDED_TABLES: tuple[str, ...] = (
    "import_batches",
    "transactions",
    "trades",
    "securities",
    "transaction_events",
    "acb_pools",
)


@pytest.fixture(autouse=True)
def _clean_stub():
    """Fresh provider state per test; the default stub is restored afterwards."""
    stub = default_stub()
    stub.reset()
    yield
    stub.reset()
    register_provider(stub)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _auth(client: TestClient, tag: str) -> tuple[dict[str, str], int]:
    email = f"e2ee-boundary-{tag}-{uuid.uuid4().hex[:8]}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    body = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert body.status_code == 200, body.text
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    me = client.get(f"{API}/users/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, me.json()["id"]


def _set_encryption(client: TestClient, headers: dict[str, str], value: bool) -> None:
    """Flip the opt-in. Always called AFTER any account is created: an enrolled
    tenant's `POST /accounts` wants a `name_cipher` (docs/33 Phase 2)."""
    response = client.patch(f"{API}/users/me", json={"encryption_required": value}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["encryption_required"] is value


def _account(client: TestClient, headers: dict[str, str], type_: str = "chequing") -> dict:
    response = client.post(
        f"{API}/accounts",
        json={"name": f"M181 {type_}", "type": type_, "currency": "CAD"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _security(session: Session, user_id: int, symbol: str = "MSFT") -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} Inc",
        kind="stock",
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _counts(session: Session, user_id: int) -> dict[str, int]:
    """User-scoped row counts for every table the boundary protects."""
    connection = session.connection()
    counts: dict[str, int] = {}
    for table in GUARDED_TABLES:
        sql = f"select count(*) from {table} where user_id = :uid"  # noqa: S608 -- table from fixed tuple in this test
        counts[table] = connection.execute(text(sql), {"uid": user_id}).scalar_one()
    return counts


def _assert_refused(response, *, capability: str) -> None:
    """The one shape every guarded route answers with (docs/08 envelope)."""
    assert response.status_code == 422, response.text
    body = response.json()
    assert set(body) == {"detail", "code"}, body
    assert body["code"] == "encryption_required"
    detail = body["detail"]
    assert capability in detail, detail
    assert "unavailable" in detail, detail
    assert "cannot safely write encrypted data" in detail, detail
    # Rule 7d: a remedy named in copy must actually restore the feature at that
    # moment. Unlocking the vault does NOT enable a server-side import, so the
    # word must not appear anywhere in the refusal.
    assert "unlock" not in detail.lower(), detail


def _statement(client: TestClient, headers: dict[str, str], account_id: int, *, preview: bool):
    url = f"{API}/imports/preview" if preview else f"{API}/imports/upload"
    raw = (FIXTURES / "sync_dup.csv").read_bytes()
    return client.post(
        url,
        headers=headers,
        data={"account_id": str(account_id), "profile_json": CSV_PROFILE},
        files={"file": ("sync_dup.csv", raw, "text/csv")},
    )


def _brokerage(
    client: TestClient,
    headers: dict[str, str],
    account_id: int,
    *,
    preview: bool,
    name: str = "wealthsimple_activity.csv",
    slug: str = "wealthsimple",
):
    url = f"{API}/trades/import/preview" if preview else f"{API}/trades/import"
    raw = (BROKERAGE / name).read_bytes()
    return client.post(
        url,
        headers=headers,
        data={"account_id": str(account_id)},
        params={"profile": slug},
        files={"file": (name, raw, "text/csv")},
    )


def _agreement(client: TestClient, headers: dict[str, str], account_id: int) -> dict:
    response = client.post(
        f"{API}/sync/agreements",
        json={"provider": "stub", "account_ids": [account_id]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _trade_payload(account_id: int, security_id: int) -> dict:
    return {
        "account_id": account_id,
        "security_id": security_id,
        "action": "buy",
        "date": "2025-11-03",
        "quantity": "2",
        "price_native": "100.00",
        "commission": "1.00",
    }


def _dividend_payload(account_id: int, security_id: int) -> dict:
    return {
        "account_id": account_id,
        "security_id": security_id,
        "date": "2025-11-03",
        "gross_amount": "12.50",
        "action": "dividend",
    }


# --------------------------------------------------------------------------- #
# The statement (OFX/QFX/CSV) pipeline
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("encrypted", [False, True])
def test_statement_upload_refuses_an_opted_in_tenant_and_leaves_ordinary_tenants_alone(
    client: TestClient, session: Session, encrypted: bool
) -> None:
    headers, user_id = _auth(client, f"upload-{encrypted}")
    account = _account(client, headers)
    _set_encryption(client, headers, encrypted)
    before = _counts(session, user_id)

    response = _statement(client, headers, account["id"], preview=False)

    if encrypted:
        _assert_refused(response, capability="Importing a statement file")
        assert _counts(session, user_id) == before
        # Nothing was even staged, so the Imports page stays empty.
        assert client.get(f"{API}/imports", headers=headers).json()["total"] == 0
        return

    assert response.status_code == 201, response.text
    after = _counts(session, user_id)
    assert after["import_batches"] == before["import_batches"] + 1
    assert after["transactions"] == before["transactions"] + 2
    assert after["transaction_events"] == before["transaction_events"] + 2


def test_statement_commit_is_refused_once_the_tenant_enrols(
    client: TestClient, session: Session
) -> None:
    """The enrol-after-staging hole: a batch staged while the tenant was
    ordinary cannot be released into the ledger afterwards."""
    headers, user_id = _auth(client, "commit")
    account = _account(client, headers)

    staged = _statement(client, headers, account["id"], preview=False)
    assert staged.status_code == 201, staged.text
    batch_id = staged.json()["id"]
    before = _counts(session, user_id)
    staged_rows = client.get(f"{API}/imports/{batch_id}/rows", headers=headers).json()
    assert len(staged_rows) == 2

    _set_encryption(client, headers, True)

    refused = client.post(f"{API}/imports/{batch_id}/commit", headers=headers)
    _assert_refused(refused, capability="Committing a staged import batch")
    assert _counts(session, user_id) == before
    assert client.get(f"{API}/transactions", headers=headers).json()["total"] == 0
    assert (
        client.get(f"{API}/imports/{batch_id}", headers=headers).json()["status"]
        == "pending_review"
    )

    # The documented exception: removal stays available, and it is the only way
    # to clear rows staged before enrolment.
    discarded = client.post(f"{API}/imports/{batch_id}/discard", headers=headers)
    assert discarded.status_code == 200, discarded.text
    after_discard = _counts(session, user_id)
    assert after_discard["transactions"] == before["transactions"] - 2
    assert after_discard["import_batches"] == before["import_batches"]


# --------------------------------------------------------------------------- #
# The brokerage trade import
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("encrypted", [False, True])
def test_brokerage_import_refuses_an_opted_in_tenant_and_leaves_ordinary_tenants_alone(
    client: TestClient, session: Session, encrypted: bool
) -> None:
    headers, user_id = _auth(client, f"bimport-{encrypted}")
    account = _account(client, headers, type_="brokerage_non_registered")
    _set_encryption(client, headers, encrypted)
    before = _counts(session, user_id)

    response = _brokerage(client, headers, account["id"], preview=False)

    if encrypted:
        # `trades` has no cipher columns at all (docs/33), so the file is
        # refused rather than asked for a cipher.
        _assert_refused(response, capability="Importing a brokerage activity file")
        assert _counts(session, user_id) == before
        return

    assert response.status_code == 201, response.text
    after = _counts(session, user_id)
    assert after["trades"] > before["trades"]
    assert after["securities"] > before["securities"]


def test_brokerage_commit_is_refused_once_the_tenant_enrols(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client, "bcommit")
    account = _account(client, headers, type_="brokerage_non_registered")

    staged = _brokerage(client, headers, account["id"], preview=False)
    assert staged.status_code == 201, staged.text
    batch_id = staged.json()["batch_id"]
    before = _counts(session, user_id)
    assert before["trades"] > 0

    _set_encryption(client, headers, True)

    refused = client.post(f"{API}/trades/import/{batch_id}/commit", headers=headers)
    _assert_refused(refused, capability="Committing a staged trade import")
    assert _counts(session, user_id) == before
    # Committed-only readers see nothing new: the batch never left review.
    assert client.get(f"{API}/trades?committed_only=true", headers=headers).json()["total"] == 0
    assert client.get(f"{API}/trades", headers=headers).json()["total"] == before["trades"]

    discarded = client.post(f"{API}/trades/import/{batch_id}/discard", headers=headers)
    assert discarded.status_code == 200, discarded.text
    assert _counts(session, user_id)["trades"] == 0


# --------------------------------------------------------------------------- #
# Manual trade writes (trades have no cipher columns)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("encrypted", [False, True])
def test_manual_trade_create_refuses_an_opted_in_tenant_and_leaves_ordinary_tenants_alone(
    client: TestClient, session: Session, encrypted: bool
) -> None:
    headers, user_id = _auth(client, f"trade-{encrypted}")
    account = _account(client, headers, type_="brokerage_non_registered")
    security = _security(session, user_id)
    _set_encryption(client, headers, encrypted)
    before = _counts(session, user_id)

    response = client.post(
        f"{API}/trades", json=_trade_payload(account["id"], security.id), headers=headers
    )

    if encrypted:
        _assert_refused(response, capability="Recording a trade")
        assert _counts(session, user_id) == before
        assert client.get(f"{API}/trades", headers=headers).json()["total"] == 0
        return

    assert response.status_code == 201, response.text
    assert _counts(session, user_id)["trades"] == before["trades"] + 1


def test_trade_patch_is_refused_once_the_tenant_enrols_and_delete_stays_open(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client, "trade-patch")
    account = _account(client, headers, type_="brokerage_non_registered")
    security = _security(session, user_id)
    created = client.post(
        f"{API}/trades", json=_trade_payload(account["id"], security.id), headers=headers
    )
    assert created.status_code == 201, created.text
    trade_id = created.json()["id"]

    _set_encryption(client, headers, True)
    before = _counts(session, user_id)

    refused = client.patch(f"{API}/trades/{trade_id}", json={"quantity": "9"}, headers=headers)
    _assert_refused(refused, capability="Editing a trade")
    assert _counts(session, user_id) == before
    unchanged = client.get(f"{API}/trades/{trade_id}", headers=headers).json()
    # The stored quantity is untouched: the PATCH never reached the row.
    assert Decimal(unchanged["quantity"]) == Decimal("2")

    # Removal-only paths stay available (docs/33 exclusion table).
    deleted = client.delete(f"{API}/trades/{trade_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    assert _counts(session, user_id)["trades"] == before["trades"] - 1


# --------------------------------------------------------------------------- #
# Recorded dividends (they create a plaintext Transaction row)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("encrypted", [False, True])
def test_record_dividend_refuses_an_opted_in_tenant_and_leaves_ordinary_tenants_alone(
    client: TestClient, session: Session, encrypted: bool
) -> None:
    headers, user_id = _auth(client, f"dividend-{encrypted}")
    account = _account(client, headers, type_="brokerage_non_registered")
    security = _security(session, user_id)
    _set_encryption(client, headers, encrypted)
    before = _counts(session, user_id)

    response = client.post(
        f"{API}/trades/record-dividend",
        json=_dividend_payload(account["id"], security.id),
        headers=headers,
    )

    if encrypted:
        _assert_refused(response, capability="Recording a dividend")
        # The whole atomic bundle is missing: trade + transaction + receipt,
        # and no ACB pool was written either.
        assert _counts(session, user_id) == before
        assert client.get(f"{API}/transactions", headers=headers).json()["total"] == 0
        return

    assert response.status_code == 201, response.text
    after = _counts(session, user_id)
    assert after["trades"] == before["trades"] + 1
    assert after["transactions"] == before["transactions"] + 1
    assert after["transaction_events"] == before["transaction_events"] + 1


# --------------------------------------------------------------------------- #
# Bank sync (pull refuses; consent and revoke stay available)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("encrypted", [False, True])
def test_sync_pull_refuses_an_opted_in_tenant_and_leaves_ordinary_tenants_alone(
    client: TestClient, session: Session, encrypted: bool
) -> None:
    headers, user_id = _auth(client, f"pull-{encrypted}")
    account = _account(client, headers)
    _set_encryption(client, headers, encrypted)
    # Consent is metadata, not a row of user content: it stays available.
    agreement = _agreement(client, headers, account["id"])
    before = _counts(session, user_id)

    response = client.post(f"{API}/sync/agreements/{agreement['id']}/pull", headers=headers)

    if encrypted:
        _assert_refused(response, capability="Pulling transactions from a bank provider")
        assert _counts(session, user_id) == before
        assert client.get(f"{API}/imports", headers=headers).json()["total"] == 0
        revoked = client.post(f"{API}/sync/agreements/{agreement['id']}/revoke", headers=headers)
        assert revoked.status_code == 200, revoked.text
        return

    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 3
    after = _counts(session, user_id)
    assert after["import_batches"] == before["import_batches"] + 1
    assert after["transactions"] == before["transactions"] + 3


# --------------------------------------------------------------------------- #
# The documented exceptions
# --------------------------------------------------------------------------- #


def test_zero_write_previews_stay_available_for_an_opted_in_tenant(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client, "previews")
    statements = _account(client, headers)
    brokerage = _account(client, headers, type_="brokerage_non_registered")
    _set_encryption(client, headers, True)
    before = _counts(session, user_id)

    dry_run = _statement(client, headers, statements["id"], preview=True)
    assert dry_run.status_code == 200, dry_run.text
    assert dry_run.json()["rows"]

    suggested = client.post(
        f"{API}/imports/suggest-profile",
        headers=headers,
        files={"file": ("sync_dup.csv", (FIXTURES / "sync_dup.csv").read_bytes(), "text/csv")},
    )
    assert suggested.status_code == 200, suggested.text
    assert suggested.json()["columns"]

    trade_dry_run = _brokerage(client, headers, brokerage["id"], preview=True)
    assert trade_dry_run.status_code == 201, trade_dry_run.text

    assert _counts(session, user_id) == before


def test_the_guard_runs_after_the_ownership_check_never_across_tenants(
    client: TestClient,
) -> None:
    """A foreign account id answers 404 before the flag is consulted, so an
    enrolled caller learns nothing about another tenant's rows (docs/02)."""
    owner_headers, _ = _auth(client, "owner")
    owner_account = _account(client, owner_headers)

    other_headers, _ = _auth(client, "other")
    _set_encryption(client, other_headers, True)

    foreign = _statement(client, other_headers, owner_account["id"], preview=False)
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["code"] == "not_found"

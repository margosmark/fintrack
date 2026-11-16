"""M106 — per-transaction review state + the append-only activity log.

Specs pinned here (docs/02 §transactions / §transaction_events, docs/08,
docs/09 §Review queue + §History drawer, docs/11 export/E2EE, docs/43 §6):

* every mutation path writes EXACTLY one receipt per affected transaction —
  not zero, not two — and a no-op writes none;
* `payload_hash` is the canonical `mcp_audit.args_hash` sha256, hand-recomputed
  below against literals (a hash nobody can re-derive is decoration);
* the "Needs review" filter returns exactly the unreviewed set in scope, and
  its count is the same number the chip shows, live;
* `/export/full` (the E2EE vault's payload) carries `transaction_events` —
  including through the encrypted-artifact round trip — and the partner
  projection shows owner/partner receipts only, never a system or a third
  party's, and never a payload;
* pre-handler refusals (422 body, 429 limiter) write nothing — the M91-6
  ruling (docs/43 §3) applied to this log too;
* `0039_tx_review_events` itself reverses and re-applies cleanly, and a row
  that existed BEFORE it ran lands `reviewed_at IS NULL` (no backfill);
* the drawer's two acceptance clauses: a receipt set belongs to ONE row of
  ONE owner (no cross-transaction line can reach a drawer), and the sentence
  is rendered from the stored payload at read time, so a unicode body arrives
  as the exact characters the user typed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sys
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlmodel import select

from alembic import command
from app.core.ratelimit import limiter
from app.models.categories import Category
from app.models.learned_payees import LearnedPayee
from app.models.llm_suggestions import LlmSuggestion, SuggestionStatus
from app.models.transaction_events import TransactionEvent, TransactionEventKind
from app.models.transactions import Transaction
from tests.test_cat_common import (
    auth,
    autopilot_account,
    background_session_fixture,
    category_by_name,
    create_account,
    fake_provider_fixture,
)

# pytest registers a fixture from ANY module attribute carrying its marker, so
# importing these two IS the registration the M120 layer-3 test below relies
# on. Naming them here keeps the import from reading as dead (ruff F401).
M120_PIPELINE_FIXTURES = (fake_provider_fixture, background_session_fixture)

API = "/api/v1"
FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "correct-horse-battery"

# Hand-computed with the pinned formula (see
# `test_payload_hash_matches_hand_recomputed_digests`).
SHA_MANUAL_CREATE = "da17572bad6935cc6ee080013fb4a7f83c932efa8daac417aa1da123cf6453fc"
SHA_UNICODE_EDIT = "868457cbf2288e3b176f290f120aa0b4efdc69c9900d27b77e62f3665870f0bc"
SHA_REVIEWED = "ee7d43e0ebe6733bffd85a23e85bfaa73868814803a27ce15051c2e33aa785ea"

UNICODE_PAYEE = "Café Münster 😀"
# A category name the drawer has to print back verbatim: accents, an emoji and
# a non-Latin script in one string (the three ways a naive encode() breaks).
UNICODE_CATEGORY = "Café ☕ 食品 😀"


def _sha(payload: Any) -> str:
    """The pinned formula: unsalted sha256 over the canonical JSON."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _register(client: TestClient, email: str, name: str) -> tuple[dict[str, str], int]:
    client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    body = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert body.status_code == 200, body.text
    headers = auth(body.json()["access_token"])
    # A name so the drawer reads "Mark edited payee", not "user #7 edited payee".
    patched = client.patch(f"{API}/users/me/profile", headers=headers, json={"name": name})
    assert patched.status_code == 200, patched.text
    return headers, int(patched.json()["id"])


def _create(
    client: TestClient,
    headers: dict[str, str],
    account_id: int,
    payee: str = "Loblaws",
    amount: str = "-42.00",
) -> dict:
    response = client.post(
        f"{API}/transactions",
        headers=headers,
        json={
            "account_id": account_id,
            "date": "2026-08-01",
            "raw_description": payee,
            "amount_native": amount,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _events(client: TestClient, headers: dict[str, str], tx_id: int) -> list[dict]:
    response = client.get(f"{API}/transactions/{tx_id}/events", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "total"}
    assert body["total"] == len(body["items"])
    return body["items"]


def _kinds(client: TestClient, headers: dict[str, str], tx_id: int) -> list[str]:
    return [event["event_kind"] for event in _events(client, headers, tx_id)]


def _db_events(session: Session, tx_id: int) -> list[TransactionEvent]:
    """Receipts straight from the table, oldest first.

    Used for the `deleted` case: once the row is gone the drawer route 404s
    (there is nothing left to open), so the only honest reader is the table.
    """
    return list(
        session.exec(
            select(TransactionEvent)
            .where(TransactionEvent.tx_id == tx_id)
            .order_by(TransactionEvent.id)
        ).all()
    )


def _count(client: TestClient, headers: dict[str, str], **params: str) -> int:
    query = "&".join(f"{key}={value}" for key, value in params.items())
    suffix = f"?{query}" if query else ""
    response = client.get(f"{API}/transactions/unreviewed-count{suffix}", headers=headers)
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"count"}
    return response.json()["count"]


# --- 1. every mutation path, exactly one receipt -----------------------------


def test_every_mutation_path_writes_exactly_one_receipt(
    client: TestClient, session: Session
) -> None:
    headers, _uid = _register(client, "m106-owner@example.com", "Mark")
    account_id = create_account(client, headers)
    groceries = category_by_name(session, "groceries", _uid)
    tag = client.post(f"{API}/tags", headers=headers, json={"name": "pantry"})
    assert tag.status_code == 201, tag.text
    tag_id = tag.json()["id"]

    # created — and review state defaults to NULL (null = unreviewed).
    row = _create(client, headers, account_id)
    tx_id = row["id"]
    assert row["reviewed_at"] is None and row["reviewed_by"] is None
    assert _kinds(client, headers, tx_id) == ["created"]
    assert _events(client, headers, tx_id)[0]["payload"] == {"source": "manual"}
    assert _events(client, headers, tx_id)[0]["summary"] == "you created this transaction"

    # edited.
    edited = client.patch(
        f"{API}/transactions/{tx_id}",
        headers=headers,
        json={"normalized_payee": UNICODE_PAYEE},
    )
    assert edited.status_code == 200, edited.text
    assert _kinds(client, headers, tx_id) == ["edited", "created"]
    # A PATCH that re-sends the same value changes nothing → no receipt.
    client.patch(
        f"{API}/transactions/{tx_id}",
        headers=headers,
        json={"normalized_payee": UNICODE_PAYEE},
    )
    assert len(_kinds(client, headers, tx_id)) == 2

    # categorized (its own kind, not a generic edit).
    categorized = client.patch(
        f"{API}/transactions/{tx_id}",
        headers=headers,
        json={"category_id": groceries.id},
    )
    assert categorized.status_code == 200, categorized.text
    top = _events(client, headers, tx_id)[0]
    assert top["event_kind"] == "categorized"
    assert top["payload"]["category_name"] == "groceries"
    assert top["summary"] == "you set category groceries"
    client.patch(
        f"{API}/transactions/{tx_id}",
        headers=headers,
        json={"category_id": groceries.id},
    )
    assert len(_kinds(client, headers, tx_id)) == 3

    # tagged — PUT replace, then the two single-tag routes.
    put = client.put(
        f"{API}/transactions/{tx_id}/tags",
        headers=headers,
        json={"tag_ids": [tag_id]},
    )
    assert put.status_code == 200, put.text
    assert _events(client, headers, tx_id)[0]["payload"]["added"] == ["pantry"]
    assert len(_kinds(client, headers, tx_id)) == 4
    # Same set again → the junction did not move → no receipt.
    client.put(f"{API}/transactions/{tx_id}/tags", headers=headers, json={"tag_ids": [tag_id]})
    assert len(_kinds(client, headers, tx_id)) == 4
    # Re-attaching a tag the row already carries → ON CONFLICT, no receipt.
    client.post(f"{API}/transactions/{tx_id}/tags", headers=headers, json={"tag_id": tag_id})
    assert len(_kinds(client, headers, tx_id)) == 4

    detached = client.delete(f"{API}/transactions/{tx_id}/tags/{tag_id}", headers=headers)
    assert detached.status_code in (200, 204), detached.text
    assert _events(client, headers, tx_id)[0]["payload"]["removed"] == ["pantry"]
    assert len(_kinds(client, headers, tx_id)) == 5
    # Detaching a tag the row never carried → no receipt.
    client.delete(f"{API}/transactions/{tx_id}/tags/{tag_id}", headers=headers)
    assert len(_kinds(client, headers, tx_id)) == 5

    # reviewed — a flip, and only a flip.
    flipped = client.post(
        f"{API}/transactions/{tx_id}/review",
        headers=headers,
        json={"reviewed": True},
    )
    assert flipped.status_code == 200, flipped.text
    assert flipped.json()["reviewed_at"] is not None
    assert flipped.json()["reviewed_by"] is not None
    assert _events(client, headers, tx_id)[0]["event_kind"] == "reviewed"
    assert _events(client, headers, tx_id)[0]["payload"] == {"reviewed": True}
    assert _events(client, headers, tx_id)[0]["summary"] == "you marked this reviewed"
    client.post(f"{API}/transactions/{tx_id}/review", headers=headers, json={"reviewed": True})
    assert len(_kinds(client, headers, tx_id)) == 6
    unflipped = client.post(
        f"{API}/transactions/{tx_id}/review",
        headers=headers,
        json={"reviewed": False},
    )
    assert unflipped.json()["reviewed_at"] is None
    assert unflipped.json()["reviewed_by"] is None
    assert _events(client, headers, tx_id)[0]["payload"] == {"reviewed": False}
    assert len(_kinds(client, headers, tx_id)) == 7

    # deleted — the receipt outlives the row (tx_id carries no FK).
    before = _db_events(session, tx_id)
    assert client.delete(f"{API}/transactions/{tx_id}", headers=headers).status_code == 204
    after = _db_events(session, tx_id)
    assert len(after) == len(before) + 1
    assert after[-1].event_kind == TransactionEventKind.deleted
    assert after[-1].payload["payee"] == UNICODE_PAYEE
    # The drawer route now 404s: there is no row left to open, which is why
    # the receipt has to survive on its own.
    assert client.get(f"{API}/transactions/{tx_id}/events", headers=headers).status_code == 404
    # ...and the export still carries it.
    snapshot = client.get(f"{API}/export/full", headers=headers).json()
    deleted_rows = [
        event for event in snapshot["transaction_events"] if event["event_kind"] == "deleted"
    ]
    assert [event["tx_id"] for event in deleted_rows] == [tx_id]


def test_transfer_pairing_receipts_every_moved_row(client: TestClient, session: Session) -> None:
    """The four direct-mutation transfer routes receipt each row they move.

    `accept-transfer-suggestion` / `detect-transfer` / `link-transfer` /
    `unlink-transfer` do NOT go through PATCH, so each carries its own writer
    (docs/02 §transaction_events). Two rows move, two receipts land — and an
    already-unpaired unlink is a no-op that writes none.
    """
    headers, _uid = _register(client, "m106-transfer@example.com", "Mark")
    account_a = create_account(client, headers, "Probe Chequing")
    account_b = create_account(client, headers, "Probe Savings")
    source = _create(client, headers, account_a, payee="Transfer out", amount="-100.00")
    candidate = _create(client, headers, account_b, payee="Transfer in", amount="100.00")

    # --- accept: pairs BOTH rows and moves both to `transfers` --------------
    accepted = client.post(
        f"{API}/transactions/accept-transfer-suggestion",
        headers=headers,
        json={"source_id": source["id"], "candidate_id": candidate["id"]},
    )
    assert accepted.status_code == 200, accepted.text
    for row, counterpart in ((source, candidate["id"]), (candidate, source["id"])):
        assert _kinds(client, headers, row["id"]) == ["edited", "created"]
        top = _events(client, headers, row["id"])[0]
        assert top["summary"] == "you linked a transfer"
        assert top["payload"]["transfer_pair_id"] == counterpart
        assert top["payload"]["category_name"] == "transfers"
        assert top["payload"]["fields"] == ["transfer_pair_id", "category_id"]

    # --- unlink: both halves again, and only the ones that actually moved ---
    unlinked = client.post(f"{API}/transactions/{source['id']}/unlink-transfer", headers=headers)
    assert unlinked.status_code == 200, unlinked.text
    for row in (source, candidate):
        assert _kinds(client, headers, row["id"]) == ["edited", "edited", "created"]
        top = _events(client, headers, row["id"])[0]
        assert top["summary"] == "you unlinked a transfer"
        assert top["payload"]["transfer_pair_id"] is None

    lone = _create(client, headers, account_a, payee="Lone", amount="-5.00")
    assert (
        client.post(f"{API}/transactions/{lone['id']}/unlink-transfer", headers=headers).status_code
        == 200
    )
    assert _kinds(client, headers, lone["id"]) == ["created"]  # no-op, no receipt

    # --- link: the manual route, both rows ----------------------------------
    linked = client.post(
        f"{API}/transactions/{source['id']}/link-transfer",
        headers=headers,
        json={"pair_id": candidate["id"]},
    )
    assert linked.status_code == 200, linked.text
    for row, counterpart in ((source, candidate["id"]), (candidate, source["id"])):
        kinds = _kinds(client, headers, row["id"])
        assert kinds == ["edited", "edited", "edited", "created"]
        assert _events(client, headers, row["id"])[0]["payload"]["transfer_pair_id"] == counterpart

    # --- detect: auto-match by opposite amount, both rows --------------------
    first = _create(client, headers, account_a, payee="Detect out", amount="-75.00")
    second = _create(client, headers, account_b, payee="Detect in", amount="75.00")
    transfers = category_by_name(session, "transfers", _uid)
    categorized = client.patch(
        f"{API}/transactions/{second['id']}",
        headers=headers,
        json={"category_id": transfers.id},
    )
    assert categorized.status_code == 200, categorized.text
    detected = client.post(f"{API}/transactions/{first['id']}/detect-transfer", headers=headers)
    assert detected.status_code == 200, detected.text
    assert _kinds(client, headers, first["id"]) == ["edited", "created"]
    assert _kinds(client, headers, second["id"]) == ["edited", "categorized", "created"]
    assert _events(client, headers, first["id"])[0]["summary"] == "you linked a transfer"
    assert _events(client, headers, first["id"])[0]["payload"]["transfer_pair_id"] == second["id"]


def test_payload_hash_matches_hand_recomputed_digests(client: TestClient, session: Session) -> None:
    """Two digests recomputed by hand from the pinned formula (docs/02).

    A hash nobody can re-derive is decoration; these literals are what make
    `payload_hash` a check rather than a claim.
    """
    headers, _uid = _register(client, "m106-hash@example.com", "Mark")
    account_id = create_account(client, headers)

    row = _create(client, headers, account_id)
    tx_id = row["id"]
    created = _events(client, headers, tx_id)[0]
    assert created["payload"] == {"source": "manual"}
    assert created["payload_hash"] == SHA_MANUAL_CREATE
    assert created["payload_hash"] == _sha({"source": "manual"})

    # (1) a unicode body — the exact bytes the drawer renders.
    client.patch(
        f"{API}/transactions/{tx_id}",
        headers=headers,
        json={"normalized_payee": UNICODE_PAYEE},
    )
    edit = _events(client, headers, tx_id)[0]
    assert edit["payload"] == {
        "fields": ["normalized_payee"],
        "changes": {"normalized_payee": UNICODE_PAYEE},
    }
    assert edit["payload_hash"] == SHA_UNICODE_EDIT
    assert edit["payload_hash"] == _sha(edit["payload"])

    # (2) the review receipt.
    client.post(f"{API}/transactions/{tx_id}/review", headers=headers, json={"reviewed": True})
    review = _events(client, headers, tx_id)[0]
    assert review["payload_hash"] == SHA_REVIEWED
    assert review["payload_hash"] == _sha({"reviewed": True})

    # Shape is stable: every field the drawer needs, and nothing extra.
    assert set(review) == {
        "id",
        "tx_id",
        "event_kind",
        "actor",
        "actor_label",
        "at",
        "payload_hash",
        "summary",
        "payload",
    }
    assert review["actor_label"] == "you"
    assert review["tx_id"] == tx_id
    assert review["at"] is not None


# --- 2. the "Needs review" queue --------------------------------------------


def test_review_queue_filter_and_count_match_and_update_live(
    client: TestClient, session: Session
) -> None:
    headers, uid = _register(client, "m106-queue@example.com", "Mark")
    account_a = create_account(client, headers)
    account_b = create_account(client, headers, name="Savings")
    first = _create(client, headers, account_a, payee="Loblaws")
    second = _create(client, headers, account_b, payee="Payroll")
    third = _create(client, headers, account_b, payee="Hydro")
    fourth = _create(client, headers, account_a, payee="Gas")

    # Review state written directly (the shape an import leaves behind):
    # NEITHER column set, which is what makes a queue form by itself.
    staged = session.get(Transaction, fourth["id"])
    assert staged is not None
    staged.reviewed_at = None
    staged.reviewed_by = None
    session.commit()

    assert _count(client, headers) == 4

    # The filter returns EXACTLY the unreviewed set, in the current scope.
    page = client.get(f"{API}/transactions?reviewed=false", headers=headers).json()
    assert page["total"] == 4
    assert {item["id"] for item in page["items"]} == {
        first["id"],
        second["id"],
        third["id"],
        fourth["id"],
    }
    assert _count(client, headers) == page["total"]

    # Scoped: one account only, and the count follows the same predicate.
    scoped = client.get(
        f"{API}/transactions?reviewed=false&account_id={account_b}", headers=headers
    ).json()
    assert {item["id"] for item in scoped["items"]} == {second["id"], third["id"]}
    assert _count(client, headers, account_id=str(account_b)) == scoped["total"] == 2

    # `reviewed=true` is its own set (the inverse chip).
    assert client.get(f"{API}/transactions?reviewed=true", headers=headers).json()["total"] == 0

    # Flip one → both numbers move, immediately.
    flipped = client.post(
        f"{API}/transactions/{first['id']}/review",
        headers=headers,
        json={"reviewed": True},
    )
    assert flipped.status_code == 200, flipped.text
    assert flipped.json()["reviewed_by"] == uid
    assert _count(client, headers) == 3
    after = client.get(f"{API}/transactions?reviewed=false", headers=headers).json()
    assert after["total"] == 3
    assert first["id"] not in {item["id"] for item in after["items"]}
    assert _count(client, headers) == after["total"]

    # Flip it back → the queue is whole again (an unreview IS a real flip).
    client.post(
        f"{API}/transactions/{first['id']}/review",
        headers=headers,
        json={"reviewed": False},
    )
    assert _count(client, headers) == 4

    # A row marked reviewed outside the route leaves the queue too.
    staged.reviewed_at = datetime(2026, 8, 2, tzinfo=UTC)
    staged.reviewed_by = uid
    session.commit()
    assert _count(client, headers) == 3
    assert client.get(f"{API}/transactions?reviewed=true", headers=headers).json()["total"] == 1


def test_bulk_review_leaves_one_receipt_per_row(client: TestClient, session: Session) -> None:
    """The M96 multi-select toolbar drives the single-row route sequentially
    (docs/09 §Bulk selection toolbar), so a bulk run is N flips → N receipts."""
    headers, _uid = _register(client, "m106-bulk@example.com", "Mark")
    account_id = create_account(client, headers)
    ids = [_create(client, headers, account_id, payee=f"Row {index}")["id"] for index in range(3)]
    assert _count(client, headers) == 3

    for tx_id in ids:
        response = client.post(
            f"{API}/transactions/{tx_id}/review",
            headers=headers,
            json={"reviewed": True},
        )
        assert response.status_code == 200, response.text

    assert _count(client, headers) == 0
    for tx_id in ids:
        assert _kinds(client, headers, tx_id) == ["reviewed", "created"]
        assert len(_db_events(session, tx_id)) == 2

    # Re-running the same bulk action is a no-op in the log too.
    for tx_id in ids:
        client.post(f"{API}/transactions/{tx_id}/review", headers=headers, json={"reviewed": True})
    for tx_id in ids:
        assert len(_db_events(session, tx_id)) == 2


# --- 3. import-batch seeding -------------------------------------------------


def _account_with_institution(client: TestClient, headers: dict[str, str]) -> int:
    institution = client.post(f"{API}/institutions", headers=headers, json={"name": "Test Bank"})
    assert institution.status_code == 201, institution.text
    account = client.post(
        f"{API}/accounts",
        headers=headers,
        json={
            "name": "Checking",
            "type": "chequing",
            "currency": "CAD",
            "institution_id": institution.json()["id"],
        },
    )
    assert account.status_code == 201, account.text
    return account.json()["id"]


def test_import_seeds_a_review_queue_and_created_receipts(
    client: TestClient, session: Session
) -> None:
    """A committed import lands unreviewed, so a queue forms by itself — and
    every row's drawer opens on "created from import Batch #N" (docs/02)."""
    headers, uid = _register(client, "m106-import@example.com", "Mark")
    account_id = _account_with_institution(client, headers)
    upload = client.post(
        f"{API}/imports/upload",
        headers=headers,
        data={"account_id": str(account_id)},
        files={"file": ("sample.qfx", (FIXTURES / "sample.qfx").read_bytes(), "app/x")},
    )
    assert upload.status_code == 201, upload.text
    batch_id = upload.json()["id"]

    # Staged rows are invisible (docs/03), so the queue is empty until commit.
    assert _count(client, headers) == 0
    assert client.post(f"{API}/imports/{batch_id}/commit", headers=headers).status_code == 200

    page = client.get(f"{API}/transactions", headers=headers).json()
    assert page["total"] > 0
    # Every committed row is unreviewed → the count is the whole page.
    assert _count(client, headers) == page["total"]
    assert (
        _count(client, headers)
        == client.get(f"{API}/transactions?reviewed=false", headers=headers).json()["total"]
    )

    for item in page["items"]:
        events = _events(client, headers, item["id"])
        assert [event["event_kind"] for event in events] == ["created"]
        assert item["reviewed_at"] is None
        assert events[0]["payload"] == {
            "source": "import",
            "batch_id": batch_id,
            "file_name": "sample.qfx",
            "source_label": "File",
        }
        assert events[0]["actor"] == uid
        assert events[0]["summary"] == f"you created this from import Batch #{batch_id}"
        assert events[0]["payload_hash"] == _sha(events[0]["payload"])


# --- 4. exports / E2EE / the partner projection ------------------------------


def test_export_full_carries_transaction_events_and_zip_documents_the_filter(
    client: TestClient, session: Session
) -> None:
    """`/export/full` is the E2EE vault's payload byte-for-byte (docs/11), so
    covering it here covers the encrypted browser backup too."""
    headers, uid = _register(client, "m106-export@example.com", "Mark")
    account_id = create_account(client, headers)
    created = _create(client, headers, account_id)
    tx_id = created["id"]
    client.patch(f"{API}/transactions/{tx_id}", headers=headers, json={"amount_native": "-55.55"})

    snapshot = client.get(f"{API}/export/full", headers=headers).json()
    assert "transaction_events" in snapshot
    events = snapshot["transaction_events"]
    assert {event["event_kind"] for event in events} >= {"created", "edited"}
    assert all(event["user_id"] == uid for event in events)
    for event in events:
        # Hash and detail are written together, so the export is re-checkable.
        assert event["payload_hash"] == _sha(event["payload"])

    # The ZIP publishes the filter it applied (docs/11 § visibility).
    zip_response = client.get(f"{API}/export/zip", headers=headers)
    assert zip_response.status_code == 200, zip_response.text
    with zipfile.ZipFile(io.BytesIO(zip_response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert "transaction_events" in manifest["visibility_filters"]
        assert "transaction_events.csv" in archive.namelist()


def test_partner_projection_boundary(client: TestClient, session: Session) -> None:
    """docs/43 §6: the partner sees owner/partner receipts only — not a
    cloud-service one, not a third party's — and never a payload (money rides
    in payloads). The owner, by contrast, sees the whole log."""
    owner_headers, owner_id = _register(client, "m106-owner2@example.com", "Owner Mark")
    partner_headers, partner_id = _register(client, "m106-partner@example.com", "Partner Pat")
    stranger_headers, stranger_id = _register(client, "m106-stranger@example.com", "Stranger")
    account_id = create_account(client, owner_headers)

    # A synced row: created by the cloud service (actor NULL), then edited by
    # the owner — exactly the two receipts a partner's view must split.
    synced = Transaction(
        user_id=owner_id,
        account_id=account_id,
        date=datetime(2026, 8, 3).date(),
        raw_description="SYNC LATTE",
        normalized_payee="Latte",
        amount_native=Decimal("-99.99"),
        currency="CAD",
        dedup_hash="m106-synced-row",
    )
    session.add(synced)
    session.commit()
    session.refresh(synced)
    tx_id = synced.id
    assert tx_id is not None
    session.add(
        TransactionEvent(
            tx_id=tx_id,
            user_id=owner_id,
            event_kind=TransactionEventKind.created,
            payload={"source": "sync", "batch_id": 41},
            payload_hash=_sha({"source": "sync", "batch_id": 41}),
            actor=None,
        )
    )
    session.commit()

    owner_edit = client.patch(
        f"{API}/transactions/{tx_id}",
        headers=owner_headers,
        json={"amount_native": "-12.34"},
    )
    assert owner_edit.status_code == 200, owner_edit.text

    # The OTHER two receipts the boundary has to split. A partner is read-only
    # in v1 (docs/43 §1 "partner is read-only, full stop"), so the app cannot
    # produce a partner-authored receipt yet — and the filter must not depend
    # on that accident: if a partner write ever lands, "partner did X" has to
    # show. Same for a third party's receipt on a visible row: no link, no
    # exposure, whatever `tx_id` says.
    partner_payload = {"category_id": 7, "category_name": "Groceries"}
    session.add(
        TransactionEvent(
            tx_id=tx_id,
            user_id=owner_id,
            event_kind=TransactionEventKind.categorized,
            payload=partner_payload,
            payload_hash=_sha(partner_payload),
            actor=partner_id,
        )
    )
    stranger_payload = {"fields": ["amount_native"], "changes": {"amount_native": "-88.88"}}
    session.add(
        TransactionEvent(
            tx_id=tx_id,
            user_id=owner_id,
            event_kind=TransactionEventKind.edited,
            payload=stranger_payload,
            payload_hash=_sha(stranger_payload),
            actor=stranger_id,
        )
    )
    session.commit()

    invite = client.post(
        f"{API}/sharing/invites",
        headers=owner_headers,
        json={"email": "m106-partner@example.com", "account_ids": [account_id]},
    )
    assert invite.status_code == 201, invite.text
    accepted = client.post(
        f"{API}/sharing/invites/accept",
        headers=partner_headers,
        json={"token": invite.json()["token"]},
    )
    assert accepted.status_code == 200, accepted.text

    # Owner sees everything, payload included — all four receipts, whoever
    # wrote them (system, owner, partner, stranger), newest first.
    owner_events = _events(client, owner_headers, tx_id)
    assert [event["actor"] for event in owner_events] == [
        stranger_id,
        partner_id,
        owner_id,
        None,
    ]
    assert [event["event_kind"] for event in owner_events] == [
        "edited",
        "categorized",
        "edited",
        "created",
    ]
    assert any(
        event["payload"].get("changes", {}).get("amount_native") == "-12.34"
        for event in owner_events
    )
    assert any(event["payload"].get("source") == "sync" for event in owner_events)

    # Partner: owner/partner receipts only, in that boundary's own order, and
    # no payload on any of them.
    response = client.get(f"{API}/partner/transactions/{tx_id}/events", headers=partner_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(body["items"])
    assert [event["event_kind"] for event in body["items"]] == ["categorized", "edited"]
    assert {event["actor"] for event in body["items"]} == {partner_id, owner_id}
    assert all("payload" not in event for event in body["items"])

    # "partner did X" reads from the partner's own chair …
    partner_line = next(event for event in body["items"] if event["actor"] == partner_id)
    assert partner_line["summary"] == "you set category Groceries"
    # … "owner did X" names the owner, with the field names but never values.
    owner_line = next(event for event in body["items"] if event["actor"] == owner_id)
    assert owner_line["summary"] == "Owner Mark edited amount"
    assert owner_line["changed_fields"] == ["amount_native"]

    # The system receipt and the stranger's are gone entirely: no machine
    # narration, no third party, and no money — the owner's, the stranger's,
    # or the sync row's — ever crosses the seam (docs/43 §9).
    assert "sync" not in response.text
    assert "88.88" not in response.text
    assert "12.34" not in response.text
    assert "99.99" not in response.text
    assert "stranger" not in response.text.lower()

    # A row outside the partner's scope answers 404, same as an unknown id.
    hidden = create_account(client, owner_headers, name="Hidden")
    hidden_row = _create(client, owner_headers, hidden, payee="Secret")
    outside = client.get(
        f"{API}/partner/transactions/{hidden_row['id']}/events", headers=partner_headers
    )
    assert outside.status_code == 404
    assert outside.json()["code"] == "not_found"

    # A caller with no link at all is refused uniformly (docs/43 §7) — the
    # same stranger whose receipt is filtered out above.
    refused = client.get(f"{API}/partner/transactions/{tx_id}/events", headers=stranger_headers)
    assert refused.status_code == 401
    assert refused.json()["code"] == "sharing_not_linked"


# --- 5. pre-handler refusals write nothing (the M91-6 ruling) ---------------


def test_pre_handler_refusals_write_no_receipt(client: TestClient, session: Session) -> None:
    """422 body validation and the 429 limiter are answered BEFORE a handler
    exists to attribute a row to, so neither leaves a receipt (docs/43 §3)."""
    headers, _uid = _register(client, "m106-prehandler@example.com", "Mark")
    account_id = create_account(client, headers)

    def receipt_rows() -> int:
        return session.exec(select(func.count()).select_from(TransactionEvent)).one()

    assert receipt_rows() == 0

    # (a) Body validation — FastAPI answers 422 before `create_transaction`.
    rejected = client.post(
        f"{API}/transactions",
        headers=headers,
        json={
            "account_id": account_id,
            "date": "2026-08-01",
            "raw_description": "Ghost",
            "amount_native": "-1.00",
            "unexpected": 1,
        },
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "validation_error"
    assert receipt_rows() == 0

    # (b) The rate limiter — its decorator answers 429 before the handler.
    #     IMPORT_UPLOAD_LIMIT is 6/minute (docs/40 §11 / core.ratelimit).
    limiter.reset()
    limiter.enabled = True
    try:
        statuses = []
        for _ in range(7):
            upload = client.post(
                f"{API}/imports/upload",
                headers=headers,
                data={"account_id": str(account_id)},
                files={"file": ("sample.qfx", (FIXTURES / "sample.qfx").read_bytes(), "app/x")},
            )
            statuses.append(upload.status_code)
    finally:
        limiter.enabled = False
        limiter.reset()
    assert statuses == [201] * 6 + [429], statuses

    # Six uploads reached the handler; the refused seventh created neither a
    # row nor a receipt. Every row that DOES exist has exactly the receipts
    # its own staging wrote — no orphan, no gap.
    receipt_tx_ids = set(session.exec(select(TransactionEvent.tx_id)).all())
    row_ids = set(session.exec(select(Transaction.id)).all())
    assert row_ids and receipt_tx_ids == row_ids
    assert receipt_rows() == session.exec(select(func.count()).select_from(Transaction)).one()


# --- 6. migration 0039 up/down ----------------------------------------------


PRE_0039_REVISION = "0038_saved_report_views"
EVENT_TABLE = "transaction_events"
REVIEW_COLUMNS = ("reviewed_at", "reviewed_by")
EVENT_INDEXES = (
    "ix_transaction_events_tx_id",
    "ix_transaction_events_user_id",
    "ix_transaction_events_tx_created",
)
QUEUE_INDEX = "ix_transactions_user_unreviewed"
# Synthetic ids: large enough never to collide with a generated one, and the
# rows they identify are deleted again in the test's `finally`.
PRE_0039_ID = 991001

# One row per table, written into the 0038 schema (i.e. BEFORE 0039 ran) —
# this is the population "all existing rows start unreviewed" is about.
# Every NOT NULL / no-default column of the 0038 schema is pinned here; that
# schema is frozen (no later migration revises it), so the map can only go
# stale if history is rewritten, and the test then fails NAMING the column
# rather than inventing a value for it.
_PRE_0039_ROW: dict[str, dict[str, Any]] = {
    "users": {
        "id": PRE_0039_ID,
        "email": "m106-migration-0039@example.com",
        "hashed_password": "not-a-real-bcrypt-hash",
        "is_admin": False,
        "privacy_mode": False,
        "llm_provider": "ollama",
    },
    "accounts": {
        "id": PRE_0039_ID,
        "user_id": PRE_0039_ID,
        "name": "Pre-0039 account",
        "type": "chequing",
        "currency": "CAD",
        "taxable": False,
        "llm_autopilot": False,
    },
    "transactions": {
        "id": PRE_0039_ID,
        "user_id": PRE_0039_ID,
        "account_id": PRE_0039_ID,
        "date": date(2026, 1, 2),
        "raw_description": "Written before 0039 existed",
        "normalized_payee": "Pre 0039",
        "amount_native": Decimal("-1.00"),
        "currency": "CAD",
        "dedup_hash": "m106-pre-0039-row",
    },
}


def test_migration_0039_review_state_up_down() -> None:
    """`0039_tx_review_events` reverses and re-applies cleanly (AGENTS §4).

    A green `alembic upgrade head` proves none of what this pins: the review
    columns arriving NULLable with NO server default (NULL IS "unreviewed"),
    `transaction_events` and its indexes existing at head and being gone one
    revision below it, and — the acceptance criterion — a row that existed
    BEFORE 0039 landing `reviewed_at IS NULL` afterwards: no data backfill
    (docs/02 §transactions / §transaction_events).
    """
    rendered = make_url(os.environ["DATABASE_URL"]).render_as_string(hide_password=False)
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", rendered)
    engine = create_engine(rendered)

    def column(table: str, name: str):
        """The information_schema row for one column (None = it doesn't exist)."""
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT data_type, character_maximum_length, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = :table AND column_name = :name"
                ),
                {"table": table, "name": name},
            ).first()

    def columns(table: str) -> list[str]:
        with engine.connect() as conn:
            return list(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :table ORDER BY ordinal_position"
                    ),
                    {"table": table},
                ).scalars()
            )

    def index(name: str) -> str | None:
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": name},
            ).scalar()

    def table_exists(table: str) -> bool:
        with engine.connect() as conn:
            return bool(
                conn.execute(
                    text("SELECT 1 FROM pg_tables WHERE tablename = :table"),
                    {"table": table},
                ).scalar()
            )

    def insert_pre_0039_row() -> None:
        with engine.begin() as conn:
            for table, values in _PRE_0039_ROW.items():
                required = set(
                    conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = :table AND is_nullable = 'NO' "
                            "  AND column_default IS NULL"
                        ),
                        {"table": table},
                    ).scalars()
                )
                missing = required - set(values)
                assert not missing, (
                    f"{table}: no pinned value for NOT NULL column(s) {sorted(missing)}"
                )
                names = ", ".join(values)
                placeholders = ", ".join(f":{name}" for name in values)
                conn.execute(
                    text(f"INSERT INTO {table} ({names}) VALUES ({placeholders})"),  # noqa: S608
                    values,
                )

    def drop_pre_0039_row() -> None:
        # Children before parents, or the FK from accounts would refuse it.
        with engine.begin() as conn:
            for table in reversed(_PRE_0039_ROW):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE id = :id"),  # noqa: S608 -- table from fixed tuple in this test
                    {"id": PRE_0039_ID},
                )

    try:
        # (a) At head the schema says "unreviewed is a value": both columns are
        #     NULLable and carry NO default, so nothing can be pre-reviewed by
        #     the migration and an INSERT that ignores them lands unreviewed.
        for name in REVIEW_COLUMNS:
            review_column = column("transactions", name)
            assert review_column is not None, f"{name} missing at head"
            assert review_column.is_nullable == "YES", f"{name} is not NULLable"
            assert review_column.column_default is None, f"{name} carries a default"
        assert column("transactions", "reviewed_at").data_type == "timestamp with time zone"
        assert column("transactions", "reviewed_by").data_type == "integer"

        # (b) The receipt log, column for column (docs/02 §transaction_events):
        #     hashable payload pair, and NO foreign key on `tx_id` — a deleted
        #     transaction must keep the receipt that says so.
        assert columns(EVENT_TABLE) == [
            "id",
            "tx_id",
            "user_id",
            "event_kind",
            "payload",
            "payload_hash",
            "actor",
            "created_at",
            "updated_at",
        ]
        assert column(EVENT_TABLE, "event_kind").character_maximum_length == 16
        assert column(EVENT_TABLE, "payload_hash").character_maximum_length == 64
        with engine.connect() as conn:
            foreign_keys = conn.execute(
                text(
                    "SELECT a.attname FROM pg_constraint c "
                    "JOIN pg_attribute a ON a.attrelid = c.conrelid "
                    "   AND a.attnum = ANY (c.conkey) "
                    "WHERE c.contype = 'f' AND c.conrelid = 'transaction_events'::regclass"
                )
            ).scalars()
        assert list(foreign_keys) == ["user_id"], list(foreign_keys)

        # (c) The two lookups the feature runs on: the drawer's chronological
        #     per-transaction scan, and the queue's partial probe.
        for name in (*EVENT_INDEXES, QUEUE_INDEX):
            assert index(name), f"{name} missing at head"
        chronological = index("ix_transaction_events_tx_created")
        queue_index = index(QUEUE_INDEX)
        assert chronological and "tx_id, created_at DESC" in chronological
        assert queue_index and "WHERE" in queue_index and "reviewed_at IS NULL" in queue_index

        # (d) One linear chain — the forked-head trap (0033/0037/0038/0039's
        #     own re-numbering note), pinned while we hold a Config anyway.
        script = ScriptDirectory.from_config(cfg)
        assert len(script.get_heads()) == 1
        assert script.get_revision("0039_tx_review_events").down_revision == PRE_0039_REVISION

        # (e) One revision down, every artifact of 0039 is gone…
        command.downgrade(cfg, PRE_0039_REVISION)
        for name in REVIEW_COLUMNS:
            assert column("transactions", name) is None, f"{name} survived the downgrade"
        assert not table_exists(EVENT_TABLE)
        for name in (*EVENT_INDEXES, QUEUE_INDEX):
            assert index(name) is None, f"{name} survived the downgrade"

        # …and the schema below it takes an ordinary row, which is the
        # population "existing rows" means here.
        insert_pre_0039_row()

        # (f) Up again: the columns come back over a REAL pre-existing row and
        #     leave it unreviewed — no backfill, no derived state — while the
        #     log and its indexes are rebuilt from scratch.
        command.upgrade(cfg, "head")
        for name in REVIEW_COLUMNS:
            assert column("transactions", name) is not None, f"{name} missing after upgrade"
        with engine.connect() as conn:
            pre_existing = conn.execute(
                text(
                    "SELECT reviewed_at, reviewed_by, raw_description FROM transactions "
                    "WHERE id = :id"
                ),
                {"id": PRE_0039_ID},
            ).first()
        assert pre_existing is not None, "the pre-0039 row did not survive the upgrade"
        assert pre_existing.reviewed_at is None and pre_existing.reviewed_by is None
        assert pre_existing.raw_description == "Written before 0039 existed"
        assert table_exists(EVENT_TABLE)
        for name in (*EVENT_INDEXES, QUEUE_INDEX):
            assert index(name), f"{name} missing after the re-upgrade"
    finally:
        # Restore first — a suite that keeps running from a half-applied
        # schema is worse than anything this test can print. A cleanup error
        # must not MASK the body's failure, so it only surfaces when the body
        # itself passed.
        body_failed = sys.exc_info()[1] is not None
        try:
            command.upgrade(cfg, "head")  # restore head for every later test
            drop_pre_0039_row()
        except Exception:
            if not body_failed:
                raise
        finally:
            engine.dispose()


# --- 7. the drawer's own acceptance: one row's history, verbatim ------------


def test_receipts_are_scoped_to_one_transaction_and_one_owner(client: TestClient) -> None:
    """The drawer prints whatever `GET /transactions/{id}/events` returns, so
    "no cross-transaction events shown" is THAT route's contract (docs/08).

    Two rows the owner can see side by side, plus a stranger's row mutated the
    same way: each history is its own, the two share no receipt id, and the
    stranger's row is not an empty list but a 404 — same answer as an id that
    never existed.
    """
    headers, _uid = _register(client, "m106-scope@example.com", "Mark")
    alpha_id = create_account(client, headers, "Alpha Chequing")
    beta_id = create_account(client, headers, "Beta Savings")

    alpha = _create(client, headers, alpha_id, payee="Alpha")
    beta = _create(client, headers, beta_id, payee="Beta")
    assert (
        client.patch(
            f"{API}/transactions/{alpha['id']}",
            headers=headers,
            json={"normalized_payee": UNICODE_PAYEE},
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"{API}/transactions/{beta['id']}",
            headers=headers,
            json={"amount_native": "-7.77"},
        ).status_code
        == 200
    )

    # A stranger's row, mutated through the stranger's own session.
    other_headers, _other_uid = _register(client, "m106-scope-other@example.com", "Stranger")
    other_account = create_account(client, other_headers)
    stranger = _create(client, other_headers, other_account, payee="Stranger")
    assert (
        client.patch(
            f"{API}/transactions/{stranger['id']}",
            headers=other_headers,
            json={"normalized_payee": "Not yours"},
        ).status_code
        == 200
    )

    alpha_events = _events(client, headers, alpha["id"])
    # EVERY line of this history names the row it is about — one id, twice.
    assert {event["tx_id"] for event in alpha_events} == {alpha["id"]}
    assert [event["event_kind"] for event in alpha_events] == ["edited", "created"]
    assert alpha_events[0]["payload"]["changes"]["normalized_payee"] == UNICODE_PAYEE

    beta_events = _events(client, headers, beta["id"])
    assert {event["tx_id"] for event in beta_events} == {beta["id"]}
    assert [event["event_kind"] for event in beta_events] == ["edited", "created"]
    assert beta_events[0]["payload"]["changes"]["amount_native"] == "-7.77"

    # The two histories are disjoint: no id, and no payload, crosses over.
    assert {event["id"] for event in alpha_events}.isdisjoint(event["id"] for event in beta_events)
    assert not any(
        event["payload"].get("changes", {}).get("amount_native") for event in alpha_events
    )
    assert not any(
        event["payload"].get("changes", {}).get("normalized_payee") == UNICODE_PAYEE
        for event in beta_events
    )

    # Another owner's row answers 404 — never a stranger's sentences, never an
    # empty 200 that would read as "this row has no history".
    foreign = client.get(f"{API}/transactions/{stranger['id']}/events", headers=headers)
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "not_found"


def test_drawer_summary_renders_a_unicode_body_verbatim(client: TestClient) -> None:
    """`summary` is computed from the STORED payload at read time, so the line
    the drawer prints must carry the user's characters exactly — accents, an
    emoji and a non-Latin script — and the bytes on the wire must be those
    characters rather than `\\uXXXX` escapes."""
    headers, _uid = _register(client, "m106-unicode@example.com", "Mark")
    account_id = create_account(client, headers)
    category = client.post(f"{API}/categories", headers=headers, json={"name": UNICODE_CATEGORY})
    assert category.status_code == 201, category.text

    row = _create(client, headers, account_id)
    patched = client.patch(
        f"{API}/transactions/{row['id']}",
        headers=headers,
        json={"category_id": category.json()["id"]},
    )
    assert patched.status_code == 200, patched.text

    raw = client.get(f"{API}/transactions/{row['id']}/events", headers=headers)
    assert raw.status_code == 200, raw.text
    # The bytes the drawer's fetch actually receives.
    assert UNICODE_CATEGORY.encode() in raw.content
    assert b"\\u98df" not in raw.content

    top = raw.json()["items"][0]
    assert top["event_kind"] == "categorized"
    assert top["payload"]["category_name"] == UNICODE_CATEGORY
    assert top["summary"] == f"you set category {UNICODE_CATEGORY}"
    # The hash is over the payload that carries the unicode body, so the pair
    # still re-derives (the "hash and detail are written together" rule).
    assert top["payload_hash"] == _sha(top["payload"])


# --- M120: the four surfaces that used to change rows silently -----------------
#
# Every test below reuses the five helpers above (`_register` / `_create` /
# `_events` / `_kinds` / `_sha`) and none of the twelve M106 expectations
# above is revised — the M120 contract is additive (docs/M120_RECEIPTS_SPEC
# §"Verification", list 1-8).


def _m120_receipts(session: Session) -> int:
    """Every receipt in the table — refusals are asserted against the WHOLE log."""
    return session.exec(select(func.count()).select_from(TransactionEvent)).one()


def _m120_tag(client: TestClient, headers: dict[str, str], name: str) -> int:
    response = client.post(f"{API}/tags", headers=headers, json={"name": name})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _m120_rule(
    client: TestClient,
    headers: dict[str, str],
    *,
    pattern: str,
    category_id: int,
    tag_ids: list[int] | None = None,
    priority: int = 10,
) -> dict:
    response = client.post(
        f"{API}/rules",
        headers=headers,
        json={
            "match_type": "contains",
            "pattern": pattern,
            "priority": priority,
            "category_id": category_id,
            "tag_ids": tag_ids or [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _m120_suggester(mapping: dict[int, int], confidence: float):
    """Scripted layer-3 responder — the same shape `test_cat_pipeline` uses."""

    def respond(messages: list[dict], schema: dict) -> dict:
        user_text = messages[1]["content"]
        ids = [int(line.split(":", 1)[0]) for line in user_text.splitlines()[1:] if ":" in line]
        return {
            "suggestions": [
                {
                    "transaction_id": tid,
                    "category_id": mapping[tid],
                    "confidence": confidence,
                }
                for tid in ids
            ]
        }

    return respond


def test_m120_suggestion_accept_writes_a_categorized_receipt(
    client: TestClient, session: Session
) -> None:
    """1. An Accept click moves `category_id`, so it is a receipt like any other
    category move — same commit, `via=suggestion_accept`, actor = the clicker."""
    headers, uid = _register(client, "m120-accept@example.com", "Mark")
    account_id = create_account(client, headers)
    groceries = category_by_name(session, "groceries", uid)
    row = _create(client, headers, account_id, payee="Fresh Mart")
    tx_id = row["id"]
    session.add(
        LlmSuggestion(
            user_id=uid,
            transaction_id=tx_id,
            proposed_category_id=groceries.id,
            confidence=0.82,
            model_used="fake-model",
        )
    )
    session.commit()
    suggestion_id = session.exec(select(LlmSuggestion)).one().id
    assert suggestion_id is not None
    assert _kinds(client, headers, tx_id) == ["created"]

    accepted = client.post(f"{API}/suggestions/{suggestion_id}/accept", headers=headers)
    assert accepted.status_code == 200, accepted.text

    top = _events(client, headers, tx_id)[0]
    assert top["event_kind"] == "categorized"
    assert top["payload"] == {
        "category_id": groceries.id,
        "category_name": "groceries",
        "via": "suggestion_accept",
        "suggestion_id": suggestion_id,
    }
    # The hash re-derives from exactly the bytes the drawer rendered.
    assert top["payload_hash"] == _sha(top["payload"])
    assert top["actor"] == uid
    assert top["summary"] == "you set category groceries (accepted suggestion)"
    assert _kinds(client, headers, tx_id) == ["categorized", "created"]

    # M107's split guard is untouched, and the 409 not-pending path exits
    # before its first write — neither widens the log (M120 §6).
    refused = client.post(f"{API}/suggestions/{suggestion_id}/accept", headers=headers)
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "suggestion_not_pending"
    assert _kinds(client, headers, tx_id) == ["categorized", "created"]


def test_m120_rule_backfill_receipts_every_affected_row(
    client: TestClient, session: Session
) -> None:
    """2. `POST /rules/{id}/apply` — one receipt per row it moves, in the SAME
    commit; a skipped row writes nothing; `mode=forward` writes nothing."""
    headers, uid = _register(client, "m120-backfill@example.com", "Mark")
    account_id = create_account(client, headers)
    groceries = category_by_name(session, "groceries", uid)
    tag_id = _m120_tag(client, headers, "pantry")
    rows = [
        _create(client, headers, account_id, payee=f"Coffee Shop {index}") for index in range(3)
    ]
    rule = _m120_rule(client, headers, pattern="coffee", category_id=groceries.id, tag_ids=[tag_id])

    applied = client.post(
        f"{API}/rules/{rule['id']}/apply",
        headers=headers,
        json={"mode": "range", "from": "2026-08-01", "to": "2026-08-31"},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["matched_count"] == 3
    assert applied.json()["applied_count"] == 3

    for row in rows:
        events = _events(client, headers, row["id"])
        # Newest first: the two M120 receipts this call appended, then the
        # row's own `created`.
        assert [event["event_kind"] for event in events] == ["tagged", "categorized", "created"]
        categorized = events[1]
        assert categorized["payload"] == {
            "category_id": groceries.id,
            "category_name": "groceries",
            "via": "rule_backfill",
        }
        assert categorized["payload_hash"] == _sha(categorized["payload"])
        assert categorized["actor"] == uid
        assert categorized["summary"] == "you set category groceries (rule backfill)"
        # The `tagged` rider mirrors `PUT /transactions/{id}/tags` field for
        # field — names AND ids, never a slimmer shape (M120 §1).
        tagged = events[0]
        assert tagged["payload"] == {
            "added": ["pantry"],
            "removed": [],
            "added_ids": [tag_id],
            "removed_ids": [],
        }
        assert tagged["payload_hash"] == _sha(tagged["payload"])
        assert tagged["actor"] == uid

    # Every row is now categorized, so `overwrite=false` skips ALL of them and
    # writes NOTHING — not a category receipt, not a tag rider.
    again = client.post(
        f"{API}/rules/{rule['id']}/apply",
        headers=headers,
        json={"mode": "range", "from": "2026-08-01", "to": "2026-08-31", "overwrite": False},
    )
    assert again.status_code == 200, again.text
    assert again.json()["applied_count"] == 0
    for row in rows:
        assert len(_events(client, headers, row["id"])) == 3

    # `mode=forward` returns before the engine ever runs (M120 §2).
    forward = client.post(
        f"{API}/rules/{rule['id']}/apply", headers=headers, json={"mode": "forward"}
    )
    assert forward.status_code == 200, forward.text
    for row in rows:
        assert len(_events(client, headers, row["id"])) == 3


def test_m120_recurring_accept_receipts_every_tagged_row(
    client: TestClient, session: Session
) -> None:
    """3. A group a person just tagged writes one plain `edited` receipt per row
    — no `via` key, no new wording, the `record_edit_event` shape verbatim."""
    headers, uid = _register(client, "m120-recurring@example.com", "Mark")
    account_id = create_account(client, headers)
    rows = [
        _create(client, headers, account_id, payee="Netflix", amount="-15.99"),
        _create(client, headers, account_id, payee="Netflix", amount="-16.49"),
    ]

    accepted = client.post(
        f"{API}/transactions/recurring/accept",
        headers=headers,
        json={
            "group_key": "netflix|monthly",
            "transaction_ids": [row["id"] for row in rows],
            "cadence": "monthly",
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["updated"] == 2
    next_date = accepted.json()["next_expected_date"]

    for row in rows:
        events = _events(client, headers, row["id"])
        assert [event["event_kind"] for event in events] == ["edited", "created"]
        top = events[0]
        assert top["payload"] == {
            "fields": ["is_recurring", "recurring_cadence", "recurring_next_date"],
            "changes": {
                "is_recurring": True,
                "recurring_cadence": "monthly",
                "recurring_next_date": next_date,
            },
        }
        assert "via" not in top["payload"]
        assert top["payload_hash"] == _sha(top["payload"])
        assert top["actor"] == uid
        # No `via` → the plain `edited` fallback, byte-identical to what a
        # multi-field PATCH on these columns would render.
        assert top["summary"] == "you edited this transaction"


def test_m120_pipeline_layer1_receipts_rule_hits_with_a_system_actor(
    client: TestClient, session: Session
) -> None:
    """4a. Layer 1 applies AND receipts: `categorized` `via=rule` with
    `actor=None` per affected row, plus a `tagged` rider when the junction
    moved — including the §6 symmetric write on a row that already carries the
    target value."""
    headers, uid = _register(client, "m120-layer1@example.com", "Mark")
    account_id = create_account(client, headers)
    groceries = category_by_name(session, "groceries", uid)
    tag_id = _m120_tag(client, headers, "fuel")
    row = _create(client, headers, account_id, payee="Shell Gas")
    tx_id = row["id"]
    _m120_rule(client, headers, pattern="shell", category_id=groceries.id, tag_ids=[tag_id])

    run = client.post(f"{API}/suggestions/run", headers=headers, json={"transaction_ids": [tx_id]})
    assert run.status_code == 202, run.text
    assert run.json()["queued"] == 0

    events = _events(client, headers, tx_id)
    assert [event["event_kind"] for event in events] == ["tagged", "categorized", "created"]
    categorized = events[1]
    assert categorized["payload"] == {
        "category_id": groceries.id,
        "category_name": "groceries",
        "via": "rule",
    }
    assert categorized["actor"] is None
    assert categorized["summary"] == "the system set category groceries (rule)"
    assert categorized["payload_hash"] == _sha(categorized["payload"])
    tagged = events[0]
    assert tagged["payload"] == {
        "added": ["fuel"],
        "removed": [],
        "added_ids": [tag_id],
        "removed_ids": [],
    }
    assert tagged["actor"] is None
    assert tagged["summary"] == "the system added tag fuel"

    # The §6 symmetric-write reading, pinned because it was the contested
    # point: a rule hit that CONFIRMS the value the row already carries still
    # receipts — layer 1 is symmetric with layers 2/3, which can only ever
    # write a null previous. The junction did NOT move, so the rider does NOT
    # double up.
    target = session.get(Transaction, tx_id)
    assert target is not None
    assert target.category_id == groceries.id
    from app.services.rules_engine import apply_rules

    apply_rules(session, uid, [target], receipt_via="rule", actor=None)
    session.commit()
    events = _events(client, headers, tx_id)
    categorized = [event for event in events if event["event_kind"] == "categorized"]
    assert len(categorized) == 2
    assert [event["payload"]["via"] for event in categorized] == ["rule", "rule"]
    assert [event["actor"] for event in categorized] == [None, None]
    assert [event["summary"] for event in categorized] == [
        "the system set category groceries (rule)",
        "the system set category groceries (rule)",
    ]
    assert len([event for event in events if event["event_kind"] == "tagged"]) == 1


def test_m120_pipeline_layer2_receipts_a_learned_map_hit(
    client: TestClient, session: Session
) -> None:
    """4b. A learned-map hit writes `categorized` `via=learned`, `actor=None`."""
    headers, uid = _register(client, "m120-layer2@example.com", "Mark")
    account_id = create_account(client, headers)
    groceries = category_by_name(session, "groceries", uid)
    row = _create(client, headers, account_id, payee="Fresh Mart", amount="-10.00")
    tx_id = row["id"]
    target = session.get(Transaction, tx_id)
    assert target is not None
    # The learning loop's own table, seeded exactly the way an accept fills it.
    session.add(
        LearnedPayee(
            user_id=uid,
            normalized_payee=target.normalized_payee,
            category_id=groceries.id,
        )
    )
    session.commit()

    run = client.post(f"{API}/suggestions/run", headers=headers, json={"transaction_ids": [tx_id]})
    assert run.status_code == 202, run.text

    events = _events(client, headers, tx_id)
    assert [event["event_kind"] for event in events] == ["categorized", "created"]
    top = events[0]
    assert top["payload"] == {
        "category_id": groceries.id,
        "category_name": "groceries",
        "via": "learned",
    }
    assert top["actor"] is None
    assert top["summary"] == "the system set category groceries (learned from your edits)"
    assert top["payload_hash"] == _sha(top["payload"])


def test_m120_pipeline_layer3_receipts_an_autopilot_apply(
    client: TestClient, session: Session, fake_provider, background_session
) -> None:
    """4c. A high-confidence auto-apply on an `llm_autopilot` account writes
    `categorized` `via=llm`, `actor=None`."""
    headers, uid = _register(client, "m120-layer3@example.com", "Mark")
    groceries = category_by_name(session, "groceries", uid)
    account = autopilot_account(session, uid, enabled=True)
    row = _create(client, headers, account.id, payee="Hydro One")
    tx_id = row["id"]
    fake_provider.responder = _m120_suggester({tx_id: groceries.id}, confidence=0.95)

    run = client.post(f"{API}/suggestions/run", headers=headers, json={"transaction_ids": [tx_id]})
    assert run.status_code == 202, run.text
    assert run.json()["queued"] == 1

    events = _events(client, headers, tx_id)
    assert [event["event_kind"] for event in events] == ["categorized", "created"]
    top = events[0]
    assert top["payload"] == {
        "category_id": groceries.id,
        "category_name": "groceries",
        "via": "llm",
    }
    assert top["actor"] is None
    assert (
        top["summary"] == "the system set category groceries (assistant suggestion, auto-applied)"
    )
    assert top["payload_hash"] == _sha(top["payload"])

    # The suggestion row itself is unchanged by M120 — still auto-accepted.
    suggestion = session.exec(select(LlmSuggestion)).one()
    assert suggestion.status == SuggestionStatus.accepted


def test_m120_merge_and_replace_split_gain_no_new_receipt(
    client: TestClient, session: Session
) -> None:
    """5. The exclusion, pinned: merge-back and replace-split touch no field on
    the parent (M107/D15 already receipts them), so M120 widens neither — and
    a split parent is LEDGER-invisible to BOTH new receipting surfaces."""
    headers, _uid = _register(client, "m120-split@example.com", "Mark")
    account_id = create_account(client, headers)
    groceries = category_by_name(session, "groceries", _uid)
    tag_id = _m120_tag(client, headers, "warehouse")
    parent = _create(client, headers, account_id, payee="COSTCO #123", amount="-187.42")
    parent_id = parent["id"]
    # A rule that WOULD move this row the moment the parent is countable again.
    rule = _m120_rule(client, headers, pattern="costco", category_id=groceries.id, tag_ids=[tag_id])

    split = client.post(
        f"{API}/transactions/{parent_id}/split",
        headers=headers,
        json={"children": [{"amount_native": "-120.00"}, {"amount_native": "-67.42"}]},
    )
    assert split.status_code == 201, split.text
    child_ids = [child["id"] for child in split.json()["children"]]
    assert _kinds(client, headers, parent_id) == ["split", "created"]
    for child_id in child_ids:
        assert _events(client, headers, child_id) == [], "children still write no receipts"

    # Neither new surface can reach a split parent: rules-apply scopes through
    # `countable_tx_conditions` (LEDGER) and the pipeline adds
    # `LEDGER_EXCLUDE_SPLIT` itself.
    applied = client.post(f"{API}/rules/{rule['id']}/apply", headers=headers, json={"mode": "all"})
    assert applied.status_code == 200, applied.text
    # The CHILDREN are LEDGER rows and may well have moved; the parent is not
    # in the population at all, so its log cannot have grown.
    assert _kinds(client, headers, parent_id) == ["split", "created"]

    run = client.post(
        f"{API}/suggestions/run", headers=headers, json={"transaction_ids": [parent_id]}
    )
    assert run.status_code == 202, run.text
    assert run.json()["queued"] == 0
    assert _kinds(client, headers, parent_id) == ["split", "created"]

    replaced = client.put(
        f"{API}/transactions/{parent_id}/split",
        headers=headers,
        json={"children": [{"amount_native": "-90.00"}, {"amount_native": "-97.42"}]},
    )
    assert replaced.status_code == 200, replaced.text
    assert _kinds(client, headers, parent_id) == ["split", "split", "created"]

    merged = client.delete(f"{API}/transactions/{parent_id}/split", headers=headers)
    assert merged.status_code == 200, merged.text
    assert _kinds(client, headers, parent_id) == ["merge", "split", "split", "created"]


def test_m120_pipeline_receipts_never_reach_the_partner_view(
    client: TestClient, session: Session
) -> None:
    """6. `actor=None` + the EXISTING `actor.in_(allowed)` filter (no new
    predicate) keeps a pipeline receipt in the owner's drawer and out of the
    partner's, while the payload stays stripped either way."""
    owner_headers, owner_id = _register(client, "m120-owner@example.com", "Owner Mark")
    partner_headers, _partner_id = _register(client, "m120-partner@example.com", "Partner Pat")
    account_id = create_account(client, owner_headers)
    groceries = session.exec(
        select(Category).where(Category.user_id == owner_id, Category.name == "groceries")
    ).one()
    row = _create(client, owner_headers, account_id, payee="Shell Gas")
    tx_id = row["id"]

    invite = client.post(
        f"{API}/sharing/invites",
        headers=owner_headers,
        json={"email": "m120-partner@example.com", "account_ids": [account_id]},
    )
    assert invite.status_code == 201, invite.text
    accepted = client.post(
        f"{API}/sharing/invites/accept",
        headers=partner_headers,
        json={"token": invite.json()["token"]},
    )
    assert accepted.status_code == 200, accepted.text

    _m120_rule(client, owner_headers, pattern="shell", category_id=groceries.id)
    run = client.post(
        f"{API}/suggestions/run", headers=owner_headers, json={"transaction_ids": [tx_id]}
    )
    assert run.status_code == 202, run.text

    owner_events = _events(client, owner_headers, tx_id)
    assert [event["event_kind"] for event in owner_events] == ["categorized", "created"]
    assert owner_events[0]["actor"] is None
    assert owner_events[0]["summary"] == "the system set category groceries (rule)"

    response = client.get(f"{API}/partner/transactions/{tx_id}/events", headers=partner_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(body["items"])
    # The pipeline receipt is gone entirely; the owner's own `created` is not.
    assert [event["event_kind"] for event in body["items"]] == ["created"]
    assert {event["actor"] for event in body["items"]} == {owner_id}
    assert all("payload" not in event for event in body["items"])
    # Nothing from the dropped receipt's payload or summary crossed the seam.
    assert "(rule)" not in response.text
    assert "groceries" not in response.text
    assert "via" not in response.text


def test_m120_pre_handler_refusals_on_the_new_surfaces_write_nothing(
    client: TestClient, session: Session
) -> None:
    """7. M91-6 extended (M120 §6): a refusal answered BEFORE a handler body
    runs, and a handler refusal that exits before its first write, both leave
    the log byte-identical — on the surfaces M120 just receipted."""
    headers, uid = _register(client, "m120-refusal@example.com", "Mark")
    account_id = create_account(client, headers)
    groceries = category_by_name(session, "groceries", uid)
    row = _create(client, headers, account_id, payee="Netflix", amount="-15.99")
    tx_id = row["id"]
    session.add(
        LlmSuggestion(
            user_id=uid,
            transaction_id=tx_id,
            proposed_category_id=groceries.id,
            confidence=0.8,
            model_used="fake-model",
        )
    )
    session.commit()
    suggestion_id = session.exec(select(LlmSuggestion)).one().id
    assert suggestion_id is not None
    rule = _m120_rule(client, headers, pattern="netflix", category_id=groceries.id)

    def unchanged(before: tuple[list[str], int]) -> None:
        assert _kinds(client, headers, tx_id) == before[0]
        assert _m120_receipts(session) == before[1]

    baseline = (_kinds(client, headers, tx_id), _m120_receipts(session))
    assert baseline == (["created"], 1)

    # (a) 422 body validation — FastAPI answers before `accept_recurring_group`.
    rejected = client.post(
        f"{API}/transactions/recurring/accept",
        headers=headers,
        json={
            "group_key": "netflix|monthly",
            "transaction_ids": [tx_id],
            "cadence": "monthly",
            "unexpected": 1,
        },
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "validation_error"
    unchanged(baseline)

    # (b) 422 body validation on the backfill's own enum field.
    rejected = client.post(
        f"{API}/rules/{rule['id']}/apply", headers=headers, json={"mode": "sideways"}
    )
    assert rejected.status_code == 422, rejected.text
    unchanged(baseline)

    # (c) a handler refusal that fires BEFORE the engine's first write.
    rejected = client.post(
        f"{API}/rules/{rule['id']}/apply",
        headers=headers,
        json={"mode": "range", "from": "2026-09-01", "to": "2026-08-01"},
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "invalid_date_range"
    unchanged(baseline)

    # (d) 404 for a suggestion that does not exist — no handler body reached.
    rejected = client.post(f"{API}/suggestions/999999/accept", headers=headers)
    assert rejected.status_code == 404
    assert rejected.json()["code"] == "not_found"
    unchanged(baseline)

    # (e) the SAME route after a real accept: the 409 adds nothing either.
    assert (
        client.post(f"{API}/suggestions/{suggestion_id}/accept", headers=headers).status_code == 200
    )
    assert _kinds(client, headers, tx_id) == ["categorized", "created"]
    after_accept = (_kinds(client, headers, tx_id), _m120_receipts(session))
    refused = client.post(f"{API}/suggestions/{suggestion_id}/accept", headers=headers)
    assert refused.status_code == 409, refused.text
    unchanged(after_accept)

    # (f) the 429 limiter — same decorator mechanism as every other mutating
    #     route (imports/upload is the one that carries a limit). Six uploads
    #     reach the handler and stage their rows; the refused seventh stages
    #     NOTHING, and the rows primed above keep exactly the receipts they
    #     had. One receipt per row exists, plus exactly one M120 receipt.
    limiter.reset()
    limiter.enabled = True
    try:
        statuses = []
        for _ in range(7):
            upload = client.post(
                f"{API}/imports/upload",
                headers=headers,
                data={"account_id": str(account_id)},
                files={"file": ("sample.qfx", (FIXTURES / "sample.qfx").read_bytes(), "app/x")},
            )
            statuses.append(upload.status_code)
    finally:
        limiter.enabled = False
        limiter.reset()
    assert statuses == [201] * 6 + [429], statuses
    # The refusals staged nothing of their own: the primed row still carries
    # exactly the receipts it had, and every OTHER row is a staged row with
    # exactly one `created` receipt — so the whole log is one receipt per row
    # plus the single M120 receipt the accept above wrote.
    assert _kinds(client, headers, tx_id) == after_accept[0]
    assert _m120_receipts(session) == (
        session.exec(select(func.count()).select_from(Transaction)).one() + 1
    )


def test_m120_receipts_reach_the_export_and_obey_the_zip_visibility_filter(
    client: TestClient, session: Session
) -> None:
    """8. `/export/full` carries the new receipts on a committed row AND on a
    staged one (it is a backup); the ZIP — the file a human carries away —
    carries the committed row's and drops the staged row's, because that row
    is invisible in the app too."""
    headers, uid = _register(client, "m120-export@example.com", "Mark")
    account_id = _account_with_institution(client, headers)
    groceries = category_by_name(session, "groceries", uid)
    committed = _create(client, headers, account_id, payee="Coffee Shop", amount="-4.50")
    committed_tx_id = committed["id"]

    rule = _m120_rule(client, headers, pattern="coffee", category_id=groceries.id)
    applied = client.post(f"{API}/rules/{rule['id']}/apply", headers=headers, json={"mode": "all"})
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied_count"] == 1
    assert any(
        event["payload"].get("via") == "rule_backfill"
        for event in _events(client, headers, committed_tx_id)
    )

    # A STAGED row: the pipeline's own scope never asked about staging, so it
    # receipts there too — visibility is the EXPORT's job, not the writer's.
    upload = client.post(
        f"{API}/imports/upload",
        headers=headers,
        data={"account_id": str(account_id)},
        files={"file": ("sample.qfx", (FIXTURES / "sample.qfx").read_bytes(), "app/x")},
    )
    assert upload.status_code == 201, upload.text
    batch_id = upload.json()["id"]
    staged = list(
        session.exec(select(Transaction).where(Transaction.import_batch_id == batch_id)).all()
    )
    assert staged, "the QFX fixture staged no rows"
    target = staged[0]
    staged_tx_id = target.id
    assert staged_tx_id is not None
    session.add(
        LearnedPayee(
            user_id=uid,
            normalized_payee=target.normalized_payee,
            category_id=groceries.id,
        )
    )
    session.commit()
    run = client.post(
        f"{API}/suggestions/run", headers=headers, json={"transaction_ids": [staged_tx_id]}
    )
    assert run.status_code == 202, run.text
    staged_events = _events(client, headers, staged_tx_id)
    assert any(event["payload"].get("via") == "learned" for event in staged_events)

    # `/export/full` is the backup: every receipt it holds re-derives.
    snapshot = client.get(f"{API}/export/full", headers=headers).json()
    full_events = snapshot["transaction_events"]
    assert any(
        event["tx_id"] == committed_tx_id and event["payload"].get("via") == "rule_backfill"
        for event in full_events
    )
    assert any(
        event["tx_id"] == staged_tx_id and event["payload"].get("via") == "learned"
        for event in full_events
    )
    for event in full_events:
        assert event["payload_hash"] == _sha(event["payload"])

    # The ZIP publishes the filter it applied and then honours it.
    zip_response = client.get(f"{API}/export/zip", headers=headers)
    assert zip_response.status_code == 200, zip_response.text
    with zipfile.ZipFile(io.BytesIO(zip_response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert "transaction_events" in manifest["visibility_filters"]
        assert "transaction_events.csv" in archive.namelist()
        rows = list(csv.DictReader(io.StringIO(archive.read("transaction_events.csv").decode())))
    shipped = {int(row["tx_id"]) for row in rows if row.get("tx_id")}
    assert committed_tx_id in shipped, "a committed row's receipts ship"
    assert staged_tx_id not in shipped, "a staged row's receipts stay home until commit"

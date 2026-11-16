"""Targeted apply (docs/31, M46) + the M137 P1-2 RE-MOUNT of `POST /rules/preview`.

M48-S3 deleted the route and had the browser dry-run the draft over a
full-ledger download (`frontend/src/lib/s3Engine.ts::previewRule`). M137
reverses that deliberately (docs/33 A12): the verdict is the SERVER's again,
from the same `match_transactions` engine `POST /rules/{id}/apply` writes with
— so a preview and the apply it precedes cannot disagree about what matched.

The client twin's tests (`frontend/tests/s3ClientCompute.test.ts`, the s3
goldens) STAYED until M160/P2-5 deleted the twin with `s3Engine`; those same
goldens are now replayed here-adjacent at the mounted route by
`tests/test_m160_replay_s3.py`. What is pinned HERE is the route contract: it answers, it needs
auth, it never writes, and it is indifferent to `encryption_required` (P1-5
pattern — the matcher reads plaintext columns).
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transactions import Transaction
from tests.test_deep_common import (
    auth,
    create_account,
    get_category,
    register,
    seed_transaction,
)


def _setup(client: TestClient, session: Session) -> tuple[dict, int, int, int, int]:
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    acct = create_account(client, headers)
    dining = get_category(session, user_id, "dining")
    groceries = get_category(session, user_id, "groceries")
    return headers, user_id, acct, dining, groceries


def _seeded(headers: dict, user_id: int, acct: int, dining: int, session: Session) -> None:
    seed_transaction(session, user_id, acct, "NETFLIX.COM", amount="-16.99", day=date(2026, 9, 2))
    seed_transaction(session, user_id, acct, "NETFLIX.COM", amount="-16.99", day=date(2026, 8, 2))
    seed_transaction(
        session,
        user_id,
        acct,
        "NETFLIX.COM",
        amount="-16.99",
        day=date(2026, 9, 3),
        category_id=dining,
    )


def _rule(client: TestClient, headers: dict, cat_id: int, **over) -> dict:
    body = {
        "match_type": "contains",
        "pattern": "NETFLIX",
        "priority": 10,
        "category_id": cat_id,
        **over,
    }
    r = client.post("/api/v1/rules", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# --- M137 P1-2: POST /rules/preview is MOUNTED again ------------------------


def test_preview_is_mounted_and_needs_auth(client: TestClient) -> None:
    """The route exists, so its auth dependency runs: 401, never 405/404."""
    r = client.post("/api/v1/rules/preview", json={"match_type": "contains", "pattern": "x"})
    assert r.status_code == 401, r.text


def test_preview_dry_runs_server_side(client: TestClient, session: Session) -> None:
    """Count + sample from the SERVER's engine — the same one apply writes with."""
    headers, user_id, acct, dining, _ = _setup(client, session)
    _seeded(headers, user_id, acct, dining, session)

    r = client.post(
        "/api/v1/rules/preview",
        json={"match_type": "contains", "pattern": "NETFLIX"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # `only_unassigned` defaults true: two uncategorized rows match, the
    # already-categorized one is counted as skipped, never listed.
    assert body["matched_count"] == 2
    assert body["assigned_skipped"] == 1
    assert len(body["items"]) == 2
    assert {item["payee"] for item in body["items"]} == {"NETFLIX.COM"}
    # Newest first — the ordering apply and the preview share (docs/31).
    assert [item["date"] for item in body["items"]] == sorted(
        (item["date"] for item in body["items"]), reverse=True
    )

    # `only_unassigned=false` widens to the categorized row too.
    everything = client.post(
        "/api/v1/rules/preview",
        json={"match_type": "contains", "pattern": "NETFLIX", "only_unassigned": False},
        headers=headers,
    ).json()
    assert everything["matched_count"] == 3
    assert everything["assigned_skipped"] == 0
    assert len(everything["items"]) == 3


def test_preview_never_writes(client: TestClient, session: Session) -> None:
    """A dry-run must leave the ledger untouched."""
    headers, user_id, acct, dining, _ = _setup(client, session)
    _seeded(headers, user_id, acct, dining, session)
    before = session.exec(
        select(Transaction.category_id).where(Transaction.user_id == user_id)
    ).all()
    client.post(
        "/api/v1/rules/preview",
        json={"match_type": "contains", "pattern": "NETFLIX", "only_unassigned": False},
        headers=headers,
    )
    session.expire_all()
    after = session.exec(
        select(Transaction.category_id).where(Transaction.user_id == user_id)
    ).all()
    assert after == before


def test_preview_rejects_a_pattern_the_apply_would_reject(
    client: TestClient, session: Session
) -> None:
    """Validation runs in the handler, so the dry-run can never promise a match
    the write would refuse."""
    headers, _, _, _, _ = _setup(client, session)
    r = client.post(
        "/api/v1/rules/preview",
        json={"match_type": "regex", "pattern": "(unclosed"},
        headers=headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_regex"


@pytest.mark.parametrize("encryption_required", [False, True])
def test_preview_is_indifferent_to_encryption_required(
    client: TestClient, session: Session, encryption_required: bool
) -> None:
    """P1-5 pattern: the matcher reads `normalized_payee` (mandatory
    plaintext), so the verdict is identical under the write guard."""
    headers, user_id, acct, dining, _ = _setup(client, session)
    enabled = client.patch(
        "/api/v1/users/me",
        json={"encryption_required": encryption_required},
        headers=headers,
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["encryption_required"] is encryption_required
    _seeded(headers, user_id, acct, dining, session)

    r = client.post(
        "/api/v1/rules/preview",
        json={"match_type": "contains", "pattern": "NETFLIX"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["matched_count"] == 2
    assert r.json()["assigned_skipped"] == 1


# --- POST /rules/{id}/apply: targeted backfill, writes ----------------------


def test_apply_all_and_range(client: TestClient, session: Session) -> None:
    headers, user_id, acct, dining, _ = _setup(client, session)
    _seeded(headers, user_id, acct, dining, session)
    rule = _rule(client, headers, dining)

    r = client.post(
        f"/api/v1/rules/{rule['id']}/apply",
        json={"mode": "range", "from": "2026-09-01", "to": "2026-09-30"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Sept 2 unassigned matches; Sept 3 already categorized, overwrite=false.
    assert body["matched_count"] == 2
    assert body["applied_count"] == 1

    r = client.post(
        f"/api/v1/rules/{rule['id']}/apply",
        json={"mode": "all", "overwrite": True},
        headers=headers,
    )
    assert r.json()["applied_count"] == 3


def test_apply_forward_writes_nothing(client: TestClient, session: Session) -> None:
    headers, user_id, acct, dining, _ = _setup(client, session)
    _seeded(headers, user_id, acct, dining, session)
    rule = _rule(client, headers, dining)

    r = client.post(f"/api/v1/rules/{rule['id']}/apply", json={"mode": "forward"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["applied_count"] == 0
    assert r.json()["note"]


def test_apply_range_requires_valid_dates(client: TestClient, session: Session) -> None:
    headers, _, _, dining, _ = _setup(client, session)
    rule = _rule(client, headers, dining)
    r = client.post(f"/api/v1/rules/{rule['id']}/apply", json={"mode": "range"}, headers=headers)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_date_range"

    r = client.post(
        f"/api/v1/rules/{rule['id']}/apply",
        json={"mode": "range", "from": "2026-09-30", "to": "2026-09-01"},
        headers=headers,
    )
    assert r.status_code == 422


def test_apply_tenancy_and_auth(client: TestClient, session: Session) -> None:
    headers, _, _, dining, _ = _setup(client, session)
    rule = _rule(client, headers, dining)

    r = client.post(f"/api/v1/rules/{rule['id']}/apply", json={"mode": "all"})
    assert r.status_code == 401

    other = register(client)
    r = client.post(
        f"/api/v1/rules/{rule['id']}/apply",
        json={"mode": "all"},
        headers=auth(other["access_token"]),
    )
    assert r.status_code == 404


def test_stop_on_match_controls_pipeline_continuation(client: TestClient, session: Session) -> None:
    """A continue-rule lets later rules also apply; a stop-rule halts."""
    from app.services.rules_engine import apply_rules

    headers, user_id, acct, dining, groceries = _setup(client, session)
    seed_transaction(session, user_id, acct, "CAFE LUNA", amount="-20.00", day=date(2026, 9, 2))
    first = _rule(client, headers, dining, pattern="CAFE", priority=10, stop_on_match=False)
    _rule(client, headers, groceries, pattern="CAFE", priority=20)

    txn = session.exec(select(Transaction).where(Transaction.user_id == user_id)).all()
    assert len(txn) == 1
    apply_rules(session, user_id, txn)
    # Both matched: later rule's category wins, first did not stop.
    assert txn[0].category_id == groceries

    # Flip first to stop: its category sticks.
    client.patch(f"/api/v1/rules/{first['id']}", json={"stop_on_match": True}, headers=headers)
    txn[0].category_id = None
    apply_rules(session, user_id, txn)
    assert txn[0].category_id == dining


def test_apply_with_tags_is_idempotent(client: TestClient, session: Session) -> None:
    """Double-applying a rule with tags converges without a 500.

    Regression: _apply_tags did select-then-insert, so concurrent appliers
    500'd on uq_transaction_tags_txn_tag (live incident).
    """
    from app.models.transaction_tags import TransactionTag

    headers, user_id, acct, dining, _ = _setup(client, session)
    _seeded(headers, user_id, acct, dining, session)
    tag_a = client.post("/api/v1/tags", json={"name": "taga"}, headers=headers).json()
    tag_b = client.post("/api/v1/tags", json={"name": "tagb"}, headers=headers).json()
    rule = _rule(client, headers, dining, tag_ids=[tag_a["id"], tag_b["id"]])

    for _ in range(2):
        r = client.post(
            f"/api/v1/rules/{rule['id']}/apply",
            json={"mode": "all", "overwrite": True},
            headers=headers,
        )
        assert r.status_code == 200, r.text

    rows = session.exec(
        select(TransactionTag).where(TransactionTag.tag_id.in_([tag_a["id"], tag_b["id"]]))
    ).all()
    # Every matched row carries exactly the rule's two tags, no duplicates.
    by_txn: dict[int, list[int]] = {}
    for t in rows:
        by_txn.setdefault(t.transaction_id, []).append(t.tag_id)
    assert len(by_txn) == 3, "all three NETFLIX rows tagged"
    for tids in by_txn.values():
        assert sorted(tids) == sorted([tag_a["id"], tag_b["id"]])


# --- staging scope: pending/discarded rows are never backfilled (docs/03) -----


def _staged_txn(
    session: Session, user_id: int, account_id: int, status: ImportBatchStatus, tag: str, day: date
) -> Transaction:
    """A NETFLIX.COM row that belongs to a batch in the given lifecycle state."""
    batch = ImportBatch(
        user_id=user_id,
        account_id=account_id,
        institution="Test Bank",
        file_name=f"{tag}.csv",
        status=status,
    )
    session.add(batch)
    session.flush()
    txn = seed_transaction(session, user_id, account_id, "NETFLIX.COM", amount="-16.99", day=day)
    txn.import_batch_id = batch.id
    session.commit()
    session.refresh(txn)
    return txn


def test_apply_skips_non_committed_batches(client: TestClient, session: Session) -> None:
    """Backfill covers manual + committed rows only.

    docs/03 staging: `pending_review` rows are not live ledger data yet, and
    `discarded` rows must never reappear. Backfilling them would silently
    categorize data the user may throw away. (Preview no longer exists — its
    staged-row exclusion is now the `GET /transactions` population the client
    reads, and the client port is golden-tested against the same predicate.)
    """
    headers, user_id, acct, dining, _ = _setup(client, session)
    manual = seed_transaction(
        session, user_id, acct, "NETFLIX.COM", amount="-16.99", day=date(2026, 9, 2)
    )
    committed = _staged_txn(
        session, user_id, acct, ImportBatchStatus.committed, "live", date(2026, 9, 3)
    )
    pending = _staged_txn(
        session, user_id, acct, ImportBatchStatus.pending_review, "staged", date(2026, 9, 4)
    )
    discarded = _staged_txn(
        session, user_id, acct, ImportBatchStatus.discarded, "dropped", date(2026, 9, 5)
    )

    rule = _rule(client, headers, dining)
    applied = client.post(
        f"/api/v1/rules/{rule['id']}/apply", json={"mode": "all"}, headers=headers
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["matched_count"] == 2
    assert body["applied_count"] == 2

    session.expire_all()
    watched = [manual.id, committed.id, pending.id, discarded.id]
    cats = {
        t.id: t.category_id
        for t in session.exec(select(Transaction).where(Transaction.id.in_(watched))).all()
    }
    assert cats[manual.id] == dining
    assert cats[committed.id] == dining
    assert cats[pending.id] is None, "pending_review row must stay uncategorized"
    assert cats[discarded.id] is None, "discarded row must stay uncategorized"


def test_apply_scope_is_not_widened_by_another_users_committed_batch(
    client: TestClient, session: Session
) -> None:
    """A committed batch for user A never widens user B's backfill scope."""
    _headers_a, user_a, acct_a, _dining, _ = _setup(client, session)
    _staged_txn(session, user_a, acct_a, ImportBatchStatus.committed, "a-live", date(2026, 9, 4))

    other = register(client)
    headers_b = auth(other["access_token"])
    user_b = other["user"]["id"]
    acct_b = create_account(client, headers_b)
    seed_transaction(session, user_b, acct_b, "NETFLIX.COM", amount="-16.99", day=date(2026, 9, 2))
    # Categories are per-user: B's rule must target B's own category.
    dining_b = get_category(session, user_b, "dining")

    rule_b = _rule(client, headers_b, dining_b)
    applied = client.post(
        f"/api/v1/rules/{rule_b['id']}/apply", json={"mode": "all"}, headers=headers_b
    )
    assert applied.status_code == 200, applied.text
    # B matches only B's own row: A's committed NETFLIX row is out of scope.
    assert applied.json()["matched_count"] == 1

    session.expire_all()
    rows_b = session.exec(select(Transaction).where(Transaction.user_id == user_b)).all()
    assert len(rows_b) == 1
    assert rows_b[0].category_id == dining_b

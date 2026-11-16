"""Tags CRUD + transaction attach/detach.

Regression: migration 0016 omitted timestamp server defaults, so every
insert into tags / transaction_tags failed with NotNullViolation — and
POST /tags misreported it as 409 duplicate_tag. Migration 0022 restores
the defaults; these tests pin the behavior.
"""

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.test_deep_common import (
    auth,
    create_account,
    get_category,
    register,
    seed_transaction,
)


def _setup(client: TestClient, session: Session) -> tuple[dict, int, int]:
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    acct = create_account(client, headers)
    return headers, user_id, acct


def test_create_tag_and_duplicate(client: TestClient, session: Session) -> None:
    headers, _, _ = _setup(client, session)
    r = client.post("/api/v1/tags", json={"name": "receipt"}, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "receipt"

    r = client.post("/api/v1/tags", json={"name": "receipt"}, headers=headers)
    assert r.status_code == 409
    assert r.json()["code"] == "duplicate_tag"


def test_attach_and_detach_tag(client: TestClient, session: Session) -> None:
    headers, user_id, acct = _setup(client, session)
    txn = seed_transaction(
        session, user_id, acct, "CAFE LUNA", amount="-12.00", day=date(2026, 9, 2)
    )
    tag_id = client.post("/api/v1/tags", json={"name": "food"}, headers=headers).json()["id"]

    r = client.put(
        f"/api/v1/transactions/{txn.id}/tags", json={"tag_ids": [tag_id]}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert [t["id"] for t in r.json()["items"]] == [tag_id]

    r = client.get(f"/api/v1/transactions/{txn.id}/tags", headers=headers)
    assert [t["name"] for t in r.json()["items"]] == ["food"]

    r = client.delete(f"/api/v1/transactions/{txn.id}/tags/{tag_id}", headers=headers)
    assert r.status_code == 200
    r = client.get(f"/api/v1/transactions/{txn.id}/tags", headers=headers)
    assert r.json()["items"] == []


def test_tags_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/tags").status_code == 401
    assert client.post("/api/v1/tags", json={"name": "x"}).status_code == 401


def test_list_filters_by_tag(client: TestClient, session: Session) -> None:
    """GET /transactions?tag_id= returns only carrying transactions."""
    headers, user_id, acct = _setup(client, session)
    tagged = seed_transaction(
        session, user_id, acct, "TAGGED SHOP", amount="-20.00", day=date(2026, 9, 2)
    )
    seed_transaction(session, user_id, acct, "PLAIN SHOP", amount="-30.00", day=date(2026, 9, 2))
    tag_id = client.post("/api/v1/tags", json={"name": "filterme"}, headers=headers).json()["id"]
    client.put(
        f"/api/v1/transactions/{tagged.id}/tags", json={"tag_ids": [tag_id]}, headers=headers
    )

    r = client.get("/api/v1/transactions", params={"tag_id": tag_id}, headers=headers)
    assert r.status_code == 200, r.text
    assert [t["id"] for t in r.json()["items"]] == [tagged.id]

    # Foreign tag -> 404, not a leak.
    other = register(client)
    r = client.get(
        "/api/v1/transactions",
        params={"tag_id": tag_id},
        headers=auth(other["access_token"]),
    )
    assert r.status_code == 404


def test_set_tags_dedupes_and_replaces(client: TestClient, session: Session) -> None:
    """Duplicate ids in one payload can't 500; replace drops removed tags."""
    headers, user_id, acct = _setup(client, session)
    txn = seed_transaction(
        session, user_id, acct, "CAFE LUNA", amount="-12.00", day=date(2026, 9, 2)
    )
    a = client.post("/api/v1/tags", json={"name": "a"}, headers=headers).json()["id"]
    b = client.post("/api/v1/tags", json={"name": "b"}, headers=headers).json()["id"]

    r = client.put(
        f"/api/v1/transactions/{txn.id}/tags", json={"tag_ids": [a, a, b]}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert sorted(t["id"] for t in r.json()["items"]) == sorted([a, b])

    r = client.put(f"/api/v1/transactions/{txn.id}/tags", json={"tag_ids": [b]}, headers=headers)
    assert [t["id"] for t in r.json()["items"]] == [b]

    # Re-adding an attached tag is a no-op, not a 500.
    r = client.post(f"/api/v1/transactions/{txn.id}/tags", json={"tag_id": b}, headers=headers)
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["items"]] == [b]


def test_tag_list_carries_usage_counts(client: TestClient, session: Session) -> None:
    """GET /tags reports how much each tag is used (M104 management surface)."""
    headers, user_id, acct = _setup(client, session)
    txn = seed_transaction(
        session, user_id, acct, "CAFE LUNA", amount="-12.00", day=date(2026, 9, 2)
    )
    used = client.post("/api/v1/tags", json={"name": "used"}, headers=headers).json()["id"]
    client.post("/api/v1/tags", json={"name": "unused"}, headers=headers)
    client.put(f"/api/v1/transactions/{txn.id}/tags", json={"tag_ids": [used]}, headers=headers)
    rule = client.post(
        "/api/v1/rules",
        json={
            "match_type": "contains",
            "pattern": "LUNA",
            "priority": 10,
            "category_id": get_category(session, user_id, "dining"),
            "tag_ids": [used],
        },
        headers=headers,
    )
    assert rule.status_code == 201, rule.text

    r = client.get("/api/v1/tags", headers=headers)
    assert r.status_code == 200, r.text
    counts = {
        item["name"]: (item["transaction_count"], item["rule_count"]) for item in r.json()["items"]
    }
    assert counts["used"] == (1, 1)
    assert counts["unused"] == (0, 0)


def test_merge_republishes_links_to_the_survivor(client: TestClient, session: Session) -> None:
    """POST /tags/merge folds sources into the target (M104 typo case)."""
    headers, user_id, acct = _setup(client, session)
    both = seed_transaction(
        session, user_id, acct, "GOCERIES RUN", amount="-40.00", day=date(2026, 9, 4)
    )
    only_source = seed_transaction(
        session, user_id, acct, "GOCERIES TOPUP", amount="-5.00", day=date(2026, 9, 5)
    )
    target = client.post("/api/v1/tags", json={"name": "groceries"}, headers=headers).json()["id"]
    source = client.post("/api/v1/tags", json={"name": "goceries"}, headers=headers).json()["id"]
    client.put(
        f"/api/v1/transactions/{both.id}/tags", json={"tag_ids": [target, source]}, headers=headers
    )
    client.put(
        f"/api/v1/transactions/{only_source.id}/tags", json={"tag_ids": [source]}, headers=headers
    )

    r = client.post(
        "/api/v1/tags/merge",
        json={"source_ids": [source], "target_id": target},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target"]["id"] == target
    assert body["merged_tag_ids"] == [source]

    # The survivor is still listed, the source is gone.
    names = {t["name"] for t in client.get("/api/v1/tags", headers=headers).json()["items"]}
    assert names == {"groceries"}
    assert client.get(f"/api/v1/tags/{source}", headers=headers).status_code == 404

    # Links moved, and the row that carried BOTH keeps exactly one row.
    for txn_id in (both.id, only_source.id):
        items = client.get(f"/api/v1/transactions/{txn_id}/tags", headers=headers).json()["items"]
        assert [t["id"] for t in items] == [target]
    counts = {
        t["name"]: (t["transaction_count"], t["rule_count"])
        for t in client.get("/api/v1/tags", headers=headers).json()["items"]
    }
    assert counts["groceries"] == (2, 0)


def test_merge_moves_rule_links_too(client: TestClient, session: Session) -> None:
    headers, user_id, _ = _setup(client, session)
    target = client.post("/api/v1/tags", json={"name": "recurring"}, headers=headers).json()["id"]
    source = client.post("/api/v1/tags", json={"name": "recurng"}, headers=headers).json()["id"]
    rule = client.post(
        "/api/v1/rules",
        json={
            "match_type": "contains",
            "pattern": "NETFLIX",
            "priority": 10,
            "category_id": get_category(session, user_id, "dining"),
            "tag_ids": [source],
        },
        headers=headers,
    )
    assert rule.status_code == 201, rule.text

    r = client.post(
        "/api/v1/tags/merge",
        json={"source_ids": [source], "target_id": target},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    listed = {t["name"]: t for t in client.get("/api/v1/tags", headers=headers).json()["items"]}
    assert listed["recurring"]["rule_count"] == 1
    assert listed["recurring"]["transaction_count"] == 0
    # The rule itself still resolves to a tag (rule_tags rows re-pointed).
    rule_row = client.get(f"/api/v1/rules/{rule.json()['id']}", headers=headers)
    assert rule_row.status_code == 200
    assert [t["id"] for t in rule_row.json()["tags"]] == [target]


def test_merge_refuses_self_empty_and_foreign(client: TestClient, session: Session) -> None:
    headers, _, _ = _setup(client, session)
    tag = client.post("/api/v1/tags", json={"name": "solo"}, headers=headers).json()["id"]

    r = client.post(
        "/api/v1/tags/merge", json={"source_ids": [tag], "target_id": tag}, headers=headers
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_error"

    r = client.post(
        "/api/v1/tags/merge", json={"source_ids": [], "target_id": tag}, headers=headers
    )
    assert r.status_code == 422
    assert r.json()["code"] == "validation_error"

    other = register(client)
    other_headers = auth(other["access_token"])
    foreign = client.post("/api/v1/tags", json={"name": "theirs"}, headers=other_headers).json()[
        "id"
    ]
    r = client.post(
        "/api/v1/tags/merge", json={"source_ids": [foreign], "target_id": tag}, headers=headers
    )
    assert r.status_code == 404

    r = client.post(
        "/api/v1/tags/merge",
        json={"source_ids": [tag], "target_id": tag, "bogus": 1},
        headers=headers,
    )
    assert r.status_code == 422
    assert (
        client.post("/api/v1/tags/merge", json={"source_ids": [tag], "target_id": tag}).status_code
        == 401
    )


def test_list_filters_by_every_selected_tag(client: TestClient, session: Session) -> None:
    """`?tag_ids=` AND-composes: only rows carrying ALL of them match (M104)."""
    headers, user_id, acct = _setup(client, session)
    both = seed_transaction(
        session, user_id, acct, "BOTH TAGS", amount="-10.00", day=date(2026, 9, 2)
    )
    one = seed_transaction(session, user_id, acct, "ONE TAG", amount="-11.00", day=date(2026, 9, 3))
    a = client.post("/api/v1/tags", json={"name": "alpha"}, headers=headers).json()["id"]
    b = client.post("/api/v1/tags", json={"name": "beta"}, headers=headers).json()["id"]
    client.put(f"/api/v1/transactions/{both.id}/tags", json={"tag_ids": [a, b]}, headers=headers)
    client.put(f"/api/v1/transactions/{one.id}/tags", json={"tag_ids": [a]}, headers=headers)

    r = client.get("/api/v1/transactions", params=[("tag_ids", a), ("tag_ids", b)], headers=headers)
    assert r.status_code == 200, r.text
    assert [t["id"] for t in r.json()["items"]] == [both.id]

    # One id still behaves like the single-tag filter.
    r = client.get("/api/v1/transactions", params={"tag_ids": a}, headers=headers)
    assert sorted(t["id"] for t in r.json()["items"]) == sorted([both.id, one.id])

    # A tag neither row carries intersects to nothing, and a foreign id 404s.
    empty = client.post("/api/v1/tags", json={"name": "gamma"}, headers=headers).json()["id"]
    r = client.get(
        "/api/v1/transactions", params=[("tag_ids", a), ("tag_ids", empty)], headers=headers
    )
    assert r.json()["items"] == []
    other = register(client)
    r = client.get(
        "/api/v1/transactions",
        params=[("tag_ids", a)],
        headers=auth(other["access_token"]),
    )
    assert r.status_code == 404

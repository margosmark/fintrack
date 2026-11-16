"""Registry-driven export completeness — audit M13 (docs/11 step 2).

`GET /export/full` must contain EVERY table in the SQLModel/SQLAlchemy model
registry except the explicit, reviewed exclusion list. The old test pinned a
literal key list, so tables added after migration M7 (budgets, goals, debts,
tags, etf_holdings, fundamentals, ...) silently vanished from the export — and
from the encrypted browser backup, which encrypts this same payload and is
docs/33's lost-passphrase recovery guarantee — i.e. they were unrecoverable.

Every assertion here is made against `SQLModel.metadata` (the live registry)
or against the export module's own exclusion dict, never against a pinned
literal table list.
"""

from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import SQLModel, select

import app.api.export as export_module
from app.models import (
    Budget,
    Category,
    Goal,
    Rule,
    RuleMatchType,
    RuleTag,
    Tag,
    Transaction,
    TransactionEvent,
    TransactionEventKind,
    TransactionTag,
    User,
)
from app.services.tx_events import payload_hash
from tests.test_privacy_export import EXPORT_URL, _forbid_network, _register, _seed

# The ONLY tables allowed to stay out of a full export; each justification
# lives in app/api.export.EXCLUDED_TABLES. Growing this set fails the test
# below until it is deliberately added here too, so dropping a user-data table
# always requires a reviewed change instead of happening by default.
JUSTIFIED_EXCLUSIONS = {
    "refresh_tokens",
    "mcp_tokens",
    "alembic_version",
    "backup_artifacts",
    # M74 (docs/38): second-factor secrets / passkey material / SSO links.
    "totp_factors",
    "recovery_codes",
    "webauthn_credentials",
    "oidc_links",
    # M168 (docs/07 §Provider registry): issuer verdicts shared by every
    # tenant — server configuration, not this caller's content, and never
    # writable from a snapshot (backup RESTORE_FORBIDDEN_TABLES).
    "provider_fund_registry",
    # M173 (docs/28 decision 5): the benchmark price series — market data
    # shared by every tenant, the same operator level as `fx_rates`. Not
    # exported and not restorable: a crafted snapshot must not be able to
    # write closes that every tenant's vs-Benchmark panel then reports as the
    # market's own answer.
    "benchmark_prices",
}

# Auth secrets that must never appear in any row of the payload. The four
# M74 names are here as well: those tables are excluded entirely (above), so
# their secret columns appearing would mean the exclusion silently broke.
#
# ONE deliberate exception (docs/43 §10, M90): `sharing_links.token_hash`
# ships. It is the sha256 of a single-use invite token the export's owner
# could mint at will — a digest, not a credential — and the snapshot must
# round-trip through /backup/restore, so the column cannot be stripped.
FORBIDDEN_EXCEPTIONS = {("sharing_links", "token_hash")}
FORBIDDEN_COLUMNS = {
    "hashed_password",
    "llm_api_key",
    "token_hash",
    "secret",
    "pending_secret",
    "code_hash",
}


def _seed_post_m7_tables(session: Session, email_a: str, email_b: str) -> dict[str, int]:
    """Rows in the post-M7 tables for BOTH tenants.

    Coverage assertions must be non-vacuous (each tenant's rows really flow
    through the export) and the junction tables exercise the parent-scoped
    path, which is where a cross-tenant leak would hide.

    M106 adds `transaction_events` here for the same reason: a zero-row table
    makes the caller-scoped loop below vacuous, and it is the table the E2EE
    backup round trip (`tests/test_backup.py`) has to carry — a receipt that
    never reaches the artifact is a receipt a lost passphrase cannot recover.
    Tenant A's receipt is a person's (`actor = user`), tenant B's is a
    cloud-service one (`actor = NULL`), so both halves of the docs/43 §6 actor
    split are represented in the seeded population.
    """
    ids: dict[str, int] = {}
    for label, email in (("a", email_a), ("b", email_b)):
        user = session.exec(select(User).where(User.email == email)).one()
        assert user.id is not None
        category = Category(user_id=user.id, name=f"export-cat-{label}")
        tag = Tag(user_id=user.id, name=f"export-tag-{label}")
        session.add_all([category, tag])
        session.flush()
        assert category.id is not None and tag.id is not None

        budget = Budget(user_id=user.id, category_id=category.id, amount=Decimal("500.00"))
        goal = Goal(
            user_id=user.id,
            name=f"export-goal-{label}",
            target_amount=Decimal("10000.00"),
        )
        rule = Rule(
            user_id=user.id,
            match_type=RuleMatchType.contains,
            pattern="CAFE",
            priority=1,
            category_id=category.id,
        )
        session.add_all([budget, goal, rule])
        session.flush()

        transaction = session.exec(select(Transaction).where(Transaction.user_id == user.id)).one()
        assert transaction.id is not None
        rule_tag = RuleTag(rule_id=rule.id, tag_id=tag.id)
        transaction_tag = TransactionTag(transaction_id=transaction.id, tag_id=tag.id)
        session.add_all([rule_tag, transaction_tag])
        session.flush()
        assert rule_tag.id is not None and transaction_tag.id is not None
        assert budget.id is not None and goal.id is not None

        # M106: one receipt per tenant on that tenant's own row — the payload
        # deliberately carries a money value and a unicode body, because those
        # are exactly what a round trip (JSON snapshot -> ciphertext ->
        # decrypt -> restore, or a CSV cell) is most likely to mangle.
        event_payload = {
            "fields": ["amount_native", "note"],
            "changes": {"amount_native": f"-{label}5.50", "note": "Café ☕ 食品 😀"},
        }
        event = TransactionEvent(
            tx_id=transaction.id,
            user_id=user.id,
            event_kind=TransactionEventKind.edited,
            payload=event_payload,
            payload_hash=payload_hash(event_payload),
            actor=user.id if label == "a" else None,
        )
        session.add(event)
        session.flush()
        assert event.id is not None

        ids[f"tag_{label}"] = tag.id
        ids[f"budget_{label}"] = budget.id
        ids[f"goal_{label}"] = goal.id
        ids[f"rule_tag_{label}"] = rule_tag.id
        ids[f"transaction_tag_{label}"] = transaction_tag.id
        ids[f"event_{label}"] = event.id
    session.commit()
    return ids


def _snapshot(client: TestClient, headers: dict[str, str]) -> dict[str, list]:
    response = client.get(EXPORT_URL, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_export_covers_every_registered_table(
    client: TestClient, session: Session, monkeypatch: Any
) -> None:
    _forbid_network(monkeypatch)
    headers_a, email_a = _register(client)
    _headers_b, email_b = _register(client)
    _seed(session, email_a, email_b)
    seeded = _seed_post_m7_tables(session, email_a, email_b)

    # The exclusion list may not grow without this test being updated too.
    assert set(export_module.EXCLUDED_TABLES) == JUSTIFIED_EXCLUSIONS

    snap = _snapshot(client, headers_a)

    registry = set(SQLModel.metadata.tables)
    # The M13 guard: every registry table reaches the export unless it is on
    # the reviewed exclusion list, and the export invents no keys of its own.
    assert registry - JUSTIFIED_EXCLUSIONS == set(snap)

    # The post-M7 tables named in the audit carry this tenant's rows.
    for table_name in (
        "tags",
        "budgets",
        "goals",
        "rule_tags",
        "transaction_tags",
        "transaction_events",
    ):
        assert table_name in snap, f"{table_name} missing from export"
    assert seeded["tag_a"] in {row["id"] for row in snap["tags"]}
    assert seeded["budget_a"] in {row["id"] for row in snap["budgets"]}
    assert seeded["goal_a"] in {row["id"] for row in snap["goals"]}
    assert seeded["rule_tag_a"] in {row["id"] for row in snap["rule_tags"]}
    assert seeded["transaction_tag_a"] in {row["id"] for row in snap["transaction_tags"]}
    # M106: the receipt rides the SAME snapshot the encrypted browser backup
    # stores (docs/11 § E2EE vault), payload and hash intact, scoped to the
    # caller (`users[0]` is the one row `/export/full` emits).
    owner_id = snap["users"][0]["id"]
    event_row = next(row for row in snap["transaction_events"] if row["id"] == seeded["event_a"])
    assert event_row["user_id"] == owner_id
    assert event_row["event_kind"] == "edited"
    assert event_row["actor"] == owner_id
    assert event_row["payload"]["changes"]["note"] == "Café ☕ 食品 😀"
    assert event_row["payload_hash"] == payload_hash(event_row["payload"])
    assert seeded["event_b"] not in {row["id"] for row in snap["transaction_events"]}


def test_every_exported_table_is_caller_scoped(
    client: TestClient, session: Session, monkeypatch: Any
) -> None:
    _forbid_network(monkeypatch)
    headers_a, email_a = _register(client)
    _headers_b, email_b = _register(client)
    ids = _seed(session, email_a, email_b)
    seeded = _seed_post_m7_tables(session, email_a, email_b)

    snap = _snapshot(client, headers_a)

    # Every table carrying a user_id holds only the caller's rows; fx_rates
    # (no user_id) is the documented shared-cache exception (docs/02).
    for table_name, rows in snap.items():
        if "user_id" not in SQLModel.metadata.tables[table_name].columns:
            continue
        foreign = {row["user_id"] for row in rows} - {ids["user_a"]}
        assert not foreign, f"{table_name} leaked user_id(s) {sorted(foreign)}"

    assert len(snap["users"]) == 1
    assert snap["users"][0]["id"] == ids["user_a"]

    # Junction tables carry no user_id, so they must follow their parent rows.
    assert {row["id"] for row in snap["tags"]} == {seeded["tag_a"]}
    assert {row["id"] for row in snap["rule_tags"]} == {seeded["rule_tag_a"]}
    assert {row["id"] for row in snap["transaction_tags"]} == {seeded["transaction_tag_a"]}

    # Tenant B is absent from the post-M7 tables, junctions included.
    assert seeded["tag_b"] not in {row["id"] for row in snap["tags"]}
    assert seeded["budget_b"] not in {row["id"] for row in snap["budgets"]}
    assert seeded["goal_b"] not in {row["id"] for row in snap["goals"]}
    assert seeded["rule_tag_b"] not in {row["id"] for row in snap["rule_tags"]}
    assert seeded["transaction_tag_b"] not in {row["id"] for row in snap["transaction_tags"]}
    assert ids["transaction_b"] not in {row["id"] for row in snap["transactions"]}

    # No auth secret reaches the payload — excluded tables stay excluded.
    leaked = [
        f"{table_name}.{column}"
        for table_name, rows in snap.items()
        for row in rows
        for column in row
        if column in FORBIDDEN_COLUMNS and (table_name, column) not in FORBIDDEN_EXCEPTIONS
    ]
    assert leaked == []

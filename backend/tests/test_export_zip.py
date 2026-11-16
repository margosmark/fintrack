"""M75 one-click export — the ZIP bundle (docs/11 § "One-click export").

Pins the acceptance behaviours of the ZIP path:

1. the ZIP contains the manifest plus exactly one CSV per exported registry
   table, and every CSV's row count matches the database;
2. visibility is the app's: committed/manual rows in, staged (pending review,
   discarded, reverted) rows out — for `transactions`, `trades`,
   `import_batches` and the `transaction_tags` junction alike;
3. a locked vault exports `E2EE:<ciphertext>` for cipher-backed fields and
   never that field's plaintext, while `vault=unlocked` ships plaintext;
4. `/export/full` keeps its complete machine-snapshot population (a backup
   has to restore staged rows too).

The demo-tenant 200 lives in tests/test_export_zip_demo.py (hermetic demo
database). Registry expectations stay DERIVED
(`tests/test_privacy_export.EXPECTED_KEYS`) — a pinned literal table list is
exactly the defect audit M13 flagged.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.export import STRIPPED_COLUMNS, VISIBILITY_FILTERS
from app.models import (
    Account,
    AccountType,
    ImportBatch,
    ImportBatchStatus,
    Security,
    SecurityKind,
    Tag,
    Trade,
    TradeAction,
    Transaction,
    TransactionEvent,
    TransactionEventKind,
    TransactionTag,
    User,
)
from app.services.tx_events import payload_hash
from tests.test_privacy_export import EXPECTED_KEYS, _forbid_network, _register, _seed

ZIP_URL = "/api/v1/export/zip"
FULL_URL = "/api/v1/export/full"


def _zip_entries(response) -> dict[str, bytes]:
    """(name -> bytes) for a 200 ZIP response, with the wire contract pinned."""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="fin-tracker-export-'), disposition
    assert disposition.endswith('.zip"'), disposition
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None, "corrupt ZIP member"
        return {name: archive.read(name) for name in archive.namelist()}


def _csv_rows(payload: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(payload.decode("utf-8"))))


def _csv_map(entries: dict[str, bytes]) -> dict[str, list[list[str]]]:
    return {
        name.removesuffix(".csv"): _csv_rows(payload)
        for name, payload in entries.items()
        if name.endswith(".csv")
    }


def _manifest(entries: dict[str, bytes]) -> dict:
    assert "manifest.json" in entries, sorted(entries)
    return json.loads(entries["manifest.json"])


def _ids(rows: list[list[str]], column: str) -> set[int]:
    header = rows[0]
    assert column in header, header
    index = header.index(column)
    return {int(row[index]) for row in rows[1:] if row[index] != ""}


def _txn(
    user_id: int,
    account_id: int,
    day: date,
    *,
    raw_description: str,
    dedup_hash: str,
    batch_id: int | None = None,
    **extra,
) -> Transaction:
    return Transaction(
        user_id=user_id,
        account_id=account_id,
        date=day,
        raw_description=raw_description,
        normalized_payee=raw_description,
        amount_native=Decimal("-4.50"),
        currency="CAD",
        cad_amount=Decimal("-4.50"),
        dedup_hash=dedup_hash,
        import_batch_id=batch_id,
        **extra,
    )


def _seed_staging(session: Session, email: str) -> dict[str, int]:
    """One committed batch (2 txns, one tagged) + one pending batch (1 tagged txn)."""
    user = session.exec(select(User).where(User.email == email)).one()
    assert user.id is not None
    account = Account(
        user_id=user.id, name="Staging Chequing", type=AccountType.chequing, currency="CAD"
    )
    session.add(account)
    session.flush()
    assert account.id is not None
    today = datetime.now(UTC).date()

    committed = ImportBatch(
        user_id=user.id,
        account_id=account.id,
        institution="RBC",
        file_name="committed.csv",
        status=ImportBatchStatus.committed,
    )
    pending = ImportBatch(
        user_id=user.id,
        account_id=account.id,
        institution="RBC",
        file_name="pending.csv",
        status=ImportBatchStatus.pending_review,
    )
    session.add_all([committed, pending])
    session.flush()
    assert committed.id is not None and pending.id is not None

    tagged = _txn(
        user.id,
        account.id,
        today,
        raw_description="COMMITTED WITH TAG",
        dedup_hash="zip-hash-1",
        batch_id=committed.id,
    )
    manual = _txn(user.id, account.id, today, raw_description="MANUAL ROW", dedup_hash="zip-hash-2")
    staged = _txn(
        user.id,
        account.id,
        today,
        raw_description="STAGED ROW",
        dedup_hash="zip-hash-3",
        batch_id=pending.id,
    )
    session.add_all([tagged, manual, staged])
    session.flush()

    visible_tag = Tag(user_id=user.id, name="visible-tag")
    hidden_tag = Tag(user_id=user.id, name="hidden-tag")
    session.add_all([visible_tag, hidden_tag])
    session.flush()
    assert visible_tag.id is not None and hidden_tag.id is not None
    assert tagged.id is not None and staged.id is not None
    session.add_all(
        [
            TransactionTag(transaction_id=tagged.id, tag_id=visible_tag.id),
            TransactionTag(transaction_id=staged.id, tag_id=hidden_tag.id),
        ]
    )
    session.commit()

    return {
        "user_id": user.id,
        "account_id": account.id,
        "committed_id": tagged.id,
        "manual_id": manual.id,
        "staged_id": staged.id,
        "committed_batch_id": committed.id,
        "pending_batch_id": pending.id,
    }


def test_zip_has_manifest_plus_one_csv_per_exported_table(
    client: TestClient, session: Session, monkeypatch
) -> None:
    headers, email = _register(client)
    _headers_b, email_b = _register(client)
    _seed(session, email, email_b)
    _forbid_network(monkeypatch)  # no LLM, no market data, ever

    entries = _zip_entries(client.get(ZIP_URL, headers=headers))

    expected = {"manifest.json"} | {f"{table}.csv" for table in EXPECTED_KEYS}
    assert set(entries) == expected, sorted(expected ^ set(entries))

    # The auth-secret columns are absent from the header, not blanked inside it.
    users_csv = entries["users.csv"].decode("utf-8")
    for secret in ("hashed_password", "llm_api_key", "token_hash"):
        assert secret not in users_csv, f"{secret} leaked into users.csv"
    assert STRIPPED_COLUMNS["users"] == ("hashed_password", "llm_api_key")


def test_zip_row_counts_match_the_database(client: TestClient, session: Session) -> None:
    headers, email = _register(client)
    ids = _seed_staging(session, email)

    entries = _zip_entries(client.get(ZIP_URL, headers=headers))
    tables = _csv_map(entries)
    manifest = _manifest(entries)

    # Every table not on the visibility list has the population the long-
    # standing full snapshot returns (both are plain database reads).
    full = client.get(FULL_URL, headers=headers)
    assert full.status_code == 200, full.text
    snapshot = full.json()

    for table, rows in tables.items():
        assert len(rows) >= 1, f"{table}.csv has no header row"
        if table in VISIBILITY_FILTERS:
            continue
        assert len(rows) - 1 == len(snapshot[table]), (
            f"{table}: CSV {len(rows) - 1} rows vs snapshot {len(snapshot[table])}"
        )

    # The staged population is exactly what disappears.
    assert _ids(tables["transactions"], "id") == {ids["committed_id"], ids["manual_id"]}
    assert ids["staged_id"] not in _ids(tables["transactions"], "id")
    # ...and it is still in the machine snapshot (a backup restores it).
    assert ids["staged_id"] in {row["id"] for row in snapshot["transactions"]}
    assert _ids(tables["import_batches"], "id") == {ids["committed_batch_id"]}
    assert len(snapshot["import_batches"]) == 2

    # The junction follows its parent: the staged transaction's tag link goes.
    assert _ids(tables["transaction_tags"], "transaction_id") == {ids["committed_id"]}
    assert len(tables["transaction_tags"]) == 2  # header + one link

    # The manifest documents the file it ships with: counts and columns per CSV.
    assert manifest["kind"] == "fintrack-export"
    assert manifest["v"] == 1
    assert manifest["visibility"] == "committed"
    assert manifest["vault"] == "locked"  # fail-closed default
    assert manifest["visibility_filters"] == VISIBILITY_FILTERS
    assert manifest["user"] == {"id": ids["user_id"], "email": email}
    assert manifest["markers"]["e2ee"].startswith("E2EE:")
    assert set(manifest["excluded_tables"]) == {
        "refresh_tokens",
        "backup_artifacts",
        "mcp_tokens",
        "alembic_version",
        # M74 (docs/38): second-factor secrets / passkey material / SSO links.
        "totp_factors",
        "recovery_codes",
        "webauthn_credentials",
        "oidc_links",
        # M168 (docs/07 §Provider registry): operator-level coverage data.
        "provider_fund_registry",
        # M173 (docs/28 decision 5): the shared benchmark price series.
        "benchmark_prices",
    }

    listed = {entry["name"]: entry for entry in manifest["files"]}
    assert set(listed) == {f"{table}.csv" for table in EXPECTED_KEYS}
    for name, entry in listed.items():
        rows = tables[name.removesuffix(".csv")]
        assert entry["rows"] == len(rows) - 1, name
        assert entry["columns"] == rows[0], name


def test_zip_trades_exclude_staged_rows(client: TestClient, session: Session) -> None:
    """The investment CSV stages trades through the same batch lifecycle."""
    headers, email = _register(client)
    user = session.exec(select(User).where(User.email == email)).one()
    assert user.id is not None
    account = Account(user_id=user.id, name="TFSA", type=AccountType.tfsa, currency="CAD")
    security = Security(
        user_id=user.id,
        symbol="XIC",
        name="iShares Core S&P/TSX",
        kind=SecurityKind.etf,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add_all([account, security])
    session.flush()
    assert account.id is not None and security.id is not None
    pending = ImportBatch(
        user_id=user.id,
        account_id=account.id,
        institution="",
        file_name="trades.csv",
        status=ImportBatchStatus.pending_review,
    )
    session.add(pending)
    session.flush()
    assert pending.id is not None
    today = datetime.now(UTC).date()
    manual_trade = Trade(
        user_id=user.id,
        account_id=account.id,
        security_id=security.id,
        action=TradeAction.buy,
        date=today,
        quantity=Decimal("10"),
        price_native=Decimal("30"),
    )
    staged_trade = Trade(
        user_id=user.id,
        account_id=account.id,
        security_id=security.id,
        action=TradeAction.buy,
        date=today,
        quantity=Decimal("5"),
        price_native=Decimal("31"),
        import_batch_id=pending.id,
    )
    session.add_all([manual_trade, staged_trade])
    session.commit()
    assert manual_trade.id is not None

    entries = _zip_entries(client.get(ZIP_URL, headers=headers))
    trades = _csv_map(entries)["trades"]
    assert len(trades[1:]) == 1, "only the manual trade is visible to the app"
    assert _ids(trades, "id") == {manual_trade.id}


def test_zip_transaction_events_follow_the_row_and_keep_the_model_shape(
    client: TestClient, session: Session
) -> None:
    """M106 (docs/11 § visibility, docs/02 §transaction_events): a receipt is
    as visible as the row it describes — except `deleted`, whose whole purpose
    is to outlive its subject — and the CSV keeps the model's own column order
    so the file can be parsed back without a schema guess.
    """
    headers, email = _register(client)
    _other_headers, other_email = _register(client)
    tenant = _seed(session, email, other_email)
    ids = _seed_staging(session, email)

    receipt_payload = {
        "fields": ["amount_native", "note"],
        "changes": {"amount_native": "-99.99", "note": "Café ☕ 食品 😀"},
    }
    visible = TransactionEvent(
        tx_id=ids["committed_id"],
        user_id=ids["user_id"],
        event_kind=TransactionEventKind.edited,
        payload=receipt_payload,
        payload_hash=payload_hash(receipt_payload),
        actor=ids["user_id"],
    )
    staged_payload = {"source": "import", "batch_id": ids["pending_batch_id"]}
    staged = TransactionEvent(
        tx_id=ids["staged_id"],
        user_id=ids["user_id"],
        event_kind=TransactionEventKind.created,
        payload=staged_payload,
        payload_hash=payload_hash(staged_payload),
        actor=None,
    )
    # `tx_id` has no FK (docs/02), so a receipt CAN name a row that exists and
    # belongs to another tenant. "The row is gone" is the rule, not "the row is
    # not mine" — a foreign receipt must never ride this tenant's file.
    foreign_payload = {"fields": ["amount_native"], "changes": {"amount_native": "-1.00"}}
    foreign = TransactionEvent(
        tx_id=tenant["transaction_b"],
        user_id=ids["user_id"],
        event_kind=TransactionEventKind.edited,
        payload=foreign_payload,
        payload_hash=payload_hash(foreign_payload),
        actor=ids["user_id"],
    )
    session.add_all([visible, staged, foreign])
    session.commit()
    assert visible.id is not None and staged.id is not None and foreign.id is not None

    # Delete through the API: the `deleted` receipt is written by the real
    # path and its tx_id dangles the moment the row goes.
    doomed = ids["manual_id"]
    deleted = client.delete(f"/api/v1/transactions/{doomed}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    receipt_ids = [
        event.id
        for event in session.exec(
            select(TransactionEvent).where(TransactionEvent.tx_id == doomed)
        ).all()
    ]
    assert len(receipt_ids) == 1
    dead_receipt = receipt_ids[0]

    entries = _zip_entries(client.get(ZIP_URL, headers=headers))
    table = _csv_map(entries)["transaction_events"]
    header = table[0]

    # Stable shape: the header IS the model's field order (docs/11 § CSV
    # dialect), so `_columns()` and the ORM cannot drift apart silently.
    assert header == list(TransactionEvent.model_fields)
    shipped = _ids(table, "id")
    assert shipped == {visible.id, dead_receipt}, shipped

    # The staged and the foreign receipts are out of the human file …
    assert staged.id not in shipped
    assert foreign.id not in shipped
    # … and every receipt is still in the machine snapshot a backup restores
    # (docs/11 § visibility: /export/full keeps the whole population).
    snapshot = client.get(FULL_URL, headers=headers)
    assert snapshot.status_code == 200, snapshot.text
    machine_ids = {row["id"] for row in snapshot.json()["transaction_events"]}
    assert {visible.id, staged.id, foreign.id, dead_receipt} <= machine_ids

    # The payload cell parses back to the exact object it was written from,
    # unicode and nested money value included (raw UTF-8, never \uXXXX).
    row = next(row for row in table[1:] if row[header.index("id")] == str(visible.id))
    assert len(row) == len(header), "a ragged row is not a stable shape"
    parsed = json.loads(row[header.index("payload")])
    assert parsed == receipt_payload
    assert "Café ☕ 食品 😀" in row[header.index("payload")]
    assert row[header.index("payload_hash")] == payload_hash(parsed)
    assert row[header.index("actor")] == str(ids["user_id"])
    assert row[header.index("event_kind")] == "edited"

    # The manifest documents the population it applied (docs/11 § visibility).
    assert "transaction_events" in _manifest(entries)["visibility_filters"]


def test_locked_vault_ships_the_marker_never_the_plaintext(
    client: TestClient, session: Session
) -> None:
    """docs/11 § Export & the vault: ciphertext + marker, never fake plaintext."""
    headers, email = _register(client)
    user = session.exec(select(User).where(User.email == email)).one()
    assert user.id is not None
    account = Account(
        user_id=user.id,
        name="SECRET ACCOUNT NAME",
        type=AccountType.chequing,
        currency="CAD",
        name_cipher="v1.YWNjb3VudA==",
    )
    session.add(account)
    session.flush()
    assert account.id is not None
    session.add(
        _txn(
            user.id,
            account.id,
            datetime.now(UTC).date(),
            raw_description="SECRET PAYEE COFFEE",
            dedup_hash="cipher-hash-1",
            payee_cipher="v1.cGF5ZWU=",
            raw_desc_cipher="v1.cmF3",
            amount_cipher="v1.YW1vdW50",
            cad_amount_cipher="v1.Y2FkZW1v.cGF5",
        )
    )
    session.commit()

    locked = client.get(ZIP_URL, headers=headers)  # default vault=locked
    entries = _zip_entries(locked)
    transactions = _csv_rows(entries["transactions.csv"])
    header = transactions[0]
    joined = entries["transactions.csv"].decode("utf-8")
    assert "SECRET PAYEE COFFEE" not in joined, "plaintext leaked while locked"
    assert "SECRET ACCOUNT NAME" not in entries["accounts.csv"].decode("utf-8")

    row = transactions[1]
    assert row[header.index("raw_description")] == "E2EE:v1.cmF3", row
    assert row[header.index("raw_desc_cipher")] == "v1.cmF3", "the ciphertext column still ships"
    accounts = _csv_rows(entries["accounts.csv"])
    assert accounts[1][accounts[0].index("name")] == "E2EE:v1.YWNjb3VudA=="

    manifest = _manifest(entries)
    assert manifest["vault"] == "locked"
    assert manifest["markers"]["e2ee"].startswith("E2EE:")
    assert manifest["masked_cells"] == 5  # 4 transaction fields + the account name
    assert int(locked.headers["x-export-masked-cells"]) == 5

    unlocked = _zip_entries(client.get(ZIP_URL, headers=headers, params={"vault": "unlocked"}))
    assert "SECRET PAYEE COFFEE" in unlocked["transactions.csv"].decode("utf-8")
    assert "E2EE:" not in unlocked["transactions.csv"].decode("utf-8")
    assert _manifest(unlocked)["vault"] == "unlocked"
    assert _manifest(unlocked)["masked_cells"] == 0


def test_unknown_vault_state_is_a_422_envelope(client: TestClient) -> None:
    headers, _email = _register(client)
    response = client.get(ZIP_URL, headers=headers, params={"vault": "maybe"})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "export_invalid_vault"
    assert "locked" in body["detail"] and "unlocked" in body["detail"]


def test_zip_requires_authentication(client: TestClient) -> None:
    assert client.get(ZIP_URL).status_code == 401


def test_zip_is_tenant_scoped(client: TestClient, session: Session) -> None:
    headers_a, email_a = _register(client)
    _headers_b, email_b = _register(client)
    _seed(session, email_a, email_b)

    entries = _zip_entries(client.get(ZIP_URL, headers=headers_a))
    accounts = _csv_rows(entries["accounts.csv"])
    names = [row[accounts[0].index("name")] for row in accounts[1:]]
    assert "TD" not in names, "tenant B's account leaked into A's export"
    assert "Savings" not in names, "tenant B's account leaked into A's export"
    assert "Chequing" in names, names

    institutions = _csv_rows(entries["institutions.csv"])
    inst_names = [row[institutions[0].index("name")] for row in institutions[1:]]
    assert "TD" not in inst_names and "RBC" in inst_names

"""M56 scheduled client-side-encrypted backup — docs/11 § Scheduled backup & restore.

The acceptance test is `test_restore_roundtrip_after_wipe`: backup → wipe the
tenant's rows in this test DB → restore → semantic equality on the sampled
tables (and the full snapshot). Everything else pins the schedule contract,
the artifact wire format, keep-last-N retention and every error code docs/11
lists for `/backup/*`.

Crypto here is an INDEPENDENT Python implementation of the format the browser
produces (`v1.<base64(iv)>.<base64(ciphertext)>`, AES-GCM-256 under the DEK);
the client's own implementation is pinned by
`frontend/tests/backupFixture.test.ts`, whose committed output
`frontend/tests/fixtures/backupArtifact.v1.json` this file decrypts end to
end (PBKDF2 KEK → unwrap DEK → field envelope) — neither side is trusted to
agree with the other by declaration.
"""

import base64
import copy
import hashlib
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import SQLModel, select

import app.api.backup as backup_module
from app.api.export import _MODELS, EXCLUDED_TABLES, VIA_PARENT
from app.api.users import _USER_OWNED_MODELS, delete_user_rows
from app.models import (
    Category,
    FxRate,
    Goal,
    McpToken,
    RefreshToken,
    Transaction,
    User,
)
from app.services.demo_seed import DEMO_EMAIL
from app.services.tx_events import payload_hash
from tests.test_auth_security_hardening import _auth, _make_user
from tests.test_export_registry import _seed_post_m7_tables
from tests.test_privacy_export import EXPORT_URL, _forbid_network, _register, _seed

SCHEDULE_URL = "/api/v1/backup/schedule"
ARTIFACTS_URL = "/api/v1/backup/artifacts"
RESTORE_URL = "/api/v1/backup/restore"

PERIOD = "2026-09-28"
PBKDF2_ITERATIONS = 600_000  # docs/33 KEK parameters — mirrored by vault.ts
DEK_DOMAIN = 0x01  # vault.ts wrapIV domain separation byte for the DEK

# Produced by the REAL client code (`WRITE_BACKUP_FIXTURE=1` in
# frontend/tests/backupFixture.test.ts); decrypted below in Python.
FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "tests"
    / "fixtures"
    / "backupArtifact.v1.json"
)


# ── crypto helpers (second implementation of the documented format) ──────────


def _encrypt(payload: dict[str, Any], dek: bytes) -> str:
    iv = os.urandom(12)
    ciphertext = AESGCM(dek).encrypt(iv, json.dumps(payload).encode(), None)
    return "v1." + base64.b64encode(iv).decode() + "." + base64.b64encode(ciphertext).decode()


def _decrypt(ciphertext: str, dek: bytes) -> dict[str, Any]:
    version, iv_b64, body_b64 = ciphertext.split(".")
    assert version == "v1"
    plaintext = AESGCM(dek).decrypt(base64.b64decode(iv_b64), base64.b64decode(body_b64), None)
    return json.loads(plaintext)


def _unwrap_dek(passphrase: str, salt: bytes, wrapped_dek: bytes) -> bytes:
    """PBKDF2-SHA256/600k → KEK → AES-GCM unwrap (vault.ts::unwrap/deriveKEK)."""
    kek = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, PBKDF2_ITERATIONS, dklen=32)
    iv = bytearray(salt[:12])
    iv[11] ^= DEK_DOMAIN  # vault.ts wrapIV(salt, DEK_DOMAIN)
    return AESGCM(kek).decrypt(bytes(iv), wrapped_dek, None)


def _payload(snapshot: dict[str, list], period: str = PERIOD) -> dict[str, Any]:
    # Deep copy: a test that corrupts a payload row to provoke a failure must
    # not be corrupting the snapshot it later compares the tenant against.
    return {
        "v": 1,
        "kind": "fintrack-backup",
        "period": period,
        "exported_at": "2026-09-28T03:12:44.123Z",
        "snapshot": copy.deepcopy(snapshot),
    }


def _upload(
    client: TestClient, headers: dict[str, str], snapshot: dict[str, list], period: str = PERIOD
) -> tuple[bytes, dict[str, Any]]:
    """Encrypt like the browser and POST the wire object. Returns (dek, response)."""
    dek = os.urandom(32)
    body = {"period": period, "ciphertext": _encrypt(_payload(snapshot, period), dek)}
    response = client.post(ARTIFACTS_URL, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return dek, response.json()


def _snapshot(client: TestClient, headers: dict[str, str]) -> dict[str, list]:
    response = client.get(EXPORT_URL, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _restore(
    client: TestClient, headers: dict[str, str], payload: dict[str, Any], confirm: str = "restore"
):
    return client.post(RESTORE_URL, json={"confirm": confirm, "payload": payload}, headers=headers)


def _fx_row(session: Session, base: str, quote: str, day: str) -> FxRate:
    # The snapshot carries `date` as an ISO string; the column is a DATE, so
    # comparing them directly would be `date = varchar` and PG refuses.
    return session.exec(
        select(FxRate).where(
            FxRate.base == base, FxRate.quote == quote, FxRate.date == date.fromisoformat(day)
        )
    ).one()


def _columns(table: str) -> set[str]:
    """Column names of an exported table (from the live ORM metadata)."""
    return set(SQLModel.metadata.tables[table].columns.keys())


def _enrich(session: Session, email: str) -> dict[str, Any]:
    """Extra rows so the round trip covers more than `_seed`'s five tables."""
    user = session.exec(select(User).where(User.email == email)).one()
    category = Category(user_id=user.id, name="m6-roundtrip-cat")
    mcp_token = McpToken(user_id=user.id, name="cli", token_hash="f" * 64)
    session.add_all([category, mcp_token])
    session.commit()
    return {"user": user, "category": category.id}


# ── schedule ────────────────────────────────────────────────────────────────


def test_schedule_defaults_roundtrip_and_every_range_code(client: TestClient) -> None:
    headers, _ = _register(client)

    fresh = client.get(SCHEDULE_URL, headers=headers)
    assert fresh.status_code == 200, fresh.text
    assert fresh.json() == {
        "frequency": "off",
        "hour": 3,
        "weekday": 0,
        "day_of_month": 1,
        "keep_last_n": 5,
    }

    wanted = {
        "frequency": "weekly",
        "hour": 4,
        "weekday": 2,
        "day_of_month": 7,
        "keep_last_n": 3,
    }
    saved = client.put(SCHEDULE_URL, json=wanted, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json() == wanted
    assert client.get(SCHEDULE_URL, headers=headers).json() == wanted

    # Unknown key: Pydantic's `extra="forbid"` → generic validation_error.
    typo = client.put(SCHEDULE_URL, json={**wanted, "keep_lastn": 3}, headers=headers)
    assert typo.status_code == 422
    assert typo.json()["code"] == "validation_error"

    # Wrong type → validation_error (shape), not the domain code.
    wrong_type = client.put(SCHEDULE_URL, json={**wanted, "hour": "three"}, headers=headers)
    assert wrong_type.status_code == 422
    assert wrong_type.json()["code"] == "validation_error"

    # Every out-of-range / unknown-frequency value → backup_invalid_schedule,
    # with the offending field named in the message (docs/11 error table).
    bad_cases: list[dict[str, Any]] = [
        {**wanted, "frequency": "yearly"},
        {**wanted, "frequency": "off-duty"},
        {**wanted, "hour": 24},
        {**wanted, "hour": -1},
        {**wanted, "weekday": 7},
        {**wanted, "day_of_month": 29},
        {**wanted, "day_of_month": 0},
        {**wanted, "keep_last_n": 0},
        {**wanted, "keep_last_n": 21},
    ]
    for case in bad_cases:
        response = client.put(SCHEDULE_URL, json=case, headers=headers)
        assert response.status_code == 422, (case, response.text)
        assert response.json()["code"] == "backup_invalid_schedule", case
        assert (
            "must be" in response.json()["detail"] or "is not one of" in (response.json()["detail"])
        )

    # The failed writes must not have moved the stored schedule.
    assert client.get(SCHEDULE_URL, headers=headers).json() == wanted

    assert client.get(SCHEDULE_URL).status_code == 401
    assert client.put(SCHEDULE_URL, json=wanted).status_code == 401


def test_schedule_is_per_tenant_and_demo_reads_allowed_writes_blocked(
    client: TestClient, session: Session
) -> None:
    headers_a, _ = _register(client)
    headers_b, _ = _register(client)
    daily = {
        "frequency": "daily",
        "hour": 6,
        "weekday": 0,
        "day_of_month": 1,
        "keep_last_n": 5,
    }
    assert client.put(SCHEDULE_URL, json=daily, headers=headers_a).status_code == 200
    assert client.get(SCHEDULE_URL, headers=headers_b).json()["frequency"] == "off"

    demo_user = _make_user(session, DEMO_EMAIL)
    demo_headers = _auth(demo_user)
    # Reads stay open for the shared demo tenant (the guard is method-scoped).
    assert client.get(SCHEDULE_URL, headers=demo_headers).status_code == 200
    blocked = client.put(SCHEDULE_URL, json=daily, headers=demo_headers)
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "demo_account_restricted"


# ── artifacts ───────────────────────────────────────────────────────────────


def test_artifact_upload_list_get_delete_roundtrip(client: TestClient) -> None:
    headers_a, _ = _register(client)
    headers_b, _ = _register(client)
    snapshot = _snapshot(client, headers_a)
    dek, created = _upload(client, headers_a, snapshot)

    assert created["period"] == PERIOD
    assert created["size_bytes"] > 0
    artifact_id = created["id"]

    # The list is metadata-only: a 8 MB artifact must not ride on every render.
    listed = client.get(ARTIFACTS_URL, headers=headers_a)
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert [item["id"] for item in items] == [artifact_id]
    assert set(items[0]) == {"id", "period", "created_at", "size_bytes"}

    # Detail carries the ciphertext, and it decrypts to exactly what was sent.
    detail = client.get(f"{ARTIFACTS_URL}/{artifact_id}", headers=headers_a)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert set(body) == {"id", "period", "created_at", "size_bytes", "ciphertext"}
    assert _decrypt(body["ciphertext"], dek) == _payload(snapshot)

    # Tenancy: foreign ids are 404, indistinguishable from unknown ids.
    assert client.get(f"{ARTIFACTS_URL}/{artifact_id}", headers=headers_b).status_code == 404
    assert client.get(f"{ARTIFACTS_URL}/{artifact_id + 9999}").status_code == 401
    assert client.get(f"{ARTIFACTS_URL}/{artifact_id + 9999}", headers=headers_a).status_code == 404
    assert client.delete(f"{ARTIFACTS_URL}/{artifact_id}", headers=headers_b).status_code == 404

    assert client.delete(f"{ARTIFACTS_URL}/{artifact_id}", headers=headers_a).status_code == 204
    assert client.get(f"{ARTIFACTS_URL}/{artifact_id}", headers=headers_a).status_code == 404
    assert client.get(ARTIFACTS_URL, headers=headers_a).json()["items"] == []

    assert client.get(ARTIFACTS_URL).status_code == 401
    assert (
        client.post(
            ARTIFACTS_URL,
            json={"period": PERIOD, "ciphertext": "v1.YQ==.Yg=="},
        ).status_code
        == 401
    )


def test_artifact_post_is_an_upsert_on_the_period(client: TestClient) -> None:
    """The scheduler's idempotency key: one row per (user, period)."""
    headers, _ = _register(client)
    snapshot = _snapshot(client, headers)

    _dek_first, first = _upload(client, headers, snapshot)
    snapshot["accounts"].append({"anything": "replaced"})  # a different payload
    _dek_second, second = _upload(client, headers, snapshot)

    assert second["id"] == first["id"]
    assert second["size_bytes"] != first["size_bytes"]

    items = client.get(ARTIFACTS_URL, headers=headers).json()["items"]
    assert len(items) == 1
    stored = client.get(f"{ARTIFACTS_URL}/{first['id']}", headers=headers).json()
    assert stored["period"] == PERIOD
    # The stored ciphertext is the SECOND upload's (fresh data wins).
    assert _decrypt(stored["ciphertext"], _dek_second) == _payload(snapshot)


def test_artifact_rejects_bad_period_envelope_size_and_shape(
    client: TestClient, monkeypatch: Any
) -> None:
    headers, _ = _register(client)

    # The documented ceiling itself (docs/11 artifact format).
    assert backup_module.MAX_ARTIFACT_CHARS == 8_000_000

    for bad_period in ("2026-13-99", "2026-09-28T03:00", "garbage", "2026-0928", ""):
        response = client.post(
            ARTIFACTS_URL,
            json={"period": bad_period, "ciphertext": "v1.YQ==.Yg=="},
            headers=headers,
        )
        assert response.status_code == 422, (bad_period, response.text)
        assert response.json()["code"] == "backup_invalid_period"

    for bad_envelope in ("", "hello", "v1.only-two-parts", "v2.YQ==.Yg==", "v1..Yg=="):
        response = client.post(
            ARTIFACTS_URL,
            json={"period": PERIOD, "ciphertext": bad_envelope},
            headers=headers,
        )
        assert response.status_code == 422, (bad_envelope, response.text)
        assert response.json()["code"] == "backup_invalid_envelope"

    # Oversize: shrink the constant so the test does not move 8 MB around;
    # the boundary value itself is pinned above.
    monkeypatch.setattr(backup_module, "MAX_ARTIFACT_CHARS", 10)
    response = client.post(
        ARTIFACTS_URL,
        json={"period": PERIOD, "ciphertext": "v1." + "A" * 40 + ".Bb=="},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "backup_too_large"

    # Shape: unknown key and missing key are Pydantic validation_errors.
    assert (
        client.post(
            ARTIFACTS_URL,
            json={"period": PERIOD, "ciphertext": "v1.YQ==.Yg==", "extra": 1},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.post(
            ARTIFACTS_URL,
            json={"period": PERIOD},
            headers=headers,
        ).status_code
        == 422
    )
    # Nothing above may have created a row.
    assert client.get(ARTIFACTS_URL, headers=headers).json()["items"] == []


def test_retention_keeps_newest_n_and_prunes_when_lowered(client: TestClient) -> None:
    headers, _ = _register(client)
    assert (
        client.put(
            SCHEDULE_URL,
            json={
                "frequency": "daily",
                "hour": 3,
                "weekday": 0,
                "day_of_month": 1,
                "keep_last_n": 3,
            },
            headers=headers,
        ).status_code
        == 200
    )

    periods = [f"2026-09-{day:02d}" for day in range(1, 6)]
    for period in periods:
        response = client.post(
            ARTIFACTS_URL,
            json={"period": period, "ciphertext": _encrypt(_payload({}, period), os.urandom(32))},
            headers=headers,
        )
        assert response.status_code == 201, response.text

    kept = [item["period"] for item in client.get(ARTIFACTS_URL, headers=headers).json()["items"]]
    assert kept == list(reversed(periods[-3:]))  # newest first, oldest dropped

    # Lowering keep_last_n prunes immediately, inside the PUT (docs/11).
    lowered = client.put(
        SCHEDULE_URL,
        json={
            "frequency": "daily",
            "hour": 3,
            "weekday": 0,
            "day_of_month": 1,
            "keep_last_n": 1,
        },
        headers=headers,
    )
    assert lowered.status_code == 200
    kept = [item["period"] for item in client.get(ARTIFACTS_URL, headers=headers).json()["items"]]
    assert kept == ["2026-09-05"]

    # Raising it back does not resurrect deleted artifacts (permanent delete).
    client.put(
        SCHEDULE_URL,
        json={
            "frequency": "daily",
            "hour": 3,
            "weekday": 0,
            "day_of_month": 1,
            "keep_last_n": 20,
        },
        headers=headers,
    )
    assert len(client.get(ARTIFACTS_URL, headers=headers).json()["items"]) == 1


def test_demo_and_anonymous_cannot_upload_artifacts(client: TestClient, session: Session) -> None:
    demo_user = _make_user(session, DEMO_EMAIL)
    demo_headers = _auth(demo_user)
    body = {"period": PERIOD, "ciphertext": "v1.YQ==.Yg=="}

    assert client.get(ARTIFACTS_URL, headers=demo_headers).status_code == 200
    uploaded = client.post(ARTIFACTS_URL, json=body, headers=demo_headers)
    assert uploaded.status_code == 403
    assert uploaded.json()["code"] == "demo_account_restricted"
    assert client.delete(f"{ARTIFACTS_URL}/1", headers=demo_headers).status_code == 403
    assert (
        client.post(
            RESTORE_URL, json={"confirm": "restore", "payload": {}}, headers=demo_headers
        ).status_code
        == 403
    )

    assert client.post(ARTIFACTS_URL, json=body).status_code == 401
    assert client.post(RESTORE_URL, json={"confirm": "restore", "payload": {}}).status_code == 401


# ── restore: the acceptance test ────────────────────────────────────────────

SAMPLED_TABLES = (
    "accounts",
    "transactions",
    "trades",
    "categories",
    "budgets",
    "goals",
    "tags",
    "rules",
    "prices",
    "institutions",
    "securities",
    "rule_tags",
    "transaction_tags",
    # M106: the receipt log is in the sampled set on purpose — the artifact's
    # payload is an event row's `payload` JSON, the cell most likely to
    # survive encryption but not the round trip (unicode, nested objects).
    "transaction_events",
    "vault_envelopes",
    "fx_rates",
)


def test_restore_roundtrip_after_wipe(
    client: TestClient, session: Session, monkeypatch: Any
) -> None:
    """THE acceptance test (docs/11 Verification #2).

    backup → wipe this tenant's rows in the test DB → restore → semantic
    equality on the sampled tables and byte-equality of the whole snapshot.
    """
    _forbid_network(monkeypatch)
    headers_a, email_a = _register(client)
    headers_b, email_b = _register(client)
    _seed(session, email_a, email_b)
    _seed_post_m7_tables(session, email_a, email_b)
    _enrich(session, email_a)

    # A device envelope: skipped by restore, so it must survive unchanged.
    envelope = client.post(
        "/api/v1/vault/envelopes",
        json={
            "device_id": "0" * 32,
            "label": "Chrome · Linux",
            "wrapped_blob": "ZmFrZS1ibG9i",
        },
        headers=headers_a,
    )
    assert envelope.status_code == 201, envelope.text

    before = _snapshot(client, headers_a)
    assert len(before["transactions"]) >= 1
    assert len(before["vault_envelopes"]) == 1
    # M106 (docs/11 § E2EE vault): the receipt log rides the SAME payload the
    # browser encrypts, so it must be non-empty here or the round trip below
    # would prove nothing about transaction_events.
    assert len(before["transaction_events"]) >= 1

    # 1. BACKUP: encrypt exactly like the browser and store the artifact.
    dek, artifact = _upload(client, headers_a, before)
    stored = client.get(f"{ARTIFACTS_URL}/{artifact['id']}", headers=headers_a).json()
    payload = _decrypt(stored["ciphertext"], dek)
    assert payload == _payload(before)
    assert payload["snapshot"] == before
    # The receipt's own bytes are INSIDE the envelope (docs/33): what the
    # browser encrypted is the whole snapshot, receipt payloads included.
    sealed = payload["snapshot"]["transaction_events"][0]["payload"]
    assert sealed["changes"]["note"] == "Café ☕ 食品 😀"
    assert "Café" not in stored["ciphertext"]

    # 2. WIPE the tenant's domain rows in the test DB (credentials survive —
    #    this is the shared cascade, not a hand-rolled DELETE).
    user = session.exec(select(User).where(User.email == email_a)).one()
    delete_user_rows(session, user, keep=(RefreshToken, McpToken))
    session.commit()
    assert session.exec(select(Transaction).where(Transaction.user_id == user.id)).all() == []
    assert session.exec(select(RefreshToken).where(RefreshToken.user_id == user.id)).all() != [], (
        "wipe must keep credentials"
    )

    # 3. RESTORE from the stored, decrypted artifact.
    restored = _restore(client, headers_a, payload)
    assert restored.status_code == 200, restored.text
    result = restored.json()
    assert result["skipped_tables"] == ["users", "vault_envelopes"]
    assert result["absent_tables"] == []
    assert result["restored"]["transactions"] == len(before["transactions"])
    assert result["restored"]["transaction_events"] == len(before["transaction_events"])
    assert result["restored"]["accounts"] == len(before["accounts"])
    assert result["fx_rates_inserted"] == 0  # shared cache was never wiped

    # 4. SEMANTIC EQUALITY on the sampled tables …
    after = _snapshot(client, headers_a)
    for table in SAMPLED_TABLES:
        assert after[table] == before[table], f"{table} differs after restore"

    # … and the whole snapshot, byte-for-byte once canonicalised.
    assert after == before
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)

    # The other tenant was never touched by any of this.
    other = _snapshot(client, headers_b)
    assert other["transactions"] != after["transactions"]
    assert {row["email"] for row in other["users"]} == {email_b}


def test_restore_validation_rejects_before_any_write(client: TestClient, session: Session) -> None:
    """Every restore 422 fires BEFORE the wipe — tenant byte-identical after."""
    headers, email_a = _register(client)
    _headers_b, email_b = _register(client)
    _seed(session, email_a, email_b)
    before = _snapshot(client, headers)

    good = _payload(before)
    bad_payloads: list[tuple[dict[str, Any], str]] = [
        ({**good, "v": 2}, "restore_invalid_payload"),
        ({**good, "kind": "fintrack-export"}, "restore_invalid_payload"),
        ({**good, "snapshot": []}, "restore_invalid_payload"),
        ({**good, "snapshot": {}}, "restore_invalid_payload"),
        ({**good, "snapshot": {"nope_table": []}}, "restore_unknown_table"),
        ({**good, "snapshot": {"accounts": [{}]}}, "restore_invalid_row"),
        ({**good, "snapshot": {"transactions": ["not-an-object"]}}, "restore_invalid_row"),
    ]
    for payload, expected_code in bad_payloads:
        response = _restore(client, headers, payload)
        assert response.status_code == 422, (expected_code, response.text)
        assert response.json()["code"] == expected_code, response.text
        # Nothing was wiped: the tenant still matches the pre-restore snapshot.
        assert _snapshot(client, headers) == before, expected_code

    # Missing/incorrect confirmation is its own code, also before any write.
    for confirm in ("yes", "", "RESTORE", "restore "):
        response = _restore(client, headers, good, confirm=confirm)
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "restore_confirm_required"
    assert _snapshot(client, headers) == before


def test_restore_rejects_tables_that_are_never_restored(
    client: TestClient, session: Session
) -> None:
    headers, email_a = _register(client)
    _headers_b, email_b = _register(client)
    _seed(session, email_a, email_b)
    before = _snapshot(client, headers)

    for table in (
        "refresh_tokens",
        "mcp_tokens",
        "alembic_version",
        "backup_artifacts",
        "totp_factors",
        "recovery_codes",
        "webauthn_credentials",
        "oidc_links",
    ):
        payload = _payload({table: []})
        response = _restore(client, headers, payload)
        assert response.status_code == 422, (table, response.text)
        assert response.json()["code"] == "restore_forbidden_table"
        assert table in response.json()["detail"]
    assert _snapshot(client, headers) == before


def test_restore_rolls_back_when_rows_violate_integrity(
    client: TestClient, session: Session
) -> None:
    """The wipe happens, the insert fails, the transaction undoes the wipe."""
    headers, email_a = _register(client)
    _headers_b, email_b = _register(client)
    _seed(session, email_a, email_b)
    before = _snapshot(client, headers)

    # Passes pydantic validation, violates the FK at insert time.
    broken = _payload(before)
    broken["snapshot"]["transactions"][0]["account_id"] = 999_999
    response = _restore(client, headers, broken)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "restore_integrity_error"

    assert _snapshot(client, headers) == before


def test_restore_rehomes_rows_and_advances_sequences(client: TestClient, session: Session) -> None:
    """user_id remap + setval: a snapshot from another account lands cleanly.

    The scenario a real restore faces: rows exported before a re-registration
    (or restored into a fresh database) carry `user_id`s this server never
    issued and primary keys its sequences never generated. Both must be
    repaired or the restore is unusable — and neither may touch another live
    tenant, so this restores A's OWN rows under foreign ids rather than
    replaying B's snapshot into A (in one shared database B's ids are live).
    """
    headers_a, email_a = _register(client)
    headers_b, email_b = _register(client)
    _seed(session, email_a, email_b)
    _seed_post_m7_tables(session, email_a, email_b)

    user_a = session.exec(select(User).where(User.email == email_a)).one()
    foreign = _snapshot(client, headers_a)
    for rows in foreign.values():
        for row in rows:
            if isinstance(row, dict) and "user_id" in row:
                row["user_id"] = 9_999  # a user id this server never issued
    # …and primary keys from that other database: a leaf table (nothing
    # references `goals`) far past our own sequence.
    foreign["goals"][0]["id"] = 900_001

    response = _restore(client, headers_a, _payload(foreign))
    assert response.status_code == 200, response.text
    assert response.json()["restored"]["goals"] == len(foreign["goals"])

    after = _snapshot(client, headers_a)
    assert after["accounts"], "vacuous check — the restore produced no rows"
    # Every tenant-scoped row is re-homed onto the caller; the foreign id
    # appears nowhere.
    for table, rows in after.items():
        if "user_id" not in _columns(table):
            continue
        owners = {row["user_id"] for row in rows}
        # Non-empty tables must be exactly the caller; empty ones (acb_pools
        # without a rebuild) trivially satisfy the subset. 9999 is excluded by
        # the subset either way.
        assert owners <= {user_a.id}, (table, owners)
    assert {row["id"] for row in after["goals"]} == {900_001}

    # The sequence was advanced past the restored max(id): the next write gets
    # a fresh id instead of colliding with 900001 (or regressing to a low one).
    goal = Goal(user_id=user_a.id, name="sequence probe", target_amount=Decimal("1.00"))
    session.add(goal)
    session.flush()
    assert goal.id is not None and goal.id > 900_001, goal.id
    session.rollback()

    # Tenant B is untouched by a restore that ran for A: no foreign id landed
    # on its rows.
    still_b = _snapshot(client, headers_b)
    assert 900_001 not in {row["id"] for row in still_b["goals"]}
    assert all(row["user_id"] != 9_999 for row in still_b["accounts"])


def test_restore_keeps_credentials_and_device_envelopes(
    client: TestClient, session: Session
) -> None:
    headers, email = _register(client)
    _other_headers, other_email = _register(client)
    _seed(session, email, other_email)
    _enrich(session, email)

    user = session.exec(select(User).where(User.email == email)).one()
    envelope = client.post(
        "/api/v1/vault/envelopes",
        json={
            "device_id": "a" * 32,
            "label": "Chrome · Linux",
            "wrapped_blob": "ZmFrZS1ibG9i",
        },
        headers=headers,
    )
    assert envelope.status_code == 201, envelope.text

    refresh_ids = [
        row.id for row in session.exec(select(RefreshToken).where(RefreshToken.user_id == user.id))
    ]
    mcp_ids = [row.id for row in session.exec(select(McpToken).where(McpToken.user_id == user.id))]
    assert refresh_ids and mcp_ids

    payload = _payload(_snapshot(client, headers))
    response = _restore(client, headers, payload)
    assert response.status_code == 200, response.text
    assert set(response.json()["skipped_tables"]) == {"users", "vault_envelopes"}

    # Credentials keep their ROWS (same ids): a restore never signs you out
    # and never revokes MCP access.
    assert [
        row.id for row in session.exec(select(RefreshToken).where(RefreshToken.user_id == user.id))
    ] == refresh_ids
    assert [
        row.id for row in session.exec(select(McpToken).where(McpToken.user_id == user.id))
    ] == mcp_ids
    # Device enrollment is security state: replayed snapshots do not resurrect
    # or drop envelopes, so the revoke runbook stays truthful.
    assert len(_snapshot(client, headers)["vault_envelopes"]) == 1
    # The session that posted the restore still authenticates.
    assert client.get("/api/v1/users/me", headers=headers).status_code == 200


def test_restore_gap_fills_fx_rates_without_overwriting(
    client: TestClient, session: Session
) -> None:
    from sqlalchemy import delete as sa_delete

    headers, email_a = _register(client)
    _headers_b, email_b = _register(client)
    _seed(session, email_a, email_b)

    before = _snapshot(client, headers)
    shared_id = before["fx_rates"][0]["id"]
    fx_date = before["fx_rates"][0]["date"]

    # Artifact taken BEFORE the shared cache row disappears.
    _dek, _artifact = _upload(client, headers, before)

    # Simulate a wiped cache (fx_rates is shared, so no wipe touches it —
    # this is the "restore into an empty cache" case).
    session.exec(sa_delete(FxRate).where(FxRate.id == shared_id))
    session.commit()
    assert session.exec(select(FxRate).where(FxRate.id == shared_id)).all() == []

    payload = _payload(before)
    first = _restore(client, headers, payload)
    assert first.status_code == 200, first.text
    assert first.json()["fx_rates_inserted"] == 1

    refilled = _fx_row(session, "EUR", "CAD", fx_date)
    assert refilled.id != shared_id  # fresh serial, data restored

    # Second restore: the row already exists → gap-fill is a no-op and the
    # EXISTING row is not overwritten (manual rows keep winning, docs/02).
    second = _restore(client, headers, payload)
    assert second.status_code == 200, second.text
    assert second.json()["fx_rates_inserted"] == 0
    again = _fx_row(session, "EUR", "CAD", fx_date)
    assert again.id == refilled.id
    assert again.source == refilled.source


def test_restore_coverage_matches_the_export_registry() -> None:
    """The drift guard: what restore inserts is exactly what it wipes.

    A user-owned table that reaches the model registry but not
    `_USER_OWNED_MODELS` would survive the wipe and then collide on insert
    (duplicate primary key) at the worst possible moment — this test fails
    in CI instead, the same way test_export_registry guards the export side.
    """
    exportable = set(_MODELS) - set(EXCLUDED_TABLES)
    assert backup_module.EXPORTABLE_TABLES == exportable

    wiped = {
        model.__tablename__
        for model in _USER_OWNED_MODELS
        if model.__tablename__ not in {m.__tablename__ for m in backup_module.RESTORE_KEEP_MODELS}
    } | set(VIA_PARENT)

    insertable = exportable - set(backup_module.RESTORE_SKIP_TABLES) - {"fx_rates"}
    assert insertable == wiped
    assert exportable == insertable | set(backup_module.RESTORE_SKIP_TABLES) | {"fx_rates"}

    # Skipped means skipped: restore must never wipe what it refuses to write.
    assert set(backup_module.RESTORE_SKIP_TABLES) & wiped == set()
    # The artifact store is excluded from export AND from the wipe, so a
    # backup never nests backups and a restore never deletes its own source.
    assert "backup_artifacts" in EXCLUDED_TABLES
    assert "backup_artifacts" not in wiped
    assert "vault_envelopes" not in wiped


def test_backup_endpoints_are_listed_in_openapi(client: TestClient) -> None:
    # M87 (docs/42): /openapi.json is authenticated-only over HTTP, so read the
    # schema from the app directly — this test asserts the ROUTES exist, and
    # test_api_docs_gate.py owns the transport question.
    from app.main import app

    paths = app.openapi()["paths"]
    assert paths[SCHEDULE_URL].keys() >= {"get", "put"}
    assert paths[ARTIFACTS_URL].keys() >= {"get", "post"}
    assert paths["/api/v1/backup/artifacts/{artifact_id}"].keys() >= {"get", "delete"}
    assert paths[RESTORE_URL].keys() >= {"post"}


def test_client_produced_fixture_decrypts_in_python() -> None:
    """Interop: the REAL browser code's output, decrypted by an independent
    implementation (PBKDF2 KEK → unwrap DEK → field envelope → payload)."""
    assert FIXTURE.exists(), (
        f"missing {FIXTURE} — regenerate with "
        "WRITE_BACKUP_FIXTURE=1 NODE_ENV=test npx vitest run tests/backupFixture.test.ts"
    )
    data = json.loads(FIXTURE.read_text())
    dek = _unwrap_dek(
        data["passphrase"],
        base64.b64decode(data["salt"]),
        base64.b64decode(data["wrappedDEK"]),
    )
    payload = _decrypt(data["wire"]["ciphertext"], dek)
    assert payload == data["expected"]
    assert payload["kind"] == "fintrack-backup"
    assert payload["v"] == 1
    assert payload["snapshot"]["transactions"][0]["raw_description"] == "CAFE MOCHA"
    # M106 (docs/11 § E2EE vault): the receipt the browser sealed comes back
    # with its unicode payload and its canonical hash byte-identical — this is
    # the "export round trip decrypts correctly" clause for transaction_events.
    event = payload["snapshot"]["transaction_events"][0]
    assert event["payload"]["changes"] == {
        "amount_native": "-12.34",
        "note": "Café ☕ 食品 😀",
    }
    assert event["payload_hash"] == payload_hash(event["payload"])

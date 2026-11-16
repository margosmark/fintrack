"""Vault envelopes — wrapped-DEK sync across devices (E2EE Phase 4, docs/33).

Covers the full endpoint contract pinned in docs/08 § Vault key sync (M53):
create/list/replace/revoke, every error code, tenancy, the demo write guard,
the partial-unique active-device rule and the active/revoke CHECK invariant.

Run: cd backend && uv run pytest tests/test_vault_envelopes.py -q
"""

import hashlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.vault import MAX_ACTIVE_ENVELOPES
from tests.test_auth_security_hardening import _seed_demo_tenant
from tests.test_deep_common import auth, register

PATH = "/api/v1/vault/envelopes"


def _device(seed: str) -> str:
    """A valid device id derived from a readable seed: 32 lowercase hex chars
    (docs/33 Phase 4), so no test can pass by accident on a malformed id."""
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _create(
    client: TestClient,
    headers: dict,
    device: str,
    blob: str = "YmxvYg==",
    label: str | None = "Chrome · Linux",
) -> dict:
    payload: dict = {"device_id": device, "wrapped_blob": blob}
    if label is not None:
        payload["label"] = label
    r = client.post(PATH, json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _list(client: TestClient, headers: dict, device: str) -> dict:
    r = client.get(PATH, params={"device_id": device}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# --- create + list -----------------------------------------------------------


def test_create_and_list_round_trip(client: TestClient) -> None:
    """201 on create; the blob comes back verbatim and the device is not revoked."""
    body = register(client)
    headers = auth(body["access_token"])
    device = _device("a1")

    created = _create(client, headers, device, blob="eyJ2IjoxLCJzYWx0IjoiZm9vIn0")

    assert created["device_id"] == device
    assert created["label"] == "Chrome · Linux"
    assert created["wrapped_blob"] == "eyJ2IjoxLCJzYWx0IjoiZm9vIn0"
    assert created["revoked_at"] is None

    listing = _list(client, headers, device)
    assert listing["active_count"] == 1
    assert listing["revoked_own_device"] is False
    assert [row["id"] for row in listing["items"]] == [created["id"]]


def test_second_device_sees_both_envelopes_oldest_first(client: TestClient) -> None:
    """Sync means device B can read device A's (wrapped) blob — that is the feature."""
    body = register(client)
    headers = auth(body["access_token"])
    first = _create(client, headers, _device("b1"), blob="Zmlyc3Q=")
    second = _create(client, headers, _device("b2"), blob="c2Vjb25k")

    listing = _list(client, headers, _device("b2"))
    assert listing["active_count"] == 2
    assert [row["device_id"] for row in listing["items"]] == [
        first["device_id"],
        second["device_id"],
    ]
    assert {row["wrapped_blob"] for row in listing["items"]} == {"Zmlyc3Q=", "c2Vjb25k"}


def test_active_envelope_is_not_returned_for_a_revoked_only_device(
    client: TestClient,
) -> None:
    """Revoked rows never ride along in `items` (the blob no longer exists)."""
    body = register(client)
    headers = auth(body["access_token"])
    revoked = _create(client, headers, _device("c1"))
    client.post(f"{PATH}/{revoked['id']}/revoke", headers=headers)

    listing = _list(client, headers, _device("c1"))
    assert listing["items"] == []
    assert listing["active_count"] == 0
    assert listing["revoked_own_device"] is True


# --- POST error codes --------------------------------------------------------


def test_duplicate_active_device_is_409(client: TestClient) -> None:
    body = register(client)
    headers = auth(body["access_token"])
    _create(client, headers, _device("d1"))

    r = client.post(
        PATH,
        json={"device_id": _device("d1"), "wrapped_blob": "eA=="},
        headers=headers,
    )
    assert r.status_code == 409
    assert r.json()["code"] == "device_exists"


def test_device_limit_is_409(client: TestClient) -> None:
    body = register(client)
    headers = auth(body["access_token"])
    for i in range(MAX_ACTIVE_ENVELOPES):
        _create(client, headers, _device(f"limit-{i}"))

    r = client.post(
        PATH,
        json={"device_id": _device("limit-overflow"), "wrapped_blob": "eA=="},
        headers=headers,
    )
    assert r.status_code == 409
    assert r.json()["code"] == "too_many_devices"


def test_oversize_blob_is_422(client: TestClient) -> None:
    """Same ceiling as the Phase-2 cipher columns (8000), own code."""
    body = register(client)
    headers = auth(body["access_token"])

    r = client.post(
        PATH,
        json={"device_id": _device("f1"), "wrapped_blob": "x" * 8001},
        headers=headers,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "envelope_too_large"

    ok = client.post(
        PATH,
        json={"device_id": _device("f1"), "wrapped_blob": "x" * 8000},
        headers=headers,
    )
    assert ok.status_code == 201, ok.text


def test_malformed_device_id_is_validation_error(client: TestClient) -> None:
    body = register(client)
    headers = auth(body["access_token"])

    bad = client.post(
        PATH,
        json={"device_id": "NOT-HEX", "wrapped_blob": "eA=="},
        headers=headers,
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "validation_error"

    missing = client.get(PATH, headers=headers)
    assert missing.status_code == 422
    assert missing.json()["code"] == "validation_error"

    wrong_case = client.get(PATH, params={"device_id": _device("A").upper()}, headers=headers)
    assert wrong_case.status_code == 422
    assert wrong_case.json()["code"] == "validation_error"


def test_unknown_field_is_validation_error(client: TestClient) -> None:
    """extra=forbid: a typo'd field is loud, never silently stored."""
    body = register(client)
    headers = auth(body["access_token"])

    r = client.post(
        PATH,
        json={"device_id": _device("g1"), "wrapped_blob": "eA==", "dek": "plaintext"},
        headers=headers,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "validation_error"


def test_unauthenticated_is_401(client: TestClient) -> None:
    assert client.get(PATH, params={"device_id": _device("h1")}).status_code == 401
    assert (
        client.post(PATH, json={"device_id": _device("h1"), "wrapped_blob": "eA=="}).status_code
        == 401
    )


# --- PUT (rotation) ----------------------------------------------------------


def test_replace_rewraps_blob_and_keeps_label_when_omitted(client: TestClient) -> None:
    body = register(client)
    headers = auth(body["access_token"])
    created = _create(client, headers, _device("i1"), blob="b2xk", label="Old Laptop")

    r = client.put(
        f"{PATH}/{created['id']}",
        json={"wrapped_blob": "bmV3"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["wrapped_blob"] == "bmV3"
    assert r.json()["label"] == "Old Laptop"

    # label IS applied when present, and may be cleared with an explicit null.
    r = client.put(
        f"{PATH}/{created['id']}",
        json={"wrapped_blob": "bmV3", "label": "Renamed"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["label"] == "Renamed"


def test_replace_unknown_and_foreign_ids_are_404(client: TestClient) -> None:
    owner = register(client)
    owner_headers = auth(owner["access_token"])
    created = _create(client, owner_headers, _device("j1"))

    other = register(client)
    other_headers = auth(other["access_token"])

    unknown = client.put(f"{PATH}/999999", json={"wrapped_blob": "eA=="}, headers=owner_headers)
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "not_found"

    foreign = client.put(
        f"{PATH}/{created['id']}", json={"wrapped_blob": "eA=="}, headers=other_headers
    )
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "not_found"


def test_replace_revoked_is_409(client: TestClient) -> None:
    """A tombstone is a tombstone: re-enrollment is a POST, never a PUT."""
    body = register(client)
    headers = auth(body["access_token"])
    created = _create(client, headers, _device("k1"))
    client.post(f"{PATH}/{created['id']}/revoke", headers=headers)

    r = client.put(f"{PATH}/{created['id']}", json={"wrapped_blob": "eA=="}, headers=headers)
    assert r.status_code == 409
    assert r.json()["code"] == "envelope_revoked"


# --- revoke ------------------------------------------------------------------


def test_revoke_drops_the_blob_and_is_idempotent(client: TestClient) -> None:
    body = register(client)
    headers = auth(body["access_token"])
    created = _create(client, headers, _device("l1"), blob="c2VjcmV0")

    first = client.post(f"{PATH}/{created['id']}/revoke", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["wrapped_blob"] is None
    assert first.json()["revoked_at"] is not None

    second = client.post(f"{PATH}/{created['id']}/revoke", headers=headers)
    assert second.status_code == 200
    assert second.json()["wrapped_blob"] is None
    assert second.json()["revoked_at"] == first.json()["revoked_at"]


def test_revoke_foreign_envelope_is_404(client: TestClient) -> None:
    owner = register(client)
    owner_headers = auth(owner["access_token"])
    created = _create(client, owner_headers, _device("m1"))

    other = register(client)
    other_headers = auth(other["access_token"])
    r = client.post(f"{PATH}/{created['id']}/revoke", headers=other_headers)
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_revoked_device_can_be_reenrolled_and_the_flag_clears(client: TestClient) -> None:
    """Partial unique index: a revoked row does NOT block a new active row."""
    body = register(client)
    headers = auth(body["access_token"])
    device = _device("n1")
    created = _create(client, headers, device)
    client.post(f"{PATH}/{created['id']}/revoke", headers=headers)
    assert _list(client, headers, device)["revoked_own_device"] is True

    again = _create(client, headers, device, blob="bmV3LWJsb2I=")
    assert again["id"] != created["id"]

    listing = _list(client, headers, device)
    assert listing["revoked_own_device"] is False
    assert listing["active_count"] == 1
    assert listing["items"][0]["wrapped_blob"] == "bmV3LWJsb2I="


# --- tenancy + demo ----------------------------------------------------------


def test_every_route_is_tenant_scoped(client: TestClient) -> None:
    a = register(client)
    a_headers = auth(a["access_token"])
    mine = _create(client, a_headers, _device("o1"))

    b = register(client)
    b_headers = auth(b["access_token"])

    listing = _list(client, b_headers, _device("p1"))
    assert listing["items"] == []
    assert listing["active_count"] == 0
    assert listing["revoked_own_device"] is False

    # ...and B cannot touch A's row by id.
    assert client.post(f"{PATH}/{mine['id']}/revoke", headers=b_headers).status_code == 404
    assert (
        client.put(
            f"{PATH}/{mine['id']}", json={"wrapped_blob": "eA=="}, headers=b_headers
        ).status_code
        == 404
    )


def test_demo_tenant_can_read_but_not_sync(client: TestClient, session: Session) -> None:
    """The demo is shared plaintext sample data: GET works, every write 403s."""
    tenant = _seed_demo_tenant(session)
    headers = tenant["headers"]
    device = _device("q1")

    listing = _list(client, headers, device)
    assert listing["items"] == []

    created = client.post(PATH, json={"device_id": device, "wrapped_blob": "eA=="}, headers=headers)
    assert created.status_code == 403
    assert created.json()["code"] == "demo_account_restricted"


# --- schema invariant --------------------------------------------------------


def test_state_check_rejects_a_revoked_row_that_still_holds_a_blob(
    client: TestClient, session: Session
) -> None:
    """ck_vault_envelopes_state (migration 0027): active <=> blob present.

    This is the only test that proves the CONSTRAINT exists — the handler
    tests above would still pass if the migration had omitted it.
    """
    body = register(client)
    headers = auth(body["access_token"])
    # Resolve the real user id behind the token.
    r = client.get("/api/v1/users/me", headers=headers)
    assert r.status_code == 200, r.text
    user_id = r.json()["id"]

    with pytest.raises(Exception) as excinfo:  # IntegrityError (psycopg wrapping)
        session.execute(
            text(
                "INSERT INTO vault_envelopes "
                "(user_id, device_id, wrapped_blob, revoked_at, created_at, updated_at) "
                "VALUES (:uid, :dev, 'still-here', :rev, now(), now())"
            ),
            {"uid": user_id, "dev": _device("r1"), "rev": datetime.now(UTC)},
        )
        session.commit()
    assert "ck_vault_envelopes_state" in str(excinfo.value)
    session.rollback()

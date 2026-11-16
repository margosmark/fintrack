"""M108 P4 — receipt images (docs/02 §transaction_receipts, docs/08, docs/09).

Specs pinned here:

* one image per transaction, PUT replaces rather than accumulates;
* the LIST carries `has_receipt` and NEVER the bytes;
* the image round-trips byte-for-byte through `GET /transactions/{id}/receipt`
  with the content type it was validated against;
* the 2 MB cap, the magic-byte check and the empty-body check each refuse
  BEFORE a row is written;
* ownership is the caller's own transaction or 404;
* `/export/full` carries the table, so a receipt survives the backup that
  docs/11's lost-passphrase guarantee depends on.

The demo tenant's write refusal is the CENTRAL guard in
`app.api.deps::get_current_user` (docs/20) and is covered by
`test_auth_security_hardening.py`; re-asserting it here would only test that
the same dependency is wired twice.
"""

from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.transaction_receipts import TransactionReceipt
from tests.test_cat_common import auth, create_account

API = "/api/v1"
PASSWORD = "correct-horse-battery"

# Minimal bodies with the RIGHT magic bytes — the server sniffs, so the
# content only has to be recognizable as an image, not a decodable one.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40
WEBP = b"RIFF" + (40).to_bytes(4, "little") + b"WEBP" + b"\x00" * 40


def _register(client: TestClient, email: str) -> dict[str, str]:
    client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    login = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return auth(login.json()["access_token"])


def _txn(client: TestClient, headers: dict[str, str], payee: str = "Loblaws") -> dict:
    account_id = create_account(client, headers)
    response = client.post(
        f"{API}/transactions",
        headers=headers,
        json={
            "account_id": account_id,
            "date": "2026-08-01",
            "raw_description": payee,
            "amount_native": "-42.00",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload(
    client: TestClient,
    headers: dict[str, str],
    tx_id: int,
    data: bytes = PNG,
    content_type: str = "image/png",
    name: str = "receipt.png",
):
    return client.put(
        f"{API}/transactions/{tx_id}/receipt",
        headers=headers,
        files={"file": (name, data, content_type)},
    )


def _flag(client: TestClient, headers: dict[str, str], tx_id: int) -> dict:
    """The row as the LIST renders it — the only place the flag is read."""
    page = client.get(f"{API}/transactions?limit=200", headers=headers)
    assert page.status_code == 200, page.text
    row = next((item for item in page.json()["items"] if item["id"] == tx_id), None)
    assert row is not None, f"transaction {tx_id} missing from the list"
    return row


# --- the happy path ----------------------------------------------------------


def test_upload_get_delete_round_trip(client: TestClient, session: Session) -> None:
    headers = _register(client, "rcpt-owner@example.com")
    tx = _txn(client, headers)

    # The list starts honest: no image, and no column that could hold one.
    row = _flag(client, headers, tx["id"])
    assert row["has_receipt"] is False
    assert not any(key.startswith("receipt") or key.endswith("_base64") for key in row)

    put = _upload(client, headers, tx["id"])
    assert put.status_code == 200, put.text
    assert put.json() == {
        "transaction_id": tx["id"],
        "content_type": "image/png",
        "size_bytes": len(PNG),
    }

    # The flag flips on the very next list read, and the row STILL has no bytes.
    row = _flag(client, headers, tx["id"])
    assert row["has_receipt"] is True
    assert not any(key.startswith("receipt") or key.endswith("_base64") for key in row)

    got = client.get(f"{API}/transactions/{tx['id']}/receipt", headers=headers)
    assert got.status_code == 200, got.text
    assert got.headers["content-type"].startswith("image/png")
    # Byte-for-byte: what went in is what comes out.
    assert got.content == PNG
    assert "private" in got.headers.get("cache-control", "")

    assert (
        client.delete(f"{API}/transactions/{tx['id']}/receipt", headers=headers).status_code == 204
    )
    assert _flag(client, headers, tx["id"])["has_receipt"] is False
    gone = client.get(f"{API}/transactions/{tx['id']}/receipt", headers=headers)
    assert gone.status_code == 404
    assert gone.json()["code"] == "receipt_not_found"
    # The row itself survives — deleting an image is not deleting a ledger row.
    assert client.get(f"{API}/transactions/{tx['id']}/tags", headers=headers).status_code == 200
    assert session.exec(select(TransactionReceipt)).all() == []


def test_second_upload_replaces_rather_than_accumulates(
    client: TestClient, session: Session
) -> None:
    headers = _register(client, "rcpt-replace@example.com")
    tx = _txn(client, headers)

    assert _upload(client, headers, tx["id"], PNG, "image/png").status_code == 200
    assert _upload(client, headers, tx["id"], WEBP, "image/webp", "r.webp").status_code == 200

    rows = list(session.exec(select(TransactionReceipt)).all())
    assert len(rows) == 1, "one-to-one is the v1 contract — not a gallery"
    assert rows[0].content_type == "image/webp"
    assert rows[0].size_bytes == len(WEBP)

    got = client.get(f"{API}/transactions/{tx['id']}/receipt", headers=headers)
    assert got.status_code == 200
    assert got.content == WEBP
    assert got.headers["content-type"].startswith("image/webp")


def test_every_accepted_type_round_trips(client: TestClient) -> None:
    headers = _register(client, "rcpt-types@example.com")
    tx = _txn(client, headers)
    for data, content_type, name in (
        (PNG, "image/png", "r.png"),
        (JPEG, "image/jpeg", "r.jpg"),
        (GIF, "image/gif", "r.gif"),
        (WEBP, "image/webp", "r.webp"),
    ):
        put = _upload(client, headers, tx["id"], data, content_type, name)
        assert put.status_code == 200, f"{content_type}: {put.text}"
        got = client.get(f"{API}/transactions/{tx['id']}/receipt", headers=headers)
        assert got.content == data


# --- every refusal happens BEFORE a row exists -------------------------------


def test_oversize_is_refused_before_a_byte_is_stored(client: TestClient, session: Session) -> None:
    """2 MB + 1, made of VALID png bytes, so only the size rule can reject it.

    Two guards exist for this: the declared `Content-Length` (answered before
    the body is read) and the actual read length (the backstop for a chunked
    request that declares nothing). TestClient always sends a Content-Length,
    so this exercises the first; the second is the same constant compared
    against `len(raw)` on the path this one short-circuits.
    """
    headers = _register(client, "rcpt-big@example.com")
    tx = _txn(client, headers)
    put = _upload(client, headers, tx["id"], PNG + b"\x00" * (2 * 1024 * 1024 + 1))
    assert put.status_code == 413, put.text
    assert put.json()["code"] == "receipt_too_large"
    assert list(session.exec(select(TransactionReceipt)).all()) == []
    assert _flag(client, headers, tx["id"])["has_receipt"] is False


def test_non_image_body_is_refused(client: TestClient, session: Session) -> None:
    headers = _register(client, "rcpt-html@example.com")
    tx = _txn(client, headers)
    put = _upload(client, headers, tx["id"], b"<html><body>not an image</body></html>", "image/png")
    assert put.status_code == 422, put.text
    assert put.json()["code"] == "receipt_unsupported_type"
    assert list(session.exec(select(TransactionReceipt)).all()) == []


def test_a_content_type_that_lies_about_the_bytes_is_refused(
    client: TestClient, session: Session
) -> None:
    headers = _register(client, "rcpt-lying@example.com")
    tx = _txn(client, headers)
    # Real PNG bytes, claimed as JPEG: the header and the magic disagree, so
    # the server would be serving an image under a type it did not sniff.
    put = _upload(client, headers, tx["id"], PNG, "image/jpeg", "r.jpg")
    assert put.status_code == 422, put.text
    assert put.json()["code"] == "receipt_unsupported_type"
    assert list(session.exec(select(TransactionReceipt)).all()) == []


def test_empty_upload_is_refused(client: TestClient, session: Session) -> None:
    headers = _register(client, "rcpt-empty@example.com")
    tx = _txn(client, headers)
    put = _upload(client, headers, tx["id"], b"", "image/png")
    assert put.status_code == 422, put.text
    assert put.json()["code"] == "receipt_empty"
    assert list(session.exec(select(TransactionReceipt)).all()) == []


# --- ownership & idempotency -------------------------------------------------


def test_a_receipt_never_leaves_its_owner(client: TestClient) -> None:
    owner = _register(client, "rcpt-a@example.com")
    other = _register(client, "rcpt-b@example.com")
    tx = _txn(client, owner)

    assert _upload(client, owner, tx["id"]).status_code == 200

    # Another tenant cannot read, replace or delete it — and cannot tell an
    # existing receipt from a missing one (both are the plain 404).
    for response in (
        client.get(f"{API}/transactions/{tx['id']}/receipt", headers=other),
        client.delete(f"{API}/transactions/{tx['id']}/receipt", headers=other),
        _upload(client, other, tx["id"]),
    ):
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"

    # …and their own list never contains the row at all, so there is no flag
    # for them to read in either direction.
    mine = client.get(f"{API}/transactions?limit=200", headers=other).json()["items"]
    assert all(item["id"] != tx["id"] for item in mine)

    assert client.get(f"{API}/transactions/{tx['id']}/receipt", headers=owner).status_code == 200


def test_delete_is_idempotent(client: TestClient) -> None:
    headers = _register(client, "rcpt-twice@example.com")
    tx = _txn(client, headers)
    assert _upload(client, headers, tx["id"]).status_code == 200
    assert (
        client.delete(f"{API}/transactions/{tx['id']}/receipt", headers=headers).status_code == 204
    )
    # A second tab's delete must not become an error the user has to read.
    assert (
        client.delete(f"{API}/transactions/{tx['id']}/receipt", headers=headers).status_code == 204
    )


def test_missing_receipt_is_404_not_an_empty_image(client: TestClient) -> None:
    headers = _register(client, "rcpt-none@example.com")
    tx = _txn(client, headers)
    got = client.get(f"{API}/transactions/{tx['id']}/receipt", headers=headers)
    assert got.status_code == 404
    assert got.json()["code"] == "receipt_not_found"


# --- export / backup coverage (docs/11) --------------------------------------


def test_export_full_carries_the_image_verbatim(client: TestClient) -> None:
    headers = _register(client, "rcpt-export@example.com")
    tx = _txn(client, headers)
    assert _upload(client, headers, tx["id"]).status_code == 200

    response = client.get(f"{API}/export/full", headers=headers)
    assert response.status_code == 200, response.text
    snapshot = json.loads(response.content)
    rows = snapshot["transaction_receipts"]
    assert len(rows) == 1
    assert rows[0]["transaction_id"] == tx["id"]
    assert rows[0]["content_type"] == "image/png"
    assert rows[0]["size_bytes"] == len(PNG)
    # Base64 TEXT, not bytes — this payload goes through `json.dumps`, and the
    # image has to come back out of a restore exactly as it went in.
    assert base64.b64decode(rows[0]["data_base64"]) == PNG
    for key in ("id", "user_id", "transaction_id", "content_type", "size_bytes", "data_base64"):
        assert key in rows[0]

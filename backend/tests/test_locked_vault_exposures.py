"""Locked-vault audit — the GLOBAL NEGATIVES (docs/33 §key-handling inventory).

These are the exposures a ciphertext-leak scan will NOT catch, because the
plaintext is *by design* rather than a bug: `transaction_events.payload`,
`dedup_hash`, `rules.pattern`, `learned_payees.normalized_payee`. docs/33
records each as an accepted/disclosed row (F7, F8, and the M106 receipt store).

A pin here is a TRIPWIRE, not an endorsement. Every assertion is written so
that the day someone encrypts the column or salts the hash, the test fails —
which is exactly the signal that the docs/33 inventory row and the fixture in
`scripts/locked_vault_gate.py` need to move with it. Do not "fix" these tests
by relaxing them; fix the column and update the doc.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from app.importers.base import RawTransaction, build_dedup_hash
from app.models.learned_payees import LearnedPayee
from app.models.rules import Rule
from tests.test_cat_common import auth, create_account


def _sha(payload: Any) -> str:
    """The pinned docs/02 formula: unsalted sha256 over the canonical JSON."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


API = "/api/v1"
PASSWORD = "correct-horse-battery"


def _register(client: TestClient, email: str) -> dict[str, str]:
    client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    body = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert body.status_code == 200, body.text
    return auth(body.json()["access_token"])


def test_dedup_hash_is_unsalted_sha256_over_plaintext(client: TestClient) -> None:
    """F7 tripwire: `dedup_hash` is a bare SHA-256 over
    `account|date|amount|raw_description` (`importers/base.py::build_dedup_hash`)
    — no salt, no key, so the description and the amount are dictionary-reversible
    from the digest alone.

    When a keyed HMAC ships, this assertion fails: update docs/33 F7 and the
    `N2` row in `scripts/locked_vault_gate.py` in the same change.
    """
    raw = RawTransaction(
        date=date(2026, 3, 14),
        posting_date=None,
        raw_description="LOBLAWS  TORONTO  ON",
        amount_native=Decimal("-42.10"),
        currency="CAD",
    )
    digest = build_dedup_hash(7, raw)

    # The pinned canonical string, hand-recomputed — not re-derived by calling
    # the same function, which would make the test tautological.
    canonical = "7|2026-03-14|-42.10|LOBLAWS TORONTO ON"
    assert digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # …and therefore reproducible by anyone holding the row: a second, wider
    # whitespace variant normalizes to the SAME digest.
    wider = RawTransaction(
        date=date(2026, 3, 14),
        posting_date=None,
        raw_description="LOBLAWS    TORONTO ON",
        amount_native=Decimal("-42.10"),
        currency="CAD",
    )
    assert build_dedup_hash(7, wider) == digest


def test_rules_and_learned_payees_carry_no_cipher_or_token_column() -> None:
    """F8 tripwire: `rules.pattern` and `learned_payees.normalized_payee` are
    plaintext columns with no blind-index sibling, so a locked vault protects
    neither.

    The check is structural (the mapped column set), so it holds whether or not
    a database is reachable. When a `*_cipher` / `*_token` column lands on
    either table, this fails: update docs/33 F8 and the `N3` row in the gate.
    """
    for table, plaintext_col in (
        (Rule.__table__, "pattern"),
        (LearnedPayee.__table__, "normalized_payee"),
    ):
        columns = {column.name for column in table.columns}
        assert plaintext_col in columns, f"{table.name} lost its {plaintext_col} column"
        assert not any(name.endswith(("_cipher", "_token")) for name in columns), (
            f"{table.name} now carries an encrypted sibling for {plaintext_col} — "
            "docs/33 F8 is stale, update the inventory and the gate fixture"
        )


def test_receipt_payloads_carry_plaintext_verbatim(client: TestClient) -> None:
    """M106 receipt store: an edit receipt records the payee and the amount
    VERBATIM in `transaction_events.payload`, and `/export/full` ships that
    payload byte-for-byte — under any vault state, because the server has no
    vault state to read.

    This is the accepted, disclosed exposure docs/33 lists outside the envelope
    (docs/43 §6 strips payloads from the partner projection; it does not strip
    them from the owner's own export). It is NOT a ciphertext leak, which is
    why a ciphertext-leak scan misses it.

    When a payload is encrypted or redacted, this fails: update docs/33 and the
    `N1` row in `scripts/locked_vault_gate.py`.
    """
    headers = _register(client, "m134-exposure@example.com")
    account_id = create_account(client, headers)

    distinctive_payee = "M134 EXPOSURE PROBE"
    created = client.post(
        f"{API}/transactions",
        headers=headers,
        json={
            "account_id": account_id,
            "date": "2026-09-01",
            "raw_description": distinctive_payee,
            "amount_native": "-17.25",
        },
    )
    assert created.status_code == 201, created.text
    tx_id = created.json()["id"]

    patched = client.patch(
        f"{API}/transactions/{tx_id}",
        headers=headers,
        json={"normalized_payee": "Exposure Probe Renamed", "amount_native": "-19.75"},
    )
    assert patched.status_code == 200, patched.text

    snapshot = client.get(f"{API}/export/full", headers=headers)
    assert snapshot.status_code == 200, snapshot.text
    events = snapshot.json()["transaction_events"]
    edited = [event for event in events if event["event_kind"] == "edited"]
    assert edited, "an edit must write exactly one receipt (docs/02 §transaction_events)"

    # The payload is plaintext, re-checkable against its own hash, and quotes
    # the new payee and the new amount as typed.
    payload = edited[0]["payload"]

    # The PATCH receipt nests the values under `changes` (docs/02
    # §transaction_events): the payload is a diff, and the diff's VALUES are
    # the verbatim plaintext the user typed.
    assert payload["fields"] == ["normalized_payee", "amount_native"]
    assert payload["changes"]["normalized_payee"] == "Exposure Probe Renamed"
    assert payload["changes"]["amount_native"] == "-19.75"

    # …and the original payee is still on the row the export ships, so the
    # plaintext exists in TWO places in one snapshot: the live column and the
    # receipt log's historical diff.
    rows = {row["id"]: row for row in snapshot.json()["transactions"]}
    assert rows[tx_id]["normalized_payee"] == "Exposure Probe Renamed"

    # Re-checkable: the stored hash is derivable from the payload alone, which
    # is only possible because nothing about it is encrypted.
    assert edited[0]["payload_hash"] == _sha(payload)

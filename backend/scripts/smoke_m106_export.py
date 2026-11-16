"""Live smoke for M106 export / E2EE / receipt visibility against the deployed stack.

Run from the backend checkout:  uv run python scripts/smoke_m106_export.py

Exercises the changed path (GET /export/zip's transaction_events filter) plus the
E2EE artifact round trip end-to-end, through nginx, with a freshly registered
tenant. Writes a JSON evidence file next to itself when SMOKE_OUT is set.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.models.transaction_events import TransactionEvent

BASE = os.environ.get("SMOKE_BASE", "https://fintrac.princessandmark.com/api/v1")
REPO_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sample.qfx"

checks: list[dict] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:500]})
    print(("PASS  " if ok else "FAIL  ") + name + (f"  :: {detail}" if detail else ""))


def sign_in(client: httpx.Client, email: str, password: str) -> tuple[dict | None, str]:
    """POST /auth/login, retrying once — the auth limiter answers 429 under a
    burst of smoke runs, and a 429 must surface as a failed check, not a
    KeyError in the middle of the evidence."""
    last = ""
    for attempt in range(2):
        login = client.post("/auth/login", json={"email": email, "password": password})
        body = (
            login.json()
            if login.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        if login.status_code == 200 and "access_token" in body:
            return {
                "Authorization": f"Bearer {body['access_token']}"
            }, f"200 (attempt {attempt + 1})"
        last = f"{login.status_code} {login.text[:160]}"
        time.sleep(2.0)
    return None, last


def skip(name: str, detail: object) -> None:
    """Record a section that could not run (ok=None — neither pass nor fail)."""
    checks.append({"name": name, "ok": None, "detail": str(detail)[:500]})
    print(f"SKIP  {name}  :: {detail}")


def sign_up(client: httpx.Client, email: str, password: str) -> tuple[dict | None, str]:
    """Register, then PROVE the account exists by signing in.

    `POST /auth/register` answers 200 "accepted" whether or not it wrote the
    row (anti-enumeration: the reply must not reveal whether the address was
    free), and a repeated smoke run can trip its throttle — so the only honest
    belief is a working login. Retries are spaced out; if the throttle holds,
    the caller SKIPS the section that needed the account instead of failing a
    run that proved everything else.
    """
    last = ""
    for attempt in range(3):
        client.post("/auth/register", json={"email": email, "password": password})
        auth, last = sign_in(client, email, password)
        if auth is not None:
            return auth, f"registered (attempt {attempt + 1})"
        time.sleep(3.0)
    return None, last


def emit(totals: dict) -> int:
    """Write the evidence file and print the tally. Skips never pass silently."""
    report = {
        "target": BASE,
        "when": datetime.now(UTC).isoformat(),
        "totals": totals,
        "checks": checks,
        "passed": sum(1 for item in checks if item["ok"] is True),
        "failed": sum(1 for item in checks if item["ok"] is False),
        "skipped": sum(1 for item in checks if item["ok"] is None),
    }
    out = os.environ.get("SMOKE_OUT")
    if out:
        Path(out).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    ran = len(checks) - report["skipped"]
    tail = f", {report['skipped']} skipped" if report["skipped"] else ""
    print(f"\n{report['passed']}/{ran} checks passed{tail}")
    return 0 if report["failed"] == 0 else 1


def payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=90.0, follow_redirects=True)

    health = client.get("/health")
    check("GET /api/v1/health -> 200 through nginx", health.status_code == 200, health.status_code)

    stamp = int(time.time() * 1000)
    email = f"m106-smoke-{stamp}@example.com"
    password = f"m106-smoke-{secrets.token_urlsafe(12)}"  # per-run secret (t_32cd7b8e)
    auth, owner_detail = sign_up(client, email, password)
    check("register + login", auth is not None, owner_detail)
    if auth is None:
        # Nothing below can run without a session — report and stop.
        return emit({"stage": "owner-signup", "email": email})

    account = client.post(
        "/accounts",
        headers=auth,
        json={"name": "Smoke Chequing", "type": "chequing", "currency": "CAD"},
    )
    check("POST /accounts -> 201", account.status_code == 201, account.text[:200])
    account_id = account.json()["id"]

    tx_edit = client.post(
        "/transactions",
        headers=auth,
        json={
            "account_id": account_id,
            "date": "2026-08-01",
            "raw_description": "SMOKE LATTE",
            "amount_native": "-4.25",
        },
    )
    tx_gone = client.post(
        "/transactions",
        headers=auth,
        json={
            "account_id": account_id,
            "date": "2026-08-02",
            "raw_description": "SMOKE DOOMED",
            "amount_native": "-9.99",
        },
    )
    check(
        "two manual transactions created",
        tx_edit.status_code == 201 and tx_gone.status_code == 201,
        f"{tx_edit.status_code}/{tx_gone.status_code}",
    )

    # An edit with a unicode body -> one `edited` receipt carrying it verbatim.
    patch = client.patch(
        f"/transactions/{tx_edit.json()['id']}",
        headers=auth,
        json={"raw_description": "Café ☕ 食品 😀", "amount_native": "-12.34"},
    )
    check("PATCH -> 200 (writes one receipt)", patch.status_code == 200, patch.text[:200])

    # A delete -> the `deleted` receipt whose row no longer exists.
    delete = client.delete(f"/transactions/{tx_gone.json()['id']}", headers=auth)
    check(
        "DELETE -> 204 (writes the outliving receipt)",
        delete.status_code == 204,
        delete.status_code,
    )

    # A staged import batch -> receipts that must NOT reach the human file.
    fixture = REPO_FIXTURE if REPO_FIXTURE.exists() else None
    staged_ok = False
    staged_rows = 0
    staged_detail = "fixture not found"
    if fixture is not None:
        upload = client.post(
            "/imports/upload",
            headers=auth,
            data={"account_id": str(account_id)},
            files={"file": ("sample.qfx", fixture.read_bytes(), "app/x")},
        )
        staged_ok = upload.status_code == 201
        staged_rows = int(upload.json().get("imported", 0)) if staged_ok else 0
        staged_detail = f"{upload.status_code} imported={staged_rows} {upload.text[:120]}"
    check("import staged (pending batch, not committed)", staged_ok, staged_detail)

    full = client.get("/export/full", headers=auth)
    check("GET /export/full -> 200", full.status_code == 200, full.status_code)
    snapshot = full.json()
    events = snapshot.get("transaction_events", [])
    kinds = sorted(event["event_kind"] for event in events)
    # 2 manual `created` + one per staged row + the edit + the delete.
    expected_kinds = sorted(["created", "created", "edited", "deleted"] + ["created"] * staged_rows)
    check(
        "export/full carries every receipt (committed + staged)",
        len(events) == 4 + staged_rows and kinds == expected_kinds,
        f"n={len(events)} staged={staged_rows} kinds={kinds}",
    )
    hashes_ok = all(event["payload_hash"] == payload_hash(event["payload"]) for event in events)
    check("every exported payload_hash re-derives", hashes_ok, f"{len(events)} rows")
    unicode_ok = any(
        "Café ☕ 食品 😀" in json.dumps(event["payload"], ensure_ascii=False) for event in events
    )
    check("unicode receipt payload survives export/full", unicode_ok)

    zip_response = client.get("/export/zip", headers=auth, params={"vault": "locked"})
    check(
        "GET /export/zip?vault=locked -> 200",
        zip_response.status_code == 200,
        zip_response.status_code,
    )
    archive: dict[str, bytes] = {}
    if zip_response.status_code == 200:
        import zipfile

        with zipfile.ZipFile(io.BytesIO(zip_response.content)) as zf:
            archive = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(archive.get("manifest.json", b"{}"))
    check(
        "manifest publishes the transaction_events filter",
        "transaction_events" in manifest.get("visibility_filters", {}),
        manifest.get("visibility_filters", {}).get("transaction_events", "missing")[:200],
    )

    rows = list(csv.reader(io.StringIO(archive.get("transaction_events.csv", "").decode("utf-8"))))
    header, body = rows[0], rows[1:]
    check(
        "CSV header IS the model's field order",
        header == list(TransactionEvent.model_fields),
        header,
    )
    shipped_kinds = sorted(row[header.index("event_kind")] for row in body)
    staged_shipped = any("sample.qfx" in row[header.index("payload")] for row in body)
    check(
        "ZIP keeps the 4 receipts of visible/deleted rows, drops the staged one",
        len(body) == 4
        and shipped_kinds == ["created", "created", "deleted", "edited"]
        and not staged_shipped,
        f"n={len(body)} kinds={shipped_kinds} staged_shipped={staged_shipped}",
    )
    payload_index = header.index("payload")
    parsed = [json.loads(row[payload_index]) for row in body]
    check(
        "every payload cell parses back to an object",
        all(isinstance(item, dict) and item for item in parsed),
        parsed[:2],
    )
    check(
        "unicode payload rides the CSV as raw UTF-8",
        any("Café ☕ 食品 😀" in row[payload_index] for row in body),
    )

    # ---- E2EE artifact round trip: encrypt the snapshot, store, fetch, decrypt.
    dek = os.urandom(32)
    iv = os.urandom(12)
    period = datetime.now(UTC).date().isoformat()
    plaintext = json.dumps(
        {
            "v": 1,
            "kind": "fintrack-backup",
            "period": period,
            "exported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "snapshot": snapshot,
        }
    ).encode()
    ciphertext = (
        "v1."
        + base64.b64encode(iv).decode()
        + "."
        + base64.b64encode(AESGCM(dek).encrypt(iv, plaintext, None)).decode()
    )

    upload = client.post(
        "/backup/artifacts",
        headers=auth,
        json={"period": period, "ciphertext": ciphertext},
    )
    check("POST /backup/artifacts -> 201", upload.status_code == 201, upload.text[:200])
    artifact_id = upload.json().get("id")
    fetched = client.get(f"/backup/artifacts/{artifact_id}", headers=auth)
    check("GET /backup/artifacts/{id} -> 200", fetched.status_code == 200, fetched.status_code)

    stored = fetched.json().get("ciphertext", "")
    check(
        "stored artifact is opaque (no receipt text beside the ciphertext)",
        "Café" not in stored and "SMOKE" not in stored,
        f"len={len(stored)}",
    )
    _version, iv_b64, body_b64 = stored.split(".")
    try:
        recovered = AESGCM(dek).decrypt(base64.b64decode(iv_b64), base64.b64decode(body_b64), None)
        payload = json.loads(recovered)
        decrypt_ok = True
        decrypt_detail = ""
    except Exception as error:  # reported as a failed check
        payload, decrypt_ok, decrypt_detail = {}, False, repr(error)
    check("artifact decrypts under the session DEK", decrypt_ok, decrypt_detail)
    check(
        "decrypted snapshot == /export/full byte-for-byte",
        payload.get("snapshot") == snapshot,
        f"tables={len(payload.get('snapshot', {}))}",
    )
    sealed = payload.get("snapshot", {}).get("transaction_events", [])
    check(
        "receipts are INSIDE the envelope, payload + hash intact",
        len(sealed) == len(events)
        and all(e["payload_hash"] == payload_hash(e["payload"]) for e in sealed)
        and any("Café ☕ 食品 😀" in json.dumps(e["payload"], ensure_ascii=False) for e in sealed),
        f"n={len(sealed)}",
    )

    # ---- the partner projection boundary, live (docs/43 §6).
    owner_id = client.get("/users/me", headers=auth).json()["id"]
    visible_tx = tx_edit.json()["id"]
    partner_email = f"m106-smoke-partner-{stamp}@example.com"
    partner_auth, partner_detail = sign_up(client, partner_email, password)
    check("partner can register + sign in", partner_auth is not None, partner_detail)

    if partner_auth is None:
        # The repeat signups of a long smoke run hit the register throttle;
        # skip the section rather than fake a pass for work that did not run.
        skip("partner projection section", f"registration throttled: {partner_detail}")
    else:
        client.patch("/users/me/profile", headers=partner_auth, json={"name": "Smoke Pat"})

        invite = client.post(
            "/sharing/invites",
            headers=auth,
            json={"email": partner_email, "account_ids": [account_id]},
        )
        check("owner invites the partner -> 201", invite.status_code == 201, invite.text[:200])
        accepted = client.post(
            "/sharing/invites/accept",
            headers=partner_auth,
            json={"token": invite.json()["token"]},
        )
        check("partner accepts -> 200", accepted.status_code == 200, accepted.text[:200])

        partner_view = client.get(
            f"/partner/transactions/{visible_tx}/events", headers=partner_auth
        )
        partner_body = partner_view.json() if partner_view.status_code == 200 else {}
        partner_items = partner_body.get("items", [])
        check(
            "partner sees the row's receipts -> 200",
            partner_view.status_code == 200,
            partner_view.status_code,
        )
        check(
            "every projected actor is the owner (no system, no third party)",
            bool(partner_items)
            and {item.get("actor") for item in partner_items} == {owner_id}
            and len(partner_items) == 2,
            [(item.get("actor"), item.get("event_kind")) for item in partner_items],
        )
        check(
            "no payload crosses to the partner, no written value either",
            all("payload" not in item for item in partner_items)
            and "12.34" not in partner_view.text
            and "Caf\u00e9" not in partner_view.text,
            len(partner_view.text),
        )
        gone_view = client.get(
            f"/partner/transactions/{tx_gone.json()['id']}/events", headers=partner_auth
        )
        check(
            "a row whose receipts outlive it -> 404 for the partner",
            gone_view.status_code == 404 and gone_view.json().get("code") == "not_found",
            gone_view.text[:160],
        )

        stranger_email = f"m106-smoke-stranger-{stamp}@example.com"
        stranger_auth, stranger_detail = sign_up(client, stranger_email, password)
        check(
            "stranger can register + sign in (the 401 below needs a session)",
            stranger_auth is not None,
            stranger_detail,
        )
        if stranger_auth is None:
            skip("unlinked-caller 401 section", f"registration throttled: {stranger_detail}")
        else:
            refused = client.get(
                f"/partner/transactions/{visible_tx}/events", headers=stranger_auth
            )
            check(
                "an unlinked caller -> 401 sharing_not_linked",
                refused.status_code == 401 and refused.json().get("code") == "sharing_not_linked",
                refused.text[:160],
            )

    return emit(
        {
            "email": email,
            "full_events": len(events),
            "zip_events": len(body),
            "tables_full": len(snapshot),
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())

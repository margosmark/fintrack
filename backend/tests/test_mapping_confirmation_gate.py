"""Mapping-confirmation gate — nothing is written before the user confirms (docs/58 AC).

The gate, end to end on the Monarch fixture:

1. ``POST /imports/suggest-profile`` names the format and maps the columns —
   and writes nothing.
2. ``POST /imports/upload`` WITHOUT ``profile_json`` is 422
   ``missing_importer_profile`` — still nothing written.
3. With the confirmed profile the rows are staged, but the ledger a report
   reads stays empty until ``POST /imports/{batch_id}/commit``.
4. ``POST /imports/networth/preview`` writes nothing either.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models.accounts import Account
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transactions import Transaction

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL = "map-gate@example.com"
PASSWORD = "correct-horse-battery"

# Keys the wizard actually sends back as the confirmed mapping (docs/21 §5);
# `format`/`confidence`/`reasoning` are suggestion metadata, not profile data.
PROFILE_KEYS = ("columns", "date_format", "decimal_comma", "credits_positive", "headerless")


def _register(client: TestClient) -> str:
    client.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _counts(session: Session) -> dict[str, int]:
    """Table-wide row counts — stable within one test, decoy tenant included."""
    return {
        "accounts": len(session.exec(select(Account)).all()),
        "transactions": len(session.exec(select(Transaction)).all()),
        "batches": len(session.exec(select(ImportBatch)).all()),
    }


def _live_transactions(session: Session, user_id: int) -> int:
    """Rows a report would see: committed batches or manual (no batch) rows."""
    live = 0
    for txn in session.exec(select(Transaction).where(Transaction.user_id == user_id)).all():
        if txn.import_batch_id is None:
            live += 1
            continue
        batch = session.get(ImportBatch, txn.import_batch_id)
        if batch is not None and batch.status == ImportBatchStatus.committed:
            live += 1
    return live


def _user_id(session: Session) -> int:
    from app.models.users import User

    user = session.exec(select(User).where(User.email == EMAIL)).first()
    assert user is not None
    return user.id


def _make_account(client: TestClient, token: str) -> int:
    account = client.post(
        "/api/v1/accounts",
        json={"name": "Gate Checking", "type": "chequing", "currency": "CAD"},
        headers=_headers(token),
    )
    assert account.status_code == 201, account.text
    return account.json()["id"]


def _suggest(client: TestClient, token: str) -> dict:
    raw = (FIXTURES / "monarch_transactions.csv").read_bytes()
    response = client.post(
        "/api/v1/imports/suggest-profile",
        headers=_headers(token),
        files={"file": ("monarch_transactions.csv", raw, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _upload(client: TestClient, token: str, account_id: int, profile: dict | None):
    raw = (FIXTURES / "monarch_transactions.csv").read_bytes()
    data = {"account_id": str(account_id)}
    if profile is not None:
        data["profile_json"] = json.dumps(profile)
    return client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data=data,
        files={"file": ("monarch_transactions.csv", raw, "text/csv")},
    )


def test_suggest_profile_names_the_format_and_writes_nothing(
    client: TestClient, session: Session
) -> None:
    token = _register(client)
    before = _counts(session)

    suggestion = _suggest(client, token)

    assert _counts(session) == before, "suggest-profile must not write a single row"
    assert suggestion["format"] == "monarch"
    assert suggestion["columns"] == {
        "date": "Date",
        "amount_native": "Amount",
        "raw_description": "Merchant",
    }
    assert suggestion["date_format"] == "%m/%d/%Y"
    # Monarch exports expenses as negative numbers — taken from the format.
    assert suggestion["credits_positive"] is True
    assert "confirm" in suggestion["reasoning"].lower()


def test_upload_without_a_confirmed_profile_is_refused_and_writes_nothing(
    client: TestClient, session: Session
) -> None:
    token = _register(client)
    account_id = _make_account(client, token)
    before = _counts(session)

    response = _upload(client, token, account_id, profile=None)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "missing_importer_profile"
    assert _counts(session) == before, "an unconfirmed mapping must not stage anything"
    assert _live_transactions(session, _user_id(session)) == 0


def test_confirmed_mapping_stages_then_commit_reaches_the_ledger(
    client: TestClient, session: Session
) -> None:
    token = _register(client)
    account_id = _make_account(client, token)
    user_id = _user_id(session)

    suggestion = _suggest(client, token)
    profile = {key: suggestion[key] for key in PROFILE_KEYS if key in suggestion}
    assert profile["columns"]["amount_native"] == "Amount"

    upload = _upload(client, token, account_id, profile)
    assert upload.status_code == 201, upload.text
    assert upload.json()["status"] == "pending_review"
    # Staged is written; the ledger a report reads is not.
    staged = session.exec(select(Transaction).where(Transaction.user_id == user_id)).all()
    assert len(staged) == 5
    assert _live_transactions(session, user_id) == 0, (
        "staged rows must not reach reports before the commit"
    )

    commit = client.post(f"/api/v1/imports/{upload.json()['id']}/commit", headers=_headers(token))
    assert commit.status_code == 200, commit.text
    assert commit.json()["status"] == "committed"
    assert _live_transactions(session, user_id) == 5


def test_networth_preview_writes_nothing(client: TestClient, session: Session) -> None:
    token = _register(client)
    before = _counts(session)
    raw = (FIXTURES / "projectionlab_export.json").read_bytes()

    response = client.post(
        "/api/v1/imports/networth/preview",
        headers=_headers(token),
        files={"file": ("projectionlab_export.json", raw, "application/json")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_format"] == "projectionlab_json"
    assert body["counts"]["accounts_create"] == 10
    assert _counts(session) == before, "a net-worth preview must not write a single row"

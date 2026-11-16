"""M71 import-pipeline tests: QIF/QFX parse, cancel-safe dry run, near-dup (docs/03).

The two load-bearing claims:

* ``POST /imports/preview`` performs the FULL parse — format sniff, CSV caps,
  row cap, duplicate verdicts, cached-FX application — and writes **nothing**.
  Every dry-run test asserts row counts in ``transactions`` and
  ``import_batches`` are byte-identical before and after the call.
* A near-duplicate (same amount, within the date window, fuzzy-similar payee)
  is surfaced for the user's decision — never silently skipped, never promoted
  to an exact duplicate.
"""

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models.import_batches import ImportBatch
from app.models.transactions import Transaction

FIXTURES = Path(__file__).parent / "fixtures"

EMAIL = "m71@example.com"
PASSWORD = "correct-horse-battery"

# The near-dup fixtures are plain signed-amount CSVs; credits_positive keeps
# the file's signs verbatim.
NEAR_DUP_PROFILE = {
    "columns": {
        "date": "Date",
        "amount_native": "Amount",
        "raw_description": "Description",
    },
    "date_format": "%Y-%m-%d",
    "credits_positive": True,
}


def _register(client: TestClient) -> dict:
    client.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_account(client: TestClient, token: str, profile: dict | None = None) -> int:
    payload: dict = {"name": "M71 account"}
    if profile is not None:
        payload["importer_profile"] = profile
    inst = client.post("/api/v1/institutions", json=payload, headers=_headers(token))
    assert inst.status_code == 201, inst.text
    account = client.post(
        "/api/v1/accounts",
        json={
            "name": "Chequing",
            "type": "chequing",
            "currency": "CAD",
            "institution_id": inst.json()["id"],
        },
        headers=_headers(token),
    )
    assert account.status_code == 201, account.text
    return account.json()["id"]


def _preview(
    client: TestClient, token: str, account_id: int, fixture: str, profile_json: str | None = None
):
    raw = (FIXTURES / fixture).read_bytes()
    data: dict = {"account_id": str(account_id)}
    if profile_json is not None:
        data["profile_json"] = profile_json
    return client.post(
        "/api/v1/imports/preview",
        headers=_headers(token),
        data=data,
        files={"file": (fixture, raw, "application/octet-stream")},
    )


def _preview_bytes(
    client: TestClient,
    token: str,
    account_id: int,
    filename: str,
    raw: bytes,
    profile_json: str | None = None,
):
    data: dict = {"account_id": str(account_id)}
    if profile_json is not None:
        data["profile_json"] = profile_json
    return client.post(
        "/api/v1/imports/preview",
        headers=_headers(token),
        data=data,
        files={"file": (filename, raw, "application/octet-stream")},
    )


def _upload(
    client: TestClient, token: str, account_id: int, fixture: str, profile_json: str | None = None
) -> dict:
    raw = (FIXTURES / fixture).read_bytes()
    data: dict = {"account_id": str(account_id)}
    if profile_json is not None:
        data["profile_json"] = profile_json
    response = client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data=data,
        files={"file": (fixture, raw, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _counts(session: Session) -> dict:
    session.expire_all()
    return {
        "transactions": len(session.exec(select(Transaction)).all()),
        "batches": len(session.exec(select(ImportBatch)).all()),
    }


# ---------------------------------------------------------------- QIF / QFX


def test_qif_upload_stages_rows_with_canonical_signs(client: TestClient, session: Session) -> None:
    auth = _register(client)
    account_id = _make_account(client, auth["access_token"])
    body = _upload(client, auth["access_token"], account_id, "sample.qif")

    assert body["status"] == "pending_review"
    assert body["imported"] == 3
    assert body["duplicates_skipped"] == 0

    rows = session.exec(
        select(Transaction).where(Transaction.account_id == account_id).order_by(Transaction.id)
    ).all()
    assert [str(r.date) for r in rows] == ["2026-03-15", "2026-03-16", "2026-03-17"]
    assert [r.amount_native for r in rows] == [
        Decimal("-45.67"),
        Decimal("1200.00"),
        Decimal("-15.99"),
    ]
    # P payee wins, then M memo, then L category; the account currency applies
    # because QIF carries none.
    assert [r.raw_description for r in rows] == [
        "METRO STORES #123",
        "PAYROLL DEPOSIT - ACME CORP",
        "Streaming",
    ]
    assert all(r.currency == "CAD" for r in rows)
    assert rows[0].import_batch_id == body["id"]


def test_qif_dry_run_reports_structured_mapping_and_writes_nothing(
    client: TestClient, session: Session
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)
    before = _counts(session)

    response = _preview(client, token, account_id, "sample.qif")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["format"] == "qif"
    assert body["mapping"]["kind"] == "structured"
    roles = {f["field"]: f["source"] for f in body["mapping"]["fields"]}
    assert roles["date"] == "D"
    assert roles["amount_native"] == "T"
    assert roles["raw_description"] == "P, else M, else L"
    assert body["summary"] == {
        "total": 3,
        "new": 3,
        "duplicates": 0,
        "near_duplicates": 0,
        "unresolved": 0,
    }
    assert [r["verdict"] for r in body["rows"]] == ["new", "new", "new"]
    assert Decimal(body["rows"][0]["cad_amount"]) == Decimal("-45.67")

    assert _counts(session) == before == {"transactions": 0, "batches": 0}


def test_qfx_dry_run_is_ofx_family_and_writes_nothing(client: TestClient, session: Session) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)
    before = _counts(session)

    response = _preview(client, token, account_id, "sample.qfx")
    assert response.status_code == 200, response.text
    body = response.json()
    # QFX is Quicken's branded OFX — one parser, one detect path (docs/03).
    assert body["format"] == "ofx"
    assert body["summary"]["total"] == 5
    assert body["summary"]["new"] == 5
    roles = {f["field"]: f["source"] for f in body["mapping"]["fields"]}
    assert roles["amount_native"] == "TRNAMT"
    assert roles["date"] == "DTPOSTED"

    assert _counts(session) == before == {"transactions": 0, "batches": 0}


def test_csv_dry_run_reports_profile_mapping_and_writes_nothing(
    client: TestClient, session: Session
) -> None:
    import json

    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)
    before = _counts(session)

    response = _preview(
        client, token, account_id, "near_dup_seed.csv", json.dumps(NEAR_DUP_PROFILE)
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["format"] == "csv"
    assert body["mapping"]["kind"] == "csv_profile"
    assert body["mapping"]["mode"] == "signed"
    assert body["mapping"]["date_format"] == "%Y-%m-%d"
    assert body["mapping"]["credits_positive"] is True
    columns = {f["field"]: f["source"] for f in body["mapping"]["fields"]}
    assert columns == {
        "date": "Date",
        "amount_native": "Amount",
        "raw_description": "Description",
    }
    assert body["summary"]["total"] == 3
    assert body["summary"]["new"] == 3

    assert _counts(session) == before == {"transactions": 0, "batches": 0}


def test_cancel_after_a_full_preview_persists_nothing(client: TestClient, session: Session) -> None:
    """The whole cancel-safety claim: preview, then simply do not confirm."""
    import json

    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)
    before = _counts(session)

    for fixture in ("sample.qif", "sample.qfx", "near_dup_seed.csv"):
        profile = json.dumps(NEAR_DUP_PROFILE) if fixture.endswith(".csv") else None
        response = _preview(client, token, account_id, fixture, profile)
        assert response.status_code == 200, response.text

    assert _counts(session) == before == {"transactions": 0, "batches": 0}
    assert session.exec(select(ImportBatch)).all() == []


# ------------------------------------------------------------- near-duplicate


def test_near_duplicate_is_flagged_for_user_decision(client: TestClient, session: Session) -> None:
    import json

    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token, profile=NEAR_DUP_PROFILE)
    profile_json = json.dumps(NEAR_DUP_PROFILE)

    seed = _upload(client, token, account_id, "near_dup_seed.csv")
    assert seed["imported"] == 3
    committed = client.post(
        f"/api/v1/imports/{seed['id']}/commit", headers=_headers(token), json={}
    )
    assert committed.status_code == 200, committed.text

    before = _counts(session)
    response = _preview(client, token, account_id, "near_dup_shift.csv", profile_json)
    assert response.status_code == 200, response.text
    body = response.json()

    # The dry run still writes nothing while deciding.
    assert _counts(session) == before == {"transactions": 3, "batches": 1}

    verdicts = [row["verdict"] for row in body["rows"]]
    assert verdicts == [
        "near_duplicate",  # 2026-03-11 METRO — twin of the committed 03-10 row
        "near_duplicate",  # same payee + " TORONTO"
        "near_duplicate",  # NETFLIX one day later
        "duplicate",  # byte-identical re-upload of the committed payroll row
        "new",  # different payee AND amount — nothing to match
    ]
    assert body["summary"] == {
        "total": 5,
        "new": 1,
        "duplicates": 1,
        "near_duplicates": 3,
        "unresolved": 0,
    }

    # The twin is named, with the evidence the user decides on.
    match = body["rows"][0]["near_duplicate_match"]
    assert match is not None
    assert match["source"] == "committed"
    assert match["delta_days"] == 1
    assert Decimal(match["amount_native"]) == Decimal("-45.67")
    assert match["raw_description"] == "METRO STORES #123"
    assert match["similarity"] == 1.0
    assert isinstance(match["transaction_id"], int)
    assert body["rows"][1]["near_duplicate_match"]["similarity"] >= 0.75
    # An exact duplicate never gets a near-duplicate label (verdict precedence).
    assert body["rows"][3]["near_duplicate_match"] is None


def test_staged_batch_rows_keep_the_near_duplicate_flag(
    client: TestClient, session: Session
) -> None:
    """Re-opening a pending batch re-derives the verdict (the review window)."""
    import json

    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token, profile=NEAR_DUP_PROFILE)
    profile_json = json.dumps(NEAR_DUP_PROFILE)

    seed = _upload(client, token, account_id, "near_dup_seed.csv")
    client.post(f"/api/v1/imports/{seed['id']}/commit", headers=_headers(token), json={})
    shift = _upload(client, token, account_id, "near_dup_shift.csv", profile_json)

    response = client.get(f"/api/v1/imports/{shift['id']}/rows", headers=_headers(token))
    assert response.status_code == 200, response.text
    rows = response.json()

    assert len(rows) == 5
    flagged = [r for r in rows if r["near_duplicate"]]
    assert len(flagged) == 3
    assert {r["near_duplicate_match"]["source"] for r in flagged} <= {"committed", "staged"}
    assert all(r["near_duplicate_match"]["transaction_id"] for r in flagged)
    assert all(r["near_duplicate_match"]["delta_days"] <= 3 for r in flagged)
    # The exact duplicate is reported once, as a duplicate.
    exact = [r for r in rows if r["duplicate"]]
    assert len(exact) == 1
    assert exact[0]["near_duplicate"] is False
    # A near-duplicate is staged as a normal row: it is never silently skipped.
    assert all(r["id"] > 0 for r in rows)


def test_near_duplicate_window_and_amount_are_hard_bounds(
    client: TestClient, session: Session
) -> None:
    """A row outside the 3-day window or with a different amount is `new`."""
    import json

    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token, profile=NEAR_DUP_PROFILE)
    profile_json = json.dumps(NEAR_DUP_PROFILE)
    _upload(client, token, account_id, "near_dup_seed.csv")

    cases = [
        # 15 days later: outside the 3-day window.
        ("far.csv", "Date,Description,Amount\n2026-03-25,METRO STORES #123,-45.67\n", "new"),
        # Same payee/window, one cent apart: amount must match exactly.
        ("priced.csv", "Date,Description,Amount\n2026-03-10,METRO STORES #123,-45.68\n", "new"),
        # Same day and amount, unrelated payee: similarity gate.
        (
            "unrelated.csv",
            "Date,Description,Amount\n2026-03-10,SHELL STATION 771,-45.67\n",
            "new",
        ),
        # Inside the window, exact amount, payee substring -> flagged.
        (
            "near.csv",
            "Date,Description,Amount\n2026-03-11,METRO STORES #123 EAST,-45.67\n",
            "near_duplicate",
        ),
    ]
    for filename, content, expected in cases:
        response = _preview_bytes(
            client, token, account_id, filename, content.encode(), profile_json
        )
        assert response.status_code == 200, response.text
        assert response.json()["rows"][0]["verdict"] == expected, filename


# ------------------------------------------------------------------- errors


def test_qif_missing_amount_is_422_qif_parse_error(client: TestClient, session: Session) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)
    before = _counts(session)

    raw = b"!Type:Bank\nD03/15/2026\nPNODESC\n^\n"
    response = client.post(
        "/api/v1/imports/preview",
        headers=_headers(token),
        data={"account_id": str(account_id)},
        files={"file": ("broken.qif", raw, "application/octet-stream")},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "qif_parse_error"
    assert "record 1" in response.json()["detail"]
    assert _counts(session) == before


def test_preview_unknown_format_is_422_envelope(client: TestClient, session: Session) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)
    before = _counts(session)

    response = client.post(
        "/api/v1/imports/preview",
        headers=_headers(token),
        data={"account_id": str(account_id)},
        files={
            "file": ("mystery.bin", b"\x00\x01\x02 not a statement", "application/octet-stream")
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "unsupported_format"
    assert _counts(session) == before


def test_preview_csv_without_profile_is_422_missing_importer_profile(
    client: TestClient, session: Session
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)  # institution carries no profile
    before = _counts(session)

    response = _preview(client, token, account_id, "near_dup_seed.csv")
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "missing_importer_profile"
    assert _counts(session) == before


def test_preview_is_tenancy_safe(client: TestClient, session: Session) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)

    client.post(
        "/api/v1/auth/register",
        json={"email": "m71-other@example.com", "password": PASSWORD},
    )
    other = client.post(
        "/api/v1/auth/login",
        json={"email": "m71-other@example.com", "password": PASSWORD},
    )
    other_token = other.json()["access_token"]
    response = _preview(client, other_token, account_id, "sample.qif")
    assert response.status_code == 404, response.text

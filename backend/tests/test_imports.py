"""M2 staged import pipeline tests (docs/03, w2-contract BackendIngestion pins)."""

import json
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models.import_batches import ImportBatchStatus
from app.models.transactions import Transaction

FIXTURES = Path(__file__).parent / "fixtures"

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery"


def _register(client: TestClient) -> dict:
    client.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_account(
    client: TestClient,
    token: str,
    currency: str = "CAD",
    profile: dict | None = None,
) -> int:
    inst_payload: dict = {"name": "Test Bank"}
    if profile is not None:
        inst_payload["importer_profile"] = profile
    inst = client.post("/api/v1/institutions", json=inst_payload, headers=_headers(token))
    assert inst.status_code == 201, inst.text
    account = client.post(
        "/api/v1/accounts",
        json={
            "name": "Checking",
            "type": "chequing",
            "currency": currency,
            "institution_id": inst.json()["id"],
        },
        headers=_headers(token),
    )
    assert account.status_code == 201, account.text
    return account.json()["id"]


def _upload(client: TestClient, token: str, account_id: int, fixture: str) -> dict:
    raw = (FIXTURES / fixture).read_bytes()
    response = client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data={"account_id": str(account_id)},
        files={"file": (fixture, raw, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    return response.json()


DOCS03_PROFILE = {
    "columns": {
        "date": "Trans. Date",
        "amount_native": "Amount",
        "raw_description": "Description",
    },
    "date_format": "%d/%m/%Y",
    "decimal_comma": True,
    "credits_positive": False,
}

SIGN_INVERTED_PROFILE = {
    "columns": {"date": "Date", "amount_native": "Amount", "raw_description": "Description"},
    "credits_positive": True,
}


def test_upload_qfx_creates_pending_batch_with_preview_and_counts(
    client: TestClient, session: Session
) -> None:
    auth = _register(client)
    account_id = _make_account(client, auth["access_token"])
    body = _upload(client, auth["access_token"], account_id, "sample.qfx")

    assert body["status"] == ImportBatchStatus.pending_review
    assert body["imported"] == 5
    assert body["duplicates_skipped"] == 0
    assert body["unresolved"] == 0
    assert body["file_name"] == "sample.qfx"

    preview = {row["raw_description"]: row for row in body["preview"]}
    grocery = preview["GROCERY MART #12"]
    assert str(grocery["date"]) == "2026-01-05"
    assert Decimal(grocery["amount_native"]) == Decimal("-42.50")
    assert grocery["currency"] == "CAD"
    # CAD needs no fx row: cad_amount mirrors amount_native.
    assert Decimal(grocery["cad_amount"]) == Decimal("-42.50")
    assert grocery["duplicate"] is False
    payroll = preview["PAYROLL ACME CORP"]
    assert Decimal(payroll["amount_native"]) == Decimal("2500.00")
    refund = preview["REFUND SHOE SHOP"]
    assert Decimal(refund["amount_native"]) == Decimal("19.95")

    rows = session.exec(select(Transaction).where(Transaction.account_id == account_id)).all()
    assert len(rows) == 5
    assert all(t.import_batch_id == body["id"] for t in rows)
    assert all(t.normalized_payee == " ".join(t.raw_description.split()) for t in rows)
    assert all(t.dedup_hash for t in rows)


def test_qfx_on_usd_account_counts_unresolved_when_fx_cache_empty(
    client: TestClient,
) -> None:
    auth = _register(client)
    account_id = _make_account(client, auth["access_token"], currency="USD")
    body = _upload(client, auth["access_token"], account_id, "sample.qfx")

    assert body["imported"] == 5
    assert body["unresolved"] == 5
    assert all(row["cad_amount"] is None for row in body["preview"])


def test_cached_fx_rate_fills_cad_amount_without_network(
    client: TestClient, session: Session
) -> None:
    from datetime import date

    from app.models.fx_rates import FxRate, FxSource

    auth = _register(client)
    account_id = _make_account(client, auth["access_token"], currency="USD")
    for day in ("2026-01-05", "2026-01-15"):
        session.add(
            FxRate(
                base="USD",
                quote="CAD",
                date=date.fromisoformat(day),
                rate=Decimal("1.26000000"),
                source=FxSource.manual,
            )
        )
    session.flush()

    body = _upload(client, auth["access_token"], account_id, "sample.qfx")
    preview = {row["raw_description"]: row for row in body["preview"]}
    assert Decimal(preview["GROCERY MART #12"]["cad_amount"]) == Decimal("-53.55")  # -42.50 * 1.26


def test_reupload_stages_duplicates_and_commit_drops_them_by_default(
    client: TestClient, session: Session
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)

    first = _upload(client, token, account_id, "sample.qfx")
    assert first["imported"] == 5
    assert len(session.exec(select(Transaction)).all()) == 5

    # Re-upload while first batch is still pending_review: all 5 are duplicates,
    # now STAGED (salted) instead of silently dropped.
    second = _upload(client, token, account_id, "sample.qfx")
    assert second["imported"] == 0
    assert second["duplicates_skipped"] == 5
    assert all(row["duplicate"] for row in second["preview"])
    rows = session.exec(select(Transaction)).all()
    assert len(rows) == 10  # 5 original (pending) + 5 staged salted dups
    staged = [r for r in rows if r.import_batch_id == second["id"]]
    assert len(staged) == 5
    assert all(r.is_duplicate for r in staged)
    assert all(":" in r.dedup_hash for r in staged)  # salted, unique-safe

    # Commit first batch (its 5 become committed).
    first_commit = client.post(
        f"/api/v1/imports/{first['id']}/commit",
        headers=_headers(token),
        json={"include_ids": []},
    )
    assert first_commit.status_code == 200

    # Commit second batch WITHOUT forcing includes -> staged dups deleted.
    second_commit = client.post(
        f"/api/v1/imports/{second['id']}/commit",
        headers=_headers(token),
        json={"include_ids": []},
    )
    assert second_commit.status_code == 200
    assert second_commit.json()["status"] == ImportBatchStatus.committed
    assert second_commit.json()["imported"] == 0
    assert second_commit.json()["duplicates_skipped"] == 5
    assert len(session.exec(select(Transaction)).all()) == 5  # dups gone

    # Re-upload again: dedup still spans committed rows (docs/03).
    third = _upload(client, token, account_id, "sample.qfx")
    assert third["imported"] == 0
    assert third["duplicates_skipped"] == 5


def test_commit_can_force_include_a_duplicate(client: TestClient, session: Session) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)

    _upload(client, token, account_id, "sample.qfx")  # first: 5 rows
    second = _upload(client, token, account_id, "sample.qfx")  # 5 staged dups
    dup_rows = second["preview"]
    assert len(dup_rows) == 5 and all(r["duplicate"] for r in dup_rows)
    force_id = dup_rows[0]["id"]

    # Force-include exactly one duplicate.
    resp = client.post(
        f"/api/v1/imports/{second['id']}/commit",
        headers=_headers(token),
        json={"include_ids": [force_id]},
    )
    assert resp.status_code == 200, resp.text
    cj = resp.json()
    assert cj["imported"] == 1
    assert cj["duplicates_skipped"] == 4

    rows = session.exec(select(Transaction)).all()
    # 5 original (still pending) + 1 forced duplicate kept.
    assert len(rows) == 6
    forced = session.get(Transaction, force_id)
    assert forced is not None and forced.is_duplicate and forced.force_include
    assert ":" in forced.dedup_hash  # salted, distinct from canonical
    # The other 4 duplicates were deleted.
    remaining_unforced = [
        r
        for r in rows
        if r.import_batch_id == second["id"] and r.is_duplicate and not r.force_include
    ]
    assert remaining_unforced == []


def test_discard_deletes_staged_rows_and_rows_can_reimport(
    client: TestClient,
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)

    body = _upload(client, token, account_id, "sample.qfx")
    batch_id = body["id"]
    discarded = client.post(f"/api/v1/imports/{batch_id}/discard", headers=_headers(token))
    assert discarded.status_code == 200
    assert discarded.json()["status"] == ImportBatchStatus.discarded

    again = _upload(client, token, account_id, "sample.qfx")
    assert again["imported"] == 5  # discarded rows are gone, nothing collides
    assert again["duplicates_skipped"] == 0


def test_commit_and_discard_after_terminal_status_conflict_409(
    client: TestClient,
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)
    batch_id = _upload(client, token, account_id, "sample.qfx")["id"]

    first_commit = client.post(f"/api/v1/imports/{batch_id}/commit", headers=_headers(token))
    assert first_commit.status_code == 200
    second_commit = client.post(f"/api/v1/imports/{batch_id}/commit", headers=_headers(token))
    assert second_commit.status_code == 409
    assert second_commit.json()["code"] == "batch_not_pending_review"
    late_discard = client.post(f"/api/v1/imports/{batch_id}/discard", headers=_headers(token))
    assert late_discard.status_code == 409
    assert late_discard.json()["code"] == "batch_not_pending_review"


def test_get_import_batch_summary(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token, profile=dict(SIGN_INVERTED_PROFILE))
    created = _upload(client, token, account_id, "csv_sign_inverted.csv")
    fetched = client.get(f"/api/v1/imports/{created['id']}", headers=_headers(token))
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["status"] == ImportBatchStatus.pending_review
    assert body["imported"] == 4
    assert body["duplicates_skipped"] == 0
    assert "preview" not in body  # summary shape only


def test_csv_decimal_comma_profile_parses_dates_amounts_signs(
    client: TestClient,
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token, profile=dict(DOCS03_PROFILE))
    body = _upload(client, token, account_id, "csv_decimal_comma.csv")

    assert body["imported"] == 4
    preview = {row["raw_description"]: row for row in body["preview"]}
    # credits_positive=false -> file debits are positive; signs inverted on ingest.
    assert Decimal(preview["CAFE LUNA"]["amount_native"]) == Decimal("-4.50")
    assert Decimal(preview["RENT JANUARY"]["amount_native"]) == Decimal("-1250.00")
    assert Decimal(preview["REFUND SHOE SHOP"]["amount_native"]) == Decimal("23.99")
    rent_date = preview["RENT JANUARY"]["date"]
    assert str(rent_date) == "2026-01-12"  # %d/%m/%Y parsed correctly
    assert Decimal(preview["RENT JANUARY"]["cad_amount"]) == Decimal("-1250.00")


def test_csv_sign_inverted_profile_keeps_canonical_signs(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(
        client, token, profile=dict(SIGN_INVERTED_PROFILE, credits_positive=True)
    )
    body = _upload(client, token, account_id, "csv_sign_inverted.csv")

    assert body["imported"] == 4
    preview = {row["raw_description"]: row for row in body["preview"]}
    # credits_positive=true -> already canonical (debits negative).
    assert Decimal(preview["HYDRO ONTARIO"]["amount_native"]) == Decimal("-58.20")
    assert Decimal(preview["PAYROLL ACME CORP"]["amount_native"]) == Decimal("1500.00")
    assert Decimal(preview["STREAMFLIX MONTHLY"]["amount_native"]) == Decimal("-9.99")


def test_upload_unknown_format_is_422_envelope(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)
    response = client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data={"account_id": str(account_id)},
        files={"file": ("blob.bin", b"\x00\x01\x02garbage", "application/octet-stream")},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "unsupported_format"


def test_upload_csv_without_profile_is_422(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)  # institution has no importer_profile
    response = client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data={"account_id": str(account_id)},
        files={
            "file": (
                "x.csv",
                b"Date,Amount,Description\n2026-01-01,-5,TEST\n",
                "text/csv",
            )
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "missing_importer_profile"


def test_upload_csv_with_profile_json_override_succeeds(client: TestClient) -> None:
    """Wizard-confirmed profile_json (the fixed frontend field name) parses even
    when the institution has no stored importer_profile — mirrors the
    column-mapping wizard upload path end-to-end."""
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)  # institution has no importer_profile
    profile = {
        "columns": {
            "date": "Date",
            "amount_native": "Amount",
            "raw_description": "Description",
        },
        "date_format": "%Y-%m-%d",
        "credits_positive": False,
    }
    response = client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data={"account_id": str(account_id), "profile_json": json.dumps(profile)},
        files={
            "file": (
                "x.csv",
                b"Date,Amount,Description\n2026-01-01,-5,TEST\n2026-01-02,10,PAY\n",
                "text/csv",
            )
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["imported"] == 2


def test_upload_to_other_users_account_is_404(client: TestClient) -> None:
    first = _register(client)
    client.post(
        "/api/v1/auth/register",
        json={"email": "bob@example.com", "password": PASSWORD},
    ).json()
    second = client.post(
        "/api/v1/auth/login",
        json={"email": "bob@example.com", "password": PASSWORD},
    ).json()
    account_id = _make_account(client, second["access_token"])
    response = client.post(
        "/api/v1/imports/upload",
        headers=_headers(first["access_token"]),
        data={"account_id": str(account_id)},
        files={"file": ("sample.qfx", (FIXTURES / "sample.qfx").read_bytes(), "app/x")},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# --- Revert tests -----------------------------------------------------------


def test_revert_committed_batch(client: TestClient, session: Session) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)
    batch = _upload(client, token, account_id, "sample.qfx")

    # Commit the batch
    resp = client.post(f"/api/v1/imports/{batch['id']}/commit", headers=_headers(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "committed"

    # Verify transactions exist
    txns = session.exec(select(Transaction).where(Transaction.import_batch_id == batch["id"])).all()
    assert len(txns) > 0

    # Revert
    resp = client.post(f"/api/v1/imports/{batch['id']}/revert", headers=_headers(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "reverted"

    # Verify transactions deleted
    txns = session.exec(select(Transaction).where(Transaction.import_batch_id == batch["id"])).all()
    assert len(txns) == 0


def test_revert_pending_review_batch_returns_409(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)
    batch = _upload(client, token, account_id, "sample.qfx")

    resp = client.post(f"/api/v1/imports/{batch['id']}/revert", headers=_headers(token))
    assert resp.status_code == 409
    assert resp.json()["code"] == "batch_not_committed"


def test_revert_discarded_batch_returns_409(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)
    batch = _upload(client, token, account_id, "sample.qfx")

    # Discard first
    resp = client.post(f"/api/v1/imports/{batch['id']}/discard", headers=_headers(token))
    assert resp.status_code == 200

    # Try to revert
    resp = client.post(f"/api/v1/imports/{batch['id']}/revert", headers=_headers(token))
    assert resp.status_code == 409
    assert resp.json()["code"] == "batch_not_committed"


def test_revert_unlinks_transfer_pairs(client: TestClient, session: Session) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)

    # Upload and commit a batch with transactions
    batch = _upload(client, token, account_id, "sample.qfx")
    resp = client.post(f"/api/v1/imports/{batch['id']}/commit", headers=_headers(token))
    assert resp.status_code == 200

    # Get one of the imported transactions
    txns = session.exec(select(Transaction).where(Transaction.import_batch_id == batch["id"])).all()
    assert len(txns) > 0
    imported_txn = txns[0]

    # Create a second account and a manual transaction to pair with
    account2_id = _make_account(client, token)
    import hashlib
    from decimal import Decimal

    canonical = (
        f"{account2_id}"
        f"|{imported_txn.date.isoformat()}"
        f"|{(-imported_txn.amount_native).quantize(Decimal('0.01'))}"
        f"|Transfer pair"
    )
    dedup = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    manual_txn = Transaction(
        user_id=auth["user"]["id"],
        account_id=account2_id,
        date=imported_txn.date,
        raw_description="Transfer pair",
        normalized_payee="transfer pair",
        amount_native=-imported_txn.amount_native,
        currency="CAD",
        dedup_hash=dedup,
    )
    session.add(manual_txn)
    session.commit()
    session.refresh(manual_txn)
    manual_txn_id = manual_txn.id

    # Link them as a transfer pair
    link_resp = client.post(
        f"/api/v1/transactions/{imported_txn.id}/link-transfer",
        headers=_headers(token),
        json={"pair_id": manual_txn_id},
    )
    assert link_resp.status_code == 200

    # Verify the manual txn now has transfer_pair_id set
    session.expire_all()
    manual_txn = session.get(Transaction, manual_txn_id)
    assert manual_txn.transfer_pair_id == imported_txn.id

    # Revert the batch
    resp = client.post(f"/api/v1/imports/{batch['id']}/revert", headers=_headers(token))
    assert resp.status_code == 200

    # Verify the manual txn's transfer_pair_id is now NULL
    session.expire_all()
    manual_txn = session.get(Transaction, manual_txn_id)
    assert manual_txn.transfer_pair_id is None


def test_get_batch_rows_returns_staged_rows(client: TestClient, session: Session) -> None:
    """GET /imports/{id}/rows exposes a pending batch's staged rows for review."""
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token)
    body = _upload(client, token, account_id, "sample.qfx")
    batch_id = body["id"]

    resp = client.get(f"/api/v1/imports/{batch_id}/rows", headers=_headers(token))
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == body["imported"] == 5
    for r in rows:
        assert set(
            ["id", "date", "amount_native", "currency", "raw_description", "duplicate"]
        ).issubset(r.keys())

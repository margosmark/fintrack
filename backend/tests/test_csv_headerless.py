"""Headerless CSV import: positional column mapping (docs/03).

Mirrors test_csv_flex.py helpers (_register / _headers / _upload / account
creation) but drives the positional (no-header-row) code path end to end:
parse, server-side validation, split mode, detection, and the upload endpoint.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.importers.base import AccountView, RawTransaction
from app.importers.csv_profile import CsvImporter, CsvProfileError
from app.services.column_mapping import suggest_column_mapping

EMAIL = "headerless@example.com"
PASSWORD = "correct-horse-battery"
FIXTURE = Path(__file__).parent / "fixtures" / "csv_no_header.csv"


def _register(client: TestClient) -> dict:
    client.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _parse(profile: dict, raw: bytes) -> list[RawTransaction]:
    account = AccountView(currency="CAD", importer_profile=profile)
    return CsvImporter.parse(raw, account)


def _upload(client: TestClient, token: str, account_id: int, profile: dict, raw: bytes) -> dict:
    return client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data={"account_id": str(account_id), "profile_json": json.dumps(profile)},
        files={"file": ("no_header.csv", raw, "application/octet-stream")},
    ).json()


def _account_no_profile(client: TestClient, token: str) -> int:
    inst = client.post(
        "/api/v1/institutions",
        json={"name": "Headerless Bank"},
        headers=_headers(token),
    )
    assert inst.status_code == 201, inst.text
    account = client.post(
        "/api/v1/accounts",
        json={
            "name": "Checking",
            "type": "chequing",
            "currency": "CAD",
            "institution_id": inst.json()["id"],
        },
        headers=_headers(token),
    )
    assert account.status_code == 201, account.text
    return account.json()["id"]


def test_headerless_signed_parse() -> None:
    raw = FIXTURE.read_bytes()
    profile = {
        "columns": {"date": "1", "raw_description": "2", "amount_native": "3"},
        "headerless": True,
        "date_format": "%Y-%m-%d",
    }
    rows = _parse(profile, raw)
    assert len(rows) == 3
    by_desc = {r.raw_description: r for r in rows}
    assert by_desc["COFFEE SHOP"].date.isoformat() == "2024-01-05"
    # credits_positive defaults to False: file magnitudes are debits-positive and invert.
    assert by_desc["COFFEE SHOP"].amount_native == Decimal("12.50")
    assert by_desc["GROCERY"].amount_native == Decimal("-30.00")
    assert by_desc["SALARY"].amount_native == Decimal("-2000.00")


def test_headerless_invalid_non_numeric() -> None:
    raw = FIXTURE.read_bytes()
    # "Date" is not a 1-based column index -> rejected at profile validation.
    with pytest.raises(CsvProfileError):
        _parse(
            {
                "columns": {"date": "Date", "raw_description": "2", "amount_native": "3"},
                "headerless": True,
            },
            raw,
        )


def test_headerless_split_parse() -> None:
    raw = b"12.50,5.00,2024-01-05,COFFEE\n30.00,0,2024-01-06,GROCERY\n"
    profile = {
        "columns": {
            "debit": "1",
            "credit": "2",
            "date": "3",
            "raw_description": "4",
        },
        "headerless": True,
        "date_format": "%Y-%m-%d",
    }
    rows = _parse(profile, raw)
    assert len(rows) == 2
    by_desc = {r.raw_description: r for r in rows}
    # Debit positive -> negative (structural sign); credit -> positive.
    assert by_desc["COFFEE"].amount_native == Decimal("-12.50")
    assert by_desc["GROCERY"].amount_native == Decimal("-30.00")


def test_headerless_detection() -> None:
    raw = FIXTURE.read_bytes()
    result = suggest_column_mapping(raw, None)
    assert result["headerless"] is True
    assert result["columns"] == {"date": "1", "raw_description": "2", "amount_native": "3"}
    assert result["date_format"] == "%Y-%m-%d"


def test_headerless_upload_endpoint(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _account_no_profile(client, token)
    profile = {
        "columns": {"date": "1", "raw_description": "2", "amount_native": "3"},
        "date_format": "%Y-%m-%d",
        "headerless": True,
    }
    body = _upload(client, token, account_id, profile, FIXTURE.read_bytes())
    assert body["status"] == "pending_review"
    assert body["imported"] == 3
    assert len(body["preview"]) == 3

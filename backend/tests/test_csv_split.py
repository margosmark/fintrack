"""CSV split debit/credit column mode (docs/03 profiles, w9-contract BackendCsvSplit)."""

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.importers.base import AccountView
from app.importers.csv_profile import CsvImporter, CsvProfileError

FIXTURES = Path(__file__).parent / "fixtures"

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery"

SPLIT_PROFILE = {
    "columns": {
        "date": "Date",
        "raw_description": "Description",
        "debit": "Withdrawal",
        "credit": "Deposit",
    },
    "date_format": "%d/%m/%Y",
    "decimal_comma": True,
}


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
    return client.post(  # type: ignore[return-value]
        "/api/v1/imports/upload",
        headers=_headers(token),
        data={"account_id": str(account_id)},
        files={"file": (fixture, raw, "application/octet-stream")},
    ).json()


def test_split_mode_upload_parses_debit_negative_credit_positive(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account(client, token, profile=dict(SPLIT_PROFILE))
    body = _upload(client, token, account_id, "csv_credit_debit.csv")

    # 5 data rows, but GROCERY MART #12 has both columns empty -> skipped entirely.
    assert body["imported"] == 4
    preview = {row["raw_description"]: row for row in body["preview"]}
    assert set(preview) == {
        "CAFE LUNA",
        "RENT JANUARY",
        "REFUND SHOE SHOP",
        "PAYROLL ACME CORP",
    }
    # Debits negative, credits positive; decimal_comma applied before parsing.
    assert Decimal(preview["CAFE LUNA"]["amount_native"]) == Decimal("-4.50")
    assert Decimal(preview["RENT JANUARY"]["amount_native"]) == Decimal("-1250.00")
    assert Decimal(preview["REFUND SHOE SHOP"]["amount_native"]) == Decimal("23.99")
    assert Decimal(preview["PAYROLL ACME CORP"]["amount_native"]) == Decimal("1500.00")
    # %d/%m/%Y: 05/01/2026 -> 2026-01-05, not May 1st.
    assert str(preview["CAFE LUNA"]["date"]) == "2026-01-05"
    assert str(preview["PAYROLL ACME CORP"]["date"]) == "2026-02-15"


def test_split_mode_ignores_credits_positive(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    # Signs are structural in split mode; flipping credits_positive must change nothing.
    account_id = _make_account(client, token, profile=dict(SPLIT_PROFILE, credits_positive=True))
    body = _upload(client, token, account_id, "csv_credit_debit.csv")

    preview = {row["raw_description"]: row for row in body["preview"]}
    assert Decimal(preview["CAFE LUNA"]["amount_native"]) == Decimal("-4.50")
    assert Decimal(preview["REFUND SHOE SHOP"]["amount_native"]) == Decimal("23.99")


def test_split_mode_debit_wins_when_both_columns_filled() -> None:
    raw = b"Date,Description,Withdrawal,Deposit\n01/03/2026,ODD ROW,10,00\n"
    view = AccountView(currency="CAD", importer_profile=dict(SPLIT_PROFILE))
    rows = CsvImporter.parse(raw, view)
    assert len(rows) == 1
    assert rows[0].amount_native == Decimal("-10")


def test_invalid_profile_missing_credit_is_rejected_with_named_key(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    debit_only = {
        "columns": {
            "date": "Date",
            "raw_description": "Description",
            "debit": "Withdrawal",
        },
    }
    response = client.post(
        "/api/v1/institutions",
        json={"name": "Test Bank", "importer_profile": debit_only},
        headers=_headers(token),
    )
    # Institution creation validates the profile up front (generic envelope;
    # the missing-key detail is surfaced by the parser path below).
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"

    # Parser path surfaces the same rule via the CsvProfileError 422 envelope.
    view = AccountView(currency="CAD", importer_profile=debit_only)
    try:
        CsvImporter.parse(b"Date,Description,Withdrawal\n", view)
        raise AssertionError("expected CsvProfileError")
    except CsvProfileError as exc:
        assert "credit" in str(exc)


def test_mixed_profile_auto_resolves_to_split_mode(client: TestClient) -> None:
    """Mixed profile (amount_native + debit/credit) is accepted and parsed in
    split mode: `_resolve_mode_conflict` drops `amount_native`, so signed-mode
    values in the CSV are ignored (docs/03 profiles, docs/21 mode rule)."""
    auth = _register(client)
    token = auth["access_token"]
    mixed = {
        "columns": {
            "date": "Date",
            "amount_native": "Amount",
            "raw_description": "Description",
            "debit": "Withdrawal",
            "credit": "Deposit",
        },
    }
    response = client.post(
        "/api/v1/institutions",
        json={"name": "Test Bank", "importer_profile": mixed},
        headers=_headers(token),
    )
    assert response.status_code == 201

    # Parsing auto-resolves: the signed Amount column is dropped, and the row's
    # split columns decide the amount (debit 10 → -10, not the +99 Amount).
    raw = b"Date,Description,Amount,Withdrawal,Deposit\n2026-03-01,ODD ROW,99,10,\n"
    view = AccountView(currency="CAD", importer_profile=mixed)
    rows = CsvImporter.parse(raw, view)
    assert len(rows) == 1
    assert rows[0].amount_native == Decimal("-10")


def test_signed_mode_regression_unchanged(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    signed_profile = {
        "columns": {
            "date": "Trans. Date",
            "amount_native": "Amount",
            "raw_description": "Description",
        },
        "date_format": "%d/%m/%Y",
        "decimal_comma": True,
        "credits_positive": False,
    }
    account_id = _make_account(client, token, profile=signed_profile)
    body = _upload(client, token, account_id, "csv_decimal_comma.csv")

    assert body["imported"] == 4
    preview = {row["raw_description"]: row for row in body["preview"]}
    assert Decimal(preview["CAFE LUNA"]["amount_native"]) == Decimal("-4.50")
    assert Decimal(preview["RENT JANUARY"]["amount_native"]) == Decimal("-1250.00")

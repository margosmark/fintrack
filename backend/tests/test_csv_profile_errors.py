"""M111 — profile payload shape + the error the user gets when it is wrong.

Two claims, one per side of the wire:

* The import wizard's payload may never carry an empty-string column mapping
  (frontend), and when such a payload arrives anyway the answer is an
  ACTIONABLE line, not a truncate of pydantic's own record — Mark's live prod
  report read ``Invalid profile: 1 validation error for CsvProfile …
  columns.credit must be a non-empty header name [input_value={'columns': …]``.
* A blank OPTIONAL mapping means "unmapped" (the UI's "None"), so it
  normalises to an absent key instead of failing every later upload.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.importers.base import AccountView
from app.importers.csv_profile import CsvImporter, CsvProfile, CsvProfileError

FIXTURES = Path(__file__).parent / "fixtures"

EMAIL = "m111@example.com"
PASSWORD = "correct-horse-battery"

# Mark's exact shape: single-amount mode selected, split keys left behind from
# an earlier trip through Debit & Credit (one of them populated, one blank).
WIZARD_STALE_SIGNED_PROFILE = {
    "columns": {
        "date": "Transaction Date",
        "raw_description": "Description",
        "debit": "Withdrawal",  # stale split value the signed UI never shows
        "credit": "",  # stale AND blank — the key that failed validation
        "amount_native": "Amount",  # the single-amount column the user picked
    },
    "date_format": "%Y-%m-%d",
}

FRIENDLY_CREDIT_LINE = (
    "Invalid profile: credit column mapping is empty — "
    "set the Credit column or switch to single-amount mode"
)

SPLIT_PROFILE = {
    "columns": {
        "date": "Date",
        "raw_description": "Description",
        "debit": "Withdrawal",
        "credit": "Deposit",
    },
}


def _register(client: TestClient) -> dict:
    client.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_account_without_profile(client: TestClient, token: str) -> int:
    inst = client.post("/api/v1/institutions", json={"name": "M111 Bank"}, headers=_headers(token))
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


def test_stale_blank_credit_in_profile_json_is_a_friendly_422(client: TestClient) -> None:
    """M111 regression: no pydantic truncate reaches the user (named after it)."""
    auth = _register(client)
    token = auth["access_token"]
    account_id = _make_account_without_profile(client, token)
    raw = (FIXTURES / "csv_credit_debit.csv").read_bytes()

    response = client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data={
            "account_id": str(account_id),
            "profile_json": json.dumps(WIZARD_STALE_SIGNED_PROFILE),
        },
        files={"file": ("csv_credit_debit.csv", raw, "application/octet-stream")},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_profile"
    detail = body["detail"]
    # The internals that made the report unreadable are gone …
    assert "validation error for CsvProfile" not in detail
    assert "input_value" not in detail
    # … replaced by the line that says what to change.
    assert detail == FRIENDLY_CREDIT_LINE


def test_stale_blank_credit_from_a_stored_profile_is_friendly_too() -> None:
    """The parser path (`invalid importer_profile: …`) gets the same rewrite."""
    view = AccountView(currency="CAD", importer_profile=WIZARD_STALE_SIGNED_PROFILE)
    try:
        CsvImporter.parse(b"Transaction Date,Description,Withdrawal,Amount\n", view)
        raise AssertionError("expected CsvProfileError")
    except CsvProfileError as exc:
        assert str(exc) == (
            "invalid importer_profile: credit column mapping is empty — "
            "set the Credit column or switch to single-amount mode"
        )


def test_split_profile_with_blank_optional_columns_is_accepted() -> None:
    """docs/03: an unmapped optional column is OMITTED — a blank value means
    the same thing (the UI's "None"), so it normalises instead of 422ing."""
    profile = CsvProfile.model_validate(
        {
            "columns": {
                **SPLIT_PROFILE["columns"],
                "posting_date": "",
                "currency": "   ",
            }
        }
    )
    assert set(profile.columns) == {"date", "raw_description", "debit", "credit"}

    # Populated optionals are untouched.
    kept = CsvProfile.model_validate(
        {
            "columns": {
                **SPLIT_PROFILE["columns"],
                "posting_date": "Posted",
                "currency": "Cur",
            }
        }
    )
    assert kept.columns["posting_date"] == "Posted"
    assert kept.columns["currency"] == "Cur"


def test_blank_required_mapping_is_still_rejected_not_normalised_away() -> None:
    """Tolerance is for optionals only — a blank required column is a real gap."""
    try:
        CsvProfile.model_validate(
            {"columns": {"date": "Date", "raw_description": "", "amount_native": "Amount"}}
        )
    except Exception as exc:  # pydantic.ValidationError
        assert "columns.raw_description must be a non-empty header name" in str(exc)
    else:
        raise AssertionError("expected a blank raw_description to be rejected")

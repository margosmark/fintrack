"""Profile JSON v2 flexible columns (w10-contract BackendFlexColumns).

Covers optional posting_date/currency mappings (both modes), configurable
split-mode signs, and backward compatibility of v1 profiles.
"""

from decimal import Decimal

from fastapi.testclient import TestClient

from app.importers.base import AccountView
from app.importers.csv_profile import CsvImporter, CsvProfileError

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
}

FLEX_CSV = (
    b"Date,Posted,Currency,Description,Withdrawal,Deposit\n"
    b"01/02/2026,02/02/2026,USD,GROCERY MART,1,\n"
    b"05/02/2026,,CAD,SALARY ACME,,100\n"
    b"07/02/2026,09/02/2026,,COFFEE BAR,2.5,\n"
)

SIGNED_CSV = b'Date,Description,Amount\n2026-02-01,"RENT",50\n'


def _register(client: TestClient) -> dict:
    client.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _parse(profile: dict, raw: bytes = FLEX_CSV) -> list:
    account = AccountView(currency="CAD", importer_profile=profile)
    return CsvImporter.parse(raw, account)


def _upload(client: TestClient, token: str, account_id: int, profile: dict, raw: bytes) -> dict:
    return client.post(  # type: ignore[return-value]
        "/api/v1/imports/upload",
        headers=_headers(token),
        data={"account_id": str(account_id)},
        files={"file": ("flex.csv", raw, "application/octet-stream")},
    ).json()


def _account_with_profile(client: TestClient, token: str, profile: dict) -> int:
    inst = client.post(
        "/api/v1/institutions",
        json={"name": "Flex Bank", "importer_profile": profile},
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


def test_flipped_signs_invert_split_amounts() -> None:
    profile = {**SPLIT_PROFILE, "signs": {"debit": 1, "credit": -1}}
    rows = _parse(profile)
    amounts = {r.raw_description: r.amount_native for r in rows}
    # Debit normally -1 now +1; credit normally +100 now -100.
    assert amounts["GROCERY MART"] == Decimal("1")
    assert amounts["SALARY ACME"] == Decimal("-100")
    assert amounts["COFFEE BAR"] == Decimal("2.5")


def test_partial_signs_merge_with_defaults() -> None:
    profile = {**SPLIT_PROFILE, "signs": {"debit": 1}}
    rows = _parse(profile)
    amounts = {r.raw_description: r.amount_native for r in rows}
    assert amounts["GROCERY MART"] == Decimal("1")  # flipped
    assert amounts["SALARY ACME"] == Decimal("100")  # default credit sign kept


def test_default_signs_preserve_v1_behavior() -> None:
    rows = _parse(dict(SPLIT_PROFILE))
    amounts = {r.raw_description: r.amount_native for r in rows}
    assert amounts["GROCERY MART"] == Decimal("-1")
    assert amounts["SALARY ACME"] == Decimal("100")


def test_invalid_sign_values_are_rejected_naming_the_key() -> None:
    for bad in (0, 2, "x"):
        profile = {**SPLIT_PROFILE, "signs": {"debit": bad}}
        try:
            _parse(profile)
        except CsvProfileError as exc:
            assert "debit" in str(exc)
        else:
            raise AssertionError(f"expected rejection for signs.debit={bad!r}")


def test_unknown_sign_key_is_rejected_naming_the_key() -> None:
    profile = {**SPLIT_PROFILE, "signs": {"withdrawal": -1}}
    try:
        _parse(profile)
    except CsvProfileError as exc:
        assert "withdrawal" in str(exc)
    else:
        raise AssertionError("expected rejection for unknown signs key")


def test_signs_are_ignored_in_signed_mode() -> None:
    profile = {
        "columns": {
            "date": "Date",
            "raw_description": "Description",
            "amount_native": "Amount",
        },
        "credits_positive": False,
        "signs": {"debit": -1, "credit": 1},
    }
    rows = _parse(profile, SIGNED_CSV)
    assert rows[0].amount_native == Decimal("-50")


def test_posting_date_mapped_blank_and_missing_cells() -> None:
    profile = {**SPLIT_PROFILE, "columns": {**SPLIT_PROFILE["columns"], "posting_date": "Posted"}}
    rows = _parse(profile)
    posting = {r.raw_description: r.posting_date for r in rows}
    assert str(posting["GROCERY MART"]) == "2026-02-02"
    assert posting["SALARY ACME"] is None  # blank cell -> NULL posting_date
    assert str(posting["COFFEE BAR"]) == "2026-02-09"


def test_unmapped_posting_date_is_none() -> None:
    rows = _parse(dict(SPLIT_PROFILE))
    assert all(r.posting_date is None for r in rows)


def test_currency_mapping_gives_per_row_currencies_with_fallback() -> None:
    profile = {**SPLIT_PROFILE, "columns": {**SPLIT_PROFILE["columns"], "currency": "Currency"}}
    rows = _parse(profile)
    currencies = {r.raw_description: r.currency for r in rows}
    assert currencies["GROCERY MART"] == "USD"
    assert currencies["SALARY ACME"] == "CAD"
    # Blank/invalid token falls back to account currency (row NOT dropped).
    assert currencies["COFFEE BAR"] is None


def test_invalid_currency_token_falls_back_to_account() -> None:
    profile = {**SPLIT_PROFILE, "columns": {**SPLIT_PROFILE["columns"], "currency": "Currency"}}
    csv_text = b"Date,Currency,Description,Withdrawal,Deposit\n01/02/2026,EURO,WIRE OUT,10,\n"
    rows = _parse(profile, csv_text)
    assert len(rows) == 1
    assert rows[0].currency is None


def test_invalid_sign_profile_creation_is_422_and_parser_names_the_key(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    bad_signs = {**SPLIT_PROFILE, "signs": {"debit": 2}}
    response = client.post(
        "/api/v1/institutions",
        json={"name": "Flex Bank", "importer_profile": bad_signs},
        headers=_headers(token),
    )
    # Institution creation validates the profile up front (generic envelope;
    # the named-key detail is surfaced by the parser path below).
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"

    view = AccountView(currency="CAD", importer_profile=bad_signs)
    try:
        CsvImporter.parse(FLEX_CSV, view)
        raise AssertionError("expected CsvProfileError")
    except CsvProfileError as exc:
        assert "debit" in str(exc)


def test_flex_profile_upload_end_to_end(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    profile = {
        **SPLIT_PROFILE,
        "columns": {
            **SPLIT_PROFILE["columns"],
            "posting_date": "Posted",
            "currency": "Currency",
        },
        "signs": {"debit": 1, "credit": -1},
    }
    account_id = _account_with_profile(client, token, profile)
    body = _upload(client, token, account_id, profile, FLEX_CSV)
    assert body["imported"] == 3
    preview = {row["raw_description"]: row for row in body["preview"]}
    assert Decimal(preview["GROCERY MART"]["amount_native"]) == Decimal("1")
    assert Decimal(preview["SALARY ACME"]["amount_native"]) == Decimal("-100")

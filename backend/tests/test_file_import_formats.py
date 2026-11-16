"""One synthetic fixture per format, and a round trip for each (docs/58 AC).

Formats covered: YNAB (budget export), Quicken (QIF register), Monarch
(transaction export), ProjectionLab (net-worth JSON), generic CSV
(decimal-comma spreadsheet) and a balance spreadsheet. "Round trip" means:
fixture → import → committed rows/accounts for this tenant → import the same
file again → zero net-new, i.e. the merge/dedup path held.

No real personal data: every fixture in ``tests/fixtures`` is synthetic.
"""

import json
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models.accounts import Account
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transactions import Transaction

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL = "format-roundtrip@example.com"
PASSWORD = "correct-horse-battery"

PROFILE_KEYS = ("columns", "date_format", "decimal_comma", "credits_positive", "headerless")

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


def _register(client: TestClient) -> str:
    client.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _user_id(session: Session) -> int:
    from app.models.users import User

    user = session.exec(select(User).where(User.email == EMAIL)).first()
    assert user is not None
    return user.id


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


def _make_account(client: TestClient, token: str, name: str = "Roundtrip Checking") -> int:
    account = client.post(
        "/api/v1/accounts",
        json={"name": name, "type": "chequing", "currency": "CAD"},
        headers=_headers(token),
    )
    assert account.status_code == 201, account.text
    return account.json()["id"]


def _upload(
    client: TestClient,
    token: str,
    account_id: int,
    fixture: str,
    profile: dict | None = None,
    content_type: str = "application/octet-stream",
) -> dict:
    raw = (FIXTURES / fixture).read_bytes()
    data = {"account_id": str(account_id)}
    if profile is not None:
        data["profile_json"] = json.dumps(profile)
    response = client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data=data,
        files={"file": (fixture, raw, content_type)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _commit(client: TestClient, token: str, batch_id: int) -> dict:
    response = client.post(f"/api/v1/imports/{batch_id}/commit", headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def _networth(
    client: TestClient,
    token: str,
    fixture: str,
    action: str,
    content_type: str = "application/json",
    form: dict | None = None,
) -> dict:
    raw = (FIXTURES / fixture).read_bytes()
    response = client.post(
        f"/api/v1/imports/networth/{action}",
        headers=_headers(token),
        data={key: str(value) for key, value in (form or {}).items()},
        files={"file": (fixture, raw, content_type)},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ── YNAB ────────────────────────────────────────────────────────────────────


def test_ynab_budget_round_trip(client: TestClient, session: Session) -> None:
    token = _register(client)
    user_id = _user_id(session)

    response = client.post(
        "/api/v1/imports/ynab/preview",
        headers=_headers(token),
        files={
            "file": (
                "ynab_budget.json",
                (FIXTURES / "ynab_budget.json").read_bytes(),
                "application/json",
            )
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["transactions"]["importable"] == 10

    commit = client.post(
        "/api/v1/imports/ynab/commit",
        headers=_headers(token),
        files={
            "file": (
                "ynab_budget.json",
                (FIXTURES / "ynab_budget.json").read_bytes(),
                "application/json",
            )
        },
    )
    assert commit.status_code in (200, 201), commit.text  # 201: the YNAB commit shape
    result = commit.json()
    assert result["net_new"] == 10
    # The YNAB commit CREATES accounts/categories/tags but its batches are
    # only STAGED (docs/03 / docs/38): a report sees nothing until each batch
    # is committed through the regular /imports/{batch_id}/commit route.
    assert _live_transactions(session, user_id) == 0  # still staged
    for batch in result["batches"]:
        staged = client.post(f"/api/v1/imports/{batch['batch_id']}/commit", headers=_headers(token))
        assert staged.status_code == 200, staged.text
    assert _live_transactions(session, user_id) == 10

    # Second import of the same budget: everything is a duplicate.
    again = client.post(
        "/api/v1/imports/ynab/commit",
        headers=_headers(token),
        files={
            "file": (
                "ynab_budget.json",
                (FIXTURES / "ynab_budget.json").read_bytes(),
                "application/json",
            )
        },
    )
    assert again.status_code in (200, 201), again.text
    again_result = again.json()
    assert again_result["net_new"] == 0
    assert again_result["duplicates_skipped"] == 10
    assert again_result["plan"]["transactions"]["duplicates"] == 10
    for batch in again_result["batches"]:
        staged = client.post(f"/api/v1/imports/{batch['batch_id']}/commit", headers=_headers(token))
        assert staged.status_code == 200, staged.text
    # The round trip held: re-importing added no live row.
    assert _live_transactions(session, user_id) == 10


# ── Quicken ─────────────────────────────────────────────────────────────────


def test_quicken_qif_round_trip(client: TestClient, session: Session) -> None:
    token = _register(client)
    user_id = _user_id(session)
    account_id = _make_account(client, token)

    first = _upload(client, token, account_id, "sample.qif", content_type="text/plain")
    assert first["imported"] == 3
    assert first["duplicates_skipped"] == 0
    _commit(client, token, first["id"])
    assert _live_transactions(session, user_id) == 3

    second = _upload(client, token, account_id, "sample.qif", content_type="text/plain")
    result = _commit(client, token, second["id"])
    assert result["imported"] == 0
    assert result["duplicates_skipped"] == 3
    assert _live_transactions(session, user_id) == 3, "re-import must not duplicate rows"


# ── Monarch ─────────────────────────────────────────────────────────────────


def test_monarch_round_trip(client: TestClient, session: Session) -> None:
    token = _register(client)
    user_id = _user_id(session)
    account_id = _make_account(client, token)

    suggestion = client.post(
        "/api/v1/imports/suggest-profile",
        headers=_headers(token),
        files={
            "file": (
                "monarch_transactions.csv",
                (FIXTURES / "monarch_transactions.csv").read_bytes(),
                "text/csv",
            )
        },
    )
    assert suggestion.status_code == 200, suggestion.text
    body = suggestion.json()
    assert body["format"] == "monarch"
    profile = {key: body[key] for key in PROFILE_KEYS if key in body}

    first = _upload(client, token, account_id, "monarch_transactions.csv", profile, "text/csv")
    assert first["imported"] == 5
    _commit(client, token, first["id"])

    rows = session.exec(
        select(Transaction).where(
            Transaction.user_id == user_id, Transaction.account_id == account_id
        )
    ).all()
    by_payee = {row.raw_description: row.amount_native for row in rows}
    assert by_payee == {
        "Grocery Mart": Decimal("-58.12"),
        "Paycheck Corp": Decimal("2500.00"),
        "Coffee Hut": Decimal("-4.50"),  # accounting parentheses, docs/58 §A
        "Electric Co": Decimal("-120.00"),
        "Bookstore": Decimal("-25.99"),
    }
    assert _live_transactions(session, user_id) == 5

    second = _upload(client, token, account_id, "monarch_transactions.csv", profile, "text/csv")
    result = _commit(client, token, second["id"])
    assert result["imported"] == 0
    assert result["duplicates_skipped"] == 5
    assert _live_transactions(session, user_id) == 5


# ── ProjectionLab ───────────────────────────────────────────────────────────


def test_projectionlab_round_trip(client: TestClient, session: Session) -> None:
    token = _register(client)
    user_id = _user_id(session)

    plan = _networth(client, token, "projectionlab_export.json", "preview")
    counts = plan["counts"]
    assert counts["accounts_create"] == 10  # 3 savings + 5 investments + 2 debts
    assert counts["accounts_reuse"] == 0
    assert counts["assets_skipped"] == 2
    assert counts["progress_snapshots_skipped"] == 2
    skipped_kinds = sorted(item["kind"] for item in plan["skipped"])
    assert skipped_kinds == ["asset", "asset", "plan_data", "progress_history"]
    assert plan["can_commit"] is True
    assert not session.exec(
        select(Account).where(Account.user_id == user_id, Account.name == "Family Home")
    ).first(), "a preview must not create anything, least of all an asset"

    result = _networth(client, token, "projectionlab_export.json", "commit")
    assert result["created"] == 10
    assert result["reused"] == 0

    created = {
        account.name: account
        for account in session.exec(select(Account).where(Account.user_id == user_id)).all()
    }
    assert len(created) == 10
    assert "Family Home" not in created  # assets are never written
    assert "Old Sedan" not in created
    assert created["Old 401k"].type == "rrsp"
    assert created["Old 401k"].taxable is False
    assert created["Taxable Brokerage"].type == "brokerage_non_registered"
    assert created["Taxable Brokerage"].taxable is True  # server-derived (docs/14 C1)
    assert created["Roth Account"].type == "tfsa"
    assert created["Student Loan"].type == "line_of_credit"
    assert created["Student Loan"].starting_balance == Decimal("-18000")
    assert created["Master Card"].type == "credit_card"
    assert created["Vacation Fund"].starting_balance == Decimal("850")  # from initialValue
    health = next(a for a in plan["accounts"] if a["name"] == "Health Savings")
    assert any("no Canadian equivalent" in note for note in health["notes"])

    # Round trip: the same file again creates nothing and touches nothing.
    again = _networth(client, token, "projectionlab_export.json", "commit")
    assert again["created"] == 0
    assert again["reused"] == 10
    total = session.exec(select(Account).where(Account.user_id == user_id)).all()
    assert len(total) == 10
    assert total[0].starting_balance is not None


def test_networth_unsupported_json_is_a_named_422(client: TestClient) -> None:
    token = _register(client)
    response = client.post(
        "/api/v1/imports/networth/preview",
        headers=_headers(token),
        files={"file": ("whatever.json", b'{"hello": 1}', "application/json")},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "unsupported_networth_format"


def _networth_bytes(client: TestClient, token: str, action: str, raw: bytes) -> dict:
    """POST inline (non-fixture) bytes at a net-worth route; status not assumed."""
    response = client.post(
        f"/api/v1/imports/networth/{action}",
        headers=_headers(token),
        data={"currency": "CAD"},
        files={"file": ("inline_balance.csv", raw, "text/csv")},
    )
    assert response.headers["content-type"].startswith("application/json")
    return {"status": response.status_code, **response.json()}


# ── per-row currency rule (docs/58 §B, security S-1) ──────────────────────


def test_networth_junk_row_currency_is_a_named_422_and_writes_nothing(
    client: TestClient, session: Session
) -> None:
    """A row's own `currency` cell obeys the same 3-letter rule as the form.

    The file is refused WHOLE — not even the row whose currency was valid may
    be written — on both the preview and the commit path.
    """
    token = _register(client)
    user_id = _user_id(session)
    raw = (
        b"Account,Type,Balance,Date,Currency\n"
        b"Everyday Chequing,checking,2411.27,2026-02-01,CAD\n"
        b"Junk Fund,savings,100.00,2026-02-01,CANADIAN DOLLARS\n"
    )

    preview = _networth_bytes(client, token, "preview", raw)
    assert preview["status"] == 422, preview
    assert preview["code"] == "networth_row_invalid"

    commit = _networth_bytes(client, token, "commit", raw)
    assert commit["status"] == 422, commit
    assert commit["code"] == "networth_row_invalid"

    written = session.exec(select(Account).where(Account.user_id == user_id)).all()
    assert written == [], "a refused file must leave zero accounts behind"


def test_networth_row_currency_is_normalized_and_falls_back_to_the_form_value(
    client: TestClient, session: Session
) -> None:
    """Valid cells: stripped/upper-cased (` usd ` → USD); empty → form default."""
    token = _register(client)
    user_id = _user_id(session)
    raw = (
        b"Account,Type,Balance,Date,Currency\n"
        b"US Brokerage,brokerage,100.00,2026-02-01, usd \n"
        b"Everyday Chequing,checking,2411.27,2026-02-01,\n"
    )

    plan = _networth_bytes(client, token, "preview", raw)
    assert plan["status"] == 200, plan
    by_name = {account["name"]: account for account in plan["accounts"]}
    assert by_name["US Brokerage"]["currency"] == "USD"
    assert by_name["Everyday Chequing"]["currency"] == "CAD"  # form default

    result = _networth_bytes(client, token, "commit", raw)
    assert result["status"] == 200, result
    assert result["created"] == 2
    accounts = {
        account.name: account
        for account in session.exec(select(Account).where(Account.user_id == user_id)).all()
    }
    assert accounts["US Brokerage"].currency == "USD"
    assert accounts["Everyday Chequing"].currency == "CAD"


# ── generic CSV ─────────────────────────────────────────────────────────────


def test_generic_csv_round_trip(client: TestClient, session: Session) -> None:
    token = _register(client)
    user_id = _user_id(session)
    account_id = _make_account(client, token)

    first = _upload(client, token, account_id, "csv_decimal_comma.csv", DOCS03_PROFILE, "text/csv")
    assert first["imported"] == 4
    _commit(client, token, first["id"])

    rows = session.exec(
        select(Transaction).where(
            Transaction.user_id == user_id, Transaction.account_id == account_id
        )
    ).all()
    by_description = {row.raw_description: row.amount_native for row in rows}
    assert by_description == {
        "CAFE LUNA": Decimal("-4.50"),
        "RENT JANUARY": Decimal("-1250.00"),
        "REFUND SHOE SHOP": Decimal("23.99"),
        "GROCERY MART #12": Decimal("-67.80"),
    }
    assert _live_transactions(session, user_id) == 4

    second = _upload(client, token, account_id, "csv_decimal_comma.csv", DOCS03_PROFILE, "text/csv")
    result = _commit(client, token, second["id"])
    assert result["imported"] == 0
    assert result["duplicates_skipped"] == 4
    assert _live_transactions(session, user_id) == 4


# ── balance spreadsheet ─────────────────────────────────────────────────────


def test_networth_spreadsheet_round_trip(client: TestClient, session: Session) -> None:
    token = _register(client)
    user_id = _user_id(session)

    plan = _networth(
        client,
        token,
        "networth_spreadsheet.csv",
        "preview",
        content_type="text/csv",
    )
    assert plan["source_format"] == "balance_csv"
    assert plan["counts"]["accounts_create"] == 5  # 6 rows, Vacation Fund collapses
    assert plan["counts"]["balance_rows_collapsed"] == 1
    by_name = {account["name"]: account for account in plan["accounts"]}
    assert by_name["Master Card"]["mapped_type"] == "credit_card"
    assert by_name["Master Card"]["file_balance"] == "-1500.00"  # accounting parentheses
    assert by_name["Vacation Fund"]["file_balance"] == "850.00"  # latest dated row
    assert any("older balance" in note for note in by_name["Vacation Fund"]["notes"])
    assert any("not recognised" in note for note in by_name["Weird Fund"]["notes"])

    result = _networth(client, token, "networth_spreadsheet.csv", "commit", content_type="text/csv")
    assert result["created"] == 5
    assert result["reused"] == 0
    accounts = session.exec(select(Account).where(Account.user_id == user_id)).all()
    assert len(accounts) == 5

    again = _networth(client, token, "networth_spreadsheet.csv", "commit", content_type="text/csv")
    assert again["created"] == 0
    assert again["reused"] == 5
    accounts = session.exec(select(Account).where(Account.user_id == user_id)).all()
    assert len(accounts) == 5


def test_networth_existing_account_is_reused_never_overwritten(
    client: TestClient, session: Session
) -> None:
    """Merge review: the file's balance is reported, the account's is kept."""
    token = _register(client)
    user_id = _user_id(session)
    account = client.post(
        "/api/v1/accounts",
        json={
            "name": "Rainy Day",
            "type": "savings",
            "currency": "CAD",
            "starting_balance": "999.99",
        },
        headers=_headers(token),
    )
    assert account.status_code == 201, account.text
    account_id = account.json()["id"]

    plan = _networth(client, token, "networth_spreadsheet.csv", "preview", content_type="text/csv")
    rainy = next(a for a in plan["accounts"] if a["name"] == "Rainy Day")
    assert rainy["action"] == "reuse"
    assert rainy["existing_id"] == account_id
    assert rainy["file_balance"] == "12000.00"
    assert rainy["current_balance"] == "999.99"
    assert any("kept" in note for note in rainy["notes"])

    result = _networth(client, token, "networth_spreadsheet.csv", "commit", content_type="text/csv")
    assert result["created"] == 4
    assert result["reused"] == 1

    kept = session.get(Account, account_id)
    assert kept is not None
    assert kept.starting_balance == Decimal("999.99"), (
        "a file must never overwrite an existing balance"
    )
    assert kept.user_id == user_id

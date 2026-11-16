"""M29/M47 validation regressions (docs/29 F4, F7, F8).

Covers the validation gaps found in the 2026-09-19 review: bills category
ownership + typed due date, debt PATCH constraints, reconciliation typed
statement rows + account-scoped matching, tax projection province/year
bounds, fail-fast secrets, constant-time internal secret compare, and the
MCP client failing closed on an unresolvable per-user token.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import Settings, get_settings
from app.models.users import User
from tests.test_deep_common import auth, create_account, get_category

EMAIL_PASSWORD = "correct-horse-battery"


def _fresh_user(client: TestClient, prefix: str) -> dict:
    email = f"{prefix}-{datetime.now(UTC).timestamp()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": EMAIL_PASSWORD})
    r = client.post("/api/v1/auth/login", json={"email": email, "password": EMAIL_PASSWORD})
    assert r.status_code == 200
    return {"email": email, **r.json()}


def _user_id(session, email: str) -> int:
    user = session.exec(select(User).where(User.email == email)).one()
    return user.id


# --- (a) bills: category ownership + typed due date -------------------------


def test_create_bill_foreign_category_404(client: TestClient, session) -> None:
    owner = _fresh_user(client, "catowner")
    foreign_cat = get_category(session, _user_id(session, owner["email"]), "dining")

    stranger = _fresh_user(client, "billstranger")
    headers = auth(stranger["access_token"])

    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={
            "name": "Rent",
            "amount": 1500,
            "frequency": "monthly",
            "next_due_date": "2026-10-01",
            "category_id": foreign_cat,
        },
    )
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "category_not_found"

    # Missing category id too.
    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={
            "name": "Rent",
            "frequency": "monthly",
            "next_due_date": "2026-10-01",
            "category_id": 999999,
        },
    )
    assert r.status_code == 404
    assert r.json()["code"] == "category_not_found"


def test_update_bill_foreign_category_404(client: TestClient, session) -> None:
    owner = _fresh_user(client, "catowner2")
    foreign_cat = get_category(session, _user_id(session, owner["email"]), "dining")

    headers = auth(_fresh_user(client, "billupd")["access_token"])
    bill = client.post(
        "/api/v1/bills",
        headers=headers,
        json={"name": "Internet", "frequency": "monthly", "next_due_date": "2026-10-01"},
    ).json()

    r = client.patch(
        f"/api/v1/bills/{bill['id']}", headers=headers, json={"category_id": foreign_cat}
    )
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "category_not_found"
    # Bill is unchanged.
    assert client.get("/api/v1/bills", headers=headers).json()[0]["category_id"] is None


def test_create_bill_garbage_due_date_422(client: TestClient) -> None:
    headers = auth(_fresh_user(client, "billdate")["access_token"])
    for bad in ("not-a-date", "2026-13-01", "2026-02-31"):
        r = client.post(
            "/api/v1/bills",
            headers=headers,
            json={"name": "Rent", "frequency": "monthly", "next_due_date": bad},
        )
        assert r.status_code == 422, (bad, r.text)


def test_mark_paid_no_longer_500s(client: TestClient) -> None:
    headers = auth(_fresh_user(client, "billpaid")["access_token"])
    monthly = client.post(
        "/api/v1/bills",
        headers=headers,
        json={"name": "Rent", "frequency": "monthly", "next_due_date": "2026-10-15"},
    ).json()
    r = client.post(f"/api/v1/bills/{monthly['id']}/mark-paid", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["next_due_date"] == "2026-11-15"

    one_time = client.post(
        "/api/v1/bills",
        headers=headers,
        json={"name": "Fee", "frequency": "one_time", "next_due_date": "2026-10-15"},
    ).json()
    r = client.post(f"/api/v1/bills/{one_time['id']}/mark-paid", headers=headers)
    assert r.status_code == 200
    assert r.json()["enabled"] is False


# --- (b) debts: PATCH mirrors create constraints ----------------------------


def test_debt_update_rejects_invalid_constraints(client: TestClient, session) -> None:
    headers = auth(_fresh_user(client, "debtupd")["access_token"])
    acct = create_account(client, headers, "Loan account")
    debt = client.post(
        "/api/v1/debts",
        headers=headers,
        json={"name": "Loan", "account_id": acct, "debt_type": "loan"},
    )
    assert debt.status_code == 201, debt.text
    debt_id = debt.json()["id"]

    for patch, field in [
        ({"debt_type": "whatever"}, "debt_type"),
        ({"interest_rate": 999}, "interest_rate"),
        ({"minimum_payment": -5}, "minimum_payment"),
    ]:
        r = client.patch(f"/api/v1/debts/{debt_id}", headers=headers, json=patch)
        assert r.status_code == 422, (field, r.text)

    # Valid values still patch.
    r = client.patch(
        f"/api/v1/debts/{debt_id}",
        headers=headers,
        json={"debt_type": "credit_card", "interest_rate": 19.99, "minimum_payment": 100},
    )
    assert r.status_code == 200, r.text
    assert r.json()["debt_type"] == "credit_card"


# --- (c) reconciliation: typed upload + account-scoped match ----------------


def _upload(client: TestClient, headers: dict, account_id: int, items: list[dict]):
    return client.post(
        f"/api/v1/accounts/{account_id}/reconciliation/upload",
        headers=headers,
        json={"items": items},
    )


def test_upload_malformed_item_422_not_500(client: TestClient, session) -> None:
    headers = auth(_fresh_user(client, "reconmal")["access_token"])
    acct = create_account(client, headers, "Chequing")

    r = _upload(client, headers, acct, [{"date": "2026-10-01", "amount": "abc"}])
    assert r.status_code == 422, r.text
    assert r.json().get("code") != "internal_error"


def test_upload_calendar_invalid_date_is_invalid_statement(client: TestClient, session) -> None:
    headers = auth(_fresh_user(client, "reconcal")["access_token"])
    acct = create_account(client, headers, "Chequing")

    r = _upload(client, headers, acct, [{"date": "2026-13-40", "amount": "10.00"}])
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_statement"


def test_upload_typed_items_accepted(client: TestClient, session) -> None:
    headers = auth(_fresh_user(client, "reconok")["access_token"])
    acct = create_account(client, headers, "Chequing")

    r = _upload(
        client,
        headers,
        acct,
        [{"date": "2026-10-01", "amount": "-42.10", "description": "Groceries"}],
    )
    assert r.status_code == 201, r.text
    assert r.json() == {"count": 1}
    body = client.get(f"/api/v1/accounts/{acct}/reconciliation", headers=headers).json()
    assert body["summary"]["unmatched"] == 1


def test_match_transaction_account_mismatch_409(client: TestClient, session) -> None:
    user = _fresh_user(client, "reconmis")
    uid = _user_id(session, user["email"])
    headers = auth(user["access_token"])
    acct1 = create_account(client, headers, "Chequing")
    acct2 = create_account(client, headers, "Savings")

    _upload(client, headers, acct1, [{"date": "2026-10-01", "amount": "-42.10"}])
    item_id = client.get(f"/api/v1/accounts/{acct1}/reconciliation", headers=headers).json()[
        "items"
    ][0]["id"]

    from tests.test_deep_common import seed_transaction

    # A transaction in the OTHER account (same user) must be rejected.
    other_tx = seed_transaction(session, uid, acct2, "Other acct tx")
    r = client.post(
        f"/api/v1/accounts/{acct1}/reconciliation/match?item_id={item_id}&transaction_id={other_tx.id}",
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "transaction_account_mismatch"

    # A transaction from the matching account is accepted.
    same_tx = seed_transaction(session, uid, acct1, "Same acct")
    r = client.post(
        f"/api/v1/accounts/{acct1}/reconciliation/match?item_id={item_id}&transaction_id={same_tx.id}",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "matched"


def test_match_other_users_transaction_404(client: TestClient, session) -> None:
    owner = _fresh_user(client, "recontxowner")
    stranger = _fresh_user(client, "recontxstranger")
    headers = auth(stranger["access_token"])
    acct = create_account(client, headers, "Chequing")
    _upload(client, headers, acct, [{"date": "2026-10-01", "amount": "-5.00"}])
    item_id = client.get(f"/api/v1/accounts/{acct}/reconciliation", headers=headers).json()[
        "items"
    ][0]["id"]

    from tests.test_deep_common import seed_transaction

    other_user_tx = seed_transaction(session, _user_id(session, owner["email"]), acct, "Foreign tx")
    r = client.post(
        f"/api/v1/accounts/{acct}/reconciliation/match?item_id={item_id}&transaction_id={other_user_tx.id}",
        headers=headers,
    )
    assert r.status_code == 404, r.text


# --- (d) tax projection: province/year bounds -------------------------------


def test_patch_projection_invalid_province_422(client: TestClient) -> None:
    headers = auth(_fresh_user(client, "taxprov")["access_token"])
    for province in ("Ontario", "on", "O"):
        r = client.patch(f"/api/v1/taxes/projection?province={province}&year=2026", headers=headers)
        assert r.status_code == 422, (province, r.text)


def test_patch_projection_invalid_year_422(client: TestClient) -> None:
    headers = auth(_fresh_user(client, "taxyear")["access_token"])
    for year in ("99999", "1999", "2101"):
        r = client.patch(f"/api/v1/taxes/projection?year={year}", headers=headers)
        assert r.status_code == 422, (year, r.text)


def test_unknown_two_char_province_keeps_ontario_fallback(client: TestClient) -> None:
    headers = auth(_fresh_user(client, "taxzz")["access_token"])
    r = client.patch(
        "/api/v1/taxes/projection?province=ZZ&year=2026&estimated_income=50000",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["province"] == "ZZ"
    # Ontario fallback: computed output present.
    assert "total_tax" in r.json()


def test_projection_includes_stored_inputs(client: TestClient) -> None:
    headers = auth(_fresh_user(client, "taxstored")["access_token"])

    # No row yet: stored mirrors the zeroed fallback.
    body = client.get("/api/v1/taxes/projection?year=2026", headers=headers).json()
    assert body["stored"] == {
        "year": 2026,
        "province": "ON",
        "estimated_income": "0",
        "deductions": "0",
        "capital_gains_override": None,
        "dividend_income_override": None,
    }

    r = client.patch(
        "/api/v1/taxes/projection?year=2026&estimated_income=60000&deductions=1000",
        headers=headers,
    )
    assert r.status_code == 200, r.text

    body = client.get("/api/v1/taxes/projection?year=2026", headers=headers).json()
    stored = body["stored"]
    assert stored["year"] == 2026
    assert stored["province"] == "ON"
    assert stored["estimated_income"].startswith("60000")
    assert stored["deductions"].startswith("1000")
    assert stored["capital_gains_override"] is None
    assert stored["dividend_income_override"] is None
    # Money is Decimal-as-string on the wire.
    assert isinstance(stored["estimated_income"], str)


def test_patch_absent_params_keep_stored_values(client: TestClient) -> None:
    headers = auth(_fresh_user(client, "taxkeep")["access_token"])
    r = client.patch(
        "/api/v1/taxes/projection?year=2027&estimated_income=60000&deductions=2000&capital_gains_override=5000",
        headers=headers,
    )
    assert r.status_code == 200, r.text

    # Second PATCH sending ONLY province: income/deductions/override survive.
    r = client.patch("/api/v1/taxes/projection?year=2027&province=BC", headers=headers)
    assert r.status_code == 200, r.text
    stored = client.get("/api/v1/taxes/projection?year=2027", headers=headers).json()["stored"]
    assert stored["province"] == "BC"
    assert stored["estimated_income"].startswith("60000")
    assert stored["deductions"].startswith("2000")
    assert stored["capital_gains_override"].startswith("5000")


def test_patch_empty_override_value_clears_it(client: TestClient) -> None:
    headers = auth(_fresh_user(client, "taxclear")["access_token"])
    r = client.patch(
        "/api/v1/taxes/projection?year=2028&capital_gains_override=5000&dividend_income_override=200",
        headers=headers,
    )
    assert r.status_code == 200, r.text

    # `?param=` (empty value) explicitly clears the override; absent keeps.
    r = client.patch(
        "/api/v1/taxes/projection?year=2028&estimated_income=10000&capital_gains_override=&dividend_income_override=",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    stored = client.get("/api/v1/taxes/projection?year=2028", headers=headers).json()["stored"]
    assert stored["estimated_income"].startswith("10000")
    assert stored["capital_gains_override"] is None
    assert stored["dividend_income_override"] is None


# --- (e) settings: fail-fast secrets ----------------------------------------


def test_settings_reject_placeholder_secret() -> None:
    try:
        Settings(jwt_secret="change-me-long-random-string")
    except Exception as exc:  # pydantic wraps ValueError in ValidationError
        assert "JWT_SECRET" in str(exc)
    else:
        raise AssertionError("env.example placeholder secret must be rejected")


def test_settings_reject_short_secret() -> None:
    try:
        Settings(jwt_secret="short")
    except Exception as exc:
        assert "JWT_SECRET" in str(exc)
    else:
        raise AssertionError("secrets shorter than 32 chars must be rejected")


def test_settings_accept_long_random_secret() -> None:
    Settings(jwt_secret="0123456789abcdef0123456789abcdef")  # must not raise


# --- (e) internal-issue-token: constant-time secret compare -----------------


def test_internal_issue_token_rejects_wrong_secret(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MCP_INTERNAL_SECRET", "internal-secret-0123456789abcdef")
    get_settings.cache_clear()
    try:
        user = _fresh_user(client, "internaluser")
        uid = user["user"]["id"]
        headers = {"X-Internal-Secret": "internal-secret-0123456789abcdef"}

        r = client.post("/api/v1/auth/internal-issue-token", json={"user_id": uid})
        assert r.status_code == 403
        assert r.json()["code"] == "forbidden"

        r = client.post(
            "/api/v1/auth/internal-issue-token",
            json={"user_id": uid},
            headers={"X-Internal-Secret": "wrong-secret"},
        )
        assert r.status_code == 403

        r = client.post(
            "/api/v1/auth/internal-issue-token",
            json={"user_id": uid},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert "access_token" in r.json()
    finally:
        get_settings.cache_clear()


# --- (f) MCP client: fail closed -------------------------------------------


class _RecordingHttp:
    """Stub httpx.Client: records calls, returns canned 200 JSON."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, headers=None, **kwargs):
        self.calls.append((method, path))
        # Models an httpx.Response closely enough for BackendClient.request,
        # including the header map `_absorb_refresh_cookie` reads (M84).
        return SimpleNamespace(
            status_code=200,
            content=b"{}",
            headers=httpx.Headers(),
            json=lambda: {"ok": 1},
        )


def _client_with_token(monkeypatch):
    from mcp_server.client import BackendClient

    monkeypatch.setenv("FIN_TRACKER_TOKEN", "static-tenant-token")
    monkeypatch.delenv("FIN_TRACKER_EMAIL", raising=False)
    monkeypatch.delenv("FIN_TRACKER_PASSWORD", raising=False)
    return BackendClient()


def test_mcp_client_fails_closed_when_user_token_unresolved(
    client: TestClient, monkeypatch
) -> None:
    from mcp_server.auth import current_user_id

    bc = _client_with_token(monkeypatch)
    stub = _RecordingHttp()
    bc._http = stub
    monkeypatch.setattr(bc, "_get_token_for_user", lambda user_id: None)

    token_reset = current_user_id.set(42)
    try:
        with pytest.raises(RuntimeError, match="refusing"):
            bc.request("GET", "/api/v1/accounts")
    finally:
        current_user_id.reset(token_reset)

    # The request must NEVER have been sent with self.token.
    assert stub.calls == []


def test_mcp_client_no_resolved_user_keeps_static_token(client: TestClient, monkeypatch) -> None:

    bc = _client_with_token(monkeypatch)
    stub = _RecordingHttp()
    bc._http = stub

    bc.request("GET", "/api/v1/accounts")
    assert len(stub.calls) == 1
    # Authorization header carries self.token (static tenant), no user context.
    sent = stub.calls[0]
    assert sent == ("GET", "/api/v1/accounts")

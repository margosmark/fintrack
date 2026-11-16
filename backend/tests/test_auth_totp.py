"""M74 TOTP second factor — behavior tests (docs/38).

What this pins:
- The ALGORITHM itself, by RFC 6238's own test vectors (not by our output).
- enroll -> challenge -> login: valid, wrong, expired and REPLAYED codes.
- Recovery codes: single-use (conditional UPDATE), hashed at rest.
- Challenge-token integrity: tampered / expired / wrong purpose / nonexistent
  user all collapse to one 401, and a challenge is never a bearer token.
- Demo tenant: cannot enroll, and is never challenged even if a factor row is
  smuggled into the DB (defense in depth — its password is public).
- Disable paths + the 10/min login-burst limit on /auth/login/totp.

Run: cd backend && uv run pytest tests/test_auth_totp.py -q
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote

from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.core.jwt_keys import encode_jwt
from app.core.ratelimit import limiter
from app.core.security import PASSWORD_CHALLENGE_METHOD, create_challenge_token, hash_password
from app.models.auth_factors import RecoveryCode, TotpFactor
from app.models.users import User
from app.services import totp as totp_service
from app.services.demo_seed import DEMO_EMAIL

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/users/me"
TOTP_STATUS = "/api/v1/auth/totp/status"
TOTP_SETUP = "/api/v1/auth/totp/setup"
TOTP_CONFIRM = "/api/v1/auth/totp/confirm"
TOTP_DISABLE = "/api/v1/auth/totp/disable"
LOGIN_TOTP = "/api/v1/auth/login/totp"
LOGIN_RECOVERY = "/api/v1/auth/login/recovery"

EMAIL = "totp-ada@example.com"
PASSWORD = "correct-horse-battery"
RATE_ENVELOPE = {"detail": "Too many attempts", "code": "rate_limited"}

# RFC 6238 Appendix B, SHA-1 mode, 8-digit codes — we keep the last 6.
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32("12345678901234567890")


@contextmanager
def _cookies_over_http() -> Iterator[None]:
    """Relax `Secure` so httpx actually sends the cookie over plain http."""
    settings = get_settings()
    original = settings.cookie_secure
    settings.cookie_secure = False
    try:
        yield
    finally:
        settings.cookie_secure = original


def _register(client: TestClient, email: str = EMAIL) -> dict:
    client.post(REGISTER, json={"email": email, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def _headers(body: dict) -> dict:
    return {"Authorization": f"Bearer {body['access_token']}"}


def _enroll(client: TestClient, headers: dict) -> tuple[str, list[str]]:
    """setup -> confirm with a real code; returns (secret, recovery codes)."""
    setup = client.post(TOTP_SETUP, headers=headers)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    confirm = client.post(
        TOTP_CONFIRM,
        json={"code": totp_service.totp_code(secret, datetime.now(UTC))},
        headers=headers,
    )
    assert confirm.status_code == 200, confirm.text
    codes = confirm.json()["recovery_codes"]
    assert len(codes) == totp_service.RECOVERY_CODE_COUNT
    return secret, codes


def _wrong_code(secret: str) -> str:
    """A 6-digit code that is definitely NOT the live one (no 1-in-1e6 flakes)."""
    live = totp_service.totp_code(secret, datetime.now(UTC))
    for candidate in ("000000", "999999", "111111", "222222"):
        if candidate != live:
            return candidate
    raise AssertionError("no wrong code candidate")


def _challenge(client: TestClient, email: str = EMAIL) -> dict:
    """Password step -> the challenge body (never a token pair)."""
    response = client.post(LOGIN, json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    body = response.json()
    # `.get` + body in the message: a failed challenge shows WHAT came back.
    assert body.get("challenge") == "totp", body
    assert "access_token" not in body
    return body


# --- algorithm (pinned by the RFC, not by ourselves) -------------------------


def test_rfc6238_sha1_vectors() -> None:
    for timestamp, eight_digit in RFC_VECTORS:
        at = datetime.fromtimestamp(timestamp, UTC)
        expected = eight_digit[-6:]
        assert totp_service.totp_code(RFC_SECRET, at) == expected, timestamp


def test_verify_window_and_replay() -> None:
    at = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
    code = totp_service.totp_code(RFC_SECRET, at)

    assert totp_service.verify_totp(RFC_SECRET, code, at, None) is not None
    # ±1 step is inside the window; t-2 is expired.
    assert totp_service.verify_totp(RFC_SECRET, code, at + timedelta(seconds=30), None) is not None
    assert totp_service.verify_totp(RFC_SECRET, code, at + timedelta(seconds=90), None) is None
    # Replay: the accepted step is consumed.
    step = totp_service.verify_totp(RFC_SECRET, code, at, None)
    assert step is not None
    assert totp_service.verify_totp(RFC_SECRET, code, at, step) is None
    # Malformed input never matches.
    assert totp_service.verify_totp(RFC_SECRET, "abcdef", at, None) is None
    assert totp_service.verify_totp(RFC_SECRET, "12345", at, None) is None


# --- enrollment --------------------------------------------------------------


def test_setup_stages_encrypted_pending_secret(client: TestClient, session) -> None:
    body = _register(client)
    headers = _headers(body)

    setup = client.post(TOTP_SETUP, headers=headers)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    otpauth = setup.json()["otpauth_url"]
    assert otpauth.startswith("otpauth://totp/")
    # Label and params are parsed, not string-matched: the email is
    # percent-encoded inside the label, which a substring check would miss.
    label, _, query = otpauth.removeprefix("otpauth://totp/").partition("?")
    assert unquote(label) == f"{get_settings().totp_issuer}:{EMAIL}"
    params = parse_qs(query)
    assert params["secret"] == [secret]
    assert params["issuer"] == [get_settings().totp_issuer]
    assert params["algorithm"] == ["SHA1"]
    assert params["digits"] == ["6"]
    assert params["period"] == ["30"]

    status = client.get(TOTP_STATUS, headers=headers).json()
    assert status == {"enabled": False, "pending": True}

    # At rest: Fernet ciphertext, never the base32 secret (docs/02).
    factor = session.exec(select(TotpFactor)).one()
    assert factor.pending_secret is not None
    assert factor.secret is None
    assert factor.pending_secret != secret
    assert factor.pending_secret.startswith("gAAAAA")


def test_confirm_activates_and_mints_hashed_recovery_codes(client: TestClient, session) -> None:
    body = _register(client)
    headers = _headers(body)
    secret, codes = _enroll(client, headers)

    status = client.get(TOTP_STATUS, headers=headers).json()
    assert status == {"enabled": True, "pending": False}

    # Codes are stored as SHA-256 hashes — plaintext appears in NO row.
    rows = session.exec(select(RecoveryCode)).all()
    assert len(rows) == totp_service.RECOVERY_CODE_COUNT
    stored = {row.code_hash for row in rows}
    assert stored == {totp_service.hash_recovery(code) for code in codes}
    assert not any(code in row.code_hash for code in codes for row in rows)
    assert len(set(codes)) == totp_service.RECOVERY_CODE_COUNT
    for code in codes:
        left, right = code.split("-")
        assert len(left) == 5 and len(right) == 5

    # Confirm without a staged secret is refused: `pending` is gone now.
    again = client.post(
        TOTP_CONFIRM,
        json={"code": totp_service.totp_code(secret, datetime.now(UTC))},
        headers=headers,
    )
    assert again.status_code == 409
    assert again.json()["code"] == "totp_not_enrolled"
    setup = client.post(TOTP_SETUP, headers=headers)
    assert setup.status_code == 200
    new_codes = client.post(
        TOTP_CONFIRM,
        json={"code": totp_service.totp_code(setup.json()["secret"], datetime.now(UTC))},
        headers=headers,
    ).json()["recovery_codes"]
    assert set(new_codes).isdisjoint(codes)
    assert len(session.exec(select(RecoveryCode)).all()) == (totp_service.RECOVERY_CODE_COUNT)


def test_confirm_requires_a_real_code(client: TestClient) -> None:
    body = _register(client)
    headers = _headers(body)

    no_enrollment = client.post(TOTP_CONFIRM, json={"code": "123456"}, headers=headers)
    assert no_enrollment.status_code == 409
    assert no_enrollment.json()["code"] == "totp_not_enrolled"

    setup = client.post(TOTP_SETUP, headers=headers)
    assert setup.status_code == 200
    wrong = client.post(
        TOTP_CONFIRM,
        json={"code": _wrong_code(setup.json()["secret"])},
        headers=headers,
    )
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "invalid_totp_code"


# --- login: challenge then second factor ------------------------------------


def test_login_challenges_then_totp_issues_pair(client: TestClient) -> None:
    with _cookies_over_http():
        body = _register(client)
        headers = _headers(body)
        secret, _ = _enroll(client, headers)

        # Password alone is no longer a session.
        challenge = _challenge(client)
        assert challenge["challenge"] == "totp"
        assert challenge["expires_in"] == 300
        # A signed JWT (header.payload.signature), not a session token.
        assert len(challenge["challenge_token"].split(".")) == 3

        code = totp_service.totp_code(secret, datetime.now(UTC))
        login = client.post(
            LOGIN_TOTP, json={"challenge_token": challenge["challenge_token"], "code": code}
        )
        assert login.status_code == 200, login.text
        pair = login.json()
        assert pair["token_type"] == "bearer"
        assert pair["access_token"]
        assert "refresh_token" not in pair
        assert client.cookies.get("refresh_token")

        # The session is real: an authenticated read works with it.
        me = client.get(ME, headers=_headers(pair))
        assert me.status_code == 200
        assert me.json()["email"] == EMAIL


def test_login_totp_rejects_wrong_expired_and_replayed_codes(
    client: TestClient,
) -> None:
    body = _register(client)
    headers = _headers(body)
    secret, _ = _enroll(client, headers)

    challenge = _challenge(client)
    token = challenge["challenge_token"]

    wrong = client.post(LOGIN_TOTP, json={"challenge_token": token, "code": _wrong_code(secret)})
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "invalid_totp_code"

    # Malformed code -> the same 401, never a 422.
    malformed = client.post(LOGIN_TOTP, json={"challenge_token": token, "code": "abc"})
    assert malformed.status_code == 401
    assert malformed.json()["code"] == "invalid_totp_code"

    # Expired: 10 steps (5 minutes) in the past.
    stale = totp_service.totp_code(secret, datetime.now(UTC) - timedelta(seconds=300))
    expired = client.post(LOGIN_TOTP, json={"challenge_token": token, "code": stale})
    assert expired.status_code == 401
    assert expired.json()["code"] == "invalid_totp_code"

    # Valid once...
    code = totp_service.totp_code(secret, datetime.now(UTC))
    ok = client.post(LOGIN_TOTP, json={"challenge_token": token, "code": code})
    assert ok.status_code == 200, ok.text

    # ...then REPLAYED on a fresh challenge: the step is already consumed.
    replay_challenge = _challenge(client)
    replay = client.post(
        LOGIN_TOTP,
        json={"challenge_token": replay_challenge["challenge_token"], "code": code},
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == "invalid_totp_code"


def test_login_without_factor_still_returns_pair(client: TestClient) -> None:
    """No enrollment -> no challenge: the old contract is untouched."""
    _register(client)
    response = client.post(LOGIN, json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert "challenge" not in body
    assert body["access_token"]


# --- recovery codes ----------------------------------------------------------


def test_recovery_code_is_single_use(client: TestClient, session) -> None:
    headers = _headers(_register(client))
    _, codes = _enroll(client, headers)

    first = _challenge(client)
    used = client.post(
        LOGIN_RECOVERY,
        json={"challenge_token": first["challenge_token"], "code": codes[0]},
    )
    assert used.status_code == 200, used.text
    assert "refresh_token" not in used.json()

    # Same code again, brand-new challenge -> refused.
    second = _challenge(client)
    again = client.post(
        LOGIN_RECOVERY,
        json={"challenge_token": second["challenge_token"], "code": codes[0]},
    )
    assert again.status_code == 401
    assert again.json()["code"] == "invalid_recovery_code"

    # A different code still works, and the rest are untouched.
    third = _challenge(client)
    other = client.post(
        LOGIN_RECOVERY,
        json={"challenge_token": third["challenge_token"], "code": codes[1]},
    )
    assert other.status_code == 200, other.text

    rows = session.exec(select(RecoveryCode)).all()
    used_rows = [row for row in rows if row.used_at is not None]
    assert len(used_rows) == 2
    # Separator-insensitive: the client may send it without the dash.
    fourth = _challenge(client)
    dashed = client.post(
        LOGIN_RECOVERY,
        json={
            "challenge_token": fourth["challenge_token"],
            "code": codes[2].replace("-", ""),
        },
    )
    assert dashed.status_code == 200, dashed.text


# --- challenge-token integrity ----------------------------------------------


def _expired_challenge(user_id: int) -> str:
    now = datetime.now(UTC)
    # Signed with the real SESSION key (RS256) so the signature + kid verify;
    # only the `exp` is in the past, which is what this case must exercise.
    return encode_jwt(
        "session",
        {
            "sub": str(user_id),
            "type": "challenge",
            "method": PASSWORD_CHALLENGE_METHOD,
            "iat": now - timedelta(minutes=10),
            "exp": now - timedelta(minutes=5),
        },
    )


def test_challenge_token_integrity(client: TestClient, session) -> None:
    body = _register(client)
    headers = _headers(body)
    secret, _ = _enroll(client, headers)  # no factor -> login answers a pair
    user = session.exec(select(User).where(User.email == EMAIL)).one()
    live = _challenge(client)
    token = live["challenge_token"]

    cases = {
        "tampered": token[:-6] + "AAAAAA",
        "expired": _expired_challenge(user.id),
        "wrong purpose": create_challenge_token(user.id, "passkey"),
        "nonexistent user": create_challenge_token(9_999_999, PASSWORD_CHALLENGE_METHOD),
        "not a challenge at all": "not-a-jwt",
    }
    for label, candidate in cases.items():
        response = client.post(LOGIN_TOTP, json={"challenge_token": candidate, "code": "123456"})
        assert response.status_code == 401, (label, response.status_code)
        assert response.json()["code"] == "invalid_or_expired_challenge", label

    # A live challenge for an account with the factor removed still reaches
    # /login/totp but must not mint a session.
    disabled = client.post(
        TOTP_DISABLE,
        json={"code": totp_service.totp_code(secret, datetime.now(UTC))},
        headers=headers,
    )
    assert disabled.status_code == 204, disabled.text
    no_factor = client.post(LOGIN_TOTP, json={"challenge_token": token, "code": "123456"})
    assert no_factor.status_code == 401
    assert no_factor.json()["code"] == "totp_not_enabled"
    _enroll(client, headers)  # restore an active factor for the checks below

    # A challenge is NOT a bearer token (type claim mismatch)...
    as_bearer = client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert as_bearer.status_code == 401
    assert as_bearer.json()["code"] == "invalid_token"
    # ...and an access token is not a challenge.
    # The register token: a password login now answers a CHALLENGE while the
    # factor is active, which is exactly the behaviour under test.
    access = body["access_token"]
    wrong_token = client.post(LOGIN_TOTP, json={"challenge_token": access, "code": "123456"})
    assert wrong_token.status_code == 401
    assert wrong_token.json()["code"] == "invalid_or_expired_challenge"


# --- demo exemption ----------------------------------------------------------


def test_demo_tenant_cannot_enroll_and_is_never_challenged(client: TestClient, session) -> None:
    demo = User(email=DEMO_EMAIL, hashed_password=hash_password("demo-password"))
    session.add(demo)
    session.flush()
    # Defense in depth: even a factor row smuggled into the DB must not stand
    # between the public demo password and its session.
    session.add(
        TotpFactor(
            user_id=demo.id,
            secret=encrypt_secret(totp_service.new_secret()),
        )
    )
    session.commit()

    login = client.post(LOGIN, json={"email": DEMO_EMAIL, "password": "demo-password"})
    assert login.status_code == 200, login.text
    body = login.json()
    assert "challenge" not in body
    assert body["access_token"]
    headers = _headers(body)

    setup = client.post(TOTP_SETUP, headers=headers)
    assert setup.status_code == 403
    assert setup.json()["code"] == "demo_account_restricted"
    confirm = client.post(TOTP_CONFIRM, json={"code": "123456"}, headers=headers)
    assert confirm.status_code == 403
    disable = client.post(TOTP_DISABLE, json={"code": "123456"}, headers=headers)
    assert disable.status_code == 403

    # Reads stay open for the demo tenant (GET /auth/totp/status).
    status = client.get(TOTP_STATUS, headers=headers)
    assert status.status_code == 200
    assert status.json()["enabled"] is True


# --- disable -----------------------------------------------------------------


def test_disable_with_totp_code(client: TestClient) -> None:
    body = _register(client)
    headers = _headers(body)
    secret, _ = _enroll(client, headers)

    wrong = client.post(TOTP_DISABLE, json={"code": _wrong_code(secret)}, headers=headers)
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "invalid_totp_code"

    disabled = client.post(
        TOTP_DISABLE,
        json={"code": totp_service.totp_code(secret, datetime.now(UTC))},
        headers=headers,
    )
    assert disabled.status_code == 204, disabled.text

    status = client.get(TOTP_STATUS, headers=headers).json()
    assert status == {"enabled": False, "pending": False}
    # And the login challenge is gone again.
    after = client.post(LOGIN, json={"email": EMAIL, "password": PASSWORD}).json()
    assert "challenge" not in after


def test_disable_with_recovery_code(client: TestClient, session) -> None:
    body = _register(client)
    headers = _headers(body)
    _, codes = _enroll(client, headers)

    disabled = client.post(TOTP_DISABLE, json={"code": codes[3]}, headers=headers)
    assert disabled.status_code == 204, disabled.text
    assert session.exec(select(RecoveryCode)).all() == []
    assert session.exec(select(TotpFactor)).all() == []


def test_disable_when_not_enrolled(client: TestClient) -> None:
    body = _register(client)
    response = client.post(TOTP_DISABLE, json={"code": "123456"}, headers=_headers(body))
    assert response.status_code == 409
    assert response.json()["code"] == "totp_not_enrolled"


# --- rate limit --------------------------------------------------------------


def test_login_totp_rate_limit_engages(client: TestClient) -> None:
    body = _register(client)
    headers = _headers(body)
    secret, _ = _enroll(client, headers)
    challenge = _challenge(client)
    token = challenge["challenge_token"]

    limiter.enabled = True
    try:
        bad = _wrong_code(secret)
        for _ in range(10):
            response = client.post(LOGIN_TOTP, json={"challenge_token": token, "code": bad})
            assert response.status_code == 401  # rejected, but bounded
        limited = client.post(LOGIN_TOTP, json={"challenge_token": token, "code": bad})
        assert limited.status_code == 429
        assert limited.json() == RATE_ENVELOPE
    finally:
        limiter.enabled = False
        limiter.reset()

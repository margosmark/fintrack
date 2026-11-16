"""M74 passkeys (WebAuthn) — behavior tests (docs/38).

There is no real authenticator in CI, so this module carries a small software
one: one ES256/SECP256R1 credential, real CBOR attestation and real signatures
over `authenticatorData || SHA-256(clientDataJSON)`. Everything the SERVER
decides — challenge binding, rpId/origin pinning, sign-count clone detection,
caps, uniqueness — is exercised through HTTP, because that is what the browser
sends.

Run: cd backend && uv run pytest tests/test_auth_passkeys.py -q
"""

import hashlib
import json
import logging
import secrets
from base64 import urlsafe_b64encode
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import cbor2
import jwt
from cryptography.hazmat.primitives import hashes as crypto_hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlmodel import select

from app.api.auth_second_factor import (
    DISCOVERABLE_CHALLENGE_METHOD,
    PASSKEY_CAP,
    THROWAWAY_USER_ID,
)
from app.core.config import get_settings
from app.core.jwt_keys import encode_jwt
from app.core.ratelimit import limiter
from app.core.security import (
    PASSWORD_CHALLENGE_METHOD,
    create_challenge_token,
    decode_challenge_token,
    hash_password,
)
from app.models.auth_factors import WebAuthnCredential
from app.models.users import User
from app.services.demo_seed import DEMO_EMAIL

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/users/me"
PASSKEYS = "/api/v1/auth/passkeys"
PASSKEY_OPTIONS = "/api/v1/auth/passkeys/options"
PASSKEY_LOGIN_OPTIONS = "/api/v1/auth/passkeys/login/options"
PASSKEY_LOGIN = "/api/v1/auth/passkeys/login"

EMAIL = "passkey-ada@example.com"
PASSWORD = "correct-horse-battery"
RP_ID = "localhost"  # hostname of the default APP_ORIGIN
RATE_ENVELOPE = {"detail": "Too many attempts", "code": "rate_limited"}


# --- helpers ----------------------------------------------------------------


def _b64u(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _register(client: TestClient, email: str = EMAIL) -> dict:
    client.post(REGISTER, json={"email": email, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def _headers(body: dict) -> dict:
    return {"Authorization": f"Bearer {body['access_token']}"}


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


class FakeAuthenticator:
    """One credential, real signatures. `origin` is settable so a tampered
    clientDataJSON can be sent exactly like a spoofing browser would."""

    def __init__(self, origin: str | None = None) -> None:
        self.origin = origin or get_settings().app_origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)

    # --- building blocks ---------------------------------------------------

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps(
            {
                "type": kind,
                "challenge": challenge,
                "origin": self.origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def _cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _auth_data(self, flags: int, counter: int, extra: bytes = b"") -> bytes:
        rp_hash = hashlib.sha256(RP_ID.encode("ascii")).digest()
        return rp_hash + bytes([flags]) + counter.to_bytes(4, "big") + extra

    # --- outputs -----------------------------------------------------------

    def create(self, options: dict) -> dict:
        """A registration credential for the given creation options."""
        attested = (
            b"\x00" * 16  # aaguid
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self._cose_key()
        )
        # UP (0x01) | UV (0x04, CTAP2 — the bit `webauthn` actually reads)
        # | AT (0x40)
        auth_data = self._auth_data(0x45, 0, attested)
        attestation_object = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": _b64u(self.credential_id),
            "rawId": _b64u(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64u(self._client_data("webauthn.create", options["challenge"])),
                "attestationObject": _b64u(attestation_object),
                "transports": ["internal", "not-a-transport"],
            },
            "clientExtensionResults": {},
        }

    def get(
        self,
        options: dict,
        sign_count: int,
        *,
        wrong_key: bool = False,
        user_handle: str | None = None,
        flags: int = 0x05,
    ) -> dict:
        """An assertion for the given request options.

        `user_handle` is the base64url `userHandle` a RESIDENT credential
        returns (null for a non-discoverable one); `flags` defaults to
        UP (0x01) | UV (0x04 — CTAP2's bit, which is where `webauthn`
        reads user verification from), so a test can drop to UP-only
        (0x01) to exercise the user-verification requirement.
        """
        key = ec.generate_private_key(ec.SECP256R1()) if wrong_key else self.key
        client_data = self._client_data("webauthn.get", options["challenge"])
        # UP (0x01) | UV (0x04) by default — see `flags`.
        auth_data = self._auth_data(flags, sign_count)
        signed = auth_data + hashlib.sha256(client_data).digest()
        # DER, exactly what a real ES256 authenticator sends (the verifier
        # hands it straight to cryptography's ECDSA verifier).
        signature = key.sign(signed, ec.ECDSA(crypto_hashes.SHA256()))
        return {
            "id": _b64u(self.credential_id),
            "rawId": _b64u(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64u(client_data),
                "authenticatorData": _b64u(auth_data),
                "signature": _b64u(signature),
                "userHandle": user_handle,
            },
            "clientExtensionResults": {},
        }


def _begin_register(client: TestClient, headers: dict) -> dict:
    response = client.post(PASSKEY_OPTIONS, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _register_passkey(
    client: TestClient, headers: dict, auth: FakeAuthenticator, name: str | None = None
):
    """options -> credential -> verify; returns the POST response."""
    options = _begin_register(client, headers)
    payload = {
        "challenge_token": options["challenge_token"],
        "credential": auth.create(options["options"]),
    }
    if name is not None:
        payload["name"] = name
    return client.post(PASSKEYS, json=payload, headers=headers)


def _login_options(client: TestClient, email: str = EMAIL) -> dict:
    response = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": email})
    assert response.status_code == 200, response.text
    return response.json()


# --- registration + passwordless login --------------------------------------


def test_register_then_passwordless_login(client: TestClient, session) -> None:
    headers = _headers(_register(client))
    empty = client.get(PASSKEYS, headers=headers)
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "total": 0}

    auth = FakeAuthenticator()
    created = _register_passkey(client, headers, auth, name="Laptop")
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["name"] == "Laptop"
    assert row["transports"] == "internal"  # "not-a-transport" was filtered out
    assert row["created_at"] and row["id"]
    # The public shape leaks no key material.
    assert set(row) == {"id", "name", "created_at", "last_used_at", "transports"}

    listed = client.get(PASSKEYS, headers=headers).json()
    assert listed["total"] == 1
    assert listed["items"][0]["name"] == "Laptop"

    with _cookies_over_http():
        options = _login_options(client)
        assert options["options"]["rpId"] == RP_ID
        allow = options["options"]["allowCredentials"]
        assert len(allow) == 1
        assert allow[0]["id"] == _b64u(auth.credential_id)

        assertion = auth.get(options["options"], sign_count=1)
        login = client.post(
            PASSKEY_LOGIN,
            json={"challenge_token": options["challenge_token"], "credential": assertion},
        )
        assert login.status_code == 200, login.text
        pair = login.json()
        assert pair["access_token"]
        assert "refresh_token" not in pair  # cookie-only, docs/38
        assert client.cookies.get("refresh_token")

        # The session is real and usable.
        me = client.get(ME, headers={"Authorization": f"Bearer {pair['access_token']}"})
        assert me.status_code == 200
        assert me.json()["email"] == EMAIL

    # Sign count advanced and last_used_at stamped.
    credential = session.exec(select(WebAuthnCredential)).one()
    assert credential.sign_count == 1
    assert credential.last_used_at is not None

    # A second sign-in with a higher counter works...
    options = _login_options(client)
    again = client.post(
        PASSKEY_LOGIN,
        json={
            "challenge_token": options["challenge_token"],
            "credential": auth.get(options["options"], sign_count=2),
        },
    )
    assert again.status_code == 200, again.text

    # ...but a counter that does not advance is the clone signal: refused.
    options = _login_options(client)
    cloned = client.post(
        PASSKEY_LOGIN,
        json={
            "challenge_token": options["challenge_token"],
            "credential": auth.get(options["options"], sign_count=1),
        },
    )
    assert cloned.status_code == 401
    assert cloned.json()["code"] == "invalid_passkey"
    assert session.exec(select(WebAuthnCredential)).one().sign_count == 2


def test_registration_rejects_tampered_payloads(client: TestClient) -> None:
    headers = _headers(_register(client))

    # Origin spoofing: rpId is pinned to APP_ORIGIN, never taken from the client.
    spoofed = FakeAuthenticator(origin="https://evil.example")
    options = _begin_register(client, headers)
    bad_origin = client.post(
        PASSKEYS,
        json={
            "challenge_token": options["challenge_token"],
            "credential": spoofed.create(options["options"]),
        },
        headers=headers,
    )
    assert bad_origin.status_code == 422
    assert bad_origin.json()["code"] == "invalid_passkey_response"

    # Challenge substitution: credential minted for a DIFFERENT options call.
    first = _begin_register(client, headers)
    second = _begin_register(client, headers)
    swapped = client.post(
        PASSKEYS,
        json={
            "challenge_token": first["challenge_token"],
            "credential": FakeAuthenticator().create(second["options"]),
        },
        headers=headers,
    )
    assert swapped.status_code == 422
    assert swapped.json()["code"] == "invalid_passkey_response"

    # A challenge token from a different purpose never verifies a credential.
    wrong_purpose = client.post(
        PASSKEYS,
        json={
            "challenge_token": create_challenge_token(
                1, PASSWORD_CHALLENGE_METHOD, secrets.token_bytes(32)
            ),
            "credential": FakeAuthenticator().create(first["options"]),
        },
        headers=headers,
    )
    assert wrong_purpose.status_code == 401
    assert wrong_purpose.json()["code"] == "invalid_or_expired_challenge"

    # Nothing was stored.
    assert client.get(PASSKEYS, headers=headers).json()["total"] == 0


def test_assertion_rejects_tampered_payloads(client: TestClient) -> None:
    headers = _headers(_register(client))
    auth = FakeAuthenticator()
    assert _register_passkey(client, headers, auth).status_code == 201

    # Unknown credential id: no row to check the signature against.
    options = _login_options(client)
    stranger = FakeAuthenticator()
    stranger.credential_id = secrets.token_bytes(32)
    unknown = client.post(
        PASSKEY_LOGIN,
        json={
            "challenge_token": options["challenge_token"],
            "credential": stranger.get(options["options"], sign_count=1),
        },
    )
    assert unknown.status_code == 401
    assert unknown.json()["code"] == "invalid_passkey"

    # Origin spoofing on the assertion.
    options = _login_options(client)
    spoofed = FakeAuthenticator(origin="https://evil.example")
    spoofed.credential_id = auth.credential_id
    bad_origin = client.post(
        PASSKEY_LOGIN,
        json={
            "challenge_token": options["challenge_token"],
            "credential": spoofed.get(options["options"], sign_count=1),
        },
    )
    assert bad_origin.status_code == 401
    assert bad_origin.json()["code"] == "invalid_passkey"

    # Right credential id, WRONG key: signature cannot verify.
    options = _login_options(client)
    wrong_key = FakeAuthenticator()
    wrong_key.credential_id = auth.credential_id
    forged = client.post(
        PASSKEY_LOGIN,
        json={
            "challenge_token": options["challenge_token"],
            "credential": wrong_key.get(options["options"], sign_count=1),
        },
    )
    assert forged.status_code == 401
    assert forged.json()["code"] == "invalid_passkey"

    # Assertion minted against another challenge.
    options_a = _login_options(client)
    options_b = _login_options(client)
    stale = client.post(
        PASSKEY_LOGIN,
        json={
            "challenge_token": options_a["challenge_token"],
            "credential": auth.get(options_b["options"], sign_count=1),
        },
    )
    assert stale.status_code == 401
    assert stale.json()["code"] == "invalid_passkey"

    # Every refusal was a refusal: the credential never logged in, so nothing
    # about the stored counter can have changed through this path.
    assert client.get(PASSKEYS, headers=headers).json()["total"] == 1


# --- purpose-bound challenge tokens ------------------------------------------


def test_challenge_tokens_are_purpose_bound(client: TestClient) -> None:
    body = _register(client)
    headers = _headers(body)

    # A password-proven challenge is not a passkey challenge.
    from_challenge = client.post(
        PASSKEYS,
        json={
            "challenge_token": create_challenge_token(
                1, PASSWORD_CHALLENGE_METHOD, secrets.token_bytes(32)
            ),
            "credential": FakeAuthenticator().create({"challenge": _b64u(secrets.token_bytes(32))}),
        },
        headers=headers,
    )
    assert from_challenge.status_code == 401
    assert from_challenge.json()["code"] == "invalid_or_expired_challenge"

    auth = FakeAuthenticator()
    assert _register_passkey(client, headers, auth).status_code == 201

    # A passkey challenge is not a TOTP challenge, and vice versa.
    register_challenge = _begin_register(client, headers)["challenge_token"]
    totp_rejection = client.post(
        "/api/v1/auth/login/totp",
        json={"challenge_token": register_challenge, "code": "123456"},
    )
    assert totp_rejection.status_code == 401
    assert totp_rejection.json()["code"] == "invalid_or_expired_challenge"

    # ...and a passkey-REGISTER challenge cannot be spent on PASSKEY LOGIN.
    login_attempt = client.post(
        PASSKEY_LOGIN,
        json={
            "challenge_token": register_challenge,
            "credential": auth.get({"challenge": _b64u(secrets.token_bytes(32))}, sign_count=1),
        },
    )
    assert login_attempt.status_code == 401
    assert login_attempt.json()["code"] == "invalid_or_expired_challenge"


# --- delete ------------------------------------------------------------------


def test_delete_passkey(client: TestClient) -> None:
    headers = _headers(_register(client))
    auth = FakeAuthenticator()
    created = _register_passkey(client, headers, auth, name="Phone")
    passkey_id = created.json()["id"]

    missing = client.delete(f"{PASSKEYS}/{passkey_id + 9999}", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"

    deleted = client.delete(f"{PASSKEYS}/{passkey_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    assert client.get(PASSKEYS, headers=headers).json() == {"items": [], "total": 0}
    # Gone for good: the passwordless start still answers 200 — with a
    # THROWAWAY challenge, never the deleted credential (M87, docs/38).
    gone = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": EMAIL})
    assert gone.status_code == 200
    assert gone.json()["challenge_token"]
    assert [e["id"] for e in gone.json()["options"]["allowCredentials"]] != [
        _b64u(auth.credential_id)
    ]
    # And the credential can no longer assert.
    reuse = client.post(PASSKEY_LOGIN, json={"challenge_token": "x", "credential": {}})
    assert reuse.status_code == 401
    # Deleting the same id twice is still 404, not a 500.
    again = client.delete(f"{PASSKEYS}/{passkey_id}", headers=headers)
    assert again.status_code == 404


def test_passkey_is_scoped_to_its_owner(client: TestClient) -> None:
    alice = _headers(_register(client, "alice@example.com"))
    alice_auth = FakeAuthenticator()
    created = _register_passkey(client, alice, alice_auth)
    alice_id = created.json()["id"]

    bob = _headers(_register(client, "bob@example.com"))
    # Bob cannot see or delete Alice's passkey (404, not 403: no existence leak).
    assert client.get(PASSKEYS, headers=bob).json() == {"items": [], "total": 0}
    stolen = client.delete(f"{PASSKEYS}/{alice_id}", headers=bob)
    assert stolen.status_code == 404
    assert stolen.json()["code"] == "not_found"
    # The passwordless start is public BY DESIGN (it precedes any session) and
    # resolves per email: Alice's start offers exactly her credential, while
    # Bob's — with or without an account — gets a throwaway challenge whose
    # single descriptor is random, so nothing of Alice's reaches Bob (M87,
    # docs/38: this used to be a 404/200 split, which was the oracle itself).
    bob_start = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "bob@example.com"})
    assert bob_start.status_code == 200
    assert bob_start.json()["options"]["allowCredentials"][0]["id"] != _b64u(
        alice_auth.credential_id
    )
    alice_start = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "alice@example.com"})
    assert alice_start.status_code == 200
    allow = alice_start.json()["options"]["allowCredentials"]
    assert [entry["id"] for entry in allow] == [_b64u(alice_auth.credential_id)]


# --- M87: uniform status/shape responders + counter hardening (M83 F-6/F-7) --


def test_passkey_login_options_responder_shape_is_uniform(client: TestClient) -> None:
    """M87 / M83 F-7: the passwordless start's STATUS and SHAPE are uniform.

    Three cases must be indistinguishable on the status channel (200 for all
    three — the retired `404 no_passkeys` split was the oracle itself) and in
    envelope SHAPE: same keys, same types, same `allowCredentials` element
    shape and count. The two keyless cases get a throwaway challenge bound to
    no user, so the ceremony is where they fail.

    What is deliberately NOT asserted here: the `id` VALUE. Email-first
    non-discoverable login must name the address's real credential ids, so
    the value, length and cross-request stability of `allowCredentials[0].id`
    still disclose passkey enrollment — the documented residual (docs/42 §2)
    pinned by
    `test_passkey_login_options_id_channel_reveals_enrollment_as_documented`.
    A blanket claim that all three cases are indistinguishable AT THE
    RESPONDER is false and must not come back in docs or docstrings
    (t_a199b88f).
    """
    holder = _headers(_register(client, "with-key@example.com"))
    auth = FakeAuthenticator()
    assert _register_passkey(client, holder, auth, name="Key").status_code == 201

    _register(client, "no-key@example.com")  # account, zero credentials

    with_key = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "with-key@example.com"})
    no_key = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "no-key@example.com"})
    missing = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "ghost@example.com"})

    assert [r.status_code for r in (with_key, no_key, missing)] == [200, 200, 200]

    def _shape(body: dict) -> dict:
        options = body["options"]
        return {
            "top": sorted(body),
            "options": sorted(options),
            "challenge_type": type(options["challenge"]).__name__,
            "rp_id": options["rpId"],
            "allow_element_keys": sorted(options["allowCredentials"][0]),
            "allow_len": len(options["allowCredentials"]),
            "token_type": type(body["challenge_token"]).__name__,
        }

    shapes = [_shape(r.json()) for r in (with_key, no_key, missing)]
    assert shapes[0] == shapes[1] == shapes[2]
    assert all(r.json()["challenge_token"] for r in (with_key, no_key, missing))

    # Only the credential holder names a REAL credential id; the two throwaway
    # answers never mention it — and never mention each other's either.
    real_id = _b64u(auth.credential_id)
    assert [e["id"] for e in with_key.json()["options"]["allowCredentials"]] == [real_id]
    for keyless in (no_key, missing):
        assert [e["id"] for e in keyless.json()["options"]["allowCredentials"]] != [real_id]

    # The throwaway challenge is usable for exactly one thing: failing.
    for keyless in (no_key, missing):
        invented = client.post(
            PASSKEY_LOGIN,
            json={"challenge_token": keyless.json()["challenge_token"], "credential": {}},
        )
        assert invented.status_code == 401
        assert invented.json()["code"] in {"invalid_or_expired_challenge", "invalid_passkey"}


def test_passkey_login_options_id_channel_reveals_enrollment_as_documented(
    client: TestClient,
) -> None:
    """docs/42 §2 residual, pinned honestly (t_a199b88f).

    Email-first non-discoverable login MUST return the address's real
    credential ids — an empty `allowCredentials` would be discoverable-only
    and strand non-resident credentials (product-gated, PM note on
    t_a199b88f) — so the `allowCredentials[0].id` channel is a documented
    PARTIAL enumeration oracle, not a uniform one:

    * **length tell (ONE request):** the real id's length follows the stored
      credential — a 16-byte credential is 22 base64url chars vs the 32-byte
      throwaway's 43 (measured live on production, t_52491006);
    * **stability tell (TWO requests):** real ids are read from the DB, so
      they are stable; keyless/ghost throwaways regenerate per request.
      This is the only tell for an enrolled account whose credential happens
      to be 32 bytes — the same length as a throwaway;
    * **scope:** only the passkey-holding subset is confirmed — a keyless
      account and a ghost both get throwaways from the same distribution, so
      those two are indistinguishable from each other.

    Status and envelope shape stay uniform in every case; that half is
    `test_passkey_login_options_responder_shape_is_uniform`.

    These assertions PIN the residual as documented: closing the channel
    (the discoverable-only product decision) flips this test, and the
    docs/42 §2 residual note plus the `passkey_login_options` docstring must
    move in the same change.
    """
    short_holder = _headers(_register(client, "short-id@example.com"))
    short_auth = FakeAuthenticator()
    short_auth.credential_id = secrets.token_bytes(16)  # production's shape
    assert _register_passkey(client, short_holder, short_auth, name="Key").status_code == 201

    long_holder = _headers(_register(client, "long-id@example.com"))
    long_auth = FakeAuthenticator()  # 32 bytes — same length as a throwaway
    assert _register_passkey(client, long_holder, long_auth, name="Key").status_code == 201

    _register(client, "residual-nokey@example.com")  # account, zero credentials

    def _options(email: str) -> str:
        response = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": email})
        assert response.status_code == 200
        allow = response.json()["options"]["allowCredentials"]
        assert len(allow) == 1  # element count is uniform in every case
        return allow[0]["id"]

    short_first = _options("short-id@example.com")
    short_second = _options("short-id@example.com")
    long_first = _options("long-id@example.com")
    long_second = _options("long-id@example.com")
    nokey_first = _options("residual-nokey@example.com")
    nokey_second = _options("residual-nokey@example.com")
    ghost_first = _options("residual-ghost@example.com")
    ghost_second = _options("residual-ghost@example.com")

    # Length tell — one request separates a short-credential account from
    # either keyless flavour (production measured 22 vs 43, t_52491006).
    real_short = _b64u(short_auth.credential_id)
    assert len(real_short) == 22
    assert short_first == real_short
    assert {len(nokey_first), len(ghost_first)} == {43}

    # Value + stability tells — a second request separates enrolled from
    # keyless even when the lengths match (32-byte credential vs throwaway).
    real_long = _b64u(long_auth.credential_id)
    assert len(real_long) == 43  # length alone cannot separate this case
    assert long_first == long_second == real_long
    assert short_second == real_short  # stable: read from the DB
    assert nokey_first != nokey_second  # unstable: regenerated per request
    assert ghost_first != ghost_second

    # No enrolled id ever reaches a keyless or ghost request…
    enrolled_ids = {real_short, real_long}
    assert not ({nokey_first, nokey_second, ghost_first, ghost_second} & enrolled_ids)

    # …and the two keyless flavours stay indistinguishable from each other
    # (same length class, same per-request regeneration): the oracle is
    # partial — it cannot tell "account without a passkey" from "no account".
    for keyless_id in (nokey_first, nokey_second, ghost_first, ghost_second):
        assert len(keyless_id) == 43


def test_email_first_challenge_token_subject_is_blinded(client: TestClient, session) -> None:
    """SEC-1 (t_4476a739): no positive-integer subject on an email-first
    challenge token, whichever case minted it.

    `challenge_token` is RS256-signed but NOT encrypted, so anybody holding
    it reads the payload without the key: a `sub` in the clear said whether
    the address had an account with a passkey, in one request. A keyless
    reader must now see the same claim set, the same value types and the
    same byte length for an account with a passkey, an account without one
    and a ghost address (docs/42 §S1b) — while the sealed binding still
    round-trips for the ceremony.
    """
    holder = _headers(_register(client, "blinded@example.com"))
    auth = FakeAuthenticator()
    assert _register_passkey(client, holder, auth, name="Key").status_code == 201
    holder_id = session.exec(select(User).where(User.email == "blinded@example.com")).one().id
    _register(client, "blinded-nokey@example.com")  # account, zero credentials

    with_key = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "blinded@example.com"})
    no_key = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "blinded-nokey@example.com"})
    ghost = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "blinded-ghost@example.com"})
    assert [r.status_code for r in (with_key, no_key, ghost)] == [200, 200, 200]

    def _keyless_read(token: str) -> dict:
        # Exactly what any holder can do with a signed-but-unencrypted JWT.
        return jwt.decode(token, options={"verify_signature": False, "verify_exp": False})

    keyed = _keyless_read(with_key.json()["challenge_token"])
    payloads = [
        keyed,
        _keyless_read(no_key.json()["challenge_token"]),
        _keyless_read(ghost.json()["challenge_token"]),
    ]

    # Same claim set, same value types on all three — the payload no longer
    # separates the cases (envelope shape is pinned by the test above).
    assert all(sorted(p) == sorted(keyed) for p in payloads)
    assert all(
        {k: type(v) for k, v in p.items()} == {k: type(v) for k, v in keyed.items()}
        for p in payloads
    )

    def _readable_subject(payload: dict) -> int | None:
        """What a keyless reader can make of `sub` — `None` when it is opaque."""
        try:
            return int(str(payload["sub"]))
        except (TypeError, ValueError):
            return None

    # The SEC-1 criterion: no positive-integer subject in any of them...
    for payload in payloads:
        subject = _readable_subject(payload)
        assert subject is None or subject <= 0, f"readable subject: {payload['sub']!r}"
    # ...and neither the real id nor the literal throwaway sits in the clear.
    assert keyed["sub"] != str(holder_id)
    assert all(p["sub"] != str(THROWAWAY_USER_ID) for p in payloads)

    # No length tell either: fixed-width sealing ⇒ equal byte lengths.
    tokens = [r.json()["challenge_token"] for r in (with_key, no_key, ghost)]
    assert len({len(token) for token in tokens}) == 1

    # The binding is still in there — only the minting signature's key opens
    # it, and it opens to exactly the right account (or the throwaway).
    assert decode_challenge_token(tokens[0], "passkey_login")[0] == holder_id
    assert decode_challenge_token(tokens[1], "passkey_login")[0] == THROWAWAY_USER_ID
    assert decode_challenge_token(tokens[2], "passkey_login")[0] == THROWAWAY_USER_ID


def test_zero_sign_count_never_erases_a_stored_counter(client: TestClient, session) -> None:
    """M87 / M83 F-6: a reported `0` must never erase a real counter.

    Two layers hold this; the test pins the OBSERVABLE contract both produce:

    1. the `webauthn` library applies the clone rule itself — an assertion
       carrying `0` while a non-zero count is stored fails verification
       BEFORE the app's bookkeeping runs;
    2. M87 made the app's own rule local and explicit (this is the rule
       docs/38 had been documenting): adopt `new_count` only when `> 0`.

    Either way the observable behaviour is identical: the attempt fails AND
    `sign_count` stays at 7 — the 0 never lands, so a later clone cannot be
    handed a hollowed-out baseline to advance from.
    """
    headers = _headers(_register(client))
    auth = FakeAuthenticator()
    assert _register_passkey(client, headers, auth, name="Counting").status_code == 201

    def _login_with(count: int):
        with _cookies_over_http():
            options = _login_options(client)
            return client.post(
                PASSKEY_LOGIN,
                json={
                    "challenge_token": options["challenge_token"],
                    "credential": auth.get(options["options"], sign_count=count),
                },
            )

    # A real advance lands...
    assert _login_with(7).status_code == 200
    assert session.exec(select(WebAuthnCredential)).one().sign_count == 7

    # ...the same credential reporting 0 afterwards is refused, and the 0 does
    # not overwrite the stored 7 (the erasure M83 F-6 was about).
    refused = _login_with(0)
    assert refused.status_code == 401, refused.text
    assert refused.json()["code"] == "invalid_passkey"
    assert session.exec(select(WebAuthnCredential)).one().sign_count == 7

    # Ordinary regressions are still caught too, and still move nothing...
    assert _login_with(6).status_code == 401
    assert session.exec(select(WebAuthnCredential)).one().sign_count == 7

    # ...and a genuine advance still works afterwards.
    assert _login_with(8).status_code == 200, "clone rule must not latch shut"
    assert session.exec(select(WebAuthnCredential)).one().sign_count == 8


def test_a_never_counting_authenticator_stays_usable(client: TestClient, session) -> None:
    """The other half of the same rule (docs/38): `0` that was ALWAYS `0` is
    normal. WebAuthn §6.1.3 explicitly allows authenticators that do not
    maintain a counter — refusing them would lock real users out, and storing
    0 over 0 changes nothing."""
    headers = _headers(_register(client))
    auth = FakeAuthenticator()
    assert _register_passkey(client, headers, auth, name="NoCounter").status_code == 201

    for attempt in range(2):
        with _cookies_over_http():
            options = _login_options(client)
            response = client.post(
                PASSKEY_LOGIN,
                json={
                    "challenge_token": options["challenge_token"],
                    "credential": auth.get(options["options"], sign_count=0),
                },
            )
        assert response.status_code == 200, f"attempt {attempt}: {response.text}"
    assert session.exec(select(WebAuthnCredential)).one().sign_count == 0


# --- cap + uniqueness --------------------------------------------------------


def test_passkey_cap_and_duplicate_credential(client: TestClient) -> None:
    headers = _headers(_register(client))

    # Same credential offered again under a fresh challenge: 409, and the
    # stored set is unchanged. (Done first — once the cap is reached no
    # registration is attempted at all, so the two checks stay independent.)
    auth = FakeAuthenticator()
    first = _register_passkey(client, headers, auth, name="dupe")
    assert first.status_code == 201, first.text
    duplicate = _register_passkey(client, headers, auth, name="dupe again")
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "passkey_exists"
    assert client.get(PASSKEYS, headers=headers).json()["total"] == 1

    for index in range(PASSKEY_CAP - 1):
        created = _register_passkey(client, headers, FakeAuthenticator(), name=f"Key {index}")
        assert created.status_code == 201, created.text

    assert client.get(PASSKEYS, headers=headers).json()["total"] == PASSKEY_CAP
    capped = client.post(PASSKEY_OPTIONS, headers=headers)
    assert capped.status_code == 400
    assert capped.json()["code"] == "passkey_cap"


# --- auth + demo -------------------------------------------------------------


def test_passkey_routes_require_authentication(client: TestClient) -> None:
    # Routes behind get_current_user.
    unauthenticated = [
        client.get(PASSKEYS),
        client.post(PASSKEY_OPTIONS),
        client.delete(f"{PASSKEYS}/1"),
    ]
    for response in unauthenticated:
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "invalid_token"

    # The passkey LOGIN route is public but challenge-gated: no bearer token
    # exists there to be missing, so a garbage challenge is the 401 that shows.
    public_login = client.post(PASSKEY_LOGIN, json={"challenge_token": "x", "credential": {}})
    assert public_login.status_code == 401
    assert public_login.json()["code"] == "invalid_or_expired_challenge"

    # The passwordless start is the one public passkey route: it precedes any
    # session, and all three addresses — passkey-holding, keyless account,
    # unknown — answer the same 200 envelope shape with a challenge_token
    # (M87, docs/38), so the status and token channels are uniform. What is
    # NOT uniform is the `allowCredentials[0].id` VALUE: real ids are stable
    # across requests and follow the stored credential's length, so one
    # request separates the passkey-holding subset (⇒ an account exists) —
    # the documented residual (docs/42 §2). A keyless account and a ghost are
    # indistinguishable from each other. The ceremony is where a wrong case
    # fails: a throwaway challenge cannot be satisfied, and a fabricated
    # assertion for a credential that does not exist is 401.
    start = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": EMAIL})
    assert start.status_code == 200
    assert start.json()["challenge_token"]
    assert isinstance(start.json()["options"]["allowCredentials"], list)
    invented = client.post(
        PASSKEY_LOGIN, json={"challenge_token": start.json()["challenge_token"], "credential": {}}
    )
    assert invented.status_code == 401


def test_demo_tenant_cannot_register_passkeys(client: TestClient, session) -> None:
    demo = User(email=DEMO_EMAIL, hashed_password=hash_password("demo-password"))
    session.add(demo)
    session.commit()
    login = client.post(LOGIN, json={"email": DEMO_EMAIL, "password": "demo-password"})
    assert login.status_code == 200, login.text
    headers = _headers(login.json())

    # Reads stay open — the demo is for looking at.
    assert client.get(PASSKEYS, headers=headers).status_code == 200

    # Writes are refused by the shared demo guard before any handler runs.
    options = client.post(PASSKEY_OPTIONS, headers=headers)
    register = client.post(
        PASSKEYS,
        json={
            "challenge_token": "whatever",
            "credential": FakeAuthenticator().create({"challenge": _b64u(secrets.token_bytes(32))}),
        },
        headers=headers,
    )
    for response in (options, register):
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "demo_account_restricted"
    assert client.get(PASSKEYS, headers=headers).json()["total"] == 0


def test_passkey_login_options_rate_limit(client: TestClient) -> None:
    limiter.enabled = True
    try:
        for _ in range(10):
            response = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "nobody@example.com"})
            # M87: answered (200, uniform), but bounded — the 404 that used to
            # mark "no such account" was the oracle this route closed.
            assert response.status_code == 200
        limited = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "nobody@example.com"})
        assert limited.status_code == 429
        assert limited.json() == RATE_ENVELOPE
    finally:
        limiter.enabled = False
        limiter.reset()


# --- discoverable (passwordless) sign-in — no email supplied ----------------


def _discoverable_options(client: TestClient) -> dict:
    """The passwordless start: the request carries NO address at all."""
    response = client.post(PASSKEY_LOGIN_OPTIONS, json={})
    assert response.status_code == 200, response.text
    return response.json()


def _assert_login(client: TestClient, options: dict, assertion: dict):
    return client.post(
        PASSKEY_LOGIN,
        json={"challenge_token": options["challenge_token"], "credential": assertion},
    )


def _handle_for(session, email: str) -> str:
    """The base64url `userHandle` a resident credential returns for a user —
    registration mints `str(users.id)` as the WebAuthn user id (docs/38)."""
    user_id = session.exec(select(User).where(User.email == email)).one().id
    return _b64u(str(user_id).encode("utf-8"))


def test_discoverable_login_resolves_the_account_from_the_credential(
    client: TestClient, session
) -> None:
    """Passwordless: no email in, session out — the assertion names the account."""
    headers = _headers(_register(client))
    auth = FakeAuthenticator()
    assert _register_passkey(client, headers, auth, name="Phone").status_code == 201

    options = _discoverable_options(client)
    body = options["options"]
    assert options["challenge_token"]
    assert body["rpId"] == RP_ID
    assert body["allowCredentials"] == []  # any discoverable credential for this rpId
    assert body["userVerification"] == "required"  # the assertion IS the second factor

    with _cookies_over_http():
        assertion = auth.get(body, sign_count=1, user_handle=_handle_for(session, EMAIL))
        login = _assert_login(client, options, assertion)
        assert login.status_code == 200, login.text
        pair = login.json()
        assert pair["access_token"]
        assert "refresh_token" not in pair  # cookie-only, docs/38
        assert client.cookies.get("refresh_token")
        me = client.get(ME, headers={"Authorization": f"Bearer {pair['access_token']}"})
        assert me.status_code == 200
        # Resolved from the credential — no address was ever supplied.
        assert me.json()["email"] == EMAIL

    credential = session.exec(select(WebAuthnCredential)).one()
    assert credential.sign_count == 1
    assert credential.last_used_at is not None


def test_discoverable_rejects_missing_and_mismatched_user_handles(
    client: TestClient, session
) -> None:
    headers = _headers(_register(client))
    auth = FakeAuthenticator()
    assert _register_passkey(client, headers, auth).status_code == 201

    # No `userHandle`: on the passwordless path nothing else names the
    # account, so the assertion is refused.
    options = _discoverable_options(client)
    missing = _assert_login(client, options, auth.get(options["options"], sign_count=1))
    assert missing.status_code == 401, missing.text
    assert missing.json()["code"] == "invalid_passkey"

    # A handle that is not base64url at all: the library's own parse raises
    # outside its exception hierarchy, so this also pins that the envelope
    # holds (401, never a bare 500).
    options = _discoverable_options(client)
    garbage = _assert_login(
        client,
        options,
        auth.get(options["options"], sign_count=1, user_handle="!!!not-base64!!!"),
    )
    assert garbage.status_code == 401, garbage.text
    assert garbage.json()["code"] == "invalid_passkey"

    # A handle naming a DIFFERENT account: cross-account assertion refused.
    _register(client, "bob-handle@example.com")
    options = _discoverable_options(client)
    crossed = _assert_login(
        client,
        options,
        auth.get(
            options["options"],
            sign_count=1,
            user_handle=_handle_for(session, "bob-handle@example.com"),
        ),
    )
    assert crossed.status_code == 401, crossed.text
    assert crossed.json()["code"] == "invalid_passkey"

    # Nothing about the credential moved through any of the refusals.
    stored = session.exec(select(WebAuthnCredential)).one()
    assert stored.sign_count == 0
    assert stored.last_used_at is None


def test_discoverable_rejects_unknown_forged_and_unverified_assertions(
    client: TestClient, session
) -> None:
    headers = _headers(_register(client))
    auth = FakeAuthenticator()
    assert _register_passkey(client, headers, auth).status_code == 201
    handle = _handle_for(session, EMAIL)

    # Unknown credential id: no row, no key to verify a signature against.
    options = _discoverable_options(client)
    stranger = FakeAuthenticator()
    unknown = _assert_login(
        client, options, stranger.get(options["options"], sign_count=1, user_handle=handle)
    )
    assert unknown.status_code == 401, unknown.text
    assert unknown.json()["code"] == "invalid_passkey"

    # Right credential id, WRONG key: the signature cannot verify.
    options = _discoverable_options(client)
    forged = _assert_login(
        client,
        options,
        auth.get(options["options"], sign_count=1, wrong_key=True, user_handle=handle),
    )
    assert forged.status_code == 401, forged.text
    assert forged.json()["code"] == "invalid_passkey"

    # Origin spoofing with the REAL key — only clientDataJSON's origin differs.
    options = _discoverable_options(client)
    spoofed = FakeAuthenticator(origin="https://evil.example")
    spoofed.credential_id = auth.credential_id
    spoofed.key = auth.key
    bad_origin = _assert_login(
        client, options, spoofed.get(options["options"], sign_count=1, user_handle=handle)
    )
    assert bad_origin.status_code == 401, bad_origin.text
    assert bad_origin.json()["code"] == "invalid_passkey"

    # Assertion minted against ANOTHER discoverable challenge (stale).
    options_a = _discoverable_options(client)
    options_b = _discoverable_options(client)
    stale = _assert_login(
        client, options_a, auth.get(options_b["options"], sign_count=1, user_handle=handle)
    )
    assert stale.status_code == 401, stale.text
    assert stale.json()["code"] == "invalid_passkey"

    # User verification is REQUIRED on this path: no UV flag, no sign-in.
    options = _discoverable_options(client)
    unverified = _assert_login(
        client,
        options,
        auth.get(options["options"], sign_count=1, user_handle=handle, flags=0x01),
    )
    assert unverified.status_code == 401, unverified.text
    assert unverified.json()["code"] == "invalid_passkey"

    stored = session.exec(select(WebAuthnCredential)).one()
    assert stored.sign_count == 0
    assert stored.last_used_at is None


def test_discoverable_challenge_is_purpose_bound_expired_and_single_use(
    client: TestClient, session
) -> None:
    headers = _headers(_register(client))
    auth = FakeAuthenticator()
    assert _register_passkey(client, headers, auth).status_code == 201
    handle = _handle_for(session, EMAIL)

    # Not a TOTP challenge: the discoverable purpose never satisfies the
    # second-factor endpoints.
    options = _discoverable_options(client)
    totp_rejection = client.post(
        "/api/v1/auth/login/totp",
        json={"challenge_token": options["challenge_token"], "code": "123456"},
    )
    assert totp_rejection.status_code == 401, totp_rejection.text
    assert totp_rejection.json()["code"] == "invalid_or_expired_challenge"

    # Not an email-first challenge either: the throwaway token minted for an
    # address with NO account cannot be turned into passwordless sign-in.
    ghost = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "ghost@example.com"})
    assert ghost.status_code == 200, ghost.text
    crossed = _assert_login(
        client,
        ghost.json(),
        auth.get(ghost.json()["options"], sign_count=1, user_handle=handle),
    )
    assert crossed.status_code == 401, crossed.text
    assert crossed.json()["code"] == "invalid_or_expired_challenge"

    # An EXPIRED discoverable token: same signature, `exp` in the past.
    options = _discoverable_options(client)
    now = datetime.now(UTC)
    expired = encode_jwt(
        "session",
        {
            "sub": str(THROWAWAY_USER_ID),
            "type": "challenge",
            "method": DISCOVERABLE_CHALLENGE_METHOD,
            "iat": now - timedelta(minutes=10),
            "exp": now - timedelta(minutes=5),
            "challenge": options["options"]["challenge"],
        },
    )
    late = _assert_login(
        client,
        {"challenge_token": expired},
        auth.get(options["options"], sign_count=1, user_handle=handle),
    )
    assert late.status_code == 401, late.text
    assert late.json()["code"] == "invalid_or_expired_challenge"

    # Single-use: a spent challenge cannot mint a second session — shown on a
    # NON-COUNTING assertion so the sign-count clone rule has nothing to catch
    # and the single-use guard is what refuses the replay.
    options = _discoverable_options(client)
    assertion = auth.get(options["options"], sign_count=0, user_handle=handle)
    first = _assert_login(client, options, assertion)
    assert first.status_code == 200, first.text
    replay = _assert_login(client, options, assertion)
    assert replay.status_code == 401, replay.text
    assert replay.json()["code"] == "invalid_or_expired_challenge"

    # On a counting authenticator the same replay dies even earlier — on the
    # clone rule — and still never issues a session.
    options = _discoverable_options(client)
    counting = auth.get(options["options"], sign_count=1, user_handle=handle)
    assert _assert_login(client, options, counting).status_code == 200
    counted_replay = _assert_login(client, options, counting)
    assert counted_replay.status_code == 401, counted_replay.text
    assert counted_replay.json()["code"] == "invalid_passkey"


def test_email_first_path_checks_the_handle_when_present(client: TestClient, session) -> None:
    """The M87 email-first ceremony is unchanged — and a PRESENT `userHandle`
    must name the account the challenge selected. Absence stays legal there:
    that is exactly the non-discoverable credential this path exists for."""
    headers = _headers(_register(client))
    auth = FakeAuthenticator()
    assert _register_passkey(client, headers, auth).status_code == 201

    # Correct handle: works (the resident-credential case).
    options = _login_options(client)
    ok = _assert_login(
        client,
        options,
        auth.get(options["options"], sign_count=1, user_handle=_handle_for(session, EMAIL)),
    )
    assert ok.status_code == 200, ok.text

    # No handle at all: still works — the non-discoverable fallback.
    options = _login_options(client)
    plain = _assert_login(client, options, auth.get(options["options"], sign_count=2))
    assert plain.status_code == 200, plain.text

    # A handle naming someone else: cross-account, refused.
    options = _login_options(client)
    crossed = _assert_login(
        client, options, auth.get(options["options"], sign_count=3, user_handle=_b64u(b"424242"))
    )
    assert crossed.status_code == 401, crossed.text
    assert crossed.json()["code"] == "invalid_passkey"


def test_discoverable_start_discloses_no_account(client: TestClient) -> None:
    """The passwordless start takes NO input, so it cannot be an oracle: same
    envelope for every caller, and it never names a credential."""
    holder = _headers(_register(client, "holder@example.com"))
    auth = FakeAuthenticator()
    assert _register_passkey(client, holder, auth, name="Key").status_code == 201

    blank = client.post(PASSKEY_LOGIN_OPTIONS, json={})
    whitespace = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "   "})
    email_first = client.post(PASSKEY_LOGIN_OPTIONS, json={"email": "holder@example.com"})

    assert [r.status_code for r in (blank, whitespace, email_first)] == [200, 200, 200]
    # Same envelope keys and same options keys as the email-first answer —
    # only the descriptor list differs, and EMPTY is the WebAuthn wire form
    # of "any discoverable credential", not a statement about any account.
    assert sorted(blank.json()) == sorted(email_first.json())
    assert sorted(blank.json()["options"]) == sorted(email_first.json()["options"])
    for start in (blank, whitespace):
        assert start.json()["options"]["allowCredentials"] == []
        assert start.json()["options"]["userVerification"] == "required"
    # The real credential id never reaches a caller who supplied no address.
    assert _b64u(auth.credential_id) not in blank.text


def test_passkey_failures_never_log_credential_material(
    client: TestClient, session, caplog
) -> None:
    """Refusals are logged (so an operator can see the attempt) with user ids
    ONLY — never a credential id, never a `userHandle` (docs/38: do not log
    credentials)."""
    headers = _headers(_register(client))
    auth = FakeAuthenticator()
    assert _register_passkey(client, headers, auth).status_code == 201
    handle = _handle_for(session, EMAIL)

    with caplog.at_level(logging.WARNING, logger="app.api.auth_second_factor"):
        options = _discoverable_options(client)
        stranger = FakeAuthenticator()
        _assert_login(
            client, options, stranger.get(options["options"], sign_count=1, user_handle=handle)
        )
        options = _discoverable_options(client)
        _assert_login(
            client,
            options,
            auth.get(options["options"], sign_count=1, user_handle=_b64u(b"424242")),
        )

    assert caplog.records  # the refusals WERE logged, so this assertion bites
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert _b64u(auth.credential_id) not in blob
    assert handle not in blob


def test_discoverable_options_rate_limit(client: TestClient) -> None:
    """The passwordless start shares the route's 10/min limit — omitting the
    address must not buy an attacker extra attempts."""
    limiter.enabled = True
    try:
        for _ in range(10):
            response = client.post(PASSKEY_LOGIN_OPTIONS, json={})
            assert response.status_code == 200
        limited = client.post(PASSKEY_LOGIN_OPTIONS, json={})
        assert limited.status_code == 429
        assert limited.json() == RATE_ENVELOPE
    finally:
        limiter.enabled = False
        limiter.reset()

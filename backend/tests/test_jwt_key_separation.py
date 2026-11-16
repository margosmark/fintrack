"""JWT signing-key separation + dedicated secrets-encryption key (t_d3176af6).

Pins the three properties this change delivers:
1. Server JWTs are RS256 with a `kid`, split across the `access` and `session`
   key families — a token from one family can never be replayed as the other,
   and `alg:none` / HS256 algorithm-confusion are rejected.
2. Verification is pinned to the family's own key + algorithm (never a
   caller-supplied alg), matching `services/oidc.py`'s strictness.
3. `APP_ENCRYPTION_KEY` decouples at-rest secret encryption from `JWT_SECRET`:
   new writes use it, old (legacy-derived) ciphertext stays decryptable, and
   rotating `JWT_SECRET` no longer orphans stored secrets once it is set.
"""

from __future__ import annotations

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from app.core import crypto
from app.core.config import Settings
from app.core.jwt_keys import clear_key_cache, get_family_keys
from app.core.security import (
    create_access_token,
    create_challenge_token,
    create_oidc_pending_token,
    decode_access_token,
    decode_challenge_token,
    decode_oidc_pending_token,
    hash_refresh_token,
    new_refresh_token,
)

_LEGACY_SECRET = "t-d3176af6-legacy-secret-0123456789abcd"


@pytest.fixture
def jwt_env(monkeypatch):
    """Apply/undo JWT + encryption env vars, clearing both settings and the
    key cache around each change so a token always reflects the current keys."""
    from app.core.config import get_settings as _gs

    def _apply(**env: str) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        _gs.cache_clear()
        clear_key_cache()

    def _clear(*names: str) -> None:
        for name in names:
            monkeypatch.delenv(name, raising=False)
        _gs.cache_clear()
        clear_key_cache()

    yield _apply, _clear
    _gs.cache_clear()
    clear_key_cache()


# --- 1/2. RS256 + kid + family separation ----------------------------------


def test_access_token_is_rs256_with_kid(jwt_env) -> None:
    apply, _ = jwt_env
    apply(JWT_SECRET=_LEGACY_SECRET)
    token = create_access_token(42)
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    assert header["kid"] == get_family_keys("access").kid
    assert decode_access_token(token) == 42


def test_challenge_and_oidc_use_the_session_family(jwt_env) -> None:
    apply, _ = jwt_env
    apply(JWT_SECRET=_LEGACY_SECRET)
    session_kid = get_family_keys("session").kid
    challenge = create_challenge_token(7, "second_factor")
    assert jwt.get_unverified_header(challenge)["kid"] == session_kid
    assert decode_challenge_token(challenge, "second_factor")[0] == 7
    oidc = create_oidc_pending_token("a@b.c", "https://iss", "sub-1")
    assert jwt.get_unverified_header(oidc)["kid"] == session_kid
    assert decode_oidc_pending_token(oidc)["email"] == "a@b.c"


def test_access_and_session_families_are_distinct_keys(jwt_env) -> None:
    apply, _ = jwt_env
    apply(JWT_SECRET=_LEGACY_SECRET)
    assert get_family_keys("access").public_pem != get_family_keys("session").public_pem
    assert get_family_keys("access").kid != get_family_keys("session").kid


def test_cross_family_tokens_are_rejected(jwt_env) -> None:
    """An access-signed token must not verify as a challenge/oidc token, and a
    session-signed challenge must not verify as an access token — the whole
    point of splitting the key families."""
    apply, _ = jwt_env
    apply(JWT_SECRET=_LEGACY_SECRET)
    access = create_access_token(42)
    challenge = create_challenge_token(7, "second_factor")
    oidc = create_oidc_pending_token("a@b.c", "https://iss", "sub-1")

    with pytest.raises(jwt.PyJWTError):
        decode_challenge_token(access, "second_factor")
    with pytest.raises(jwt.PyJWTError):
        decode_oidc_pending_token(access)
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(challenge)
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(oidc)


def test_alg_none_and_hs256_confusion_are_rejected(jwt_env) -> None:
    apply, _ = jwt_env
    apply(JWT_SECRET=_LEGACY_SECRET)
    claims = {"sub": "1", "type": "access", "iat": 1, "exp": 4_000_000_000}

    none_tok = jwt.encode(claims, None, algorithm="none")
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(none_tok)

    # An attacker who signs HS256 with some secret cannot get it verified: the
    # decoder hardcodes RS256 and never reads `alg` from the token.
    hs_tok = jwt.encode(claims, "attacker-guessable-secret", algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(hs_tok)


def test_wrong_kid_is_rejected(jwt_env) -> None:
    apply, _ = jwt_env
    apply(JWT_SECRET=_LEGACY_SECRET)
    keys = get_family_keys("access")
    claims = {"sub": "1", "type": "access", "iat": 1, "exp": 4_000_000_000}
    forged = jwt.encode(claims, keys.private_pem, algorithm="RS256", headers={"kid": "bogus"})
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(forged)


def test_missing_required_claims_are_rejected(jwt_env) -> None:
    """`exp`/`iat`/`sub` are required — a sub-less token raises, never a KeyError."""
    apply, _ = jwt_env
    apply(JWT_SECRET=_LEGACY_SECRET)
    keys = get_family_keys("access")
    no_sub = jwt.encode(
        {"type": "access", "iat": 1, "exp": 4_000_000_000},
        keys.private_pem,
        algorithm="RS256",
        headers={"kid": keys.kid},
    )
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(no_sub)


# --- file-based keys (the production provisioning path) ---------------------


def _write_keypair(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = tmp_path / "priv.pem"
    pub = tmp_path / "pub.pem"
    priv.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pub.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return priv, pub


def test_file_provisioned_keys_round_trip_and_isolate(jwt_env, tmp_path) -> None:
    apply, _ = jwt_env
    priv, pub = _write_keypair(tmp_path)
    apply(
        JWT_SECRET=_LEGACY_SECRET,
        JWT_ACCESS_PRIVATE_KEY_PATH=str(priv),
        JWT_ACCESS_PUBLIC_KEY_PATH=str(pub),
        JWT_ACCESS_KID="access-file-1",
    )
    token = create_access_token(99)
    assert jwt.get_unverified_header(token)["kid"] == "access-file-1"
    assert decode_access_token(token) == 99

    # A token minted by the ephemeral (unprovisioned) session key must NOT
    # verify under the file-provisioned access key.
    other = create_challenge_token(1, "second_factor")
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(other)


def test_settings_reject_half_configured_key_paths(tmp_path) -> None:
    """A signing key needs both halves; one path alone must fail fast."""
    with pytest.raises(ValidationError):
        Settings(
            jwt_secret=_LEGACY_SECRET,
            jwt_access_private_key_path=str(tmp_path / "only-private.pem"),
        )
    with pytest.raises(ValidationError):
        Settings(
            jwt_secret=_LEGACY_SECRET,
            jwt_session_public_key_path=str(tmp_path / "only-public.pem"),
        )


# --- 3. dedicated encryption key + migration -------------------------------


def test_encryption_defaults_to_legacy_jwt_secret_derivation(jwt_env) -> None:
    apply, _ = jwt_env
    apply(JWT_SECRET=_LEGACY_SECRET)
    token = crypto.encrypt_secret("JBSWY3DPEHPK3PXP")
    assert crypto.decrypt_secret(token) == "JBSWY3DPEHPK3PXP"


def test_setting_app_encryption_key_keeps_legacy_ciphertext_readable(jwt_env) -> None:
    """Migration: an install that has been encrypting under the jwt_secret
    derivation keeps decrypting after APP_ENCRYPTION_KEY is introduced (jwt_secret
    unchanged), while new writes move to the dedicated key."""
    apply, _ = jwt_env
    apply(JWT_SECRET=_LEGACY_SECRET)
    legacy_ct = crypto.encrypt_secret("TOTP-SEED-VALUE")

    new_key = Fernet.generate_key().decode()
    apply(JWT_SECRET=_LEGACY_SECRET, APP_ENCRYPTION_KEY=new_key)

    # New writes use the dedicated key, and the pre-existing legacy ciphertext
    # still decrypts through the always-appended legacy fallback.
    fresh = crypto.encrypt_secret("TOTP-SEED-VALUE")
    assert crypto.decrypt_secret(fresh) == "TOTP-SEED-VALUE"
    assert crypto.decrypt_secret(legacy_ct) == "TOTP-SEED-VALUE"


def test_app_encryption_key_decouples_from_jwt_secret(jwt_env) -> None:
    """The whole point: once APP_ENCRYPTION_KEY is set, rotating JWT_SECRET no
    longer orphans stored ciphertext (at-rest encryption is decoupled from
    token signing)."""
    apply, _ = jwt_env
    key = Fernet.generate_key().decode()
    apply(JWT_SECRET=_LEGACY_SECRET, APP_ENCRYPTION_KEY=key)
    ct = crypto.encrypt_secret("TOTP-SEED-VALUE")

    # Rotate jwt_secret underneath. The ciphertext is keyed to APP_ENCRYPTION_KEY,
    # so it must survive untouched.
    apply(JWT_SECRET=_LEGACY_SECRET + "-rotated", APP_ENCRYPTION_KEY=key)
    assert crypto.decrypt_secret(ct) == "TOTP-SEED-VALUE"


def test_encryption_key_rotation_via_fallbacks(jwt_env) -> None:
    apply, _ = jwt_env
    old_key = Fernet.generate_key().decode()
    apply(JWT_SECRET=_LEGACY_SECRET, APP_ENCRYPTION_KEY=old_key)
    old_ct = crypto.encrypt_secret("llm-api-key")

    new_key = Fernet.generate_key().decode()
    apply(
        JWT_SECRET=_LEGACY_SECRET,
        APP_ENCRYPTION_KEY=new_key,
        APP_ENCRYPTION_KEY_FALLBACKS=old_key,
    )
    # Old ciphertext still decrypts via the fallback; new writes use the new key.
    assert crypto.decrypt_secret(old_ct) == "llm-api-key"
    assert crypto.decrypt_secret(crypto.encrypt_secret("llm-api-key")) == "llm-api-key"


def test_decrypt_passes_through_plaintext_rows(jwt_env) -> None:
    """Rows written before encryption existed (plaintext) must not blow up."""
    apply, _ = jwt_env
    apply(JWT_SECRET=_LEGACY_SECRET, APP_ENCRYPTION_KEY=Fernet.generate_key().decode())
    assert crypto.decrypt_secret("not-a-fernet-token") == "not-a-fernet-token"


def test_invalid_app_encryption_key_fails_loudly(jwt_env) -> None:
    apply, _ = jwt_env
    apply(JWT_SECRET=_LEGACY_SECRET, APP_ENCRYPTION_KEY="not-a-fernet-key")
    with pytest.raises(ValueError, match="APP_ENCRYPTION_KEY"):
        crypto.encrypt_secret("x")


# --- refresh tokens are independent of the signing keys ---------------------


def test_refresh_tokens_are_opaque_and_unaffected_by_key_change(jwt_env) -> None:
    """The migration story: refresh tokens are opaque DB-hashed values, so they
    keep working across a JWT signing-key change (existing sessions survive)."""
    apply, _ = jwt_env
    apply(JWT_SECRET=_LEGACY_SECRET)
    raw, stored = new_refresh_token()
    assert len(raw) == 64  # 32 bytes hex
    assert hash_refresh_token(raw) == stored

    # Rotate every signing key; the refresh token still hashes the same.
    apply(
        JWT_SECRET=_LEGACY_SECRET + "-rotated",
        JWT_ACCESS_KID="access-2",
        JWT_SESSION_KID="session-2",
    )
    assert hash_refresh_token(raw) == stored

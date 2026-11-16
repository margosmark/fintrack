"""Core security: bcrypt hashing + JWT access/refresh token mechanics (docs/01)."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.security import HTTPAuthorizationCredentials

from app.api.deps import _decode_user_id
from app.core.errors import APIError
from app.core.jwt_keys import encode_jwt
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)


def test_hash_and_verify_password():
    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong password", h) is False


def test_verify_password_rejects_malformed_hash():
    # Non-bcrypt garbage must fail verification, never raise.
    assert verify_password("anything", "not-a-bcrypt-hash") is False


def test_access_token_round_trip():
    token = create_access_token(42)
    assert decode_access_token(token) == 42


def test_access_token_wrong_type_rejected():
    # A well-formed token, signed with the ACCESS key, but carrying a
    # non-access `type`. It must be rejected by the type check (not by a
    # signature/claim error), proving a challenge/refresh-like token can never
    # pass as a bearer token.
    now = datetime.now(UTC)
    refresh_like = encode_jwt(
        "access",
        {
            "sub": "42",
            "type": "refresh",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
    )
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(refresh_like)


def test_missing_sub_token_maps_to_401():
    """A token without `sub` must raise jwt.PyJWTError (caught as 401 upstream),
    not a bare KeyError that would surface as a 500."""
    now = datetime.now(UTC)
    no_sub = encode_jwt(
        "access",
        {"type": "access", "iat": now, "exp": now + timedelta(minutes=5)},
    )
    with pytest.raises(jwt.PyJWTError) as exc:
        decode_access_token(no_sub)
    assert isinstance(exc.value, jwt.InvalidTokenError)

    # End-to-end: the dependency that resolves the user returns 401 via api_error.
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=no_sub)
    with pytest.raises(APIError) as api_exc:
        _decode_user_id(creds, None)
    assert api_exc.value.status_code == 401
    assert api_exc.value.code == "invalid_token"


def test_refresh_token_create_and_verify():
    token, stored_hash = new_refresh_token()
    assert token and stored_hash
    assert hash_refresh_token(token) == stored_hash
    # A different token must not collide with the first.
    other, _other_hash = new_refresh_token()
    assert hash_refresh_token(other) != stored_hash

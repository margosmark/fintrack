"""Shared contract in app.api.deps: owned_or_404 + pagination helpers (docs/08).

Lightweight, no-DB unit tests using stubs for the session/row so the contract
stays verifiable even when the full app (and its routers) cannot import.
"""

import pytest

from app.api import deps
from app.core.errors import APIError


class _User:
    def __init__(self, id: int) -> None:
        self.id = id


class _Row:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class _Model:
    __name__ = "Thing"


class _DB:
    def __init__(self, row) -> None:
        self._row = row

    def get(self, model, id):
        return self._row


def test_owned_or_404_returns_row_when_owned():
    user = _User(7)
    db = _DB(_Row(7))
    assert deps.owned_or_404(_Model, 1, user, db) is not None


def test_owned_or_404_raises_404_when_not_owned():
    user = _User(7)
    db = _DB(_Row(99))
    with pytest.raises(APIError) as exc:
        deps.owned_or_404(_Model, 1, user, db)
    assert exc.value.status_code == 404
    assert exc.value.code == "not_found"


def test_owned_or_404_raises_404_when_missing():
    user = _User(7)
    db = _DB(None)
    with pytest.raises(APIError) as exc:
        deps.owned_or_404(_Model, 1, user, db)
    assert exc.value.code == "not_found"


def test_pagination_dataclass_and_get_pagination():
    p = deps.Pagination(limit=10, offset=5)
    assert p.limit == 10 and p.offset == 5

    # Defaults come straight from the Annotated Query() defaults at call time.
    default = deps.get_pagination()
    assert default.limit == 50 and default.offset == 0

    custom = deps.get_pagination(12, 24)
    assert custom.limit == 12 and custom.offset == 24


def test_paginate_rejects_an_offset_outside_the_ceiling_with_422():
    """M98b F-4: `paginate` re-checks the bound so a caller that builds
    its own page window without going through `get_pagination` cannot send a
    bigint OFFSET Postgres rejects ("integer out of range" -> 500).

    The guard runs BEFORE any session use, so sentinel db/stmt objects are
    the proof that no query is attempted. The HTTP path — FastAPI's own
    `le=` bound turning `offset >= 2**63` into a 422 `validation_error`
    envelope — is pinned in test_api_pagination_and_guards.py.
    """
    for bad_offset in (2**63, 1_000_001, -1):
        with pytest.raises(APIError) as exc:
            deps.paginate(object(), object(), limit=50, offset=bad_offset)
        assert exc.value.status_code == 422
        assert exc.value.code == "validation_error"

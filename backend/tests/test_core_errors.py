"""Core errors: {detail, code} envelope shape and exception handlers (docs/08)."""

import asyncio
import json

from fastapi.responses import JSONResponse

from app.core.errors import (
    APIError,
    api_error,
    api_error_handler,
    unhandled_exception_handler,
)


def test_api_error_carries_status_detail_code():
    err = api_error(404, "Account not found", "not_found")
    assert isinstance(err, APIError)
    assert err.status_code == 404
    assert err.detail == "Account not found"
    assert err.code == "not_found"


def test_api_error_handler_returns_envelope():
    err = api_error(422, "bad", "validation_error")
    resp = asyncio.run(api_error_handler(None, err))
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 422
    assert json.loads(resp.body) == {"detail": "bad", "code": "validation_error"}


def test_unhandled_exception_handler_returns_500_envelope():
    class _Req:
        method = "GET"
        url = type("U", (), {"path": "/x"})()

    resp = asyncio.run(unhandled_exception_handler(_Req(), RuntimeError("boom")))
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 500
    assert json.loads(resp.body) == {"detail": "Internal server error", "code": "internal_error"}

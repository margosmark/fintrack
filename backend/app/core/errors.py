"""Consistent error envelope: {"detail": ..., "code": ...} (docs/08, m1-contract)."""

import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# Mirror the logger name previously used by the inline handler in main.py so
# unhandled-exception log lines keep the same provenance.
_logger = logging.getLogger("app.main")


class APIError(Exception):
    def __init__(self, status_code: int, detail: str, code: str) -> None:
        self.status_code = status_code
        self.detail = detail
        self.code = code
        super().__init__(detail)


def api_error(status_code: int, detail: str, code: str) -> APIError:
    return APIError(status_code=status_code, detail=detail, code=code)


async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
    )


def _normalize_validation_errors(errors: list[dict]) -> list[dict]:
    """Ensure every value in Pydantic error dicts is JSON-serializable."""
    normalized = []
    for err in errors:
        clean = {}
        for k, v in err.items():
            if isinstance(v, dict):
                clean[k] = {
                    dk: str(dv) if not isinstance(dv, (str, int, float, bool, type(None))) else dv
                    for dk, dv in v.items()
                }
            elif isinstance(v, (list, tuple)):
                clean[k] = [
                    str(x) if not isinstance(x, (str, int, float, bool, type(None))) else x
                    for x in v
                ]
            elif not isinstance(v, (str, int, float, bool, type(None))):
                clean[k] = str(v)
            else:
                clean[k] = v
        normalized.append(clean)
    return normalized


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": _normalize_validation_errors(exc.errors()), "code": "validation_error"},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all: unhandled exceptions return the envelope, never a raw 500."""
    _logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "code": "internal_error"},
    )


def register_exception_handlers(app) -> None:
    """Attach the catch-all handler (the other envelope handlers are registered
    by the app factory; this only covers unhandled exceptions)."""
    app.add_exception_handler(Exception, unhandled_exception_handler)

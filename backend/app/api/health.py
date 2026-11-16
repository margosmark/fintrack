"""Health check endpoint for infrastructure monitoring."""

import logging
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check(db: Annotated[Session, Depends(get_db)]):
    """Check application health and dependency status.

    Returns HTTP 200 if all critical dependencies are healthy,
    HTTP 503 if any critical dependency is unreachable.

    This endpoint requires no auth, so its failure body is public: a driver
    exception string carries the database host, port, and sometimes a
    username or DSN fragment, which is reconnaissance for anyone who can reach
    the port. The response therefore carries a fixed reason string and the
    exception detail goes to the server log only, where the operator (who
    already runs the box) needs it.
    """
    checks = {}
    overall_healthy = True

    # Database check
    start = time.time()
    try:
        db.execute(text("SELECT 1"))
        latency = int((time.time() - start) * 1000)
        checks["database"] = {"status": "healthy", "latency_ms": latency}
    except Exception:
        overall_healthy = False
        # exc_info carries the full traceback to the log; the response body
        # below never sees the exception message.
        logger.error("Health check: database probe failed", exc_info=True)
        checks["database"] = {"status": "unhealthy", "error": "database unavailable"}

    status_code = 200 if overall_healthy else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "healthy" if overall_healthy else "unhealthy",
            "timestamp": datetime.now(UTC).isoformat(),
            "checks": checks,
        },
    )

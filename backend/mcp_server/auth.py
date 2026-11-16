"""MCP auth — ASGI middleware + user context for MCP server."""

from __future__ import annotations

import contextvars
import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

# The streamable-http app serves exactly one route: the MCP endpoint path,
# SDK default `streamable_http_path="/mcp"` (mcp/server/lowlevel/server.py),
# and create_auth_app() passes no override — verified for the no-auth case
# (no OAuth/SSE/custom routes are registered without an auth server provider).
MCP_ENDPOINT_PATH = "/mcp"
# Stores the resolved user_id for the current MCP request.
current_user_id: contextvars.ContextVar[int] = contextvars.ContextVar("current_user_id")
# True while a single /mcp request is being served. The SDK runs each
# call_tool in a COPIED context, so a request-scoped identity must be kept
# in contextvars to stay isolated; this flag is what tells BackendClient that
# it is inside such a request and may scope its identity state to it. Outside
# it (stdio, one long-lived session) the state is process-level, so
# auth_login still persists to the next tool call.
in_mcp_request: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "in_mcp_request", default=False
)
# M80 (docs/40 §2): session facts the toolset policy is derived from. They
# ride the same request-scoped context as `current_user_id`, and their DEFAULTS
# are the unrestricted ones — stdio (no middleware) therefore sees exactly the
# toolset it saw before this milestone.
current_mcp_policy: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_mcp_policy", default="full"
)
current_mcp_token_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_mcp_token_id", default=None
)
current_is_demo: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "current_is_demo", default=False
)


class McpAuthMiddleware(BaseHTTPMiddleware):
    """Validates Bearer tokens on /mcp requests.

    Default-deny (L20): only MCP_ENDPOINT_PATH is served — every other path
    on this listener gets 404 here, regardless of what routes the app gains.

    The token is an opaque MCP token. Validation happens via the backend's
    /auth/validate-mcp-token endpoint. On success, the user_id is stored
    in a context var so the client can create a backend JWT.
    """

    def __init__(self, app, backend_url: str | None = None) -> None:
        super().__init__(app)
        self._backend_url = (
            backend_url or os.getenv("FIN_TRACKER_URL", "http://localhost:8000")
        ).rstrip("/")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        # Default-deny (L20): only the MCP endpoint path is served. Anything
        # else 404s before reaching the app (the app itself serves only
        # /mcp anyway — this closes the "everything else default-allows"
        # gap for any route added later).
        if request.url.path.rstrip("/") != MCP_ENDPOINT_PATH:
            return JSONResponse({"detail": "Not found", "code": "not_found"}, status_code=404)

        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "Missing bearer token"}, status_code=401)

        raw_token = auth[7:]

        # Validate against backend.
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._backend_url}/api/v1/auth/validate-mcp-token",
                    headers={"Authorization": f"Bearer {raw_token}"},
                    timeout=10,
                )
        except Exception as e:
            log.warning("MCP token validation error: %s", e)
            return JSONResponse({"error": "Auth service unavailable"}, status_code=503)

        if resp.status_code != 200:
            return JSONResponse({"error": "Invalid MCP token"}, status_code=401)

        data = resp.json()
        user_id = data["user_id"]
        token = current_user_id.set(user_id)
        policy_token = current_mcp_policy.set(str(data.get("policy") or "full"))
        token_id_token = current_mcp_token_id.set(data.get("token_id"))
        demo_token = current_is_demo.set(bool(data.get("is_demo")))
        request_flag = in_mcp_request.set(True)
        try:
            response = await call_next(request)
            return response
        finally:
            current_user_id.reset(token)
            current_mcp_policy.reset(policy_token)
            current_mcp_token_id.reset(token_id_token)
            current_is_demo.reset(demo_token)
            in_mcp_request.reset(request_flag)

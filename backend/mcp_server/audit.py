"""MCP write audit — one receipt per `tools/call` (M80, docs/40 §3).

The MCP process has no database (docs/23: three env vars, no `env_file`), so
the receipt travels over the same trust boundary the identity already uses —
``MCP_INTERNAL_SECRET`` on an internal backend route. Recording is best
effort: a failed POST is logged and swallowed, because an unreachable audit
sink must never break a user's tool call.

Arguments are hashed, not stored: `auth_login` carries a password and
`users_change_password` two of them. The hash is stable and reproducible, so
"was this the same call?" stays answerable without keeping the secret.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

AUDIT_PATH = "/api/v1/auth/internal-mcp-audit"

# Response codes a receipt may carry (docs/40 §3).
CODE_REFUSED = 403  # refused at the MCP seam (policy / demo guard)
CODE_TOOL_INPUT = 400  # isError whose text is the house {detail, code} envelope
CODE_UNEXPECTED = 500  # isError with anything else (an exception leaked)
CODE_OK = 200


def args_hash(arguments: Mapping[str, Any] | None) -> str:
    """sha256 hex over the canonical MCP arguments JSON (sorted, tight)."""
    canonical = json.dumps(
        dict(arguments or {}),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def response_code(result: object) -> int:
    """Derive a receipt's HTTP-style code from the tool result the SDK built.

    Pinned in docs/40 §3: an integer top-level ``error`` is the backend's own
    status (that is exactly what ``BackendClient.request`` returns for >=400),
    then an ``isError`` carrying the house envelope is a rejected input (400),
    then any other ``isError`` is an unexpected failure (500), else 200.
    """
    if not isinstance(result, Mapping):
        return CODE_OK
    is_error = bool(result.get("isError"))
    payload: Any = None
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], Mapping):
        text = content[0].get("text")
        if isinstance(text, str):
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
    if isinstance(payload, Mapping):
        status = payload.get("error")
        if isinstance(status, int) and not isinstance(status, bool):
            return status
        if is_error and "detail" in payload and "code" in payload:
            return CODE_TOOL_INPUT
    if is_error:
        return CODE_UNEXPECTED
    return CODE_OK


def record(
    *,
    user_id: int,
    mcp_token_id: int | None,
    tool: str,
    arguments: Mapping[str, Any] | None,
    status: int,
    duration_ms: int,
) -> None:
    """POST one receipt to the backend. Never raises (best effort)."""
    from mcp_server.client import client  # lazy: the client builds at import

    if not client.post_internal(
        AUDIT_PATH,
        {
            "user_id": user_id,
            "mcp_token_id": mcp_token_id,
            "tool": tool,
            "args_hash": args_hash(arguments),
            "response_code": status,
            "duration_ms": duration_ms,
        },
    ):
        # No MCP_INTERNAL_SECRET (stdio modes 1/2): there is no per-user
        # identity to attribute a receipt to either, so silence is correct.
        log.debug("mcp_audit skipped for %s: no internal secret", tool)

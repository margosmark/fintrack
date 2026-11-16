"""MCP tool policy — read/write classification, session restrictions, refusals.

M80 (docs/40 §2). Two questions, one module:

* *Is this tool a write?* — answered by parsing ``server.py`` itself, so the
  classification can never drift from the code it describes. The AST walk
  reads the literal HTTP method of each ``@mcp.tool()``'s ``client.request``
  call; anything it cannot classify counts as a write (fail closed).
* *What does a restricted session see, and what does a refused call return?*

Restrictions come from ``mcp_server.auth``'s contextvars — set by the ASGI
middleware from ``POST /auth/validate-mcp-token`` and left at their defaults
on stdio, where there is no per-request identity.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Methods that cannot mutate tenant data through this API.
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# POSTs that write nothing. Everything here is either session lifecycle or a
# pure computation over what the caller just sent — each entry is why a
# read-only token may still call it. `auth_register` is deliberately absent:
# it creates a user. Enforced by tests/test_mcp_agent_surface.py, which fails
# if an entry disappears from server.py or if a tool stops being classified.
COMPUTE_ONLY_TOOLS: dict[str, str] = {
    "auth_login": "session lifecycle; mode 3 fixes the identity from the MCP token",
    "auth_refresh": "session lifecycle; rotates the caller's own cookie",
    "auth_logout": "session lifecycle; drops the caller's own cookie",
    "auth_demo": "session lifecycle; re-authenticates the demo tenant only",
    "projections_run": "pure computation over the posted scenario inputs",
    "users_test_llm": "connectivity probe; writes no tenant row",
    "imports_suggest_profile": "parses an uploaded CSV and returns a suggestion",
}

# Write-class tools a `read_propose` connector may still call (docs/59 §2):
# each one persists an INERT pending proposal card and never touches domain
# data — mutation happens only when the user applies the card in the web app
# (docs/57 §4), which no MCP tool can do. Deliberately NOT in
# COMPUTE_ONLY_TOOLS above: it does write a row, so it stays a write for
# `read_only` (which cannot propose) and for every other policy branch.
PROPOSE_TOOLS: frozenset[str] = frozenset({"proposals_create"})

# Marker text carried in `annotations.title` so a restricted client can see at
# a glance why 38 tools are missing. `None` = unrestricted session.
MARKER_READ_ONLY = "read-only token"
MARKER_READ_PROPOSE = "read+propose token"
MARKER_DEMO = "demo tenant is read-only"

# Session-lifecycle tools that are refused on the streamable-HTTP path (M84,
# docs/40 §2). Under HTTP the identity is fixed by the MCP token (mode 3), so
# a login/refresh/logout can only ever act on SOMEBODY ELSE's session — which
# is exactly what the shared-jar leak (F-1) turned `auth_refresh` into. On
# stdio there is one user and no cross-tenancy, so they keep working there.
# `health_check` is deliberately absent (it authenticates nobody).
AUTH_TOOLS: frozenset[str] = frozenset(
    {
        "auth_register",
        "auth_login",
        "auth_demo",
        "auth_refresh",
        "auth_logout",
    }
)

# The envelope every AUTH_TOOLS refusal returns. 403 keeps the seam's receipt
# table uniform: docs/40 §3 already pins 403 for "refused at the MCP seam"
# (mcp_read_only, demo_account_restricted), and this is the same class of
# refusal — refused BEFORE the proxy runs, no backend call, no JWT issued.
AUTH_TOOL_ENVELOPE = {
    "error": 403,
    "detail": {
        "detail": (
            "Auth tools are not available over the MCP HTTP transport: the "
            "session identity is already fixed by your MCP token, so a "
            "login/refresh/logout here could only act on another tenant's "
            "session. Use the web app for session management."
        ),
        "code": "auth_tools_unavailable_on_mcp",
    },
}

_SCAN_CACHE: dict[str, frozenset[str]] | None = None


def _is_tool_function(node: ast.FunctionDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
    return False


def _is_client_request(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "request"


def tool_methods() -> dict[str, frozenset[str]]:
    """``tool name -> HTTP methods its proxy call uses`` (parsed once).

    Reads the sibling ``server.py`` as source: the file is on disk in every
    deployment (the container runs the package, it is not frozen). A parse
    failure logs loudly and yields an EMPTY map, which makes every tool a
    write under :func:`is_write_tool` — the read-only policy then hides
    everything rather than something it cannot vouch for.
    """
    global _SCAN_CACHE
    if _SCAN_CACHE is not None:
        return _SCAN_CACHE
    source = Path(__file__).with_name("server.py")
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        log.warning("MCP tool classification unavailable (%s): %s", source, exc)
        _SCAN_CACHE = {}
        return _SCAN_CACHE
    found: dict[str, frozenset[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not _is_tool_function(node):
            continue
        methods = {
            call.args[0].value
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and _is_client_request(call)
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        }
        found[node.name] = frozenset(methods)
    _SCAN_CACHE = found
    return found


def is_write_tool(name: str) -> bool:
    """True when `name` mutates tenant data (unknown tools are writes)."""
    if name in COMPUTE_ONLY_TOOLS:
        return False
    methods = tool_methods().get(name)
    if not methods:
        return True
    return not methods <= READ_ONLY_METHODS


def split_tools(names: list[str]) -> tuple[list[str], list[str]]:
    """Partition tool names into (read, write), preserving the input order."""
    read, write = [], []
    for name in names:
        (write if is_write_tool(name) else read).append(name)
    return read, write


def restriction_marker(policy: str | None, is_demo: bool) -> str | None:
    """Visible marker for a restricted session, or None when unrestricted.

    Anything that is not exactly `full` counts as restricted (fail closed),
    which also covers a `policy` value the enum has never heard of.
    """
    markers = []
    if policy == "read_propose":
        markers.append(MARKER_READ_PROPOSE)
    elif policy != "full":
        markers.append(MARKER_READ_ONLY)
    if is_demo:
        markers.append(MARKER_DEMO)
    return " · ".join(markers) if markers else None


def tool_annotations(name: str, marker: str | None) -> dict[str, object]:
    """`tools/list` annotations for one tool (docs/40 §2).

    `readOnlyHint` is the machine signal; `title` carries the marker so the
    restriction is visible in a client that renders titles rather than hints.
    """
    write = is_write_tool(name)
    annotations: dict[str, object] = {"readOnlyHint": not write}
    if write:
        annotations["destructiveHint"] = "DELETE" in tool_methods().get(name, frozenset())
    if marker:
        annotations["title"] = f"{name} · {marker}"
    return annotations


def refusal_envelope(name: str, policy: str | None, is_demo: bool) -> dict | None:
    """The house envelope for a `tools/call` this session may not make.

    Returns None when the call is allowed. The demo branch reuses the backend
    code and wording verbatim (`app/api/deps.py`) so an MCP refusal is
    indistinguishable from the 403 the API would have returned anyway; the
    backend guard itself is untouched (docs/40 §2).
    """
    if is_demo:
        return {
            "error": 403,
            "detail": {
                "detail": "The demo account is read-only; changes are not allowed",
                "code": "demo_account_restricted",
            },
        }
    if name in PROPOSE_TOOLS and policy == "read_propose":
        return None  # the connector's one allowed write: an inert pending card
    if policy == "read_propose":
        return {
            "error": 403,
            "detail": {
                "detail": (
                    f"This MCP token can query and propose, but {name} would "
                    "modify data directly. Propose the change instead "
                    "(proposals_create), or mint a full token (POST "
                    "/auth/mcp-tokens with "
                    'policy="full") to write.'
                ),
                "code": "mcp_read_propose",
            },
        }
    if policy != "full":
        return {
            "error": 403,
            "detail": {
                "detail": (
                    f"This MCP token is read-only; {name} would modify data. "
                    "Mint a full token (POST /auth/mcp-tokens with "
                    'policy="full") to write.'
                ),
                "code": "mcp_read_only",
            },
        }
    return None

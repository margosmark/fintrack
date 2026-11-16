"""Entry point: python -m mcp_server"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

from mcp_server.server import create_auth_app, mcp

# Loopback by default (L19): never bind 0.0.0.0 implicitly. Widen only when
# explicitly asked, via --host or MCP_HOST — and note the compose stack
# publishes this port as 127.0.0.1:8001 anyway.
DEFAULT_HOST = "127.0.0.1"


def _flag_value(argv: Sequence[str], flag: str, default: str | None = None) -> str | None:
    """Value of `--flag value` in argv, or `default` when absent/truncated."""
    if flag in argv:
        idx = argv.index(flag)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return default


def resolve_bind_host(argv: Sequence[str], env: Mapping[str, str]) -> str:
    """Bind address precedence: --host flag > MCP_HOST env > loopback (L19)."""
    return _flag_value(argv, "--host") or env.get("MCP_HOST") or DEFAULT_HOST


if __name__ == "__main__":
    # If running with streamable-http, use the auth-wrapped app.
    if "--transport" in sys.argv and "streamable-http" in sys.argv:
        import uvicorn

        app = create_auth_app(host=resolve_bind_host(sys.argv, os.environ))
        port = int(_flag_value(sys.argv, "--port", "8001") or "8001")
        uvicorn.run(app, host=resolve_bind_host(sys.argv, os.environ), port=port)
    else:
        mcp.run()

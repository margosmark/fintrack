"""mcp_audit — one receipt per MCP tool call (M80, docs/40 §3).

"What did the bot do?" answered with a query instead of a vibe: the MCP server
records every `tools/call` — including the ones it refuses — through the
existing `MCP_INTERNAL_SECRET` seam. Arguments can carry credentials
(`auth_login`), so only a sha256 hash is stored.

Two deliberate ownership decisions:

* `mcp_token_id` has **no** foreign key — revoking a token must not delete the
  receipts it produced (an audit trail that shrinks when you clean up is not
  an audit trail). NULL means "not minted from a token" (stdio / mode 1-2).
* `user_id` IS a foreign key and joins `_USER_OWNED_MODELS`, so a deleted
  account and the demo reset both leave no rows behind.
"""

from sqlalchemy import Index, desc
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class McpAudit(TimestampMixin, table=True):
    __tablename__ = "mcp_audit"
    __table_args__ = (Index("ix_mcp_audit_user_created", "user_id", desc("created_at")),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    mcp_token_id: int | None = None  # no FK — see the module docstring
    tool: str = Field(max_length=100)
    # sha256 hex over the canonical MCP arguments JSON (docs/40 §3).
    args_hash: str = Field(max_length=64)
    response_code: int  # 403 refusal | backend status | 400 | 500 | 200
    duration_ms: int

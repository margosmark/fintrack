"""mcp_audit — receipt read shapes (M80, docs/40 §3)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class McpAuditRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mcp_token_id: int | None = None
    tool: str
    args_hash: str
    response_code: int
    duration_ms: int
    created_at: datetime


class McpAuditPage(BaseModel):
    items: list[McpAuditRow]
    total: int

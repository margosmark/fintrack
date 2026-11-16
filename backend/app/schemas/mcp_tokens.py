"""mcp_tokens — request/response shapes."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.mcp_tokens import McpTokenPolicy


class McpTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # M80 (docs/40 §1): omitted → `full`, so every existing client keeps
    # today's behaviour. Anything outside the enum is a 422 validation_error.
    # M81: `None` distinguishes "omitted" from an explicit `full`, which is
    # what lets the demo tenant default to read-only while still REJECTING a
    # caller who asks for `full` (422 `demo_mcp_read_only`) instead of
    # silently downgrading it. The resolved policy is decided in the handler.
    policy: McpTokenPolicy | None = None


class McpTokenPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    policy: McpTokenPolicy
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None


class McpTokenCreated(BaseModel):
    """Returned on creation — includes the raw token (shown once)."""

    id: int
    name: str
    token: str
    policy: McpTokenPolicy
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None


class McpTokenPage(BaseModel):
    items: list[McpTokenPublic]
    total: int

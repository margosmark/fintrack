"""mcp_tokens — opaque bearer tokens for MCP server auth (maps token → user)."""

import enum
import hashlib
import secrets
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, Enum, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin

# MCP tokens expire (audit M12): a leaked token must not be a perpetual
# credential. New tokens get created_at + MCP_TOKEN_TTL_DAYS; migration 0026
# backfills pre-existing rows the same way.
MCP_TOKEN_TTL_DAYS = 90


class McpTokenPolicy(enum.StrEnum):
    """What a connected MCP client may do (M80, docs/40 §1; M189, docs/59).

    `read_propose` is the BYO-AI connector scope: read tools plus
    `proposals_create` (an inert pending card) and no domain writes. Like
    `read_only`, it is enforced ONLY at the MCP seam — the server hides the
    write tools and refuses the call before it leaves the process — while the
    backend keeps its existing authorization untouched.
    """

    full = "full"
    read_only = "read_only"
    read_propose = "read_propose"


class McpToken(TimestampMixin, table=True):
    __tablename__ = "mcp_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_mcp_tokens_token_hash"),
        # SA 2.0's Enum(native_enum=False) emits no CHECK (create_constraint
        # defaults to False), so the DB enforces the policy vocabulary itself
        # (migration 0060 widens it for `read_propose` — docs/59 §1).
        CheckConstraint(
            "policy IN ('full', 'read_only', 'read_propose')",
            name="ck_mcp_tokens_policy",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=100)
    token_hash: str = Field(index=True)
    # M80 (docs/40): server_default keeps pre-0034 rows `full`, so nothing
    # changes for a deployment that has never minted a read-only token.
    policy: McpTokenPolicy = Field(
        default=McpTokenPolicy.full,
        sa_column=Column(
            Enum(McpTokenPolicy, native_enum=False, length=16),
            nullable=False,
            server_default="full",
        ),
    )
    # Nullable for legacy rows until migration 0026 backfills them; NULL means
    # "no expiry" only for rows created directly in tests.
    expires_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    last_used_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


def new_mcp_token() -> tuple[str, str]:
    """Issue an opaque 32-byte MCP token.

    Returns the client-facing token string and its SHA-256 hex hash.
    """
    token = secrets.token_hex(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def hash_mcp_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

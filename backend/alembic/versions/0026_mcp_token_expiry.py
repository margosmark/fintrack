"""MCP tokens expire + last-used tracking (audit M12).

Revision ID: 0026_mcp_token_expiry
Revises: 0025_mcp_tokens_ts_defaults

mcp_tokens previously never expired — a leaked token was a perpetual
full-API credential. Adds nullable ``expires_at`` / ``last_used_at``
(timestamptz) and backfills every existing row's expiry to
``created_at + 90 days`` (the TTL new tokens get from the API), so tokens
from before this migration die at most 90 days after they were created.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_mcp_token_expiry"
down_revision: str | None = "0025_mcp_tokens_ts_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mcp_tokens", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "mcp_tokens", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Backfill: existing tokens expire 90 days after creation (docs/08).
    op.execute(
        "UPDATE mcp_tokens SET expires_at = created_at + interval '90 days' "
        "WHERE expires_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("mcp_tokens", "last_used_at")
    op.drop_column("mcp_tokens", "expires_at")

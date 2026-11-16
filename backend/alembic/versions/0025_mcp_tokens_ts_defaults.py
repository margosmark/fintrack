"""Restore missing timestamp server defaults on mcp_tokens.

Revision ID: 0025_mcp_tokens_ts_defaults
Revises: 0024_add_e2ee_cipher_columns

Migration 0015 created mcp_tokens.created_at/updated_at NOT NULL without
server_default (same bug class as 0016 tags, fixed in 0022), so every insert
into mcp_tokens failed with NotNullViolation — POST /auth/mcp-tokens raised a
bare 500.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_mcp_tokens_ts_defaults"
down_revision: str | None = "0024_add_e2ee_cipher_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "mcp_tokens", "created_at", server_default=sa.func.now(), existing_nullable=False
    )
    op.alter_column(
        "mcp_tokens", "updated_at", server_default=sa.func.now(), existing_nullable=False
    )


def downgrade() -> None:
    op.alter_column("mcp_tokens", "created_at", server_default=None, existing_nullable=False)
    op.alter_column("mcp_tokens", "updated_at", server_default=None, existing_nullable=False)

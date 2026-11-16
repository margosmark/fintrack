"""add mcp_tokens table.

Opaque bearer tokens for MCP server auth — maps token → user.

Revision ID: 0015_add_mcp_tokens
Revises: 0014_add_user_name
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_add_mcp_tokens"
down_revision: str | None = "0014_add_user_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_hash", name="uq_mcp_tokens_token_hash"),
    )


def downgrade() -> None:
    op.drop_table("mcp_tokens")

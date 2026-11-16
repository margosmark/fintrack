"""add the watchlist table (W11): securities tracked but not held.

One row per (user, security) — UNIQUE constraint backs the
409 {"code": "already_watched"} duplicate guard on POST /watchlist.

Revision ID: 0003_watchlist
Revises: 0002_widen_account_types
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_watchlist"
down_revision: str | None = "0002_widen_account_types"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watchlist",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "security_id", name="uq_watchlist_user_security"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
    )
    op.create_index("ix_watchlist_user_id", "watchlist", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_watchlist_user_id", table_name="watchlist")
    op.drop_table("watchlist")

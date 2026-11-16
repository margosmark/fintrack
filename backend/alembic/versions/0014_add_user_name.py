"""add name column to users.

Optional display name, null until the user sets one in Settings (docs/08).

Revision ID: 0014_add_user_name
Revises: 0013_duplicate_include_flags
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_add_user_name"
down_revision: str | None = "0013_duplicate_include_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "name")

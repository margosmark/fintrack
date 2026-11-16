"""add duplicate-include staging flags to transactions and trades.

Staged imports now persist duplicate rows (with a salted dedup_hash) so the
user can choose to include specific ones at commit time. These flags track
which rows are duplicates and which the user forced in (docs/03).

Revision ID: 0013_duplicate_include_flags
Revises: 0012_add_currency_flag
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_duplicate_include_flags"
down_revision: str | None = "0012_add_currency_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("transactions", "trades"):
        op.add_column(
            table,
            sa.Column("is_duplicate", sa.Boolean, nullable=False, server_default=sa.text("false")),
        )
        op.add_column(
            table,
            sa.Column("force_include", sa.Boolean, nullable=False, server_default=sa.text("false")),
        )


def downgrade() -> None:
    for table in ("transactions", "trades"):
        op.drop_column(table, "force_include")
        op.drop_column(table, "is_duplicate")

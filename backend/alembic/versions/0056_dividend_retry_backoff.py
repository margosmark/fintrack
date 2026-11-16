"""bounded dividend-refresh retry backoff (M186)

Revision ID: 0056_dividend_retry_backoff
Revises: 0055_dividend_history
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0056_dividend_retry_backoff"
down_revision: str | None = "0055_dividend_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dividend_history_refresh",
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("dividend_history_refresh", "consecutive_failures")

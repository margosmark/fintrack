"""add currency_tracker_enabled preference to users.

Per-user feature flag controlling visibility of the Currencies page + Tools
sidebar section (defaults ON so existing users keep the tab visible).

Revision ID: 0012_add_currency_flag
Revises: 0011_add_trade_import
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_add_currency_flag"
down_revision: str | None = "0011_add_trade_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "currency_tracker_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "currency_tracker_enabled")

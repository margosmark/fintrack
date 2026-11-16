"""M76: transactions.cleared — YNAB clearing state preserved on import (docs/38).

Revision ID: 0033_tx_cleared
Revises: 0032_export_schedule
Create Date: 2026-09-30

Numbered 0033 (not 0031) because M75's 0032_export_schedule shipped first and
is already the head on production — this revises it, so `alembic upgrade head`
runs after the deployed head instead of forking a second branch.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_tx_cleared"
down_revision: str | None = "0032_export_schedule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable on purpose: OFX/QFX and generic-CSV rows carry no clearing state,
    # so NULL means "not tracked" and only the YNAB migration fills it.
    op.add_column(
        "transactions",
        sa.Column("cleared", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transactions", "cleared")

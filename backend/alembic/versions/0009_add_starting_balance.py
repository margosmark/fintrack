"""add starting_balance nullable column to accounts.

Represents the account balance before the earliest imported transaction.
Nullable → existing rows get NULL; code treats NULL as Decimal("0.00").

Revision ID: 0009_add_account_starting_balance
Revises: 0008_add_transfer_pair_id
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_add_starting_balance"
down_revision: str | None = "0008_add_transfer_pair_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("starting_balance", sa.Numeric(18, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "starting_balance")

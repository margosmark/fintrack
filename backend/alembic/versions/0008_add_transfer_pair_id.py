"""add transfer_pair_id self-referential FK to transactions.

Adds a nullable FK from transactions.transfer_pair_id → transactions.id
so two transactions can be explicitly linked as a transfer pair (source
and destination). Nullable because most transactions aren't transfers.

Revision ID: 0008_add_transfer_pair_id
Revises: 0007_add_holding_security_id
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_add_transfer_pair_id"
down_revision: str | None = "0007_add_holding_security_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("transfer_pair_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_transactions_transfer_pair",
        "transactions",
        "transactions",
        ["transfer_pair_id"],
        ["id"],
    )
    op.create_index(
        "ix_transactions_transfer_pair_id",
        "transactions",
        ["transfer_pair_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_transfer_pair_id", table_name="transactions")
    op.drop_constraint("fk_transactions_transfer_pair", "transactions", type_="foreignkey")
    op.drop_column("transactions", "transfer_pair_id")

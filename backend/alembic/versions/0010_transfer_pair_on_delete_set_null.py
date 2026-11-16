"""add ON DELETE SET NULL to transactions.transfer_pair_id FK.

Allows deleting a transaction without violating the FK when another
transaction references it as a transfer pair — the reference is set to NULL
(one side of a pair no longer exists → unlink).

Revision ID: 0010_transfer_pair_on_delete
Revises: 0009_add_starting_balance
Create Date: 2026-08-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_transfer_pair_on_delete"
down_revision: str | None = "0009_add_starting_balance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_transactions_transfer_pair",
        "transactions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_transactions_transfer_pair",
        "transactions",
        "transactions",
        ["transfer_pair_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_transactions_transfer_pair",
        "transactions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_transactions_transfer_pair",
        "transactions",
        "transactions",
        ["transfer_pair_id"],
        ["id"],
    )

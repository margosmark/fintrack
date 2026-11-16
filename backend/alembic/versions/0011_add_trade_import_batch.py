"""add dedup_hash and import_batch_id to trades.

dedup_hash: SHA256 hex for import deduplication (different from transaction
dedup which uses description). import_batch_id: nullable FK linking staged
trades to their import batch (NULL for manually entered trades).

Revision ID: 0011_add_trade_import
Revises: 0010_transfer_pair_on_delete
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_add_trade_import"
down_revision: str | None = "0010_transfer_pair_on_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("dedup_hash", sa.Text(), nullable=True))
    op.create_index("ix_trades_dedup_hash", "trades", ["dedup_hash"])
    op.add_column(
        "trades",
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_trades_import_batch_id", "trades", ["import_batch_id"])
    op.create_foreign_key(
        "fk_trades_import_batch",
        "trades",
        "import_batches",
        ["import_batch_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_trades_import_batch", "trades", type_="foreignkey")
    op.drop_index("ix_trades_import_batch_id", table_name="trades")
    op.drop_column("trades", "import_batch_id")
    op.drop_index("ix_trades_dedup_hash", table_name="trades")
    op.drop_column("trades", "dedup_hash")

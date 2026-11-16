"""Add E2EE ciphertext columns (Phase 2).

Revision ID: 0024_add_e2ee_cipher_columns
Revises: 0023_rule_stop_on_match
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_add_e2ee_cipher_columns"
down_revision: str | None = "0023_rule_stop_on_match"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in (
        "payee_cipher",
        "raw_desc_cipher",
        "amount_cipher",
        "cad_amount_cipher",
        "payee_token",
    ):
        op.add_column("transactions", sa.Column(column, sa.Text(), nullable=True))
    op.create_index(
        "ix_transactions_user_id_payee_token",
        "transactions",
        ["user_id", "payee_token"],
    )
    op.add_column("accounts", sa.Column("name_cipher", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "encryption_required",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "encryption_required")
    op.drop_column("accounts", "name_cipher")
    op.drop_index("ix_transactions_user_id_payee_token", table_name="transactions")
    for column in (
        "payee_token",
        "cad_amount_cipher",
        "amount_cipher",
        "raw_desc_cipher",
        "payee_cipher",
    ):
        op.drop_column("transactions", column)

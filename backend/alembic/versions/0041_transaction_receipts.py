"""add transaction_receipts (M108 P4, docs/02 §transaction_receipts).

Revision ID: 0041_transaction_receipts
Revises: 0040_budget_carry
Create Date: 2026-10-06

Numbered 0041 (not 0040) because M110's `0040_budget_carry` was already the
head when this landed — two `0040_*` revisions both revising `0039` would
fork the alembic history into two heads (AGENTS §6: inspect the live heads
after every rebase, renumber before pushing).

ONE table, one row per transaction (the v1 contract is one image, no bulk —
docs/09 §Receipt images). `user_id` joins `_USER_OWNED_MODELS`, so account
deletion, the demo reset and the export registry reach it through the same
rules as every other ledger table without a special case.

`data_base64` is TEXT on purpose: `GET /export/full` renders every registered
row with `model_dump(mode="json")` and `json.dumps` it, neither of which can
carry `bytea` — a binary column would turn the export (and the encrypted
browser backup sharing its payload, docs/11) into a 500. This is the same
shape `backup_artifacts.ciphertext` and `vault_envelopes.wrapped_blob` use.

NOT related to M106's `transaction_events` rows, which this codebase also
calls "receipts" — those are append-only ACTIVITY receipts; this table holds
the photographed paper one. See the model's module docstring.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0041_transaction_receipts"
down_revision: str | None = "0040_budget_carry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transaction_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "transaction_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("data_base64", sa.Text(), nullable=False),
        # TimestampMixin: timezone-aware, server now() (docs/02 conventions) —
        # the same two columns 0039 writes for `transaction_events`.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("transaction_id", name="uq_transaction_receipts_transaction"),
    )
    # `user_id` is the only one that needs its own index: account deletion
    # and the demo reset sweep by user, while EVERY per-row lookup
    # (`_load_receipt_row`, the export join) is satisfied by the UNIQUE above.
    # A plain `transaction_id` index on top of that UNIQUE would be a second
    # identical B-tree — two indexes to answer one query (docs/39).
    op.create_index("ix_transaction_receipts_user_id", "transaction_receipts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_transaction_receipts_user_id", table_name="transaction_receipts")
    op.drop_table("transaction_receipts")

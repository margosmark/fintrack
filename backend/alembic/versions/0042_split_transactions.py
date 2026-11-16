"""add split-transaction columns (M107, docs/44 §1).

Revision ID: 0042_split_transactions
Revises: 0041_transaction_receipts
Create Date: 2026-10-06

FOUR columns on the existing `transactions` table, no new table (docs/44 D1):

* `is_split`           — TRUE iff this row has >= 1 child. Denormalized so the
                         LEDGER predicate is one boolean test instead of an
                         anti-join. Near-constant-false, so it gets NO index of
                         its own (docs/44 §1).
* `parent_split_id`    — CHILDREN only. A self-reference, exactly like
                         `transfer_pair_id` (`0008`/`0010`), so the export /
                         restore dependency graph already tolerates it — but
                         ON DELETE CASCADE (a batch discard or an account
                         deletion takes the children with it) and
                         DEFERRABLE INITIALLY DEFERRED, so a snapshot whose
                         children precede their parents still restores
                         (docs/44 §10 / D14).
* `split_checksum`     — PARENTS only: the exact amount the children must sum
                         to (set to `amount_native` at split time, cleared at
                         merge). Makes the D3 sum invariant checkable by a
                         constraint rather than by a test alone.
* `note`               — plaintext free text, VARCHAR(500), like
                         `raw_description` (docs/44 §1, docs/33).

Two named CHECKs pin the shape a row may hold:

* `ck_transactions_split_flag`     — a row is never both parent and child, and
                                     a parent always carries its checksum;
* `ck_transactions_split_checksum` — while split, the parent's amount cannot
                                     drift away from the checksum.

Renumber note (docs/44 §1, AGENTS §6): M110's `0040_budget_carry` and M108's
`0041_transaction_receipts` were already the head when this landed, so this
takes 0042. `alembic heads` must be re-run after any rebase and before
pushing — two revisions revising the same parent fork the history.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0042_split_transactions"
down_revision: str | None = "0041_transaction_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("is_split", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "transactions",
        sa.Column("parent_split_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column("split_checksum", sa.Numeric(18, 2), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column("note", sa.String(length=500), nullable=True),
    )

    # Self-reference with the two semantics the feature needs. `use_alter`
    # keeps it off CREATE TABLE so the self-FK is emitted as its own ALTER —
    # the same shape `0010` gave `transfer_pair_id`.
    op.create_foreign_key(
        "fk_transactions_parent_split",
        "transactions",
        "transactions",
        ["parent_split_id"],
        ["id"],
        ondelete="CASCADE",
        deferrable=True,
        initially="deferred",
    )

    op.create_check_constraint(
        "ck_transactions_split_flag",
        "transactions",
        "is_split = false OR (parent_split_id IS NULL AND split_checksum IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_transactions_split_checksum",
        "transactions",
        "is_split = false OR split_checksum = amount_native",
    )
    op.create_index("ix_transactions_parent_split_id", "transactions", ["parent_split_id"])


def downgrade() -> None:
    op.drop_index("ix_transactions_parent_split_id", table_name="transactions")
    op.drop_constraint("ck_transactions_split_checksum", "transactions", type_="check")
    op.drop_constraint("ck_transactions_split_flag", "transactions", type_="check")
    op.drop_constraint("fk_transactions_parent_split", "transactions", type_="foreignkey")
    op.drop_column("transactions", "note")
    op.drop_column("transactions", "split_checksum")
    op.drop_column("transactions", "parent_split_id")
    op.drop_column("transactions", "is_split")

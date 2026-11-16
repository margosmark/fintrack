"""M106: manual review state on transactions + the transaction_events receipt log (docs/02).

Revision ID: 0039_tx_review_events
Revises: 0038_saved_report_views (current head)
Create Date: 2026-10-06

Numbered 0039 (not 0038): M108's `0038_saved_report_views` landed on
origin/main first and is already the deployed head — this revises it, so
`alembic upgrade head` runs after the deployed head instead of forking a
second branch (the `0033_tx_cleared` / `0037_recurring` precedent).

Two nullable columns on `transactions` — `reviewed_at` / `reviewed_by` — whose
NULL pair IS "unreviewed", so an import seeds a review queue for free (staged
rows are written with neither set) and a partial index makes the queue count
one index probe instead of a scan of the tenant's ledger.

Plus `transaction_events`: one append-only receipt per mutation, per
transaction. `tx_id` carries NO foreign key on purpose — deleting a
transaction must keep its `deleted` receipt (the `mcp_audit.mcp_token_id`
rule, docs/40 §3) — while `user_id` does, so a deleted account takes the
log with it (`api/users.py::_USER_OWNED_MODELS`).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0039_tx_review_events"
down_revision: str | None = "0038_saved_report_views"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no server_default: NULL is a real value here ("unreviewed"),
    # not a missing one, and there is no existing row to backfill.
    op.add_column(
        "transactions",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("transactions", sa.Column("reviewed_by", sa.Integer(), nullable=True))
    op.create_index(
        "ix_transactions_user_unreviewed",
        "transactions",
        ["user_id"],
        postgresql_where=sa.text("reviewed_at IS NULL"),
    )

    op.create_table(
        "transaction_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        # NO FK — the receipt must outlive the row it describes.
        sa.Column("tx_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("event_kind", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        # NO FK — deleting an account must not delete the receipts it produced.
        sa.Column("actor", sa.Integer(), nullable=True),
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
    )
    op.create_index("ix_transaction_events_tx_id", "transaction_events", ["tx_id"])
    op.create_index("ix_transaction_events_user_id", "transaction_events", ["user_id"])
    op.create_index(
        "ix_transaction_events_tx_created",
        "transaction_events",
        ["tx_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_transaction_events_tx_created", table_name="transaction_events")
    op.drop_index("ix_transaction_events_user_id", table_name="transaction_events")
    op.drop_index("ix_transaction_events_tx_id", table_name="transaction_events")
    op.drop_table("transaction_events")
    op.drop_index("ix_transactions_user_unreviewed", table_name="transactions")
    op.drop_column("transactions", "reviewed_by")
    op.drop_column("transactions", "reviewed_at")

"""assistant proposals + append-only audit (docs/57)

Two tenant tables for the plain-English assistant: one `pending` row per ask
that implies a change (`assistant_proposals`), and the append-only
before/after log written in the SAME transaction as every applied or undone
mutation (`assistant_audit`). Both are registered in `SECURED_TABLES` and
secured here through `secure_registered_table` (docs/55) — a table created
after 0058 arms its own policy. No DB-level `kind`/`status` CHECK: the closed
mutation registry lives in `app/services/assistant.py`, so widening it is a
code + docs/57 change, not a constraint rebuild.

Revision ID: 0059_assistant
Revises: 0058_row_level_security
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.rls import secure_registered_table

revision: str = "0059_assistant"
down_revision: str | None = "0058_row_level_security"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assistant_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("question", sa.String(length=2000), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=96), nullable=False),
        sa.Column("target_label", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.String(length=300), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
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
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_assistant_proposals_user_id", "assistant_proposals", ["user_id"])
    op.create_index(
        "ix_assistant_proposals_user_created",
        "assistant_proposals",
        ["user_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "assistant_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "proposal_id",
            sa.Integer(),
            sa.ForeignKey("assistant_proposals.id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=96), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False),
        sa.Column("after", sa.JSON(), nullable=False),
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
    op.create_index("ix_assistant_audit_user_id", "assistant_audit", ["user_id"])
    op.create_index("ix_assistant_audit_proposal_id", "assistant_audit", ["proposal_id"])
    op.create_index(
        "ix_assistant_audit_user_created",
        "assistant_audit",
        ["user_id", sa.text("created_at DESC")],
    )

    # The second lock for both tables (docs/55 §Approach): tenant policy,
    # keyed to `current_tenant_id()`, registered in `SECURED_TABLES`.
    secure_registered_table(op.get_bind(), "assistant_proposals")
    secure_registered_table(op.get_bind(), "assistant_audit")


def downgrade() -> None:
    op.drop_index("ix_assistant_audit_user_created", table_name="assistant_audit")
    op.drop_index("ix_assistant_audit_proposal_id", table_name="assistant_audit")
    op.drop_index("ix_assistant_audit_user_id", table_name="assistant_audit")
    op.drop_table("assistant_audit")
    op.drop_index("ix_assistant_proposals_user_created", table_name="assistant_proposals")
    op.drop_index("ix_assistant_proposals_user_id", table_name="assistant_proposals")
    op.drop_table("assistant_proposals")

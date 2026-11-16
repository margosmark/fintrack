"""add tags, transaction_tags, rule_tags tables and rules.tag_action.

Revision ID: 0016_add_tags_rule_tags
Revises: 0015_add_mcp_tokens
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_add_tags_rule_tags"
down_revision: str | None = "0015_add_mcp_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- tags ---
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "name", name="uq_tags_user_name"),
    )

    # --- transaction_tags ---
    op.create_table(
        "transaction_tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "transaction_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("tag_id", sa.Integer(), sa.ForeignKey("tags.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("transaction_id", "tag_id", name="uq_transaction_tags_txn_tag"),
    )

    # --- rule_tags ---
    op.create_table(
        "rule_tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("rules.id"), nullable=False, index=True),
        sa.Column("tag_id", sa.Integer(), sa.ForeignKey("tags.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("rule_id", "tag_id", name="uq_rule_tags_rule_tag"),
    )

    # --- rules.tag_action ---
    op.add_column(
        "rules",
        sa.Column(
            "tag_action",
            sa.String(length=10),
            nullable=False,
            server_default="set",
        ),
    )
    op.create_check_constraint(
        "ck_rules_tag_action",
        "rules",
        "tag_action IN ('set', 'add')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_rules_tag_action", "rules", type_="check")
    op.drop_column("rules", "tag_action")
    op.drop_table("rule_tags")
    op.drop_table("transaction_tags")
    op.drop_table("tags")

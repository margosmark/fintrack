"""M80: mcp_tokens.policy + mcp_audit — read-only MCP tokens with receipts (docs/40).

Revision ID: 0034_mcp_policy_audit
Revises: 0031_auth_second_factor (current head — see 0033_sync_agreements'
note on re-parenting rather than forking a second branch)
Create Date: 2026-09-30

Two halves of one milestone:

1. `mcp_tokens.policy` — `full` | `read_only`, server default `full` so every
   pre-existing token keeps working, plus the CHECK the SQLAlchemy metadata
   declares (`ck_mcp_tokens_policy`; SA 2.0's Enum(native_enum=False) emits no
   constraint of its own).
2. `mcp_audit` — one receipt per MCP tool call (docs/40 §3). `mcp_token_id`
   carries NO foreign key on purpose: revoking a token must not delete the
   receipts it produced.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_mcp_policy_audit"
down_revision: str | None = "0031_auth_second_factor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Policy column (docs/40 §1). DEFAULT keeps legacy rows `full`.
    op.add_column(
        "mcp_tokens",
        sa.Column("policy", sa.String(length=16), nullable=False, server_default="full"),
    )
    op.create_check_constraint(
        "ck_mcp_tokens_policy",
        "mcp_tokens",
        "policy IN ('full', 'read_only')",
    )

    # 2. The receipt log (docs/40 §3).
    op.create_table(
        "mcp_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("mcp_token_id", sa.Integer(), nullable=True),  # no FK — docs/40
        sa.Column("tool", sa.String(length=100), nullable=False),
        sa.Column("args_hash", sa.String(length=64), nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_mcp_audit_user_created",
        "mcp_audit",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_audit_user_created", table_name="mcp_audit")
    op.drop_table("mcp_audit")
    op.drop_constraint("ck_mcp_tokens_policy", "mcp_tokens", type_="check")
    op.drop_column("mcp_tokens", "policy")

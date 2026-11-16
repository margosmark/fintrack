"""M90: sharing_links + sharing_audit — household sharing (docs/43 §3).

Revision ID: 0035_sharing_links
Revises: 0034_mcp_policy_audit (current head)
Create Date: 2026-10-02

Two tables, both carrying `user_id` = OWNER so AGENTS §2 (tenancy on every
domain table) holds literally:

1. `sharing_links` — one consent row per relationship. Active seats are
   enforced by PARTIAL UNIQUE indexes (I2): at most one `active` row per
   owner and per partner, so the accept race is decided by the database,
   not by application checks that can race. `token_hash` is UNIQUE so a
   replay resolves to its row deterministically (`409 sharing_invite_used`).
2. `sharing_audit` — the M80 receipt pattern (docs/40 §3) applied to the
   consent log. `actor_user_id` and `invite_id` carry NO foreign key on
   purpose: deleting an account must not delete the receipts it produced
   (the `mcp_audit.mcp_token_id` rule, D7).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_sharing_links"
down_revision: str | None = "0034_mcp_policy_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sharing_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "partner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("account_ids", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("token_hash", name="uq_sharing_links_token_hash"),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'expired', 'cancelled')",
            name="ck_sharing_links_status",
        ),
    )
    op.create_index("ix_sharing_links_user", "sharing_links", ["user_id"])
    op.create_index(
        "ix_sharing_links_partner_status", "sharing_links", ["partner_user_id", "status"]
    )
    op.create_index(
        "ix_sharing_links_user_created",
        "sharing_links",
        ["user_id", sa.text("created_at DESC")],
    )
    # I2 — partial uniques: NULL partner_user_id rows (pending) never collide.
    op.create_index(
        "uq_sharing_links_active_owner",
        "sharing_links",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_sharing_links_active_partner",
        "sharing_links",
        ["partner_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "sharing_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),  # no FK — docs/43 §3
        sa.Column("invite_id", sa.Integer(), nullable=True),  # no FK — docs/43 §3
        sa.Column("event", sa.String(length=64), nullable=False),
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
        "ix_sharing_audit_user_created",
        "sharing_audit",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_sharing_audit_user_created", table_name="sharing_audit")
    op.drop_table("sharing_audit")
    op.drop_index("uq_sharing_links_active_partner", table_name="sharing_links")
    op.drop_index("uq_sharing_links_active_owner", table_name="sharing_links")
    op.drop_index("ix_sharing_links_user_created", table_name="sharing_links")
    op.drop_index("ix_sharing_links_partner_status", table_name="sharing_links")
    op.drop_index("ix_sharing_links_user", table_name="sharing_links")
    op.drop_table("sharing_links")

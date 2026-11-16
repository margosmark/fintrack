"""add vault_envelopes (E2EE Phase 4 wrapped-DEK sync, docs/33).

Revision ID: 0027_vault_envelopes
Revises: 0026_mcp_token_expiry
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_vault_envelopes"
down_revision: str | None = "0026_mcp_token_expiry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# active  <=> revoked_at IS NULL  AND wrapped_blob IS NOT NULL
# revoked <=> revoked_at IS NOT NULL AND wrapped_blob IS NULL
_STATE_CHECK = (
    "(revoked_at IS NULL AND wrapped_blob IS NOT NULL) "
    "OR (revoked_at IS NOT NULL AND wrapped_blob IS NULL)"
)


def upgrade() -> None:
    op.create_table(
        "vault_envelopes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("device_id", sa.String(length=64), nullable=False, index=True),
        sa.Column("label", sa.String(length=64), nullable=True),
        sa.Column("wrapped_blob", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_STATE_CHECK, name="ck_vault_envelopes_state"),
    )
    # Partial unique: one ACTIVE envelope per device; a revoked row does not
    # block re-creating the device (docs/33 Phase 4 table).
    op.create_index(
        "uq_vault_envelopes_active",
        "vault_envelopes",
        ["user_id", "device_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_vault_envelopes_active", table_name="vault_envelopes")
    op.drop_table("vault_envelopes")

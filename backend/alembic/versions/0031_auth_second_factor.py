"""M74 auth hardening: second-factor + SSO tables (docs/38).

Revision ID: 0031_auth_second_factor
Revises: 0030_notification_delivery
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_auth_second_factor"
down_revision: str | None = "0033_sync_agreements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "totp_factors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("secret", sa.String(), nullable=True),
        sa.Column("pending_secret", sa.String(), nullable=True),
        sa.Column("last_used_step", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.UniqueConstraint("user_id", name="uq_totp_factors_user"),
    )

    op.create_table(
        "recovery_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.UniqueConstraint("user_id", "code_hash", name="uq_recovery_codes_user_hash"),
    )

    op.create_table(
        "webauthn_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("transports", sa.String(), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.UniqueConstraint("credential_id", name="uq_webauthn_credentials_credential_id"),
    )

    op.create_table(
        "oidc_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.UniqueConstraint("issuer", "subject", name="uq_oidc_links_issuer_subject"),
    )


def downgrade() -> None:
    op.drop_table("oidc_links")
    op.drop_table("webauthn_credentials")
    op.drop_table("recovery_codes")
    op.drop_table("totp_factors")

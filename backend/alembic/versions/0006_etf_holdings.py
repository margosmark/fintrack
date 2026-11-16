"""etf_holdings — per-fund composition data (M17, docs/02, docs/07).

Top holdings of ETFs/mutual funds with asset class / region / sector
weights; source=manual rows are replaced only by PUT, source=yfinance
rows only by the import endpoint (manual-first convention).

Revision ID: 0006_etf_holdings
Revises: 0005_fundamentals
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_etf_holdings"
down_revision: str | None = "0005_fundamentals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_asset_class_enum = sa.Enum(
    "stock",
    "bond",
    "cash",
    "commodity",
    "crypto",
    "other",
    name="etf_asset_class",
    native_enum=False,
    length=16,
)
_source_enum = sa.Enum(
    "yfinance", "manual", name="fundamentals_source", native_enum=False, length=16
)


def upgrade() -> None:
    op.create_table(
        "etf_holdings",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("holding_symbol", sa.String(length=20), nullable=True),
        sa.Column("holding_name", sa.String(length=160), nullable=False),
        sa.Column("asset_class", _asset_class_enum, nullable=False, server_default="other"),
        sa.Column("region", sa.String(length=80), nullable=True),
        sa.Column("country", sa.String(length=80), nullable=True),
        sa.Column("sector", sa.String(length=80), nullable=True),
        sa.Column("source", _source_enum, nullable=False),
        sa.Column("weight_pct", sa.Numeric(7, 4), nullable=False),
        # Same underlying company may appear twice via different share
        # classes — deliberately NO unique beyond id.
        sa.CheckConstraint("weight_pct > 0 AND weight_pct <= 100", name="ck_etf_holdings_weight"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
    )
    op.create_index("ix_etf_holdings_user_id", "etf_holdings", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_etf_holdings_user_id", table_name="etf_holdings")
    op.drop_table("etf_holdings")

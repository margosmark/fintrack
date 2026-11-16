"""fundamentals — cached per-security research data (M16, docs/02, docs/07).

One row per security (UNIQUE security_id); source=yfinance rows are
refreshed on a 7-day window, source=manual rows are never overwritten.

Revision ID: 0005_fundamentals
Revises: 0004_pg_trgm_search
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_fundamentals"
down_revision: str | None = "0004_pg_trgm_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_source_enum = sa.Enum(
    "yfinance", "manual", name="fundamentals_source", native_enum=False, length=16
)


def upgrade() -> None:
    op.create_table(
        "fundamentals",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("trailing_eps", sa.Numeric(20, 8), nullable=True),
        sa.Column("pe_ratio", sa.Numeric(20, 8), nullable=True),
        sa.Column("market_cap", sa.Numeric(30, 2), nullable=True),
        sa.Column("dividend_yield_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("beta", sa.Numeric(10, 4), nullable=True),
        sa.Column("sector", sa.String(length=80), nullable=True),
        sa.Column("industry", sa.String(length=120), nullable=True),
        sa.Column("country", sa.String(length=80), nullable=True),
        sa.Column("fifty_two_week_high", sa.Numeric(20, 8), nullable=True),
        sa.Column("fifty_two_week_low", sa.Numeric(20, 8), nullable=True),
        sa.Column("source", _source_enum, nullable=False),
        sa.Column("fetched_at", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("security_id", name="uq_fundamentals_security"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
    )
    op.create_index("ix_fundamentals_user_id", "fundamentals", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_fundamentals_user_id", table_name="fundamentals")
    op.drop_table("fundamentals")

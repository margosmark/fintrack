"""add holding_security_id FK to etf_holdings (recursive enrichment).

Adds a nullable FK from etf_holdings.holding_security_id → securities.id
so holdings can link directly to their security detail pages without
query-time symbol resolution. Nullable because not all holdings are
tracked securities (e.g., foreign tickers not in the user's universe).

Revision ID: 0007_add_holding_security_id
Revises: 0006_etf_holdings
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_add_holding_security_id"
down_revision: str | None = "0006_etf_holdings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "etf_holdings",
        sa.Column("holding_security_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_etf_holdings_holding_security_id",
        "etf_holdings",
        "securities",
        ["holding_security_id"],
        ["id"],
    )
    op.create_index(
        "ix_etf_holdings_holding_security_id",
        "etf_holdings",
        ["holding_security_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_etf_holdings_holding_security_id", table_name="etf_holdings")
    op.drop_constraint("fk_etf_holdings_holding_security_id", "etf_holdings", type_="foreignkey")
    op.drop_column("etf_holdings", "holding_security_id")

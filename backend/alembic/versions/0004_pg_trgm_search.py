"""pg_trgm fuzzy search: extension + GIN trigram index on transactions.normalized_payee.

Backs GET /transactions/similar and GET /search (M12, docs/04, docs/08).
CREATE EXTENSION needs a superuser — compose and CI Postgres users both are.

Revision ID: 0004_pg_trgm_search
Revises: 0003_watchlist
Create Date: 2026-08-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_pg_trgm_search"
down_revision: str | None = "0003_watchlist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trgm_transactions_payee "
        "ON transactions USING gin (normalized_payee gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_trgm_transactions_payee")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")

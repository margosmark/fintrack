"""prices.fetched_at — real provider-fetch evidence, NO backfill (t_bd5ec95f)

Revision ID: 0057_price_fetched_at
Revises: 0056_dividend_retry_backoff
Create Date: 2026-10-28

Option A: a provable "fetched N ago" needs an instant recorded when a provider
response ACTUALLY arrived. `Price.date` is the market date, `created_at` is row
insertion and `updated_at` is any mutation (a manual edit included) — none can
serve as fetch provenance. This migration adds the column only: NULLABLE, no
server_default, and deliberately NO backfill. Existing rows carry no fetch
evidence; stamping them from created_at/updated_at would fabricate exactly the
claim this column exists to make honest (docs/07 §Fetch provenance).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0057_price_fetched_at"
down_revision: str | None = "0056_dividend_retry_backoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "prices",
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("prices", "fetched_at")

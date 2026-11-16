"""add users.recurring_intent_tag_ids (M174, docs/09 § Tags that mark recurring).

Revision ID: 0053_recurring_intent_tags
Revises: 0052_benchmark_prices
Create Date: 2026-10-12

ONE column, no table: which of the user's OWN tags mean "this is a recurring
commitment" is a per-user preference like every other `users` column (docs/02),
so it roams across devices with them and `PATCH /users/me` is its only write —
exactly the shape M108-P2 gave `saved_report_views` and M109 gave
`forecast_scenarios`. A JSON `[]` (never NULL) means "the feature is off", the
same absent-vs-empty rule `backup_frequency = off` follows.

No FK and no CHECK: the values are `tags.id`s in a JSON document, so the
database cannot express the reference — ownership is enforced at the write seam
(`api/users.py::update_me` rejects an id the caller does not own with
`422 invalid_recurring_intent_tag_ids`), which is the contract
`saved_report_views`' closed key set also lives under.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0053_recurring_intent_tags"
down_revision: str | None = "0052_benchmark_prices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "recurring_intent_tag_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "recurring_intent_tag_ids")

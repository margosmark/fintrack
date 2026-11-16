"""Add rules.stop_on_match (continue vs stop processing).

Revision ID: 0023_rule_stop_on_match
Revises: 0022_tags_ts_defaults
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_rule_stop_on_match"
down_revision: str | None = "0022_tags_ts_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rules",
        sa.Column(
            "stop_on_match",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
    )


def downgrade() -> None:
    op.drop_column("rules", "stop_on_match")

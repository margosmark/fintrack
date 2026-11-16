"""add users.dashboard_preset (M97, docs/09 § Dashboard layout presets).

Revision ID: 0036_dashboard_preset
Revises: 0035_sharing_links
Create Date: 2026-10-04

ONE column, no table: the preset is a per-user preference like every other
`users` column (docs/02), so it roams across devices with them. `server_default`
backfills every existing row to `overview` — the first-visit default the G1
tour teaches on — and the CHECK keeps any client (or MCP write) to the three
fixed orders. Nothing about a card is stored: a preset only reorders, which is
why no new table, no JSON blob and no per-card state is needed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036_dashboard_preset"
down_revision: str | None = "0035_sharing_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "dashboard_preset",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'overview'"),
        ),
    )
    op.create_check_constraint(
        "ck_users_dashboard_preset",
        "users",
        "dashboard_preset IN ('overview','import_first','budget_first')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_dashboard_preset", "users", type_="check")
    op.drop_column("users", "dashboard_preset")

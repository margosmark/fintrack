"""add users.saved_report_views (M108 P2, docs/02 §users).

Revision ID: 0038_saved_report_views
Revises: 0037_recurring
Create Date: 2026-10-06

ONE column, no table — exactly the M97 `dashboard_preset` reasoning applied to
a SET instead of a single enum (docs/09 §Saved report views): a saved report is
a per-user preference, so it belongs on the `users` row and roams with it.
`JSON NOT NULL DEFAULT '[]'` means "no saved views" is a real, complete shape
(the same "never interpret absent vs empty" rule M56/M97 pinned for `off` and
`overview`), and there is deliberately no per-account variant: a saved view
holds a date range and a chart variant, never an account scope.

The shape of each entry (`id`, `title`, `tab`, `state`) is validated at the
API seam by `app/schemas/users.py::SavedReportView`, not by a CHECK
constraint — the tab set is an application contract shared with
`frontend/src/lib/reportTabs.ts`, and a constraint would duplicate it in SQL.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0038_saved_report_views"
down_revision: str | None = "0037_recurring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "saved_report_views",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "saved_report_views")

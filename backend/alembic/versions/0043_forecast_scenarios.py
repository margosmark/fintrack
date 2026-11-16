"""add users.forecast_scenarios (M109, docs/45 §2).

Revision ID: 0043_forecast_scenarios
Revises: 0042_split_transactions
Create Date: 2026-10-06

Numbered 0043 (not 0042): M107's `0042_split_transactions` was already the head
when this landed after a rebase — two `0042_*` revisions both revising `0041`
would fork the alembic history into two heads (AGENTS §6: inspect the live
heads after every rebase, renumber before pushing).

ONE column, no table — the M108-P2 `0038_saved_report_views` reasoning applied
to what-if scenarios instead of chart presets: a scenario is per-user content
that roams with the account, so it belongs on the `users` row. `JSON NOT NULL
DEFAULT '[]'` means "no saved scenarios" is a real, complete shape (the same
"never interpret absent vs empty" rule M56/M97/M108 pinned for `off`,
`overview` and `[]`).

The entry shape — `{id, name, events[]}` where each event is one of five typed
life events — is validated at the API seam by
`app/schemas/forecast.py::ScenarioIn/ScenarioOut`, not by a CHECK constraint:
the event set is an application contract shared with the UI, and a constraint
would duplicate it in SQL (AGENTS §2 — enums become constraints rebuilds, not
table rewrites; this is a discriminated union, not an enum column).

Export/restore reach it through the derived table set (docs/11 §File list): a
JSON column on `users` travels inside `users.csv` with no registry change.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0043_forecast_scenarios"
down_revision: str | None = "0042_split_transactions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "forecast_scenarios",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "forecast_scenarios")

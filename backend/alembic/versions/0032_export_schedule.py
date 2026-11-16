"""add scheduled-export schedule columns (M75, docs/11 § "Scheduled export").

Revision ID: 0032_export_schedule
Revises: 0030_notification_delivery
Create Date: 2026-09-30

Five `users` columns, the M56 schedule shape mirrored: the schedule roams
across devices with every other preference, `server_default` keeps the ALTER
valid for existing rows, and the CHECKs make the invariants survive any
client. No table is created — the scheduled ZIP lives in the browser's local
storage (IndexedDB), because a plaintext archive belongs on the user's own
machine, not in a third server-side copy (docs/11 § Scheduled export).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032_export_schedule"
down_revision: str | None = "0030_notification_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEDULE_COLUMNS = (
    ("export_frequency", sa.String(length=16), "'off'"),
    ("export_hour", sa.SmallInteger(), "3"),
    ("export_weekday", sa.SmallInteger(), "0"),
    ("export_day_of_month", sa.SmallInteger(), "1"),
    ("export_keep_last_n", sa.Integer(), "5"),
)

_SCHEDULE_CHECKS = (
    ("ck_users_export_frequency", "export_frequency IN ('off','daily','weekly','monthly')"),
    ("ck_users_export_hour", "export_hour >= 0 AND export_hour <= 23"),
    ("ck_users_export_weekday", "export_weekday >= 0 AND export_weekday <= 6"),
    ("ck_users_export_day_of_month", "export_day_of_month >= 1 AND export_day_of_month <= 28"),
    ("ck_users_export_keep_last_n", "export_keep_last_n >= 1 AND export_keep_last_n <= 20"),
)


def upgrade() -> None:
    for name, type_, default in _SCHEDULE_COLUMNS:
        op.add_column(
            "users",
            sa.Column(name, type_, nullable=False, server_default=sa.text(default)),
        )
    for name, condition in _SCHEDULE_CHECKS:
        op.create_check_constraint(name, "users", condition)


def downgrade() -> None:
    for name, _condition in reversed(_SCHEDULE_CHECKS):
        op.drop_constraint(name, "users", type_="check")
    for name, _type, _default in reversed(_SCHEDULE_COLUMNS):
        op.drop_column("users", name)

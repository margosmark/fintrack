"""add scheduled-backup schedule columns + backup_artifacts (M56, docs/11).

Revision ID: 0029_backup_artifacts
Revises: 0028_add_inbox_reads
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_backup_artifacts"
down_revision: str | None = "0028_add_inbox_reads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The five schedule columns live on `users` so the schedule roams across
# devices (docs/11 § Schedule model). server_default keeps the ALTER valid for
# existing rows; the CHECKs make the invariants survive any client.
_SCHEDULE_COLUMNS = (
    ("backup_frequency", sa.String(length=16), "'off'"),
    ("backup_hour", sa.SmallInteger(), "3"),
    ("backup_weekday", sa.SmallInteger(), "0"),
    ("backup_day_of_month", sa.SmallInteger(), "1"),
    ("backup_keep_last_n", sa.Integer(), "5"),
)

_SCHEDULE_CHECKS = (
    ("ck_users_backup_frequency", "backup_frequency IN ('off','daily','weekly','monthly')"),
    ("ck_users_backup_hour", "backup_hour >= 0 AND backup_hour <= 23"),
    ("ck_users_backup_weekday", "backup_weekday >= 0 AND backup_weekday <= 6"),
    ("ck_users_backup_day_of_month", "backup_day_of_month >= 1 AND backup_day_of_month <= 28"),
    ("ck_users_backup_keep_last_n", "backup_keep_last_n >= 1 AND backup_keep_last_n <= 20"),
)


def upgrade() -> None:
    for name, type_, default in _SCHEDULE_COLUMNS:
        op.add_column(
            "users",
            sa.Column(name, type_, nullable=False, server_default=sa.text(default)),
        )
    for name, condition in _SCHEDULE_CHECKS:
        op.create_check_constraint(name, "users", condition)

    op.create_table(
        "backup_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # Idempotency key of the client-side scheduler: two open tabs racing
        # for the same period upsert into ONE row (docs/11 artifact format).
        sa.Column("period", sa.String(length=16), nullable=False),
        # Opaque `v1.<base64(iv)>.<base64(ciphertext)>` field envelope, AES-GCM
        # under the caller's vault DEK — the server never decodes it.
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "period", name="uq_backup_artifacts_user_period"),
    )


def downgrade() -> None:
    op.drop_table("backup_artifacts")
    for name, _condition in reversed(_SCHEDULE_CHECKS):
        op.drop_constraint(name, "users", type_="check")
    for name, _type, _default in reversed(_SCHEDULE_COLUMNS):
        op.drop_column("users", name)

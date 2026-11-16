"""Restore missing timestamp server defaults on tag tables.

Revision ID: 0022_tags_ts_defaults
Revises: 0021_add_bills_alerts

Migration 0016 created tags/rule_tags/transaction_tags timestamp columns
NOT NULL without server_default (every other migration has it), so every
insert into those tables failed with NotNullViolation — surfacing as a bogus
409 duplicate_tag on POST /tags.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_tags_ts_defaults"
down_revision: str | None = "0021_add_bills_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = ("tags", "rule_tags", "transaction_tags")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "created_at", server_default=sa.func.now(), existing_nullable=False)
        op.alter_column(table, "updated_at", server_default=sa.func.now(), existing_nullable=False)


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "created_at", server_default=None, existing_nullable=False)
        op.alter_column(table, "updated_at", server_default=None, existing_nullable=False)

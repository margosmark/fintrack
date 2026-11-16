"""multi-term match rules: `match_logic` + `bill_reminder_match_terms` (M121).

Revision ID: 0046_multi_term_match
Revises: 0045_merge_dup_securities
Create Date: 2026-10-08

One column on `bill_reminders` and one CHILD table (docs/25 §M121 D1):

* `bill_reminders.match_logic` (`any` | `all`, NOT NULL, server default
  `'any'`) — ADD COLUMN with a non-volatile default backfills every existing
  row in place, so a pre-M121 bill reads back as OR-mode with NO data fix-up;
* `bill_reminder_match_terms` — one row per term with an order column, the
  terms kept OUT of a JSON column per docs/02's schema discipline (bounded
  columns are the contract, and `ck_…_term_len` makes the 120-char bound true
  in the TABLE). `position 0` is terms[0] and mirrors `match_term`.

`BACKFILL_SQL` then copies every existing bill's `match_term` into position 0,
guarded by `NOT EXISTS` so a re-run (or a bill that already has terms) can
never duplicate a term or change an answer. It is a module-level constant on
purpose: `tests/test_m121_multi_term.py` executes THIS statement against a
pre-M121-shaped row, so the backfill the migration runs is the backfill that
is tested, not a paraphrase of it.

`match_term` itself is untouched — the legacy wire field keeps reading as
terms[0] (docs/25 §M121 D5).

No index beyond the UNIQUE: `uq_bill_reminder_match_terms_bill_position` has
`bill_reminder_id` as its leading column, so "the terms of bill N, in order"
is an index scan already.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0046_multi_term_match"
down_revision: str | None = "0045_merge_dup_securities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every pre-M121 bill keeps exactly one term — its own `match_term` — at
# position 0, under mode 'any'. Idempotent by NOT EXISTS.
BACKFILL_SQL = """
INSERT INTO bill_reminder_match_terms (bill_reminder_id, position, term)
SELECT br.id, 0, br.match_term
FROM bill_reminders br
WHERE br.match_term IS NOT NULL
  AND br.match_term <> ''
  AND NOT EXISTS (
    SELECT 1 FROM bill_reminder_match_terms t WHERE t.bill_reminder_id = br.id
  )
"""


def upgrade() -> None:
    op.add_column(
        "bill_reminders",
        sa.Column(
            "match_logic",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'any'"),
        ),
    )
    op.create_table(
        "bill_reminder_match_terms",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bill_reminder_id",
            sa.Integer(),
            sa.ForeignKey(
                "bill_reminders.id",
                name="fk_bill_reminder_match_terms_bill",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("term", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "bill_reminder_id",
            "position",
            name="uq_bill_reminder_match_terms_bill_position",
        ),
        sa.CheckConstraint(
            "length(term) BETWEEN 1 AND 120",
            name="ck_bill_reminder_match_terms_term_len",
        ),
        sa.CheckConstraint("position >= 0", name="ck_bill_reminder_match_terms_position"),
    )
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    op.drop_table("bill_reminder_match_terms")
    op.drop_column("bill_reminders", "match_logic")

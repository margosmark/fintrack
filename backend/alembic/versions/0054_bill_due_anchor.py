"""bill schedule anchor: `bill_reminders.anchor_day` + repair impossible due dates (M180).

Revision ID: 0054_bill_due_anchor
Revises: 0053_recurring_intent_tags
Create Date: 2026-10-12

One nullable column, one CHECK, and a data fix-up (docs/25 §M180, docs/02):

* `anchor_day` — the day-of-month the schedule REPEATS on, so month-end
  clamping is a property of the projection instead of the stored schedule.
  Advancing from the previously clamped date lost the schedule (Jan 31 →
  Feb 28 → Mar 28, the 31st gone for good); the anchor makes the same bill
  answer Feb 28 → **Mar 31** → Apr 30 (`services/bill_schedule.py`).
  Nullable by design: `NULL` = "the stored date's own day is the anchor",
  which is exactly the pre-M180 behaviour, so no row needs a fabricated value
  and a row written outside the API (a fixture, a seed) can never clamp
  differently from before.

* `ck_bill_reminders_anchor_day` — `anchor_day IS NULL OR (anchor_day BETWEEN
  1 AND 31)`. The bound lives in the TABLE (AGENTS §2), not only at whichever
  seam happens to write the column.

`ANCHOR_SQL` and `REPAIR_SQL` do two idempotent things, in this order:

1. **Every ISO-shaped stored date gets its own day as the anchor** (capped at
   31). For a row whose date is valid this is the honest best possible answer —
   it IS the day the schedule repeats on; for a row whose date is impossible
   (`2026-04-31`, `2029-02-29`, the values the pre-M180 `_advance_due_date`
   wrote) the day component is the ONLY record of the intended schedule, so it
   is read before the repair below rewrites the date.
2. **A stored date that is not a real calendar date is clamped to the last day
   of its own month** (`2026-04-31` → `2026-04-30`, `2029-02-29` →
   `2029-02-28`). Those strings are what made `mark-paid` answer a 500
   (`date.fromisoformat` refuses them) and silently dropped the bill from the
   cash-flow calendar; after this they are real dates that advance normally,
   and the anchor keeps the day the user actually chose. The month length comes
   from the month's OWN first-of-month date, so leap years are the database's
   answer and not a table of our own.

Both statements are module-level SQL constants on purpose:
`tests/test_bill_schedule.py` executes THESE statements (`BACKFILL_STATEMENTS`,
in this order) against pre-M180-shaped rows, so the backfill the migration runs
is the backfill that is tested, not a paraphrase of it. A row whose
`next_due_date` is not ISO-shaped at all keeps its value (nothing here can
invent a date out of garbage) and keeps `anchor_day` NULL — every read path
already skips such a row instead of raising.

No index: nothing queries `anchor_day` on its own.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0054_bill_due_anchor"
down_revision: str | None = "0053_recurring_intent_tags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every ISO-shaped stored date names the day its schedule repeats on. The month
# pattern is pinned to a real 01-12 so the `::int` cast below can never see a
# half-parsed string; the cap at 1..31 matches the CHECK constraint added in
# `upgrade()`. Idempotent by `anchor_day IS NULL`.
ANCHOR_SQL = r"""
UPDATE bill_reminders
SET anchor_day = GREATEST(LEAST(substring(next_due_date, 9, 2)::int, 31), 1)
WHERE anchor_day IS NULL
  AND next_due_date ~ '^[0-9]{4}-(0[1-9]|1[0-2])-[0-9]{2}$'
"""

# A day past the target month's length is not a date: clamp it to that month's
# last day. `to_date` is used ONLY on the month's first day (always a real date
# once the pattern above has pinned the month), and the comparison is the
# strict "does the day exist" test — so the statement is a no-op for every
# valid row and can be re-run freely.
REPAIR_SQL = r"""
UPDATE bill_reminders
SET next_due_date = to_char(
      to_date(substring(next_due_date, 1, 7) || '-01', 'YYYY-MM-DD')
        + interval '1 month' - interval '1 day',
      'YYYY-MM-DD')
WHERE next_due_date ~ '^[0-9]{4}-(0[1-9]|1[0-2])-[0-9]{2}$'
  AND substring(next_due_date, 9, 2)::int > EXTRACT(
        DAY FROM
        to_date(substring(next_due_date, 1, 7) || '-01', 'YYYY-MM-DD')
          + interval '1 month' - interval '1 day')::int
"""

BACKFILL_STATEMENTS: tuple[str, ...] = (ANCHOR_SQL, REPAIR_SQL)


def upgrade() -> None:
    op.add_column("bill_reminders", sa.Column("anchor_day", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_bill_reminders_anchor_day",
        "bill_reminders",
        "anchor_day IS NULL OR (anchor_day BETWEEN 1 AND 31)",
    )
    for statement in BACKFILL_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.drop_constraint("ck_bill_reminders_anchor_day", "bill_reminders", type_="check")
    op.drop_column("bill_reminders", "anchor_day")

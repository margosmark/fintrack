"""merge duplicate securities rows (M117, docs/02 §securities).

Revision ID: 0045_merge_dup_securities
Revises: 0044_bill_match_rules
Create Date: 2026-10-08

Data repair, not schema: no table, column or constraint changes —
``uq_securities_symbol UNIQUE (user_id, symbol, exchange)`` stays exactly as
docs/02 pins it (AGENTS §2: out of scope to change the data model here).

Why it is needed: every write path now normalizes ``symbol``/``exchange``
(``POST``/``PATCH /securities`` did already; the investment-CSV import did
not), but nothing back-filled rows written before that. A CSV cell reading
``veqt`` was matched and stored verbatim, so it could sit beside the ``VEQT``
row created through the API — two rows for one listing, splitting its trades,
prices and ACB pool across a stale twin that valuation could pick at random.

``app.services.security_dedup.repair_securities`` does the work and is what
the tests exercise: merge each group of rows whose *normalized*
``(user_id, symbol, exchange)`` collides (keeper = most price history, ties to
the lowest id) onto one row, repoint every reference, then normalize the
survivors. A group whose ``acb_pools`` would collide is reported and skipped
rather than merged — two pools for one account+security means cost basis was
computed twice, and choosing a winner would corrupt ACB (docs/06).

Idempotent: a second run finds one row per normalized key and reports
``merged_groups=0``.

``upgrade()`` commits through a Session joined to the migration connection,
so the repair lands inside alembic's own transaction; ``downgrade()`` is a
no-op because a merge is not reversible.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0045_merge_dup_securities"
down_revision: str | None = "0044_bill_match_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from sqlmodel import Session

    from app.services.security_dedup import repair_securities

    session = Session(bind=op.get_bind())
    try:
        report = repair_securities(session)
        # The Session joined alembic's in-flight connection, so committing
        # here releases the savepoint it opened and leaves the work in the
        # migration's own transaction for alembic to commit.
        session.commit()
    finally:
        session.close()
    # stdout, not logging: the merge summary must be visible in
    # `alembic upgrade` output — that is the only place an operator sees
    # which ids were merged where.
    print(f"security repair:\n{report.summary()}")


def downgrade() -> None:
    """No-op: a duplicate merge is not reversible, and no schema changed."""

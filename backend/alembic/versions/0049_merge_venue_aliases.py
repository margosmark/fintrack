"""merge venue-spelling duplicate securities rows (M164, docs/07 §Venue aliases).

Revision ID: 0049_merge_venue_aliases
Revises: 0048_securities_dead_symbol
Create Date: 2026-10-11

Data repair, not schema: no table, column or constraint changes —
``uq_securities_symbol UNIQUE (user_id, symbol, exchange)`` stays exactly as
docs/02 pins it.

Why a SECOND run of the M117 repair is needed: `services/venue_codes.py` now
holds the venue-alias table (the provider spells one venue two ways — live
2026-10-11 it answered `SHH` for `600900.SS` on one run and `SHSE` on another,
and answers a bare `7182` as `TOKY1` while `7182.T` says `JPX`). Migration
0045 grouped on the RAW exchange code, so those rows never collided and the
"two listings" one of them implied kept every bare-stem fund leg unlinked
behind an ambiguity that was not one (prod: tenant 343's `600900` and
`PNDORA` legs).

`app.services.security_dedup.repair_securities` now groups on the CANONICAL
venue code and rewrites the survivor's exchange to it, so this revision simply
runs it again: same keeper rule (most price history, tie → lowest id), same
move/repoint machinery, same ACB refusal (docs/06), same idempotence — and no
opinion at all about codes the table does not know.

``upgrade()`` commits through a Session joined to the migration connection,
exactly like 0045; ``downgrade()`` is a no-op because a merge is not
reversible.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0049_merge_venue_aliases"
down_revision: str | None = "0048_securities_dead_symbol"
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
    print(f"security repair (venue aliases):\n{report.summary()}")


def downgrade() -> None:
    """No-op: a duplicate merge is not reversible, and no schema changed."""

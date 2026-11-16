"""widen accounts.type to the full 11-value set under `ck_accounts_type` (docs/14 C1, M9).

The 0001 revision declared accounts.type as VARCHAR via
`sa.Enum(*values, native_enum=False)` — SQLAlchemy >= 1.4 defaults
`create_constraint=False`, so NO CHECK constraint existed to widen. This
revision inspects the catalog live (dropping a legacy CHECK guarding the
column if one was added out-of-band) and creates the full-set guard under a
stable explicit name (`ck_accounts_type`).

Downgrade narrows `ck_accounts_type` back to the nine 0001 values.
Assumption (documented per contract): no rows with type 'lira'/'rdsp' exist —
the narrowed CHECK would reject them otherwise.

Revision ID: 0002_widen_account_types
Revises: 0001_initial
Create Date: 2026-08-24
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_widen_account_types"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_VALUES = (
    "chequing",
    "savings",
    "credit_card",
    "line_of_credit",
    "brokerage_non_registered",
    "rrsp",
    "tfsa",
    "fhsa",
    "resp",
)
NEW_VALUES = (*OLD_VALUES, "lira", "rdsp")

CONSTRAINT_NAME = "ck_accounts_type"


def _find_type_check_name(conn: sa.Connection) -> str | None:
    """Locate a live CHECK constraint guarding accounts.type, if any."""
    rows = conn.execute(
        sa.text(
            "SELECT conname, pg_get_constraintdef(oid) AS def "
            "FROM pg_constraint "
            "WHERE conrelid = 'accounts'::regclass AND contype = 'c'"
        )
    ).all()
    matches = [name for name, definition in rows if re.search(r"\btype\b", definition)]
    if len(matches) > 1:
        raise RuntimeError(
            f"Expected at most one CHECK constraint on accounts.type, found {matches!r}"
        )
    return matches[0] if matches else None


def _recreate_check(values: tuple[str, ...], drop_name: str | None) -> None:
    if drop_name is not None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", drop_name):
            raise RuntimeError(f"Unsafe constraint name from catalog: {drop_name!r}")
        op.execute(sa.text(f'ALTER TABLE accounts DROP CONSTRAINT "{drop_name}"'))
    op.create_check_constraint(
        CONSTRAINT_NAME,
        "accounts",
        sa.text("type IN ({})".format(", ".join(f"'{v}'" for v in values))),
    )


def upgrade() -> None:
    conn = op.get_bind()
    existing = _find_type_check_name(conn)
    _recreate_check(NEW_VALUES, existing)


def downgrade() -> None:
    # Assumes no 'lira'/'rdsp' rows exist (see module docstring).
    _recreate_check(OLD_VALUES, CONSTRAINT_NAME)

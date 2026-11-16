"""index the tenancy key on four hot tables (docs/02, docs/34).

Revision ID: 0050_tenancy_idx
Revises: 0049_merge_venue_aliases
Create Date: 2026-10-12

A code review found that `user_id` — the predicate on essentially every domain
query in this app (AGENTS.md §2: tenancy is non-negotiable) — carried NO index
on four tables that are read by `user_id` alone. Postgres was answering each of
them with a sequential scan that grows with the TABLE, not with the rows the
caller wanted.

WHY THESE FOUR, AND WHY NOT THE OTHERS

* `acb_pools` — the important one. `app/tax/acb.py::rebuild_user_pools` opens
  with `SELECT ... WHERE user_id = ?` to DELETE the stale pools, and it runs on
  EVERY trade create, update and delete (11 call sites in `app/api/trades.py`).
  AGENTS.md §2 makes it the single sanctioned write path for the table, so it
  should be the best-indexed statement in the app; it was the worst. The table's
  only other index is `uq_acb_pools_account_security (account_id, security_id)`,
  whose LEADING column is `account_id`, so it cannot serve this at all.

* `prices` — read by `user_id` on every valuation: `app/api/prices.py`,
  `app/api/_price_stream.py` (twice — once for the last close, once for the
  history window) and `app/services/reports.py`.

* `rules` — `app/api/rules.py` and `app/services/rules_engine.py` list a
  tenant's rules on every categorization pass, which is every import.

* `import_batches` — `app/api/imports.py` and `app/api/transactions.py` resolve
  a batch by id within the tenant. It had `ix_import_batches_account_id` only.

DELIBERATELY NOT INDEXED — two tables that LOOK like the same gap and are not:

* `securities` — its `uq_securities_symbol (user_id, symbol, exchange)` already
  has `user_id` as the LEADING column, so `WHERE user_id = ?` is served by it.
  A second index would be pure write amplification on the hottest upsert path.

* `learned_payees` — same shape: `uq_learned_payees_user_payee (user_id,
  normalized_payee)` leads with `user_id`.

Both were verified against the live schema (`pg_indexes`) rather than assumed
from the model source, which is why the migration covers four tables and not
six.

SAFE BY CONSTRUCTION: additive only. No column, type, constraint or data
change, so this cannot change a query result — it can only make one faster, and
the whole suite is expected to pass unchanged. Reversible with a plain DROP.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0050_tenancy_idx"
down_revision: str | None = "0049_merge_venue_aliases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (index_name, table) — kept next to each other so the downgrade cannot drift.
_TENANCY_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_acb_pools_user_id", "acb_pools"),
    ("ix_prices_user_id", "prices"),
    ("ix_rules_user_id", "rules"),
    ("ix_import_batches_user_id", "import_batches"),
)


def upgrade() -> None:
    for index_name, table in _TENANCY_INDEXES:
        # `checkfirst` so a database that already carries one of these by hand
        # (a restored backup, a hand-tuned prod) upgrades instead of aborting.
        op.create_index(index_name, table, ["user_id"], unique=False, if_not_exists=True)


def downgrade() -> None:
    for index_name, table in reversed(_TENANCY_INDEXES):
        op.drop_index(index_name, table_name=table, if_exists=True)

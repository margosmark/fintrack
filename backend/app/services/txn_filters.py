"""Shared staging-filter and "today" helpers (docs/29, fix F1).

Single source for the committed-only transaction predicate (docs/03 + docs/12:
reads filter `import_batches.status='committed'`) and the spending-domain
"today" (America/Toronto). Callers must outerjoin ImportBatch themselves.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlmodel import Session, select

from app.models.categories import Category
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transactions import Transaction

_ET = ZoneInfo("America/Toronto")


def committed_tx_conditions(user_id: int) -> list[object]:
    """Where-clause conditions selecting only committed (or manual) transactions.

    Usage:
        stmt = (
            select(Transaction)
            .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
            .where(*committed_tx_conditions(user.id))
        )
    """
    return [Transaction.user_id == user_id, *committed_staging_conditions()]


def committed_staging_conditions() -> list[object]:
    """The STAGING half of `committed_tx_conditions` — ownership NOT included.

    Split out for the one caller that supplies its own ownership predicate
    instead of `Transaction.user_id == user_id`: the household scope
    (`services/reports.py::household_ownership`, M159/M179) unions the caller's
    own rows with the projection's rows, so its ownership conjunct is an `or_`
    that no per-user function can express. `committed_tx_conditions` is this
    list PLUS that conjunct, so the staging rule has exactly one definition
    either way and the two cannot drift.
    """
    return [
        or_(
            ImportBatch.status == ImportBatchStatus.committed,
            Transaction.import_batch_id.is_(None),
        ),
    ]


# M107 (docs/44 §2) — the ONE definition of each population rule's extra
# predicate. Both `countable_tx_conditions` / `identity_tx_conditions` and the
# consumers that compose these onto a different staging formulation (the list
# route's `_live_scope`, which takes an account allowlist) build from HERE, so
# the two rules cannot be re-typed slightly differently in two files.
LEDGER_EXCLUDE_SPLIT = Transaction.is_split.is_(False)
IDENTITY_EXCLUDE_CHILDREN = Transaction.parent_split_id.is_(None)


def countable_tx_conditions(user_id: int) -> list[object]:
    """LEDGER population rule (M107, docs/44 §2 / D2).

    Money or category is counted → this rule: split PARENTS are excluded
    (their value lives in their children now) and CHILDREN are included.

    Usage is identical to `committed_tx_conditions` — same outerjoin, same
    `.where(*...)` — so a consumer switches rules by changing one function
    name, which is what keeps the two from drifting apart.
    """
    return [*committed_tx_conditions(user_id), LEDGER_EXCLUDE_SPLIT]


def countable_tx_conditions_for(ownership: object) -> list[object]:
    """`countable_tx_conditions` with a caller-supplied OWNERSHIP predicate.

    Same staging rule, same LEDGER rule, one substitution — the household scope
    widens WHO is summed, never what counts (docs/12 §Owner dimension, docs/43
    §6). Only `services/reports.py` builds the ownership `or_`; every other
    consumer keeps the per-user call above.
    """
    return [ownership, *committed_staging_conditions(), LEDGER_EXCLUDE_SPLIT]


# --- Transfer population rule (Kilter adoption, docs/12 §Shared population rules) ---
#
# A transfer is money MOVING, not money SPENT, so every spend-shaped aggregate
# drops those rows. The flag is derived — never a stored boolean that could
# disagree with its own causes (docs/02 §transactions) — and it has exactly two
# spellings, because both of the app's transfer paths write one of them:
#
#   * `transfer_pair_id IS NOT NULL`  — the pairing `POST …/link-transfer`,
#     `POST …/detect-transfer` and `POST /transactions/accept-transfer-suggestion`
#     write (a link/detect pair keeps whatever category the import gave it);
#   * booked to the tenant's SYSTEM `transfers` category — what the accept route
#     also assigns, and what a user who has no counterpart to pair (an external
#     account) sets by hand.
#
# Net worth, balances and goals deliberately do NOT use these: a transfer moves
# cash from one of the user's own accounts to another, and dropping either leg
# there would delete the money.


def transfers_category_ids(session: Session, *user_ids: int | None) -> set[int]:
    """The system `transfers` category ids of the given tenants (empty if none).

    Household scopes pass BOTH tenants' ids (`services/reports.py::
    household_counterpart` resolves the second one), exactly like the merged
    category namespace every widened route already builds. `None` entries are
    dropped, so `transfers_category_ids(session, user_id, counterpart)` is safe
    to call with the counterpart this caller does not have.
    """
    wanted = [int(uid) for uid in user_ids if uid is not None]
    if not wanted:
        return set()
    rows = session.exec(
        select(Category.id).where(
            Category.user_id.in_(wanted),
            Category.name == "transfers",
            Category.is_system.is_(True),
        )
    ).all()
    return set(rows)


def transfer_exclusion_conditions(*user_ids: int | None) -> list[object]:
    """SQL conditions dropping transfers from a SPEND population.

    Callers compose these onto their own scope exactly like
    `LEDGER_EXCLUDE_SPLIT` — this is the ONE spelling of the rule (docs/12
    §Shared population rules).

    The flag category is resolved INSIDE the statement as a scalar subquery
    rather than in a round trip first: a report that gains this rule must not
    gain a SQL statement, or it blows the docs/34 query budget it is pinned
    to. The subquery is uncorrelated, so Postgres runs it once per statement,
    not once per row. `None` entries are dropped, so the household call passes
    the counterpart it may not have resolved.
    """
    wanted = sorted({int(uid) for uid in user_ids if uid is not None})
    conditions: list[object] = [Transaction.transfer_pair_id.is_(None)]
    if not wanted:
        return conditions
    flag_category = select(Category.id).where(
        Category.user_id.in_(wanted),
        Category.name == "transfers",
        Category.is_system.is_(True),
    )
    conditions.append(
        or_(
            Transaction.category_id.is_(None),
            Transaction.category_id.not_in(flag_category),
        )
    )
    return conditions


def is_transfer_row(transaction: Transaction, transfer_ids: set[int]) -> bool:
    """Python twin of `transfer_exclusion_conditions`, for code holding the
    id set (`transfers_category_ids`) in hand — row loops and the parity test.

    The two must always agree: the SQL form resolves its flag category from a
    subquery, this one from the id list, and docs/12 §Shared population rules
    promises one definition whichever shape a reader uses. Reports themselves
    compose the SQL form so gaining the rule never costs a statement
    (docs/34's query budgets).
    """
    if transaction.transfer_pair_id is not None:
        return True
    return transaction.category_id is not None and transaction.category_id in transfer_ids


def identity_tx_conditions(user_id: int) -> list[object]:
    """IDENTITY population rule (M107, docs/44 §2 / D2).

    A statement line is identified, matched or re-imported → this rule: the
    SPLIT PARENTS are the statement lines, so they stay; the CHILDREN are
    excluded (their `dedup_hash` is salted precisely so no matcher can ever
    reach them, docs/44 §7).
    """
    return [*committed_tx_conditions(user_id), IDENTITY_EXCLUDE_CHILDREN]


def committed_tx_rows(session: Session, user_id: int) -> list[Transaction]:
    """Convenience loader: all committed/manual transactions for the user."""
    stmt = (
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(*committed_tx_conditions(user_id))
        .order_by(Transaction.date, Transaction.id)  # type: ignore[attr-defined]
    )
    return list(session.exec(stmt).all())


def today_et() -> dt.date:
    """Today in America/Toronto — the single "today" for spending services."""
    return dt.datetime.now(_ET).date()

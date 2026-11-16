"""transactions — the unified ledger row for all account types (docs/02)."""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    UniqueConstraint,
    text,
)
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class RecurringCadence(enum.StrEnum):
    """Fitted cadence of a tagged recurring group (M96, docs/09).

    Closed set, exactly what `services/recurring.py::_fit_cadence` can return:
    the 7/14/28-day fixed patterns plus the monthly-same-day bucket (which the
    30- and 31-day fixed fits also land in). Stored non-natively (AGENTS §2):
    widening is a constraint rebuild, not a table rewrite.
    """

    weekly = "weekly"
    biweekly = "biweekly"
    four_weekly = "four_weekly"
    monthly = "monthly"


class Transaction(TimestampMixin, table=True):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_user_id_account_id_date", "user_id", "account_id", "date"),
        Index("ix_transactions_user_id_category_id", "user_id", "category_id"),
        Index("ix_transactions_user_id_date", "user_id", "date"),
        Index("ix_transactions_user_id_payee_token", "user_id", "payee_token"),
        Index("ix_transactions_transfer_pair_id", "transfer_pair_id"),
        # M106 / docs/02 §transactions — the "Needs review" queue + its count
        # are one indexed lookup instead of a scan of every row the tenant
        # owns (partial: the flag is a null check, so only unreviewed rows
        # are indexed at all).
        Index(
            "ix_transactions_user_unreviewed",
            "user_id",
            postgresql_where=text("reviewed_at IS NULL"),
        ),
        UniqueConstraint("account_id", "dedup_hash", name="uq_transactions_account_dedup_hash"),
        # M107 / docs/44 §1 — split transactions. A row is never both parent
        # and child, and a parent always carries the exact amount its children
        # must sum to. Named, because migration and tests reference them.
        CheckConstraint(
            "is_split = false OR (parent_split_id IS NULL AND split_checksum IS NOT NULL)",
            name="ck_transactions_split_flag",
        ),
        CheckConstraint(
            "is_split = false OR split_checksum = amount_native",
            name="ck_transactions_split_checksum",
        ),
        Index("ix_transactions_parent_split_id", "parent_split_id"),
        ForeignKeyConstraint(
            ["import_batch_id"], ["import_batches.id"], name="fk_transactions_import_batch"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    account_id: int = Field(foreign_key="accounts.id")
    date: date
    posting_date: date | None = None
    raw_description: str
    normalized_payee: str
    amount_native: Decimal = Field(sa_type=Numeric(18, 2))
    currency: str
    cad_amount: Decimal | None = Field(default=None, sa_type=Numeric(18, 2))
    # M76 / docs/38: YNAB's clearing state preserved on import —
    # "uncleared" | "cleared" | "reconciled". NULL means the source carried no
    # clearing state (OFX/QFX and most CSVs), never "uncleared".
    cleared: str | None = Field(default=None, max_length=16)
    category_id: int | None = Field(default=None, foreign_key="categories.id")
    import_batch_id: int | None = Field(default=None)
    # Named FK with ON DELETE SET NULL matching migration 0010
    # (`fk_transactions_transfer_pair`): deleting one side unlinks the pair
    # instead of blocking the delete.
    transfer_pair_id: int | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey(
                "transactions.id",
                name="fk_transactions_transfer_pair",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    # Unique per account (docs/02): computed at ingest from stable row fields.
    dedup_hash: str
    # Staged-import dedup controls: `dedup_hash` is salted for duplicates so the
    # UNIQUE(account_id, dedup_hash) constraint is not violated; `force_include`
    # marks duplicates the user chose to keep at commit time (docs/03).
    is_duplicate: bool = False
    force_include: bool = False
    # E2EE Phase 2 (docs/33): opaque ciphertext + blind index token.
    payee_cipher: str | None = None
    raw_desc_cipher: str | None = None
    amount_cipher: str | None = None
    cad_amount_cipher: str | None = None
    payee_token: str | None = None
    # M96 / docs/09 §Recurring prompt — passive, local-only detection result
    # (migration 0037). NULL cadence/next date until the user ACCEPTS a
    # candidate group; a decline writes `recurring_suppressions` instead and
    # never touches these.
    is_recurring: bool = False
    recurring_cadence: RecurringCadence | None = Field(
        default=None,
        sa_column=Column(
            Enum(RecurringCadence, native_enum=False, length=16),
            nullable=True,
        ),
    )
    # The group's NEXT expected occurrence, stored on every tagged row of the
    # group (one number to read, not one per historical row).
    recurring_next_date: date | None = None
    # M106 / docs/02 §transactions — manual review state. NULL `reviewed_at`
    # IS "unreviewed" (one nullable pair, never a second boolean that could
    # disagree with it): an import therefore seeds a review queue for free,
    # because staged rows are written with neither column set. `reviewed_by`
    # carries NO foreign key — the `mcp_audit.mcp_token_id` rule: deleting the
    # reviewer's account must not block an account deletion or blank the
    # record of who signed off. Out of scope for M106: assigning a review to
    # the partner (docs/43 consent seam) and auto-review rules (M96).
    reviewed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    reviewed_by: int | None = None
    # M107 / docs/44 §1 — split transactions. Four columns on the existing
    # table, no side table (D1): every existing reader stays one predicate
    # away from correctness.
    #
    # `is_split` is TRUE iff this row has >= 1 child — denormalized so every
    # LEDGER predicate (`services/txn_filters.countable_tx_conditions`) is one
    # indexed boolean test instead of an anti-join. Near-constant-false, so it
    # deliberately gets NO index of its own (docs/44 §1): `(user_id, date)` and
    # `(user_id, account_id, date)` already answer every scan.
    is_split: bool = False
    # Set on CHILDREN only, and only ever to their own parent. A self-FK
    # exactly like `transfer_pair_id`, so the export/restore dependency graph
    # already tolerates it — but ON DELETE CASCADE (a batch discard or an
    # account deletion takes the children with it) and DEFERRABLE INITIALLY
    # DEFERRED, so a snapshot whose children precede their parents still
    # restores (docs/44 §10, D14).
    parent_split_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey(
                "transactions.id",
                name="fk_transactions_parent_split",
                ondelete="CASCADE",
                deferrable=True,
                initially="deferred",
            ),
            nullable=True,
        ),
    )
    # On PARENTS only: the exact amount the children must sum to (set to
    # `amount_native` at split time, cleared at merge) — the sum invariant
    # (D3) made checkable by a constraint rather than by a test alone.
    split_checksum: Decimal | None = Field(default=None, sa_type=Numeric(18, 2))
    # Free text on a row (docs/44 §1). v1's UI exposes it on children and in
    # the split editor only, the API accepts it on any row. PLAINTEXT, like
    # `raw_description` — it belongs in docs/33's server-readable-plaintext
    # inventory, and does not belong in any cipher column.
    note: str | None = Field(default=None, max_length=500)

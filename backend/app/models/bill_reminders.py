"""bill_reminders — upcoming bill tracking with due dates + match rules (M116/M121).

M116 (docs/25 §M116) turns the reminder into a bill that can FIND its own
payments: `match_mode` / `match_term` / `amount_tolerance` describe HOW a
transaction belongs to this bill, `category_id` (already here) scopes it, and
the set of a bill's transactions is always the live rule re-applied over the
committed ledger — never a copy table (docs/25 §M116 D3).

M121 (docs/25 §M121) widens ONE term into a TERM LIST with AND/OR semantics:
`match_logic` (`any` = widening default, `all` = narrowing) lives on the bill,
the terms themselves live in `bill_reminder_match_terms` — one row per term
with an order column, because docs/02 keeps JSON out of these tables: a bounded
column a reader can index, count and constrain beats a blob it has to parse.
`match_term` is KEPT as terms[0] (the wire's legacy single-term field, and the
fallback every pre-M121 row reads back through).
"""

import enum
from decimal import Decimal

from sqlalchemy import CheckConstraint, Column, Enum, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class BillMatchMode(enum.StrEnum):
    """How a term is compared against a transaction's normalized payee.

    Closed set, exactly what the M116 rule editor offers (docs/25 §M116 D1).
    Stored non-natively (AGENTS §2): widening is a constraint rebuild, not a
    table rewrite.
    """

    contains = "contains"
    starts_with = "starts_with"
    exact = "exact"


class BillMatchLogic(enum.StrEnum):
    """AND/OR over a bill's term list (M121, docs/25 §M121 D2).

    `any` (OR) is the default because a single-term rule — every rule that
    existed before M121 — behaves IDENTICALLY under both modes, so the default
    can never change an existing bill's answer.
    """

    any = "any"
    all = "all"


class BillReminder(TimestampMixin, table=True):
    __tablename__ = "bill_reminders"
    __table_args__ = (
        # M180: the anchor is a day-of-month, so the bound lives in the TABLE
        # (docs/25 §M180) — a hand-written row can no more strand a 0th or a
        # 32nd than `ck_bill_reminders_match_term_len` can strand a 121-char
        # term. NULL stays legal: it means "use the stored date's own day".
        CheckConstraint(
            "anchor_day IS NULL OR (anchor_day BETWEEN 1 AND 31)",
            name="ck_bill_reminders_anchor_day",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=200)
    amount: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    category_id: int | None = Field(default=None, foreign_key="categories.id")
    frequency: str = Field(max_length=20)  # monthly, quarterly, yearly, one_time
    next_due_date: str = Field(max_length=10)  # YYYY-MM-DD
    # --- M180 (docs/25 §M180) — the schedule's own day-of-month -------------- #
    # The day the schedule REPEATS on, so month-end clamping is a property of
    # the projection and never of the stored schedule. `NULL` = un-anchored:
    # the stored date's own day is the anchor, which is exactly the pre-M180
    # behaviour for a row no API path wrote (docs/25 §M180 D1).
    anchor_day: int | None = Field(default=None)
    remind_days_before: int = Field(default=3)
    enabled: bool = Field(default=True)
    # --- M116 (docs/25 §M116 D1) — the match rule -------------------------- #
    # Which side of the normalized payee the terms have to hit.
    match_mode: BillMatchMode = Field(
        default=BillMatchMode.contains,
        sa_column=Column(
            Enum(BillMatchMode, native_enum=False, length=16),
            nullable=False,
        ),
    )
    # The FIRST term. Required — a bill with no rule cannot answer "how many
    # payments did I find" (docs/25 §M116 D1), so there is NO Python default to
    # fall back on: every constructor passes one, `POST /bills` derives it from
    # `name`, and migration 0044 backfills existing rows from `left(name, 120)`.
    # The API refuses a term that normalizes to nothing. M121 keeps it as the
    # mirror of terms[0] so the pre-M121 wire field never lies.
    match_term: str = Field(max_length=120)
    # None = the bill's `amount` must match exactly. Set = ± that many dollars.
    amount_tolerance: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    # --- M121 (docs/25 §M121 D2) — AND/OR over the term list --------------- #
    # Server default 'any' so every pre-M121 row reads back with the widening
    # mode without a data fix-up (migration 0046 backfills the column value).
    match_logic: BillMatchLogic = Field(
        default=BillMatchLogic.any,
        sa_column=Column(
            Enum(BillMatchLogic, native_enum=False, length=16),
            nullable=False,
            server_default="any",
        ),
    )


class BillReminderMatchTerm(TimestampMixin, table=True):
    """One term of a bill's match rule, in order (M121, docs/25 §M121).

    A CHILD table keyed to the bill, not a JSON column: docs/02 keeps JSON out
    of these tables, and a row per term makes the bound enforceable in the
    TABLE (`ck_…_term_len`), the order explicit (`position`), and the
    "one bill, N terms" read a single indexed query for N bills.

    `position` starts at 0 = terms[0] = `bill_reminders.match_term`.
    `bill_reminders.id` is the ONLY FK, and it cascades: deleting a bill
    deletes its terms at the database, so no orphan row can survive a raw
    `DELETE`.
    """

    __tablename__ = "bill_reminder_match_terms"
    __table_args__ = (
        UniqueConstraint(
            "bill_reminder_id",
            "position",
            name="uq_bill_reminder_match_terms_bill_position",
        ),
        CheckConstraint(
            "length(term) BETWEEN 1 AND 120",
            name="ck_bill_reminder_match_terms_term_len",
        ),
        CheckConstraint("position >= 0", name="ck_bill_reminder_match_terms_position"),
    )

    id: int | None = Field(default=None, primary_key=True)
    bill_reminder_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey(
                "bill_reminders.id",
                name="fk_bill_reminder_match_terms_bill",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
    )
    position: int = Field(default=0)
    term: str = Field(max_length=120)

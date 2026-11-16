"""Aggregation reports (docs/12) — local Postgres aggregates + cached prices/fx.

Report endpoints NEVER trigger external fetches (docs/12 Principles): every CAD
conversion reads the fx_rates cache, every valuation reads the prices table,
and anything unresolvable is EXCLUDED from aggregates rather than fetched or
backfilled. Realized figures come from the ACB engine
(app.tax.acb.compute_user_tax_state — the same single source of gain math that
app.tax.reports.capital_gains_report uses); nothing is duplicated here.
"""

from __future__ import annotations

import csv
import datetime as dt
from bisect import bisect_right
from collections import defaultdict
from decimal import Decimal
from io import StringIO
from typing import Literal

from sqlalchemy import and_, false, func, or_
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.config import get_settings
from app.models.accounts import Account
from app.models.categories import Category
from app.models.fx_rates import FxRate
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.prices import Price
from app.models.securities import Security
from app.models.sharing import SharingLink, SharingLinkStatus
from app.models.tags import Tag
from app.models.trades import (
    CASH_INFLOW_ACTIONS,
    CASH_OUTFLOW_ACTIONS,
    COMMISSION_ACTIONS,
    Trade,
    TradeAction,
)
from app.models.transaction_tags import TransactionTag
from app.models.transactions import Transaction
from app.models.users import User
from app.services.trade_filters import committed_trade_conditions
from app.services.txn_filters import (
    LEDGER_EXCLUDE_SPLIT,
    countable_tx_conditions_for,
    transfer_exclusion_conditions,
)

# `_cents` below is the canonical `app.tax.acb.cents` (ROUND_HALF_UP, docs/02
# Conventions / docs/12 §money) under this module's historical private name:
# `services/total_return.py` and `services/account_value.py` import it from
# here. It is a re-export, NOT a second definition — there is exactly one
# rounding implementation in the app (docs/02, AGENTS §2).
from app.tax.acb import cents as _cents
from app.tax.acb import compute_user_tax_state

Granularity = Literal["day", "week", "month", "year"]
# M124 (docs/12 §1): the spending reports group by ONE dimension at a time.
GroupBy = Literal["category", "tag"]

_ZERO = Decimal("0")
_UNCATEGORIZED = "Uncategorized"
_UNTAGGED = "Untagged"
_OTHER_TAGS = "Other tags"
# M124 R3 (docs/12 §1): the Category x Tag matrix keeps the LARGEST tags only.
_MATRIX_TOP_TAGS = 8


# --- time bucketing (docs/12 §Time bucketing) -------------------------------


def bucket_starts(first: dt.date, last: dt.date, granularity: Granularity) -> list[dt.date]:
    """First date of every bucket covering [first, last]; weeks start Monday."""
    if granularity == "day":
        cur, step = first, dt.timedelta(days=1)
        advance = lambda d: d + step  # noqa: E731
    elif granularity == "week":
        cur = first - dt.timedelta(days=first.weekday())
        advance = lambda d: d + dt.timedelta(days=7)  # noqa: E731
    elif granularity == "month":
        cur = first.replace(day=1)

        def advance(d: dt.date) -> dt.date:
            y, m = divmod(d.month, 12)
            return dt.date(d.year + y, m + 1, 1)
    elif granularity == "year":
        cur = first.replace(month=1, day=1)
        advance = lambda d: d.replace(year=d.year + 1)  # noqa: E731
    else:  # pragma: no cover — FastAPI constrains the query param upstream
        raise ValueError(f"unknown granularity: {granularity}")

    starts: list[dt.date] = []
    while cur <= last:
        starts.append(cur)
        cur = advance(cur)
    return starts


def bucket_key(day: dt.date, granularity: Granularity) -> dt.date:
    """First date of the bucket containing `day`."""
    return bucket_starts(day, day, granularity)[0]


def bucket_end(start: dt.date, granularity: Granularity) -> dt.date:
    if granularity == "day":
        return start
    if granularity == "week":
        return start + dt.timedelta(days=6)
    if granularity == "month":
        y, m = divmod(start.month, 12)
        return dt.date(start.year + y, m + 1, 1) - dt.timedelta(days=1)
    return dt.date(start.year, 12, 31)


# --- cache-only reference data ----------------------------------------------


class FxCache:
    """fx_rates snapshot with newest-rate-on-or-before lookups; never fetches."""

    def __init__(self, session: Session, quote: str) -> None:
        rows = session.exec(
            select(FxRate).where(FxRate.quote == quote).order_by(FxRate.base, FxRate.date)
        ).all()
        self._dates: dict[str, list[dt.date]] = defaultdict(list)
        self._rates: dict[str, list[Decimal]] = defaultdict(list)
        for row in rows:
            self._dates[row.base].append(row.date)
            self._rates[row.base].append(row.rate)

    def rate(self, base: str, quote: str, day: dt.date) -> Decimal | None:
        """Newest cached base→quote rate on or before `day`; None when unresolved."""
        info = self.rate_info(base, quote, day)
        return info[0] if info else None

    def rate_info(self, base: str, quote: str, day: dt.date) -> tuple[Decimal, dt.date] | None:
        """`(rate, rate_date)` — the DATE is what lets a caller tell a fresh
        quote from a carried-forward one (M118 honesty flag, docs/08)."""
        if base == quote:
            return Decimal("1"), day
        i = bisect_right(self._dates.get(base, ()), day)
        return (self._rates[base][i - 1], self._dates[base][i - 1]) if i else None


class PriceCache:
    """prices snapshot: newest price ≤ day wins (manual beats yfinance by the
    per-day unique constraint), falling back to the EARLIEST known price when
    nothing exists ≤ day (docs/12 net-worth fallback). None only when the
    security has no price history at all."""

    def __init__(self, session: Session, user_id: int) -> None:
        rows = session.exec(
            select(Price).where(Price.user_id == user_id).order_by(Price.security_id, Price.date)
        ).all()
        self._dates: dict[int, list[dt.date]] = defaultdict(list)
        self._prices: dict[int, list[Decimal]] = defaultdict(list)
        for row in rows:
            self._dates[row.security_id].append(row.date)
            self._prices[row.security_id].append(row.price)

    def price(self, security_id: int, day: dt.date) -> Decimal | None:
        info = self.price_on(security_id, day)
        return info[0] if info else None

    def price_on(self, security_id: int, day: dt.date) -> tuple[Decimal, dt.date] | None:
        """`(price, price_date)` — the DATE the price is from, so a caller can
        flag a carried-forward valuation instead of drawing it as a fact
        (M118 honesty flag, docs/08). None only when the security has no price
        history at all."""
        dates = self._dates.get(security_id, ())
        if not dates:
            return None
        i = bisect_right(dates, day)
        if i:
            return self._prices[security_id][i - 1], dates[i - 1]
        return self._prices[security_id][0], dates[0]


# --- shared population queries (committed-only, docs/03 + docs/12) ----------


def _household_projection(session: Session, user_id: int) -> tuple[int, list[int]] | None:
    """M159 (docs/12 §Owner dimension, docs/43 §6): the projection the caller's
    own ACTIVE `sharing_links` row grants — `(owner_id, live account scope)`.

    The grant is ONE-DIRECTIONAL (docs/43 §2, default DENY): a link lets its
    PARTNER seat read the OWNER's allowlisted accounts, so the household scope
    only ever extends a PARTNER-seat caller's rows. An owner-seat caller has no
    counterpart projection back into their own tenant and resolves `None` —
    `scope=household` is then their own rows, exactly as an unlinked caller's
    is. A privacy-mode owner (docs/43 §9, D4) projects an EMPTY scope: the
    mask hides the owner's money from the partner, and a household total is
    that money. Nothing here can be caller-supplied: the counterpart id and the
    account allowlist both come from the link row the caller already holds,
    resolved by the same indexed read `GET /partner/transactions` uses
    (`_resolve_partner_link`).
    """
    link = session.exec(
        select(SharingLink)
        .where(
            SharingLink.partner_user_id == user_id,
            SharingLink.status == SharingLinkStatus.active,
        )
        .limit(1)
    ).first()
    if link is None or link.user_id is None:
        return None
    owner_id = link.user_id
    owner = session.get(User, owner_id)
    if owner is None:
        # The owner's row is gone (CASCADE would normally have dropped the
        # link) — same uniform absence as never-linked.
        return None
    if owner.privacy_mode:
        # D4 / docs/43 §9: the owner's privacy posture governs what the
        # PARTNER may read, and a report total is a money figure exactly as
        # much as a row's `amount_native`. The projection serializers null
        # every monetary field under this flag; an aggregate has no field to
        # null, so the only honest mask is population-level — a privacy-mode
        # owner projects NOTHING into a household report, and the partner's
        # household is their own rows.
        return owner_id, []
    # I6: ids validated at write time may dangle later (account deleted) —
    # skipped at READ time, never staged into (docs/43 §3), and only the
    # owner's OWN live accounts can ever carry the projection.
    requested = list(link.account_ids or [])
    if not requested:
        return owner_id, []  # deny-by-default: the projection delivers nothing
    live = set(
        session.exec(
            select(Account.id).where(Account.user_id == owner_id, Account.id.in_(requested))
        ).all()
    )
    return owner_id, [account_id for account_id in requested if account_id in live]


def household_ownership(
    session: Session, user_id: int, *, household: bool
) -> tuple[object, int | None]:
    """M159/M179 (docs/12 §Owner dimension, docs/43 §6): the OWNERSHIP predicate
    for one report population, plus the counterpart tenant it reached.

    `household=False` is the per-user predicate every pre-M159 report ran:
    `Transaction.user_id == user_id`, byte-identical to
    `committed_tx_conditions`'s own conjunct. `household=True` widens it to
    `own OR (counterpart AND account_id IN the caller's own live allowlist)` —
    the union `_household_projection` resolves, and the SAME one every
    household-scope route uses, so a new consumer of the scope cannot invent a
    second reading of "who is in the household".

    This is the ONLY builder of that `or_`: the staging rule (committed-only),
    the LEDGER split rule and each report's own math stay shared, so widening a
    route is one call, not a re-implementation (docs/43 §6: one projection, one
    population).
    """
    ownership: object = Transaction.user_id == user_id
    if not household:
        return ownership, None
    projection = _household_projection(session, user_id)
    if projection is None:
        return ownership, None
    owner_id, projection_ids = projection
    shared = (
        and_(Transaction.user_id == owner_id, Transaction.account_id.in_(projection_ids))
        if projection_ids
        else false()
    )
    return or_(ownership, shared), owner_id


def household_counterpart(session: Session, user_id: int, *, household: bool) -> int | None:
    """The counterpart tenant whose category/tag names a household scope must
    layer over the caller's — `None` when there is nothing to merge.

    A projected row carries the OWNER's category/tag ids (docs/43 §6), so a
    household report that resolved names from the caller's tree alone would
    label a foreign id `Uncategorized`. The counterpart's tree wins a bare-id
    conflict; every id only one side carries keeps its own name.
    """
    if not household:
        return None
    projection = _household_projection(session, user_id)
    if projection is None or not projection[1]:
        # No link, or a link that shares no accounts (deny by default): the
        # household population holds no foreign id, so there is nothing to
        # merge and no second tree to read.
        return None
    return projection[0]


def _committed_transactions(
    session: Session,
    user_id: int,
    from_: dt.date | None = None,
    to: dt.date | None = None,
    account_id: int | None = None,
    household: bool = False,
    account_ids: list[int] | None = None,
    exclude_transfers_for: tuple[int | None, ...] = (),
) -> list[Transaction]:
    ownership: object = Transaction.user_id == user_id
    if household:
        # M159: the household scope widens WHO is summed by exactly the rows
        # the caller's own active link already projects (docs/43 §6) — the
        # partner seat's own ledger PLUS the owner's allowlisted accounts.
        # Everything below (committed-only, LEDGER split rule, date window,
        # sign convention) applies identically to both halves: one predicate,
        # one population, no per-side contract to drift.
        ownership, _ = household_ownership(session, user_id, household=True)
    stmt = (
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(
            ownership,
            (ImportBatch.status == ImportBatchStatus.committed)
            | (Transaction.import_batch_id.is_(None)),
            # M107 (docs/44 §8): LEDGER — children counted, split parents out.
            # Bucketing, zero-fill, sign conventions, root roll-up and the
            # Sankey's conservation rules are untouched; only WHO is summed
            # changes, and totals are preserved exactly (docs/44 §3).
            LEDGER_EXCLUDE_SPLIT,
        )
        .order_by(Transaction.date, Transaction.id)
    )
    # Spend-shaped readers opt in to the transfer rule (docs/12 §Shared
    # population rules); the balances/net-worth reader at the bottom of this
    # file passes nothing, because there a transfer really did move money.
    if exclude_transfers_for:
        stmt = stmt.where(*transfer_exclusion_conditions(*exclude_transfers_for))
    if from_ is not None:
        stmt = stmt.where(Transaction.date >= from_)
    if to is not None:
        stmt = stmt.where(Transaction.date <= to)
    # M118: narrow to ONE account without forking the committed/split contract
    # above — the predicate stays byte-identical for every existing caller.
    if account_id is not None:
        stmt = stmt.where(Transaction.account_id == account_id)
    # M145: the batched twin — same predicate, an ALLOWLIST of accounts, so a
    # many-account cash read (the /accounts list) is ONE statement instead of
    # one per account.
    if account_ids is not None:
        stmt = stmt.where(Transaction.account_id.in_(account_ids))
    return list(session.exec(stmt).all())


def _categories(session: Session, user_id: int) -> dict[int, Category]:
    """The whole category tree for one user — names AND parent links.

    Parent links are what the M124 drill-down needs (`_category_subtree`);
    the pre-M124 caller only wanted names, so this returns the objects and
    callers project what they need rather than keeping two loaders.
    """
    return {c.id: c for c in session.exec(select(Category).where(Category.user_id == user_id))}


def _top_ancestor(categories: dict[int, Category], category_id: int) -> int:
    """Walk the parent chain to the root of the category tree (docs/02)."""
    seen: set[int] = set()
    current = category_id
    while categories[current].parent_id is not None and current not in seen:
        seen.add(current)
        current = categories[current].parent_id
    return current


# --- M124: the tag dimension (docs/12 §Tag dimension) ------------------------
#
# Tags are a flat many-to-many join, so NOTHING here rolls up — there is no
# tree to walk. The one shape that differs from the category path is the
# NULL bucket: a transaction with no tags lands in "Untagged", which is the
# exact parallel of the category path's "Uncategorized".


def _tag_names(session: Session, user_id: int) -> dict[int, str]:
    return {t.id: t.name for t in session.exec(select(Tag).where(Tag.user_id == user_id))}


def _tags_by_txn(
    session: Session, user_id: int, from_: dt.date, to: dt.date
) -> dict[int, list[int]]:
    """ONE statement for every in-range transaction's tag ids (docs/34 no-N+1).

    Deliberately NOT scoped to the committed/LEDGER population: this is a
    lookup keyed by transaction id, and the caller only ever asks about
    transactions the population query already admitted. Scoping it would
    duplicate the population predicate for no gain.
    """
    rows = session.exec(
        select(TransactionTag.transaction_id, TransactionTag.tag_id)
        .join(Tag, TransactionTag.tag_id == Tag.id)
        .join(Transaction, TransactionTag.transaction_id == Transaction.id)
        .where(Tag.user_id == user_id, Transaction.date >= from_, Transaction.date <= to)
    ).all()
    by_txn: dict[int, list[int]] = defaultdict(list)
    for txn_id, tag_id in rows:
        by_txn[txn_id].append(tag_id)
    return by_txn


def _category_subtree(categories: dict[int, Category], root: int) -> set[int]:
    """`root` plus every descendant — the drill-down population (docs/12 §2).

    A leaf is its own subtree, so a drill on a leaf behaves exactly like an
    exact match; the widening only matters for a parent, and it is what makes
    the drill reproduce the donut's root-rolled slice (docs/09 §honesty: the
    number you drilled into has to be the number you get back).
    """
    children: dict[int | None, list[int]] = defaultdict(list)
    for cid, cat in categories.items():
        children[cat.parent_id].append(cid)
    subtree: set[int] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        if current in subtree:
            continue  # cycle guard — a malformed parent chain must not hang
        subtree.add(current)
        stack.extend(children.get(current, ()))
    return subtree


# --- CSV rendering (header names pinned to JSON field names, docs/12) -------

_MONEY_FIELDS = frozenset(
    {
        "total_cad",
        "cad_total",
        "income_cad",
        "expenses_cad",
        "net_cad",
        "cash_cad",
        "positions_cad",
        "realized_cad",
        "unrealized_cad",
        "acb_pool_cad",
        "market_value_cad",
        # M29 (docs/24) native money on the dividend calendar's export: these
        # are unblended per-denomination figures, and a CSV cell that printed
        # `85` instead of `85.00` would no longer state the two-decimal money
        # string the JSON contract puts on the wire.
        "avg_amount",
        "annual_projection",
    }
)

_CSV_UNSAFE_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    """Neutralize spreadsheet formula injection (security review #5): a leading
    `'` stops Excel/LibreOffice from interpreting the cell as a formula."""
    if value.startswith(_CSV_UNSAFE_PREFIXES):
        return f"'{value}"
    return value


def report_csv(rows: list[dict[str, object]], fields: list[str]) -> str:
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(fields)
    for row in rows:
        cells = []
        for field in fields:
            value = row.get(field)
            if value is None:
                cells.append("")
            elif isinstance(value, Decimal):
                cells.append(_fmt(value, cents=field in _MONEY_FIELDS))
            else:
                cells.append(csv_safe(str(value)))
        writer.writerow(cells)
    return buf.getvalue()


def _fmt(value: Decimal, *, cents: bool = True) -> str:
    if cents:
        return f"{_cents(value):f}"
    return f"{value.normalize():f}"


# --- 1 & 2. Spending ---------------------------------------------------------


def spending_report(
    session: Session,
    user_id: int,
    from_: dt.date,
    to: dt.date,
    granularity: Granularity,
    *,
    group_by: GroupBy = "category",
    category_id: int | None = None,
    uncategorized_only: bool = False,
    household: bool = False,
) -> dict[str, object]:
    """Spending by time bucket, grouped by ONE dimension (M124, docs/12 §1).

    The category path (the defaults) is byte-identical to the pre-M124
    function: same population, same sign convention, same zero-fill, same item
    shape. `group_by="tag"` swaps the aggregation key for the transaction's
    tags — a transaction with several tags lands in EACH of its tag buckets,
    so the item totals can sum to more than was actually spent. That is the
    honest shape of a many-to-many grouping; `spend_total_cad` below is the
    once-counted denominator the UI labels it against.

    `category_id` / `uncategorized_only` are the R2 drill filter: a subtree of
    the category tree (a leaf behaves as an exact match) or "no category at
    all". They compose with either `group_by`, which is what makes
    `group_by=tag&category_id=Groceries` answer "Groceries tagged vacation".

    `household` (M159, docs/12 §Owner dimension) widens ONLY who is summed —
    the caller's own committed rows plus the owner's allowlisted rows through
    the caller's own active `sharing_links` row (docs/43 §6). Every other
    contract above applies to both halves identically.
    """
    starts = bucket_starts(from_, to, granularity)
    index = {start: i for i, start in enumerate(starts)}
    categories = _categories(session, user_id)

    # M159 (docs/12 §Owner dimension): a household-scope report sums the
    # caller's own rows PLUS the owner's allowlisted projection rows, and a
    # projected row carries the OWNER's category/tag ids (docs/43 §6). The
    # caller's tree alone would then label a foreign id "Uncategorized", so the
    # counterpart's tree is layered over it — the counterpart wins a bare-id
    # conflict, and every id only one side carries keeps its own name. The
    # population loader resolves the same link again below; both reads are
    # the same indexed point read, and neither can disagree within one request.
    # Transaction-level transfers are NOT spending (docs/12 §Shared population
    # rules): both legs of a pair, and anything booked to either tenant's
    # system `transfers` category, are dropped from `spend_total_cad` and from
    # every bucket before any grouping happens.
    counterpart_id = household_counterpart(session, user_id, household=household)
    if counterpart_id is not None:
        categories.update(_categories(session, counterpart_id))

    # The drill narrows WHO is summed; everything downstream is untouched.
    subtree = _category_subtree(categories, category_id) if category_id is not None else None
    if uncategorized_only and category_id is not None:
        raise ValueError("category_id and uncategorized_only are mutually exclusive")

    tags_by_txn: dict[int, list[int]] = {}
    tag_names: dict[int, str] = {}
    if group_by == "tag":
        tag_names = _tag_names(session, user_id)
        tags_by_txn = _tags_by_txn(session, user_id, from_, to)
        if counterpart_id is not None:
            # Transaction ids are globally unique, so the two tag lookups can
            # never collide — only the TAG ids can, and those resolve through
            # the merged name map exactly like the category ids do.
            tag_names.update(_tag_names(session, counterpart_id))
            tags_by_txn.update(_tags_by_txn(session, counterpart_id, from_, to))

    grid: dict[tuple[int, int | None], Decimal] = defaultdict(lambda: _ZERO)
    spend_total = _ZERO
    for txn in _committed_transactions(
        session,
        user_id,
        from_,
        to,
        household=household,
        exclude_transfers_for=(user_id, counterpart_id),
    ):
        if txn.cad_amount is None or txn.cad_amount >= 0:
            continue  # unresolved CAD excluded; spending sums negatives only
        if uncategorized_only and txn.category_id is not None:
            continue
        if subtree is not None and txn.category_id not in subtree:
            continue
        bucket = index[bucket_key(txn.date, granularity)]
        amount = -txn.cad_amount
        spend_total += amount  # counted ONCE, whatever the grouping below does
        if group_by == "tag":
            txn_tags = tags_by_txn.get(txn.id, []) if txn.id is not None else []
            for tag_id in txn_tags:
                grid[(bucket, tag_id)] += amount
            if not txn_tags:  # no tags at all -> the Untagged bucket
                grid[(bucket, None)] += amount
        else:
            grid[(bucket, txn.category_id)] += amount

    items: list[dict[str, object]] = []
    # A drill that pinned one category owns its zero-fill row; otherwise an
    # empty bucket is still the NULL bucket (docs/12: always one entry).
    fill_key = category_id if (category_id is not None and group_by == "category") else None
    label_of = tag_names if group_by == "tag" else {cid: c.name for cid, c in categories.items()}
    empty_label = _UNTAGGED if group_by == "tag" else _UNCATEGORIZED
    for i, start in enumerate(starts):
        slices = [(key, total) for (b, key), total in grid.items() if b == i]
        if not slices:  # zero-fill: one entry per bucket, always (docs/12)
            slices = [(fill_key, _ZERO)]
        for key, total in sorted(
            slices,
            key=lambda s: (s[0] is None, "" if s[0] is None else label_of.get(s[0], "")),
        ):
            if group_by == "tag":
                items.append(
                    {
                        "bucket": start,
                        "tag_id": key,
                        "tag_name": empty_label if key is None else label_of[key],
                        "total_cad": _cents(total),
                    }
                )
            else:
                items.append(
                    {
                        "bucket": start,
                        "category_id": key,
                        "category_name": empty_label if key is None else label_of[key],
                        "total_cad": _cents(total),
                    }
                )

    total = sum((item["total_cad"] for item in items), _ZERO)
    return {
        "from_": from_,
        "to": to,
        "granularity": granularity,
        "group_by": group_by,
        "items": items,
        "total_cad": total,
        # The denominator the "multi-tag transactions count in each" label
        # (docs/09 §honesty) points at: every transaction counted exactly once.
        "spend_total_cad": _cents(spend_total),
    }


def category_breakdown_report(
    session: Session,
    user_id: int,
    from_: dt.date,
    to: dt.date,
    *,
    group_by: GroupBy = "category",
    household: bool = False,
) -> dict[str, object]:
    """The donut's slice set (M124, docs/12 §1).

    Category mode rolls children up to their ROOT — byte-identical to
    pre-M124. Tag mode has no tree to roll up: it slices the same population
    by tag, with the NULL bucket reading "Untagged".

    `household` (M159, docs/12 §Owner dimension) widens ONLY who is summed —
    the caller's own committed rows plus the owner's allowlisted rows through
    the caller's own active `sharing_links` row (docs/43 §6) — with the merged
    category/tag namespace `spending_report` uses for the same scope.
    """
    totals: dict[int | None, Decimal] = defaultdict(lambda: _ZERO)
    spend_total = _ZERO
    counterpart_id = household_counterpart(session, user_id, household=household)
    # Same transfer rule as `spending_report` — this IS the same population,
    # sliced differently (docs/12 §1/§2), so the two totals can never diverge;
    # it rides in the read below rather than as a second round trip.

    if group_by == "tag":
        tag_names = _tag_names(session, user_id)
        tags_by_txn = _tags_by_txn(session, user_id, from_, to)
        if counterpart_id is not None:
            tag_names.update(_tag_names(session, counterpart_id))
            tags_by_txn.update(_tags_by_txn(session, counterpart_id, from_, to))
        for txn in _committed_transactions(
            session,
            user_id,
            from_,
            to,
            household=household,
            exclude_transfers_for=(user_id, counterpart_id),
        ):
            if txn.cad_amount is None or txn.cad_amount >= 0:
                continue
            spend_total += -txn.cad_amount
            ids = tags_by_txn.get(txn.id, []) if txn.id is not None else []
            for tag_id in ids:
                totals[tag_id] += -txn.cad_amount
            if not ids:
                totals[None] += -txn.cad_amount
        items = [
            {
                "tag_id": tag_id,
                "name": _UNTAGGED if tag_id is None else tag_names[tag_id],
                "cad_total": _cents(total),
            }
            for tag_id, total in totals.items()
        ]
    else:
        categories = _categories(session, user_id)
        if counterpart_id is not None:
            categories.update(_categories(session, counterpart_id))
        for txn in _committed_transactions(
            session,
            user_id,
            from_,
            to,
            household=household,
            exclude_transfers_for=(user_id, counterpart_id),
        ):
            if txn.cad_amount is None or txn.cad_amount >= 0:
                continue
            spend_total += -txn.cad_amount
            root = None if txn.category_id is None else _top_ancestor(categories, txn.category_id)
            totals[root] += -txn.cad_amount
        items = [
            {
                "category_id": cid,
                "name": _UNCATEGORIZED if cid is None else categories[cid].name,
                "cad_total": _cents(total),
            }
            for cid, total in totals.items()
        ]

    items.sort(key=lambda item: (-item["cad_total"], item["name"]))  # descending
    return {
        "group_by": group_by,
        "items": items,
        "total_cad": sum((i["cad_total"] for i in items), _ZERO),
        "spend_total_cad": _cents(spend_total),
    }


def _matrix_cells(
    session: Session,
    ownership: object,
    transfer_users: tuple[int | None, ...],
    from_: dt.date,
    to: dt.date,
) -> dict[tuple[int | None, int | None], Decimal]:
    """`{(category_id, tag_id): spend}` for the range — ONE group-by LEFT JOIN.

    `ownership` is the population predicate (`household_ownership`, M179) rather
    than a user id, so the household scope widens WHO is summed here exactly as
    it does in `_committed_transactions`: same staging rule, same LEDGER split
    rule, one substitution.

    `transfer_users` is the transfer population rule (docs/12 §Shared
    population rules) — passed in rather than resolved here because this
    reader takes an ownership predicate instead of a tenant id, and the
    caller already knows whose `transfers` category to look for.

    `transaction_tags` is LEFT JOINed rather than INNER so a transaction with
    no tags still produces a row (`tag_id IS NULL` → the "Untagged" column);
    an INNER join would silently drop exactly the money the honesty label
    promises to account for (docs/09 §Hidden data).
    """
    rows = session.exec(
        select(
            Transaction.category_id,
            TransactionTag.tag_id,
            func.sum(func.coalesce(Transaction.cad_amount, 0)).label("cad_total"),
        )
        .select_from(Transaction)
        .outerjoin(TransactionTag, TransactionTag.transaction_id == Transaction.id)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(
            *countable_tx_conditions_for(ownership),
            *transfer_exclusion_conditions(*transfer_users),
            Transaction.date >= from_,
            Transaction.date <= to,
            # coalesce keeps the type non-null AND excludes unresolved CAD:
            # NULL reads as 0, and 0 is not < 0.
            func.coalesce(Transaction.cad_amount, 0) < 0,
        )
        .group_by(Transaction.category_id, TransactionTag.tag_id)
    ).all()
    cells: dict[tuple[int | None, int | None], Decimal] = defaultdict(lambda: _ZERO)
    for category_id, tag_id, cad_total in rows:
        cells[(category_id, tag_id)] += -cad_total  # SQL summed the raw negatives
    return cells


def _matrix_row_totals(
    session: Session,
    ownership: object,
    transfer_users: tuple[int | None, ...],
    from_: dt.date,
    to: dt.date,
) -> dict[int | None, Decimal]:
    """`{category_id: spend}` counting each transaction ONCE.

    Separate from `_matrix_cells` on purpose: the cell grid double-counts a
    transaction that carries several tags, so it cannot be reduced to row
    totals by summing. This is also what decides which categories are rows at
    all (docs/12 §1: a row is a category that actually spent) — which is why
    it takes the SAME transfer exclusion as the cells: a transfers-only
    category must not rank as a row that spent.
    """
    rows = session.exec(
        select(
            Transaction.category_id,
            func.sum(func.coalesce(Transaction.cad_amount, 0)).label("cad_total"),
        )
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(
            *countable_tx_conditions_for(ownership),
            *transfer_exclusion_conditions(*transfer_users),
            Transaction.date >= from_,
            Transaction.date <= to,
            func.coalesce(Transaction.cad_amount, 0) < 0,
        )
        .group_by(Transaction.category_id)
    ).all()
    totals: dict[int | None, Decimal] = defaultdict(lambda: _ZERO)
    for category_id, cad_total in rows:
        totals[category_id] += -cad_total  # SQL summed the raw negatives
    return totals


def category_tag_matrix(
    session: Session, user_id: int, from_: dt.date, to: dt.date, *, household: bool = False
) -> dict[str, object]:
    """The Category x Tag matrix (M124 R3, docs/12 §1).

    Rows are RAW categories, not root-rolled: the question this answers is
    "how much did I spend on *Groceries* tagged vacation", and Groceries is a
    child of Food — rolling up would erase the row the user came for. That is
    the one place the matrix deliberately differs from the donut above it,
    and the caption says so (docs/09 §honesty).

    Columns are the `_MATRIX_TOP_TAGS` largest tags by spend plus "Untagged",
    always last. A transaction with several tags lands in EACH of its tag
    cells — the same duplication the tag grouping does — so cells can sum to
    more than was spent; the report's `total_cad` is the once-counted figure
    the caption points at.

    `household` (M179, docs/12 §Owner dimension) widens ONLY who is summed —
    the caller's own committed rows plus the owner's allowlisted rows through
    the caller's own active `sharing_links` row (docs/43 §6). The matrix is the
    Categories tab's second panel over the same population as the donut above
    it, so leaving it on `own` under the Owner lens showed two populations on
    one screen. Rows, columns, ranking, the top-8 cut and the long-form cells
    are byte-identical to `own`: the scope changes WHO is summed, never the
    math or the shape.
    """
    ownership, _ = household_ownership(session, user_id, household=household)
    # Resolved BEFORE the two readers: the transfer rule (docs/12 §Shared
    # population rules) applies to this panel exactly as it does to the donut
    # above it — same population, same money, two shapes.
    counterpart_id = household_counterpart(session, user_id, household=household)
    transfer_users = (user_id, counterpart_id)
    cells = _matrix_cells(session, ownership, transfer_users, from_, to)
    row_totals = _matrix_row_totals(session, ownership, transfer_users, from_, to)
    categories = _categories(session, user_id)
    tag_names = _tag_names(session, user_id)
    # M179: a projected row carries the OWNER's category/tag ids (docs/43 §6),
    # so both tenants' trees are layered — the counterpart names a foreign id,
    # and the caller's entries stay for the ids it alone carries. Without it a
    # household cell would print `Uncategorized`/an id-less tag.
    if counterpart_id is not None:
        categories.update(_categories(session, counterpart_id))
        tag_names.update(_tag_names(session, counterpart_id))

    # Rank the columns by spend across every category, then truncate. Any
    # spend under a tag below the cut becomes ONE labelled "Other tags"
    # column rather than disappearing (docs/09 §Hidden data: truncation that
    # hides money has to be visible as its own state, not silently dropped).
    tag_spend: dict[int, Decimal] = defaultdict(lambda: _ZERO)
    for (_, tag_id), cad_total in cells.items():
        if tag_id is not None:
            tag_spend[tag_id] += cad_total
    ranked = sorted(tag_spend, key=lambda tid: (-tag_spend[tid], tag_names.get(tid, "")))
    leading, spilled = ranked[:_MATRIX_TOP_TAGS], ranked[_MATRIX_TOP_TAGS:]

    # `(tag_id, label, spilled)` — `spilled` marks the synthetic column that
    # absorbs every tag past the cut, so its cell is a SUM, not a lookup.
    columns: list[tuple[int | None, str, bool]] = [
        (tag_id, tag_names[tag_id], False) for tag_id in leading
    ]
    if spilled:
        columns.append((None, _OTHER_TAGS, True))
    columns.append((None, _UNTAGGED, False))

    ranked_rows = [cid for cid, total in row_totals.items() if total > _ZERO]
    ranked_rows.sort(
        key=lambda cid: (
            -row_totals[cid],
            _UNCATEGORIZED if cid is None else categories[cid].name,
        )
    )

    items: list[dict[str, object]] = []
    for category_id in ranked_rows:
        category_name = _UNCATEGORIZED if category_id is None else categories[category_id].name
        for tag_id, label, spilled_column in columns:
            if spilled_column:
                cad_total = sum((cells.get((category_id, past), _ZERO) for past in spilled), _ZERO)
            else:
                cad_total = cells.get((category_id, tag_id), _ZERO)
            if cad_total <= _ZERO:  # long form: zero cells are absent, not blank
                continue
            items.append(
                {
                    "category_id": category_id,
                    "category_name": category_name,
                    "tag_id": tag_id,
                    "tag_name": label,
                    "cad_total": _cents(cad_total),
                }
            )

    return {
        "from_": from_,
        "to": to,
        "columns": [{"tag_id": tag_id, "tag_name": label} for tag_id, label, _spilled in columns],
        "rows": [
            {
                "category_id": category_id,
                "category_name": (
                    _UNCATEGORIZED if category_id is None else categories[category_id].name
                ),
            }
            for category_id in ranked_rows
        ],
        "items": items,
        "total_cad": _cents(sum(row_totals.values(), _ZERO)),
    }


# --- 3. Cash flow ------------------------------------------------------------


def cashflow_report(
    session: Session, user_id: int, from_: dt.date, to: dt.date, granularity: Granularity
) -> dict[str, object]:
    starts = bucket_starts(from_, to, granularity)
    index = {start: i for i, start in enumerate(starts)}
    income = [_ZERO] * len(starts)
    expenses = [_ZERO] * len(starts)
    reporting = get_settings().reporting_currency
    fx = FxCache(session, reporting)

    def convert(amount: Decimal, currency: str, day: dt.date) -> Decimal | None:
        rate = fx.rate(currency, reporting, day)
        return None if rate is None else amount * rate

    # Transfers are neither income nor expense (docs/12 §Shared population
    # rules): a chequing → TFSA move must not raise Expenses on the way out AND
    # Income on the way in, which is what counting one leg and not the other
    # would do. Trade-level `transfer_in|transfer_out` was already excluded by
    # the action sets below; this is the transaction-level twin of that rule.
    for txn in _committed_transactions(
        session, user_id, from_, to, exclude_transfers_for=(user_id,)
    ):
        if txn.cad_amount is None or txn.cad_amount == 0:
            continue  # unresolved CAD excluded from both sides
        i = index[bucket_key(txn.date, granularity)]
        if txn.cad_amount > 0:
            income[i] += txn.cad_amount
        else:
            expenses[i] += -txn.cad_amount

    trades = session.exec(
        select(Trade, Account)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .join(Account, Trade.account_id == Account.id)
        .where(*committed_trade_conditions(user_id), Trade.date >= from_, Trade.date <= to)
        .order_by(Trade.date, Trade.id)  # type: ignore[attr-defined]
    ).all()
    for trade, account in trades:
        i = index[bucket_key(trade.date, granularity)]
        # M132 (docs/05 §Portfolio value): the amounts below are STATED in the
        # ROW's currency, so they convert at it — never face-converted at the
        # account's. A row with no currency (pre-M132) reads as the account's,
        # which is exactly what this did before.
        row_currency = trade.currency or account.currency
        # Principal of buy|sell|drip|corporate_action_adjustment is deliberately
        # NOT counted (position change, docs/12): only the action sets below and
        # commissions touch income/expenses.
        if trade.action in CASH_INFLOW_ACTIONS:
            amount = convert(trade.price_native, row_currency, trade.date)
            if amount is not None:
                income[i] += amount
        elif trade.action in CASH_OUTFLOW_ACTIONS:
            amount = convert(trade.price_native, row_currency, trade.date)
            if amount is not None:
                expenses[i] += amount
        if trade.action in COMMISSION_ACTIONS:
            fee = convert(trade.commission, row_currency, trade.date)
            if fee is not None:
                expenses[i] += fee

    buckets = [
        {
            "bucket": start,
            "income_cad": _cents(income[i]),
            "expenses_cad": _cents(expenses[i]),
            "net_cad": _cents(income[i] - expenses[i]),
        }
        for i, start in enumerate(starts)
    ]
    return {"from_": from_, "to": to, "granularity": granularity, "buckets": buckets}


# --- 4. Net worth history ----------------------------------------------------


def _apply_trade_to_position(positions: dict[tuple[int, int], Decimal], trade: Trade) -> None:
    key = (trade.account_id, trade.security_id)
    if trade.action == TradeAction.corporate_action_adjustment:
        # quantity>0 is the RESULTING absolute share count (split/consolidation),
        # mirroring app.tax.acb replay semantics; quantity=0 carries an ACB delta
        # and has no share effect.
        if trade.quantity != 0:
            positions[key] = trade.quantity
    else:
        positions[key] += trade.quantity  # sells are stored negative


def net_worth_report(
    session: Session, user_id: int, from_: dt.date, to: dt.date, granularity: Granularity
) -> dict[str, object]:
    starts = bucket_starts(from_, to, granularity)
    ends = [bucket_end(s, granularity) for s in starts]
    reporting = get_settings().reporting_currency
    fx = FxCache(session, reporting)
    prices = PriceCache(session, user_id)

    accounts = session.exec(select(Account).where(Account.user_id == user_id)).all()
    currency_of = {a.id: a.currency for a in accounts}

    cash: dict[int, Decimal] = {a.id: (a.starting_balance or _ZERO) for a in accounts}
    positions: dict[tuple[int, int], Decimal] = defaultdict(lambda: _ZERO)

    txns = _committed_transactions(session, user_id)  # full history: balances accumulate
    trades = session.exec(
        select(Trade)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(*committed_trade_conditions(user_id))
        .order_by(Trade.date, Trade.id)  # type: ignore[attr-defined]
    ).all()

    # ONE securities read for the whole report (docs/34). `positions` only ever
    # gains keys from `trades`, so the union of trade security ids is a superset
    # of every per-bucket `by_security` key — the per-bucket query was an N+1
    # that scaled with the bucket count (18 queries monthly, 368 daily).
    trade_security_ids = {t.security_id for t in trades}
    listings = (
        {
            s.id: s.listing_currency
            for s in session.exec(select(Security).where(Security.id.in_(trade_security_ids)))
        }
        if trade_security_ids
        else {}
    )

    buckets: list[dict[str, object]] = []
    ti = tj = 0
    for end in ends:
        while ti < len(txns) and txns[ti].date <= end:
            cash[txns[ti].account_id] += txns[ti].amount_native
            ti += 1
        while tj < len(trades) and trades[tj].date <= end:
            _apply_trade_to_position(positions, trades[tj])
            tj += 1

        cash_cad = _ZERO
        # M97 (docs/12 §4): the SAME signed sums also split into assets and
        # liabilities so the net-worth tooltip can show why the number is what
        # it is. Every contribution is routed by sign (a negative balance —
        # credit card, line of credit, overdraft — is a liability, an
        # overpaid credit card is an asset), so `assets - liabilities` is
        # exactly `cash + positions`: net_cad is untouched, this is a
        # presentation split of one sum. Each cell quantizes independently,
        # so assets_cad - liabilities_cad can differ from net_cad by a cent
        # the same way cash_cad + positions_cad can (docs/12 §4).
        assets = _ZERO
        liabilities = _ZERO
        for account in accounts:
            rate = fx.rate(currency_of[account.id], reporting, end)
            if rate is not None:  # unresolved → excluded, never fetched
                balance = cash[account.id] * rate
                cash_cad += balance
                if balance < 0:
                    liabilities -= balance
                else:
                    assets += balance

        by_security: dict[int, Decimal] = defaultdict(lambda: _ZERO)
        for (_account_id, security_id), quantity in positions.items():
            by_security[security_id] += quantity

        positions_cad = _ZERO
        for security_id, quantity in by_security.items():
            if quantity == 0:
                continue
            price = prices.price(security_id, end)
            rate = fx.rate(listings.get(security_id, reporting), reporting, end)
            if price is None or rate is None:
                continue  # excluded rather than fetched
            positions_cad += quantity * price * rate

        if positions_cad < 0:
            liabilities -= positions_cad
        else:
            assets += positions_cad

        buckets.append(
            {
                "bucket_end": end,
                "cash_cad": _cents(cash_cad),
                "positions_cad": _cents(positions_cad),
                "net_cad": _cents(cash_cad + positions_cad),
                "assets_cad": _cents(assets),
                "liabilities_cad": _cents(liabilities),
            }
        )
    return {"from_": from_, "to": to, "granularity": granularity, "buckets": buckets}


# --- 5. Investment performance ----------------------------------------------


def investment_performance_report(
    session: Session, user_id: int, from_: dt.date, to: dt.date
) -> dict[str, object]:
    state = compute_user_tax_state(session, user_id)  # single source of gain math
    realized: dict[int, Decimal] = defaultdict(lambda: _ZERO)
    touched: set[int] = set()
    for disposal in state.disposals:  # non-registered sells only, by construction
        if from_ <= disposal.date <= to:
            realized[disposal.security_id] += disposal.gain_cad
            touched.add(disposal.security_id)
    for denial in state.denials:  # layered onto in-range sales like tax/reports.py
        if from_ <= denial.sale_date <= to:
            realized[denial.security_id] += denial.denied_loss_cad
            touched.add(denial.security_id)

    # Current positions across ALL account types (net worth ≠ taxable).
    positions: dict[tuple[int, int], Decimal] = defaultdict(lambda: _ZERO)
    for trade in session.exec(
        select(Trade)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(*committed_trade_conditions(user_id))
    ).all():
        _apply_trade_to_position(positions, trade)
    holdings: dict[int, Decimal] = defaultdict(lambda: _ZERO)
    for (_account_id, security_id), quantity in positions.items():
        holdings[security_id] += quantity

    pool_qty: dict[int, Decimal] = defaultdict(lambda: _ZERO)
    pool_acb: dict[int, Decimal] = defaultdict(lambda: _ZERO)
    for (_account_id, security_id), pool in state.pools.items():  # non-registered only
        pool_qty[security_id] += pool.quantity
        pool_acb[security_id] += pool.total_acb_cad

    securities = {
        s.id: s for s in session.exec(select(Security).where(Security.user_id == user_id))
    }
    reporting = get_settings().reporting_currency
    fx = FxCache(session, reporting)
    prices = PriceCache(session, user_id)
    today = dt.date.today()

    items: list[dict[str, object]] = []
    ids = set(touched) | {sid for sid, q in holdings.items() if q != 0}
    for security_id in sorted(ids, key=lambda sid: securities[sid].symbol):
        security = securities[security_id]
        quantity = holdings[security_id]
        price = prices.price(security_id, today)
        rate = fx.rate(security.listing_currency, reporting, today)
        market_value = (
            _cents(quantity * price * rate) if price is not None and rate is not None else _ZERO
        )
        acb_pool = pool_acb[security_id] if security_id in pool_acb else None
        unrealized = None
        if acb_pool is not None and price is not None and rate is not None:
            unrealized = _cents(pool_qty[security_id] * price * rate) - acb_pool
        items.append(
            {
                "security_id": security_id,
                "symbol": security.symbol,
                "quantity": quantity,
                "realized_cad": _cents(realized[security_id]),
                "unrealized_cad": unrealized,
                "acb_pool_cad": acb_pool,
                "market_value_cad": market_value,
            }
        )

    totals = {
        "realized_cad": sum((i["realized_cad"] for i in items), _ZERO),
        "unrealized_cad": sum(
            (i["unrealized_cad"] for i in items if i["unrealized_cad"] is not None), _ZERO
        ),
        "market_value_cad": sum((i["market_value_cad"] for i in items), _ZERO),
    }
    return {"items": items, "totals": totals}


# --- 8. Cash-flow Sankey (M77, docs/12 §Sankey cash-flow) --------------------

_SANKEY_INFLOW_LABELS: dict[TradeAction, str] = {
    TradeAction.dividend: "Dividends",
    TradeAction.distribution: "Distributions",
    TradeAction.return_of_capital: "Return of capital",
    TradeAction.contribution: "Contributions",
}
_SANKEY_OUTFLOW_LABELS: dict[TradeAction, str] = {
    TradeAction.fee: "Fees",
    TradeAction.withdrawal: "Withdrawals",
}
_SANKEY_COMMISSION_LABEL = "Trading commissions"
_SANKEY_INVESTED_ID = "dst:invested"
_SANKEY_INVESTED_LABEL = "Investments"
_SANKEY_SAVED_ID = "dst:saved"
_SANKEY_SAVED_LABEL = "Savings"
_SANKEY_DRAWN_ID = "src:drawn"
_SANKEY_DRAWN_LABEL = "Transfers & savings in"
_SANKEY_TOTAL_ID = "total:in"
_SANKEY_TOTAL_LABEL = "Money in"
_BUY_LIKE = (TradeAction.buy, TradeAction.drip)


def cashflow_sankey(
    session: Session, user_id: int, from_: dt.date, to: dt.date
) -> dict[str, object]:
    """Three-column money-flow graph: sources -> total -> destinations (M77).

    Population and signs are `cashflow_report`'s exactly (docs/12 §3): positive
    transaction CAD + trade cash inflows are sources; negative transaction CAD
    + trade cash outflows + buy/sell/drip commissions are destinations. Trade
    principal is not income/expense, but `buy`/`drip` principal IS the money
    that left the cash pool for securities, so it lands on the `Investments`
    destination; whatever is left over lands on `Savings`.

    Two deliberate conventions (both documented in docs/12):

    * **Every converted row is quantized to cents once before it is summed.**
      Sums are therefore exact integers in cents and the graph conserves to
      the cent — at most one cent of divergence per trade row from the
      end-of-sum rounding `cashflow_report` uses.
    * **Source -> total -> destination is the only shape.** No per-source
      allocation exists in the data (money is fungible), so the graph never
      invents a "salary paid for groceries" link: sources sum into one
      `Money in` node and the destinations fan back out from it.

    A destination total above income (spending/investing more than came in)
    is balanced by the `Transfers & savings in` source, which also covers the
    `transfer_in`/`transfer_out` share movements that docs/06 treats as
    acquisitions/dispositions and the cash-flow report deliberately excludes —
    and the transaction-level transfer rows the same rule now drops
    (docs/12 §Shared population rules).
    """
    reporting = get_settings().reporting_currency
    fx = FxCache(session, reporting)
    categories = {
        c.id: c for c in session.exec(select(Category).where(Category.user_id == user_id))
    }

    def to_cad(amount: Decimal, currency: str, day: dt.date) -> Decimal | None:
        rate = fx.rate(currency, reporting, day)
        return None if rate is None else _cents(amount * rate)

    src_amount: dict[str, Decimal] = {}
    src_label: dict[str, str] = {}
    src_group: dict[str, str] = {}
    dst_amount: dict[str, Decimal] = {}
    dst_label: dict[str, str] = {}
    dst_group: dict[str, str] = {}

    def credit(key: str, label: str, group: str, amount: Decimal, *, income: bool) -> None:
        store_amount = src_amount if income else dst_amount
        store_label = src_label if income else dst_label
        store_group = src_group if income else dst_group
        if amount == _ZERO:
            return
        store_amount[key] = store_amount.get(key, _ZERO) + amount
        store_label[key] = label
        store_group[key] = group

    for txn in _committed_transactions(
        session, user_id, from_, to, exclude_transfers_for=(user_id,)
    ):
        if txn.cad_amount is None or txn.cad_amount == 0:
            continue  # unresolved CAD excluded from both sides (docs/12 §3)
        if txn.category_id is None:
            root: int | None = None
            label = _UNCATEGORIZED
        else:
            root = _top_ancestor(categories, txn.category_id)
            label = categories[root].name
        # `"null"` (JSON), not Python's `None` — the node id is part of the
        # byte-identical parity contract with lib/sankeyEngine.ts.
        slot = "src" if txn.cad_amount > 0 else "dst"
        suffix = str(root) if root is not None else "null"
        credit(
            f"{slot}:cat:{suffix}",
            label,
            "income" if slot == "src" else "spent",
            txn.cad_amount if slot == "src" else -txn.cad_amount,
            income=slot == "src",
        )

    trades = session.exec(
        select(Trade, Account)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .join(Account, Trade.account_id == Account.id)
        .where(*committed_trade_conditions(user_id), Trade.date >= from_, Trade.date <= to)
        .order_by(Trade.date, Trade.id)  # type: ignore[attr-defined]
    ).all()
    for trade, account in trades:
        # M132: the row's own currency (docs/02) — a USD buy inside a CAD
        # account is USD money, converted at USD, not face-converted at CAD.
        row_currency = trade.currency or account.currency
        if trade.action in CASH_INFLOW_ACTIONS:
            cad = to_cad(trade.price_native, row_currency, trade.date)
            if cad is not None:
                credit(
                    f"src:trade:{trade.action}",
                    _SANKEY_INFLOW_LABELS[trade.action],
                    "income",
                    cad,
                    income=True,
                )
        elif trade.action in CASH_OUTFLOW_ACTIONS:
            cad = to_cad(trade.price_native, row_currency, trade.date)
            if cad is not None:
                credit(
                    f"dst:trade:{trade.action}",
                    _SANKEY_OUTFLOW_LABELS[trade.action],
                    "spent",
                    cad,
                    income=False,
                )
        if trade.action in COMMISSION_ACTIONS:
            fee = to_cad(trade.commission, row_currency, trade.date)
            if fee is not None:
                credit(
                    "dst:trade:commission",
                    _SANKEY_COMMISSION_LABEL,
                    "spent",
                    fee,
                    income=False,
                )
        if trade.action in _BUY_LIKE and trade.quantity != 0:
            principal = to_cad(trade.quantity * trade.price_native, row_currency, trade.date)
            if principal is not None:
                credit(
                    _SANKEY_INVESTED_ID,
                    _SANKEY_INVESTED_LABEL,
                    "invested",
                    principal,
                    income=False,
                )

    income_total = sum(src_amount.values(), _ZERO)
    expenses_total = sum(
        (amount for key, amount in dst_amount.items() if dst_group[key] == "spent"),
        _ZERO,
    )
    invested_total = dst_amount.get(_SANKEY_INVESTED_ID, _ZERO)
    surplus = income_total - expenses_total - invested_total
    saved_total = surplus if surplus > _ZERO else _ZERO
    drawn_total = -surplus if surplus < _ZERO else _ZERO
    if saved_total != _ZERO:
        dst_amount[_SANKEY_SAVED_ID] = saved_total
        dst_label[_SANKEY_SAVED_ID] = _SANKEY_SAVED_LABEL
        dst_group[_SANKEY_SAVED_ID] = "saved"
    if drawn_total != _ZERO:
        src_amount[_SANKEY_DRAWN_ID] = drawn_total
        src_label[_SANKEY_DRAWN_ID] = _SANKEY_DRAWN_LABEL
        src_group[_SANKEY_DRAWN_ID] = "drawn"
    money_in = income_total + drawn_total

    sources = sorted(src_amount, key=lambda k: (-src_amount[k], src_label[k], k))
    destinations = sorted(dst_amount, key=lambda k: (-dst_amount[k], dst_label[k], k))

    nodes: list[dict[str, object]] = [
        {
            "id": key,
            "column": "source",
            "label": src_label[key],
            "amount_cad": src_amount[key],
            "group": src_group[key],
        }
        for key in sources
    ]
    if money_in != _ZERO or sources or destinations:
        nodes.append(
            {
                "id": _SANKEY_TOTAL_ID,
                "column": "total",
                "label": _SANKEY_TOTAL_LABEL,
                "amount_cad": money_in,
                "group": None,
            }
        )
    nodes.extend(
        {
            "id": key,
            "column": "destination",
            "label": dst_label[key],
            "amount_cad": dst_amount[key],
            "group": dst_group[key],
        }
        for key in destinations
    )

    links: list[dict[str, object]] = [
        {"source": key, "target": _SANKEY_TOTAL_ID, "amount_cad": src_amount[key]}
        for key in sources
    ]
    links.extend(
        {"source": _SANKEY_TOTAL_ID, "target": key, "amount_cad": dst_amount[key]}
        for key in destinations
    )

    return {
        "from_": from_,
        "to": to,
        "columns": ["source", "total", "destination"],
        "nodes": nodes,
        "links": links,
        "totals": {
            "income_cad": _cents(income_total),
            "expenses_cad": _cents(expenses_total),
            "invested_cad": _cents(invested_total),
            "saved_cad": _cents(saved_total),
            "drawn_cad": _cents(drawn_total),
            "money_in_cad": _cents(money_in),
        },
    }

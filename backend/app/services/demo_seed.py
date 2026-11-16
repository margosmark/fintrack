"""Demo account seeding (w8-contract): one deterministic 12-month dataset.

`ensure_demo_account` is the single entry point — idempotent, called from the
app lifespan (startup) and from POST /auth/demo. The dataset is generated with
a fixed RNG seed and dates RELATIVE TO TODAY so charts stay alive; every fake
price row is labelled source=manual (never yfinance).

`fx_rates` is NEVER seeded (H7, docs/07): it is the one SHARED cache (no
user_id) feeding every tenant's CAD conversions, ACB tax math and reports, so
fabricated rows would be permanent and uncorrectable (no fx delete endpoint,
and `_store_fetched` only fills empty dates). The demo's USD trades leave
their fx gaps empty — the ACB pool rebuild defers on the documented 422
`fx_rate_missing` (docs/06) until a BoC fetch or a manual gap-fill supplies
the trade dates.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import random
import time
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.errors import APIError
from app.core.rls import set_tenant_now
from app.core.security import hash_password
from app.models.accounts import Account, AccountType
from app.models.backup import BackupArtifact
from app.models.bill_reminders import BillReminder, BillReminderMatchTerm
from app.models.budgets import Budget
from app.models.categories import Category
from app.models.debts import Debt
from app.models.goals import Goal
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.institutions import Institution
from app.models.mcp_tokens import McpToken
from app.models.prices import Price, PriceSource
from app.models.refresh_tokens import RefreshToken
from app.models.rule_tags import RuleTag
from app.models.rules import Rule, RuleMatchType
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.transaction_tags import TransactionTag
from app.models.transactions import Transaction
from app.models.users import User
from app.models.vault import VaultEnvelope
from app.tax.acb import cents

logger = logging.getLogger(__name__)

DEMO_EMAIL = "demo@fintracker.local"
_SEED = 20260824
_ZERO = Decimal("0")

# Auth credentials a demo reset KEEPS (and `seed_fingerprint` skips): refresh
# tokens are minted per login and MCP tokens are minted per connection — by an
# operator before M81, by any visitor since M81 (docs/40 §1: the demo tenant
# mints `read_only` tokens only), so neither is seed state — wiping them would
# sign other visitors out and revoke MCP access without protecting anything a
# visitor can write: a read-only token never reaches a mutating tool, and the
# backend demo guard still answers 403 if one somehow did.
_CREDENTIAL_MODELS: tuple[type, ...] = (RefreshToken, McpToken)

# Tables a demo reset wipes BEYOND `_USER_OWNED_MODELS` (M63, extended M63b).
# Every entry here shares M63's shape: `delete_user_rows` deliberately does
# not reach the table, and an `ON DELETE CASCADE` FK never fires either,
# because the reset deliberately KEEPS the `users` row — so nothing else would
# ever remove a visitor's leftover rows. Wiped here, in the same transaction
# as the cascade, so a failure anywhere still rolls the whole reset back.
#
# `backup_artifacts` (M63): outside `_USER_OWNED_MODELS` on purpose — a
# restore must not delete the artifacts it is restoring from
# (`app/models/backup.py`, docs/11). Cross-reset custody is NOT intended for
# the demo tenant: a leftover artifact is a visitor's state, not seed content.
#
# `vault_envelopes` (M63b): outside `_USER_OWNED_MODELS` for the *restore*
# reason in docs/11 — replaying a snapshot would un-revoke a revoked device —
# and a reset wipes rather than replays, so that reason does not apply here
# either. The demo tenant can never enrol a server-side device in the first
# place: `POST /vault/envelopes` inherits `get_current_user`'s
# `403 demo_account_restricted` and docs/33 pins demo enrolment as
# local-only, so no legitimate demo row exists. A stray one (guard gap,
# migration, bug) would outlive the reset forever AND stay invisible to
# `seed_fingerprint`, which reads only what the reset wipes. Decided M63b;
# this lifts M63's "do not touch M53 files" fence for the reset's DATA
# LIFECYCLE only — no E2EE crypto behaviour changes.
_RESET_EXTRA_MODELS: tuple[type, ...] = (BackupArtifact, VaultEnvelope)

# User-owned tables a demo reset leaves ON PURPOSE, with the reason written
# down beside them — M63's "no silent behaviour" map. Every table carrying
# `user_id` must sit in `_USER_OWNED_MODELS` (minus `_CREDENTIAL_MODELS`),
# `_RESET_EXTRA_MODELS` or here, or
# `tests/test_demo_seed.py::test_every_user_owned_table_is_classified_for_demo_reset`
# fails and names it.
# Empty since M63b: `vault_envelopes`, the one entry M63 left undecided
# ("out of M63 scope"), now carries a real decision in `_RESET_EXTRA_MODELS`.
# An entry here is an assertion that a visitor's row MAY outlive a reset, so
# it needs a reason nobody could say about the seed itself.
_DELIBERATELY_SURVIVING_MODELS: dict[type, str] = {}

# Weekly price series: symbol → (start, end) gentle uptrend over ~61 Fridays.
_PRICE_ARCS: tuple[tuple[str, str, str], ...] = (
    ("RY", "118", "152"),
    ("TD", "88", "107"),
    ("BMO", "128", "161"),
    ("ENB", "57", "66"),
    ("VTI", "215", "288"),
    ("VXUS", "54", "63"),
    ("NVDA", "96", "178"),
)


def _shift_month(d: date, k: int) -> date:
    """First day of the month `k` months from `d`'s month."""
    total = d.year * 12 + (d.month - 1) + k
    return date(total // 12, total % 12 + 1, 1)


def _month_last(ms: date) -> date:
    return _shift_month(ms, 1) - timedelta(days=1)


def _add_default_categories(session: Session, user: User) -> None:
    """The ten `is_system` categories every tenant starts with (docs/02).

    Shared by the create-user path in `ensure_demo_account` and by
    `reset_demo_tenant`, which wipes them along with the rest of the tenant's
    rows and must put back exactly what a fresh seed would create.
    """
    from app.api.auth import DEFAULT_CATEGORIES  # lazy: auth imports this module

    if user.id is None:  # callers flush the user before seeding
        raise RuntimeError("demo seed called with an unflushed user (id is None)")
    for name in DEFAULT_CATEGORIES:
        session.add(Category(user_id=user.id, name=name, is_system=True))


def ensure_demo_account(session: Session) -> User | None:
    """Idempotently create the demo user and its seeded data.

    Returns None when SEED_DEMO_ACCOUNT is off. Re-seed guard per w8-contract
    pin 2: the user exists AND already has ≥1 transaction.
    """
    settings = get_settings()
    if not settings.seed_demo_account:
        return None

    user = session.exec(select(User).where(User.email == DEMO_EMAIL)).first()
    if user is None:
        # Mirrors the register() path: system categories inside ONE transaction.
        user = User(email=DEMO_EMAIL, hashed_password=hash_password(settings.demo_password))
        session.add(user)
        session.flush()
        if user.id is None:  # flush() assigned the id
            raise RuntimeError("demo seed: user id was not assigned by flush()")
        # docs/55: stamp the new tenant onto the live transaction so the
        # seed's tenant-row writes pass the RLS WITH CHECK when this session
        # is the request-path app role (POST /auth/demo). A maintenance-
        # engine session is exempt and treats it as a no-op GUC write.
        set_tenant_now(session, user.id)
        _add_default_categories(session, user)
        session.commit()
        session.refresh(user)
    else:
        # The re-seed / lifestyle-backfill arms below write tenant rows too;
        # re-stamp so an anonymous POST /auth/demo against a partially seeded
        # demo tenant is scoped as the demo tenant, not denied as nobody.
        if user.id is None:
            raise RuntimeError("demo seed: user id was not assigned by flush()")
        set_tenant_now(session, user.id)

    already = session.exec(
        select(Transaction.id).where(Transaction.user_id == user.id).limit(1)
    ).first()
    if already is not None:
        # Pre-M45 demo: backfill lifestyle tables, then return (idempotent).
        added = _ensure_lifestyle(session, user)
        if any(added.values()):
            session.commit()
        return user

    started = time.perf_counter()
    counts = _seed_dataset(session, user)
    session.commit()
    logger.info(
        "Demo dataset seeded for %s in %.2fs: %s",
        DEMO_EMAIL,
        time.perf_counter() - started,
        counts,
    )
    return user


def seed_demo_from_engine() -> User | None:
    """Lifespan helper: own engine session, isolated from request state."""
    from app.api.deps import get_engine  # lazy: deps builds on settings at boot

    eng = get_engine()
    logger.debug("demo-seed engine url: %s", eng.url)
    with Session(eng, expire_on_commit=False) as session:
        return ensure_demo_account(session)


def reset_demo_tenant(session: Session) -> User | None:
    """Wipe the demo tenant's data and re-run the M10 seed (docs/20, docs/30).

    Returns None when `SEED_DEMO_ACCOUNT` is off, so `POST /auth/demo/reset`
    can answer the same 404 `demo_disabled` kill switch as `POST /auth/demo`.

    ONE transaction end to end: `delete_user_rows` executes the FK-safe
    cascade but commits nothing, and `ensure_demo_account` commits only after
    the whole dataset is rebuilt — so a failure anywhere rolls the wipe back
    with it. A half-wiped demo would be worse than no reset.

    What survives: the `users` row (tenant identity — live sessions keep
    resolving), the credential tables (see `_CREDENTIAL_MODELS`) and the
    tables named in `_DELIBERATELY_SURVIVING_MODELS` (each with its reason —
    empty since M63b). Wiped beyond the shared cascade: `_RESET_EXTRA_MODELS`
    — M63's `backup_artifacts` (a restore keeps its own source, and that
    reason does not apply to a reset) and M63b's `vault_envelopes` (device
    enrolment: the demo tenant can never enrol a server-side device, docs/33,
    so a stray row is bug state that must not outlive the reset).
    What is never touched: `fx_rates`, the one shared, `user_id`-less cache (H7).
    """
    if not get_settings().seed_demo_account:
        return None
    user = session.exec(select(User).where(User.email == DEMO_EMAIL)).first()
    if user is None:
        # Nothing to reset: a fresh seed lands on the same end state.
        return ensure_demo_account(session)

    from app.api.users import delete_user_rows  # lazy: users imports this module

    delete_user_rows(session, user, keep=_CREDENTIAL_MODELS)
    # Tables the FK-safe cascade deliberately does not cover (M63/M63b). Same
    # session, same transaction: these DELETEs roll back with the rest.
    for model in _RESET_EXTRA_MODELS:
        session.exec(sa_delete(model).where(model.user_id == user.id))
    # Emit every DELETE before the re-seed's INSERTs — categories and accounts
    # are wiped and recreated in the same transaction, and their unique keys
    # must be free when the inserts land.
    session.flush()
    _add_default_categories(session, user)
    session.flush()
    return ensure_demo_account(session)


# Columns that carry no seed intent: serial PKs, tenancy and server timestamps.
# FK ids are unstable by construction (a wipe does not rewind the sequences),
# so they are not skipped — they are RESOLVED to natural keys below.
_FINGERPRINT_SKIP = frozenset({"id", "user_id", "created_at", "updated_at"})
# Per-column exclusions on `users` only: bcrypt salts differ per hash (so a
# fresh hash of the same password would change the digest) and `llm_api_key`
# is a secret that has no business feeding a digest input.
_USER_FINGERPRINT_SKIP = frozenset({"hashed_password", "llm_api_key"})


def seed_fingerprint(session: Session, user_id: int) -> str:
    """sha256 hex digest over a tenant's CONTENT rows (M10 / M50, docs/30).

    This is the idempotence oracle docs/10 has always claimed for the seed:
    `seed_fingerprint(fresh seed) == seed_fingerprint(after a reset)` even
    though every serial id differs. Rules:

    - ids excluded, FK ids resolved to natural keys (`account -> name|type`,
      `category -> name`, `security -> symbol`, ...), built from the rows
      themselves so the key never depends on an unstable sequence;
    - the model set is `_USER_OWNED_MODELS` minus the credential tables, plus
      `_RESET_EXTRA_MODELS` — i.e. exactly what `reset_demo_tenant` wipes,
      cascade and all (M63/M63b) — plus `users` and the two junction tables;
    - rows serialize as sorted-key JSON, are sorted as a set, then hashed, so
      neither insert order nor a table's physical order can move the digest.
    """
    from app.api.users import _USER_OWNED_MODELS  # lazy: users imports this module

    # Exactly what `reset_demo_tenant` deletes: the shared cascade minus the
    # credential tables, plus the cascade's extra tables (M63). `dict.fromkeys`
    # keeps order and de-duplicates, so a table that later joins both lists is
    # still fingerprinted once.
    cascade = dict.fromkeys([*_USER_OWNED_MODELS, *_RESET_EXTRA_MODELS])
    models = [User, *(m for m in cascade if m not in _CREDENTIAL_MODELS)]

    # FK column -> referenced table, read off the models themselves so no
    # hand-maintained map can drift when a table is added.
    fk_targets: dict[str, str] = {}

    def _record_fk(model: type) -> None:
        for column in model.__table__.columns:
            for fk in column.foreign_keys:
                target = fk.target_fullname.split(".")[0]
                seen = fk_targets.setdefault(column.name, target)
                if seen != target:
                    raise RuntimeError(
                        f"ambiguous fingerprint FK {column.name!r}: {seen} != {target}"
                    )

    for model in [*models, RuleTag, TransactionTag]:
        _record_fk(model)

    owned: dict[str, list] = {
        "users": list(session.exec(select(User).where(User.id == user_id)).all())
    }
    for model in models:
        if model is User:
            continue
        owned[model.__tablename__] = list(
            session.exec(select(model).where(model.user_id == user_id)).all()
        )
    # Junction rows carry no user_id (docs/20) — reach them through parents.
    owned["rule_tags"] = list(
        session.exec(
            select(RuleTag).where(
                RuleTag.rule_id.in_(select(Rule.id).where(Rule.user_id == user_id))
            )
        ).all()
    )
    owned["transaction_tags"] = list(
        session.exec(
            select(TransactionTag).where(
                TransactionTag.transaction_id.in_(
                    select(Transaction.id).where(Transaction.user_id == user_id)
                )
            )
        ).all()
    )

    natural: dict[str, dict[int, str]] = {}

    def _ref(table: str, key: int | None) -> str:
        if key is None:
            return "null"
        return natural.get(table, {}).get(key, f"?{key}")

    # Natural keys, built in this order; each reads only content columns (plus
    # already-built keys), never an id of its own table.
    key_specs: list[tuple[str, Callable[[Any], str]]] = [
        ("institutions", lambda r: str(r.name)),
        ("accounts", lambda r: f"{r.name}|{r.type}"),
        ("categories", lambda r: str(r.name)),
        ("securities", lambda r: str(r.symbol)),
        ("rules", lambda r: str(r.pattern)),
        ("tags", lambda r: str(r.name)),
        ("import_batches", lambda r: f"{_ref('accounts', r.account_id)}|{r.file_name}"),
        (
            "transactions",
            lambda r: (
                f"{_ref('accounts', r.account_id)}|{r.date}|{r.raw_description}|{r.amount_native}"
            ),
        ),
        ("budgets", lambda r: f"{_ref('categories', r.category_id)}|{r.amount}|{r.period}"),
    ]
    for table, build in key_specs:
        natural[table] = {row.id: build(row) for row in owned.get(table, []) if row.id is not None}

    lines: list[str] = []
    for table, rows in owned.items():
        for row in rows:
            payload: dict[str, object] = {}
            for column in row.__table__.columns:
                name = column.name
                if name in _FINGERPRINT_SKIP:
                    continue
                if table == "users" and name in _USER_FINGERPRINT_SKIP:
                    continue
                value = getattr(row, name)
                if value is not None and name in fk_targets:
                    value = _ref(fk_targets[name], value)
                payload[name] = value
            lines.append(f"{table}|{json.dumps(payload, sort_keys=True, default=str)}")
    lines.sort()
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


# --- dataset assembly --------------------------------------------------------


def _seed_dataset(session: Session, user: User) -> dict[str, int]:
    rng = random.Random(_SEED)  # noqa: S311 -- seeded deterministic demo data
    today = date.today()

    institutions = {
        name: Institution(user_id=user.id, name=name)
        for name in ("RBC Royal Bank", "TD Canada Trust", "Wealthsimple", "National Bank")
    }
    session.add_all(institutions.values())

    def _acc(name: str, type_: AccountType, currency: str, inst: str, taxable: bool = False):
        return Account(
            user_id=user.id,
            institution_id=institutions[inst].id,
            name=name,
            type=type_,
            currency=currency,
            taxable=taxable,
        )

    chequing = _acc("RBC Main Chequing", AccountType.chequing, "CAD", "RBC Royal Bank")
    card = _acc("RBC Credit Card", AccountType.credit_card, "CAD", "RBC Royal Bank")
    savings = _acc("TD Emergency Savings", AccountType.savings, "CAD", "TD Canada Trust")
    tfsa = _acc("WS TFSA", AccountType.tfsa, "CAD", "Wealthsimple")
    margin = _acc(
        "WS Margin", AccountType.brokerage_non_registered, "USD", "Wealthsimple", taxable=True
    )
    lira = _acc("NB LIRA", AccountType.lira, "CAD", "National Bank")
    session.add_all([chequing, card, savings, tfsa, margin, lira])
    session.flush()  # ids needed by every FK reference below

    securities: dict[str, Security] = {}
    # key = internal lookup key (stable), symbol = stored ticker (exchange-suffixed
    # for CAD/TSX so yfinance-style resolution works; US tickers stay bare).
    for key, symbol, name, kind, listing, exchange in (
        ("RY", "RY.TO", "Royal Bank of Canada", SecurityKind.stock, "CAD", "TSX"),
        ("TD", "TD.TO", "Toronto-Dominion Bank", SecurityKind.stock, "CAD", "TSX"),
        ("BMO", "BMO.TO", "Bank of Montreal", SecurityKind.stock, "CAD", "TSX"),
        ("ENB", "ENB.TO", "Enbridge Inc.", SecurityKind.stock, "CAD", "TSX"),
        ("VTI", "VTI", "Vanguard Total Stock Market ETF", SecurityKind.etf, "USD", "NYSE"),
        ("VXUS", "VXUS", "Vanguard Total International Stock ETF", SecurityKind.etf, "USD", "NYSE"),
        ("NVDA", "NVDA", "NVIDIA Corporation", SecurityKind.stock, "USD", "NASDAQ"),
    ):
        securities[key] = Security(
            user_id=user.id,
            symbol=symbol,
            name=name,
            kind=kind,
            listing_currency=listing,
            exchange=exchange,
        )
    session.add_all(securities.values())
    session.flush()

    prices = _seed_prices(session, user, rng, today, securities)

    bank_rows = _bank_rows(rng, today)
    trades = _trades(rng, today, user, tfsa, margin, lira, securities, prices)

    txn_counts = _insert_transactions(session, user, bank_rows)
    session.add_all(trades)
    from app.tax.acb import rebuild_user_pools  # refresh the cache Taxes page reads

    try:
        # Cache-only (docs/03): seeding never opens a network connection, so the
        # demo's fx gaps stay empty and this defers on the documented 422
        # (docs/07). `POST /acb/rebuild` — the standing demo-write exemption —
        # is the path that backfills pools once rates exist.
        rebuild_user_pools(session, user.id, allow_fetch=False)
    except APIError as exc:
        # fx_rates is shared and is never seeded (H7): with the demo's USD/CAD
        # gaps empty the rebuild raises its documented 422 fx_rate_missing
        # (docs/06). Defer the pool cache — everything else seeds — until a
        # BoC fetch or manual gap-fill covers the demo's USD trade dates;
        # POST /acb/rebuild then populates it.
        if exc.code != "fx_rate_missing":
            raise
        logger.warning("Demo ACB pools deferred, fx gap empty: %s", exc.detail)
    lifestyle_counts = _ensure_lifestyle(session, user)

    return {
        **{f"transactions_{key}": n for key, n in txn_counts.items()},
        "transactions": sum(txn_counts.values()),
        "trades": len(trades),
        "prices": sum(len(p) for p in prices.values()),
        **lifestyle_counts,
    }


# --- lifestyle tables (M45, docs/30) ------------------------------------------


def _next_due(today: date, day: int) -> str:
    """Next occurrence of day-of-month strictly after today, YYYY-MM-DD."""
    year, month = today.year, today.month
    if today.day >= day:
        month += 1
        if month > 12:
            month, year = 1, year + 1
    last = (_shift_month(date(year, month, 1), 1) - timedelta(days=1)).day
    return date(year, month, min(day, last)).isoformat()


def _ensure_lifestyle(session: Session, user: User) -> dict[str, int]:
    """Idempotently seed bills, budgets, debts, and goals (docs/30).

    Guarded per-table by name/category so re-runs (and the pre-M45 backfill
    path in `ensure_demo_account`) are no-ops. Rows whose category/account
    lookup misses are skipped, never FK-violating.
    """
    today = date.today()
    uid = user.id
    if uid is None:  # caller flushes/refreshes the user before seeding
        raise RuntimeError("demo seed called with an unflushed user (id is None)")
    added: dict[str, int] = {"bills": 0, "budgets": 0, "debts": 0, "goals": 0, "rules": 0}

    categories = {
        c.name: c.id for c in session.exec(select(Category).where(Category.user_id == uid))
    }
    accounts: dict[str, Account] = {}
    for a in session.exec(select(Account).where(Account.user_id == uid)):
        accounts.setdefault(a.type.value, a)

    have_bills = {
        r for r in session.exec(select(BillReminder.name).where(BillReminder.user_id == uid))
    }
    for name, amount, category, day in (
        ("Mapleview Rent", "1400.00", "housing", 1),
        ("Fibrestream Internet", "79.99", "utilities", 5),
        ("Airlink Mobile", "65.00", "utilities", 18),
        ("Spotify Canada", "11.99", "entertainment", 9),
        ("Netflix", "16.99", "entertainment", 14),
    ):
        if name in have_bills or category not in categories:
            continue
        bill = BillReminder(
            user_id=uid,
            name=name,
            amount=Decimal(amount),
            category_id=categories[category],
            frequency="monthly",
            next_due_date=_next_due(today, day),
            # M180: the schedule's own day, so a demo bill has the SAME shape
            # an API-created one does (docs/25 §M180 D1). None of the demo
            # cadences is near a month end, so this is shape parity, not a
            # visible difference.
            anchor_day=day,
            remind_days_before=3,
            enabled=True,
            # M116: the demo tenant gets a REAL rule, not an empty one, so
            # the drill-down, the counts and the chart have something to
            # show (docs/25 §M116 D1 — a bill always has a match term).
            # M121: the term is written into `bill_reminder_match_terms` too,
            # so demo bills have the SAME shape as API-created ones (the read
            # path would fall back to `match_term` anyway — docs/25 §M121 D6).
            match_term=name,
        )
        session.add(bill)
        session.flush()  # bill.id, so the term row can key off it
        session.add(BillReminderMatchTerm(bill_reminder_id=bill.id, position=0, term=name))
        added["bills"] += 1

    have_budgets = {
        r for r in session.exec(select(Budget.category_id).where(Budget.user_id == uid))
    }
    for category, amount in (
        ("groceries", "600.00"),
        ("dining", "250.00"),
        ("entertainment", "150.00"),
    ):
        cid = categories.get(category)
        if cid is None or cid in have_budgets:
            continue
        session.add(Budget(user_id=uid, category_id=cid, amount=Decimal(amount)))
        added["budgets"] += 1

    have_debts = {r for r in session.exec(select(Debt.name).where(Debt.user_id == uid))}
    card = accounts.get(AccountType.credit_card.value)
    chequing = accounts.get(AccountType.chequing.value)
    if "RBC Credit Card" not in have_debts and card is not None and card.id is not None:
        session.add(
            Debt(
                user_id=uid,
                account_id=card.id,
                name="RBC Credit Card",
                debt_type="credit_card",
                minimum_payment=Decimal("35.00"),
                payment_frequency="monthly",
            )
        )
        added["debts"] += 1
    if "Student Loan" not in have_debts and chequing is not None and chequing.id is not None:
        session.add(
            Debt(
                user_id=uid,
                account_id=chequing.id,
                name="Student Loan",
                debt_type="student_loan",
                original_amount=Decimal("8500.00"),
                interest_rate=Decimal("5.50"),
                minimum_payment=Decimal("120.00"),
                payment_frequency="monthly",
            )
        )
        added["debts"] += 1

    have_goals = {r for r in session.exec(select(Goal.name).where(Goal.user_id == uid))}
    savings = accounts.get(AccountType.savings.value)
    if "Emergency Fund" not in have_goals and savings is not None and savings.id is not None:
        session.add(
            Goal(
                user_id=uid,
                name="Emergency Fund",
                target_amount=Decimal("10000.00"),
                target_date=_shift_month(today, 12).isoformat(),
                account_id=savings.id,
            )
        )
        added["goals"] += 1
    if "Japan Trip" not in have_goals:
        session.add(
            Goal(
                user_id=uid,
                name="Japan Trip",
                target_amount=Decimal("3000.00"),
                target_date=_shift_month(today, 8).isoformat(),
                current_amount_override=Decimal("750.00"),
            )
        )
        added["goals"] += 1

    have_patterns = {r for r in session.exec(select(Rule.pattern).where(Rule.user_id == uid))}
    for priority, match_type, pattern, category in (
        (10, RuleMatchType.contains, "NETFLIX", "entertainment"),
        (20, RuleMatchType.contains, "GROCERY MART", "groceries"),
    ):
        cid = categories.get(category)
        if pattern in have_patterns or cid is None:
            continue
        session.add(
            Rule(
                user_id=uid,
                match_type=match_type,
                pattern=pattern,
                priority=priority,
                category_id=cid,
                enabled=True,
            )
        )
        added["rules"] += 1

    if any(added.values()):
        session.flush()
    return added


# --- reference data ----------------------------------------------------------


def _seed_prices(
    session: Session,
    user: User,
    rng: random.Random,
    today: date,
    securities: dict[str, Security],
) -> dict[str, dict[date, Decimal]]:
    """Weekly Friday closes (~61 weeks) per security; returns symbol → Friday → close."""
    fridays: list[date] = []
    day = today - timedelta(weeks=60)
    day -= timedelta(days=(day.weekday() - 4) % 7)  # back to the first Friday ≤ start
    while day <= today:
        if day.weekday() == 4:
            fridays.append(day)
        day += timedelta(days=1)

    series: dict[str, dict[date, Decimal]] = {}
    rows: list[Price] = []
    for symbol, start_s, end_s in _PRICE_ARCS:
        start, end = Decimal(start_s), Decimal(end_s)
        n = len(fridays)
        points: dict[date, Decimal] = {}
        for i, friday in enumerate(fridays):
            base = start + (end - start) * i / (n - 1)
            price = cents(base * Decimal(f"{1 + rng.uniform(-0.03, 0.03):.4f}"))
            points[friday] = price
            rows.append(
                Price(
                    user_id=user.id,
                    security_id=securities[symbol].id,
                    date=friday,
                    price=price,
                    source=PriceSource.manual,
                    # Demo rows are synthetic MANUAL data — no provider was
                    # ever contacted for them, so `fetched_at` must stay NULL
                    # (Option A, t_bd5ec95f; demo-readonly, docs/30).
                    fetched_at=None,
                )
            )
        series[symbol] = points
    session.add_all(rows)
    return series


def _price_at(series: dict[str, dict[date, Decimal]], symbol: str, day: date) -> Decimal:
    """Newest Friday close on or before `day`; earliest close as fallback."""
    best: Decimal | None = None
    for d in sorted(series[symbol]):
        if d > day:
            break
        best = series[symbol][d]
    return best if best is not None else series[symbol][min(series[symbol])]


# --- banking transactions ----------------------------------------------------

_DINING = ("CAFE LUNA", "SUSHI KATSU", "PIZZERIA ROMA")
_GROCERY = ("GROCERY MART #12", "FRESH MARKET")
_FUN = ("ODEON CINEMAS", "PIXEL ARCADE")

Row = dict[str, object]


def _add_row(
    rows: list[Row],
    acct: str,
    ms: date,
    day: int,
    payee: str,
    amount: Decimal | int,
    cat: str,
    today: date,
) -> None:
    last = min(_month_last(ms), today)
    if day > last.day:
        return
    rows.append(
        {
            "acct": acct,
            "date": ms.replace(day=day),
            "payee": payee,
            "amount": Decimal(amount),
            "cat": cat,
        }
    )


def _bank_rows(rng: random.Random, today: date) -> list[Row]:
    """12 calendar months ending with the current partial month; pinned cadences."""
    rows: list[Row] = []
    card_spend: dict[date, Decimal] = {}  # month start → total purchases

    for offset in range(-11, 1):
        ms = _shift_month(today, offset)
        # Chequing: fixed monthly items.
        _add_row(rows, "chequing", ms, 1, "PAYROLL ACME CORP", Decimal("2400.00"), "income", today)
        _add_row(
            rows, "chequing", ms, 2, "MAPLEVIEW RENTALS", Decimal("-1400.00"), "housing", today
        )
        _add_row(
            rows,
            "chequing",
            ms,
            3,
            "CITY POWER & WATER",
            -cents(Decimal(str(round(rng.uniform(85, 110), 2)))),
            "utilities",
            today,
        )
        _add_row(
            rows, "chequing", ms, 5, "FIBRESTREAM INTERNET", Decimal("-79.99"), "utilities", today
        )
        _add_row(
            rows, "chequing", ms, 9, "SPOTIFY CANADA", Decimal("-11.99"), "entertainment", today
        )
        _add_row(rows, "chequing", ms, 14, "NETFLIX.COM", Decimal("-16.99"), "entertainment", today)
        _add_row(rows, "chequing", ms, 18, "AIRLINK MOBILE", Decimal("-65.00"), "utilities", today)
        for d in (6, 13, 20, 24, 27):  # groceries x5/mo
            _add_row(
                rows,
                "chequing",
                ms,
                d,
                rng.choice(_GROCERY),
                -cents(Decimal(str(round(rng.uniform(35, 130), 2)))),
                "groceries",
                today,
            )
        for d in (2, 7, 11, 15, 19, 23, 27):  # dining x7/mo
            _add_row(
                rows,
                "chequing",
                ms,
                d,
                rng.choice(_DINING),
                -cents(Decimal(str(round(rng.uniform(12, 48), 2)))),
                "dining",
                today,
            )

        # Credit card: 8-12 purchases across dining/groceries/entertainment.
        spend = _ZERO
        for _ in range(rng.randint(8, 12)):
            pick = rng.randrange(3)
            payee, cat = (
                (rng.choice(_DINING), "dining")
                if pick == 0
                else (rng.choice(_GROCERY), "groceries")
                if pick == 1
                else (rng.choice(_FUN), "entertainment")
            )
            amount = -cents(Decimal(str(round(rng.uniform(6, 95), 2))))
            spend += amount
            _add_row(
                rows,
                "card",
                ms,
                rng.randint(1, min(_month_last(ms), today).day),
                payee,
                amount,
                cat,
                today,
            )
        card_spend[ms] = spend

        # Savings: quarterly interest bonus only (no transfer-category confusion).
        if ms.month % 3 == 0:
            _add_row(rows, "savings", ms, 25, "INTEREST BONUS", Decimal("12.50"), "income", today)

    # Card payments: positive, prior month's spend rounded to the dollar.
    months = sorted(card_spend)
    for prev, cur in itertools.pairwise(months):
        _add_row(
            rows,
            "card",
            cur,
            20,
            "CARD PAYMENT THANKYOU",
            round(-card_spend[prev]),
            "transfers",
            today,
        )

    rows.sort(key=lambda r: (r["date"], str(r["payee"])))  # stable insert order
    return rows


def _insert_transactions(session: Session, user: User, rows: list[Row]) -> dict[str, int]:
    """Wrap each account's rows in a committed demo batch, then bulk-insert."""

    def dedup(row: Row, account_name: str) -> str:
        raw = f"{account_name}|{row['date']}|{row['payee']}|{row['amount']}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    categories = {
        c.name: c.id for c in session.exec(select(Category).where(Category.user_id == user.id))
    }
    accounts: dict[str, Account] = {
        a.name: a for a in session.exec(select(Account).where(Account.user_id == user.id))
    }
    by_acct: dict[str, list[Row]] = {}
    for row in rows:
        name = {
            "chequing": "RBC Main Chequing",
            "card": "RBC Credit Card",
            "savings": "TD Emergency Savings",
        }[str(row["acct"])]
        by_acct.setdefault(name, []).append(row)

    counts: dict[str, int] = {}
    pending: list[Transaction] = []
    for account in accounts.values():
        acct_rows = by_acct.get(account.name, [])
        batch = ImportBatch(
            user_id=user.id,
            account_id=account.id,
            institution="Demo",
            file_name="demo_history.csv",
            status=ImportBatchStatus.committed,
            imported=len(acct_rows),
        )
        session.add(batch)
        session.flush()
        counts[account.name] = len(acct_rows)
        for row in acct_rows:
            native = row["amount"]
            pending.append(
                Transaction(
                    user_id=user.id,
                    account_id=account.id,
                    date=row["date"],
                    posting_date=None,
                    raw_description=str(row["payee"]),
                    normalized_payee=str(row["payee"]),
                    amount_native=native,
                    currency=account.currency,
                    cad_amount=native if account.currency == "CAD" else None,
                    category_id=categories.get(str(row["cat"])),
                    import_batch_id=batch.id,
                    dedup_hash=dedup(row, account.name),
                )
            )

    for i in range(0, len(pending), 500):  # chunked bulk insert, one flush per chunk
        session.add_all(pending[i : i + 500])
        session.flush()
    return counts


# --- brokerage trades --------------------------------------------------------

_MONTHLY = range(-11, 1)  # twelve month offsets ending with the current month


def _month_day(today: date, offset: int, day: int) -> date:
    ms = _shift_month(today, offset)
    return ms.replace(day=min(day, min(_month_last(ms), today).day))


def _trade(
    user: User,
    account: Account,
    security: Security,
    action: TradeAction,
    day: date,
    quantity: Decimal,
    price_native: Decimal,
) -> Trade:
    # fx_rate_used is display copy only (docs/06); pool math never reads it.
    fx_rate_used = Decimal("0.74") if security.listing_currency != "CAD" else None
    return Trade(
        user_id=user.id,
        account_id=account.id,
        security_id=security.id,
        action=action,
        date=day,
        quantity=quantity,
        price_native=price_native,
        commission=_ZERO,
        fx_rate_used=fx_rate_used,
    )


def _cash_trade(
    user: User, account: Account, security: Security, action: TradeAction, day: date, gross: str
) -> Trade:
    """Cash-only action: quantity=0, price_native carries the gross cash amount."""
    return _trade(user, account, security, action, day, _ZERO, Decimal(gross))


def _trades(
    rng: random.Random,
    today: date,
    user: User,
    tfsa: Account,
    margin: Account,
    lira: Account,
    securities: dict[str, Security],
    prices: dict[str, dict[date, Decimal]],
) -> list[Trade]:
    out: list[Trade] = []
    quarters = (-11, -8, -5, -2)  # quarter-ish months inside the window

    # WS TFSA (CAD): funded quarterly, holds RY plus dividend payers ENB/BMO.
    enb_day, bmo_day = _month_day(today, -11, 2), _month_day(today, -11, 3)
    out.append(
        _trade(
            user,
            tfsa,
            securities["ENB"],
            TradeAction.buy,
            enb_day,
            Decimal(120),
            _price_at(prices, "ENB", enb_day),
        )
    )
    out.append(
        _trade(
            user,
            tfsa,
            securities["BMO"],
            TradeAction.buy,
            bmo_day,
            Decimal(80),
            _price_at(prices, "BMO", bmo_day),
        )
    )
    for offset in _MONTHLY:  # monthly RY buys, 20-40 shares at the price series
        day = _month_day(today, offset, 10)
        out.append(
            _trade(
                user,
                tfsa,
                securities["RY"],
                TradeAction.buy,
                day,
                Decimal(rng.randint(20, 40)),
                _price_at(prices, "RY", day),
            )
        )
    for offset in _MONTHLY:  # monthly funding that roughly covers the RY buys
        out.append(
            _cash_trade(
                user,
                tfsa,
                securities["RY"],
                TradeAction.contribution,
                _month_day(today, offset, 28),
                "4000.00",
            )
        )
    for offset, gross in zip(  # growing dividend as the RY position builds
        quarters, ("45.00", "60.00", "75.00", "90.00"), strict=True
    ):
        day = _month_day(today, offset, 15)
        out.append(_cash_trade(user, tfsa, securities["ENB"], TradeAction.dividend, day, "79.20"))
        out.append(_cash_trade(user, tfsa, securities["BMO"], TradeAction.dividend, day, "118.40"))
        out.append(_cash_trade(user, tfsa, securities["RY"], TradeAction.dividend, day, gross))
    out.append(
        _cash_trade(
            user,
            tfsa,
            securities["ENB"],
            TradeAction.return_of_capital,
            _month_day(today, -5, 16),
            "40.00",
        )
    )

    # WS Margin (USD, non-registered): staged buys, then ~40% of VTI sold two
    # months ago above average cost → realized gain.
    vti_buys: list[tuple[date, Decimal]] = []
    for offset in (-11, -9, -7, -5, -3, -1):
        day = _month_day(today, offset, 12)
        qty = Decimal(rng.randint(15, 30))
        vti_buys.append((day, qty))
        out.append(
            _trade(
                user,
                margin,
                securities["VTI"],
                TradeAction.buy,
                day,
                qty,
                _price_at(prices, "VTI", day),
            )
        )
    for offset, qty_low in ((-10, 20), (-6, 24), (-2, 28)):
        day = _month_day(today, offset, 13)
        out.append(
            _trade(
                user,
                margin,
                securities["VXUS"],
                TradeAction.buy,
                day,
                Decimal(rng.randint(qty_low, qty_low + 7)),
                _price_at(prices, "VXUS", day),
            )
        )
    for offset, qty in ((-11, 12), (-10, 10)):  # NVDA early
        day = _month_day(today, offset, 14)
        out.append(
            _trade(
                user,
                margin,
                securities["NVDA"],
                TradeAction.buy,
                day,
                Decimal(qty),
                _price_at(prices, "NVDA", day),
            )
        )

    sell_day = _month_day(today, -2, 16)
    held = sum(qty for day, qty in vti_buys if day < sell_day)
    out.append(
        _trade(
            user,
            margin,
            securities["VTI"],
            TradeAction.sell,
            sell_day,
            -int(held * Decimal("0.4")),
            _price_at(prices, "VTI", sell_day),
        )
    )
    out.append(  # opening deposit before the first buy
        _cash_trade(
            user,
            margin,
            securities["VTI"],
            TradeAction.transfer_in,
            _month_day(today, -11, 5),
            "2000.00",
        )
    )
    for offset in _MONTHLY:  # monthly funding roughly covering the buys
        out.append(
            _cash_trade(
                user,
                margin,
                securities["VTI"],
                TradeAction.transfer_in,
                _month_day(today, offset, 22),
                "3300.00",
            )
        )
    for offset in quarters:
        day = _month_day(today, offset, 20)
        out.append(_cash_trade(user, margin, securities["VTI"], TradeAction.dividend, day, "85.00"))
        out.append(
            _cash_trade(user, margin, securities["VXUS"], TradeAction.dividend, day, "45.00")
        )

    # NB LIRA: one contribution funding a BMO block buy, then it sits.
    lira_day = _month_day(today, -11, 5)
    out.append(
        _cash_trade(user, lira, securities["BMO"], TradeAction.contribution, lira_day, "21000.00")
    )
    out.append(
        _trade(
            user,
            lira,
            securities["BMO"],
            TradeAction.buy,
            lira_day,
            Decimal(150),
            _price_at(prices, "BMO", lira_day),
        )
    )

    out.sort(key=lambda t: t.date)
    return out

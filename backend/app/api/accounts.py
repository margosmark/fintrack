"""accounts — standard CRUD (docs/08)."""

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import ScalarSelect, func
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import (
    Pagination,
    get_current_user,
    get_db,
    get_pagination,
    owned_or_404,
    paginate,
)
from app.core.config import get_settings
from app.core.errors import api_error
from app.models.acb_pools import AcbPool
from app.models.accounts import INVESTMENT_ACCOUNT_TYPES, Account, AccountType
from app.models.debts import Debt
from app.models.fx_rates import FxRate
from app.models.goals import Goal
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.institutions import Institution
from app.models.reconciliation import ReconciliationItem
from app.models.trades import Trade
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.accounts import (
    AccountCreate,
    AccountPage,
    AccountPublic,
    AccountUpdate,
    AccountValueHistory,
    LedgerEntry,
    LedgerPage,
    ValueGranularity,
)
from app.services import account_value, fx, valuation
from app.services.txn_filters import LEDGER_EXCLUDE_SPLIT
from app.tax.acb import cents

router = APIRouter(prefix="/accounts", tags=["accounts"])

_MAX_CIPHER_LEN = 8000


def _validate_account_cipher(data: dict) -> None:
    value = data.get("name_cipher")
    if value is not None and len(value) > _MAX_CIPHER_LEN:
        raise api_error(422, "name_cipher exceeds 8000 characters", "invalid_cipher")


def _require_account_cipher(data: dict, current: str | None, user: User) -> None:
    if user.encryption_required and not data.get("name_cipher", current):
        raise api_error(
            422,
            "encryption_required: name_cipher is required",
            "encryption_required",
        )


def _committed_cash_subquery(account_ids: list[int] | None = None) -> ScalarSelect:
    """Σ amount_native over committed-batch or non-batch transactions for the outer account.

    `account_ids` (M90, docs/43 §2) additionally pins the inner read to an
    allowlist — the partner projection's copy of this subquery, so a hidden
    account's rows never even enter the aggregate. Default `None` keeps the
    owner's SQL byte-identical (the tenancy regression pins it).
    """
    conditions = [
        Transaction.account_id == Account.id,
        (ImportBatch.status == ImportBatchStatus.committed)
        | (Transaction.import_batch_id.is_(None)),
        # M107 (docs/44 §2): LEDGER — a split parent's money now lives in its
        # children, so counting it too would double the account's cash.
        LEDGER_EXCLUDE_SPLIT,
    ]
    if account_ids is not None:
        conditions.append(Transaction.account_id.in_(account_ids))
    return (
        select(func.coalesce(func.sum(Transaction.amount_native), 0))
        .select_from(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(*conditions)
        .correlate(Account)
        .scalar_subquery()
    )


def _format_cash(value: object) -> str:
    """Every money string on the accounts surface, through the canonical helper.

    Was a bare `.quantize(_CENT)` with no `rounding=`, so it silently read the
    process-wide decimal context (half-even) and a `.xx5` tie could render
    differently from every other money surface. Now ROUND_HALF_UP — the
    documented money convention (docs/02, docs/06, docs/12 §money, pinned by
    `tests/test_reports_money.py`) — stated explicitly rather than inherited.

    This is a REAL behaviour change on an exact tie, and it is deliberate:
    accounts carried no documented mode, no client mirror recomputes these
    fields, and the bare call dates to M9 with no justifying comment. The only
    half-even money in the app is now the three surfaces that DOCUMENT it
    (`services/valuation.py`, `services/exposure_breakdown.py`,
    `services/budgets.py`) — each with its reason next to it.
    """
    return str(cents(Decimal(value)))  # type: ignore[arg-type]


def _pending_import_count(db: Session, account: Account) -> int:
    """Batches on *account* still waiting for review (M113, docs/08).

    A COUNT only — the account page's banner must never carry batch contents
    (E2EE row-count rule, docs/09). Only `pending_review` counts: a committed,
    discarded or reverted batch is not pending and that distinction must never
    leak. Tenancy: scoped to the account's owner, the same user every caller of
    `_public_for` has just been ownership-checked against.
    """
    count = db.scalar(
        select(func.count())
        .select_from(ImportBatch)
        .where(
            ImportBatch.account_id == account.id,
            ImportBatch.user_id == account.user_id,
            ImportBatch.status == ImportBatchStatus.pending_review,
        )
    )
    return int(count or 0)


def _account_totals(
    session: Session,
    user_id: int,
    today: date,
    account_ids: list[int] | None = None,
) -> tuple[dict[int, Decimal], str]:
    """One compute_positions call → {account_id: Σ market_value_cad}; reporting currency.

    `account_ids` narrows the computation to an allowlist for the partner
    projection (docs/43 §2); `None` (the owner's path) is unchanged.
    """
    positions = valuation.compute_positions(session, user_id, today, account_ids=account_ids)
    cad: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for p in positions:
        if p["market_value_cad"] is not None:
            cad[p["account_id"]] += p["market_value_cad"]
    return cad, get_settings().reporting_currency


def _cash_native(
    cash_cad: Decimal | None, account: Account, reporting: str, rate: FxRate | None
) -> Decimal | None:
    """The chart's CAD cash figure restated in the ACCOUNT's currency.

    M145 (docs/08 §cash_balance): the card's cash for an investment account
    is the value chart's own chain (`account_value.account_cash`), so the
    two cannot disagree — this only carries it across the currency border,
    by the SAME rule `_build_public` already applies to `investment_value`
    (divide by the account→CAD rate; one rate, both legs of one card).

    `None` passes through: an unresolvable CAD figure stays unresolvable
    natively — never a fabricated zero (docs/08 §Honesty). A zero needs no
    rate — 0 CAD *is* 0 USD.
    """
    if cash_cad is None:
        return None
    if account.currency.upper() == reporting.upper() or cash_cad == 0:
        return cash_cad
    if rate is None:
        return None
    return cash_cad / rate.rate  # type: ignore[union-attr]


def _build_public(
    session: Session,
    account: Account,
    balance: object,
    cad: dict[int, Decimal],
    reporting: str,
    today: date,
    pending_import_count: int = 0,
    investment_cash: Decimal | None = None,
    investment_account: bool = False,
) -> AccountPublic:
    """`cash_balance` is KIND-dependent (M145, docs/08 §cash_balance):

    - cash accounts — the LEDGER: committed transactions (the SQL subquery
      `balance`) + `starting_balance`. Trades never touch a cash account, so
      this is the whole truth.
    - investment accounts — the docs/05 chain the account's own value chart
      draws (`services/account_value.account_cash`, passed in as
      `investment_cash`, in REPORTING currency): starting_balance + committed
      transactions + Σ docs/05 trade cash effects, per-row currency (M132).
      Real account 459 records every dollar as a CASH-security trade with
      ZERO transactions, so the ledger-only formula read $0.00 while the
      chart read $3,486.12 — the two-source bug this contract kills.
      `starting_balance` is inside that chain exactly ONCE; the ledger's
      `+ sb` below deliberately does not apply to it again (an investment
      account's contributions ARE its starting position — docs/14).

    `investment_cash` is `None` when the chain had no honest figure (a
    foreign-currency leg past the M115 fx window) — the card then reports
    null, the same gap the chart draws, never a fabricated zero.
    """
    sb = account.starting_balance or Decimal("0")
    cad_total = cad.get(account.id, Decimal("0"))
    if account.currency.upper() == reporting.upper():
        inv_native = cad_total
        rate = None
    else:
        # cad_total is in the reporting currency (CAD); convert to the
        # account's native currency by dividing by the account->CAD rate.
        # The cache holds account->CAD (e.g. USD->CAD), never CAD->account.
        rate = fx.latest_rate(session, account.currency, reporting, today)
        inv_native = cad_total / rate.rate if rate is not None else Decimal("0")
    if investment_account:
        cash_dec = _cash_native(investment_cash, account, reporting, rate)
    else:
        cash_dec = Decimal(balance) + sb
    total_dec = None if cash_dec is None else cash_dec + inv_native
    return AccountPublic(
        id=account.id,
        institution_id=account.institution_id,
        name=account.name,
        type=account.type,
        currency=account.currency,
        taxable=account.taxable,
        llm_autopilot=account.llm_autopilot,
        cash_balance=None if cash_dec is None else _format_cash(cash_dec),
        investment_value=_format_cash(inv_native),
        total_value=None if total_dec is None else _format_cash(total_dec),
        starting_balance=_format_cash(sb),
        name_cipher=account.name_cipher,
        last_sync_at=account.last_sync_at,
        provider_status=account.provider_status,
        pending_import_count=pending_import_count,
    )


def _public_for(
    db: Session, account: Account, cad: dict[int, Decimal], reporting: str, today: date
) -> AccountPublic:
    balance = db.scalar(select(_committed_cash_subquery()).where(Account.id == account.id))
    investment = account.type in INVESTMENT_ACCOUNT_TYPES
    return _build_public(
        db,
        account,
        balance,
        cad,
        reporting,
        today,
        pending_import_count=_pending_import_count(db, account),
        investment_cash=account_value.account_cash(db, account, today) if investment else None,
        investment_account=investment,
    )


def _validate_institution(institution_id: int | None, user: User, db: Session) -> None:
    if institution_id is None:
        return
    institution = db.get(Institution, institution_id)
    if institution is None or institution.user_id != user.id:
        raise api_error(404, "Institution not found", "not_found")


@router.get("/ledger", response_model=LedgerPage)
def get_account_ledger(
    account_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> LedgerPage:
    """Per-account ledger with running balances starting from starting_balance."""
    account = owned_or_404(Account, account_id, current_user, db)
    starting = account.starting_balance or Decimal("0")

    transactions = db.exec(
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(
            Transaction.account_id == account_id,
            (ImportBatch.status == ImportBatchStatus.committed)
            | (Transaction.import_batch_id.is_(None)),
            # M107 (docs/44 §2): LEDGER — the chart's rows must be the rows its
            # sums are made of, so split parents drop out of the running balance
            # exactly as they drop out of `_cash_balance_subquery` above.
            LEDGER_EXCLUDE_SPLIT,
        )
        .order_by(Transaction.date, Transaction.id)
    ).all()

    running = starting
    items: list[LedgerEntry] = []
    for txn in transactions:
        running += txn.amount_native
        items.append(
            LedgerEntry(
                transaction_id=txn.id,
                date=txn.date,
                description=txn.normalized_payee or txn.raw_description,
                amount=_format_cash(txn.amount_native),
                running_balance=_format_cash(running),
            )
        )

    return LedgerPage(
        items=items,
        starting_balance=_format_cash(starting),
        current_balance=_format_cash(running),
        total=len(items),
    )


@router.get("", response_model=AccountPage)
def list_accounts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
) -> AccountPage:
    scope = Account.user_id == current_user.id
    stmt = select(Account, _committed_cash_subquery()).where(scope).order_by(Account.id)
    rows, total = paginate(db, stmt, limit=pagination.limit, offset=pagination.offset)
    today = datetime.now(UTC).date()
    cad, reporting = _account_totals(db, current_user.id, today)
    # M145: investment accounts read the chart's cash chain; ONE batched call
    # for the whole page (never one query per account — docs/34).
    investment = [account for account, _ in rows if account.type in INVESTMENT_ACCOUNT_TYPES]
    cash_map = (
        account_value.accounts_cash(db, current_user.id, investment, today) if investment else {}
    )
    items = [
        _build_public(
            db,
            account,
            balance,
            cad,
            reporting,
            today,
            investment_cash=cash_map.get(account.id),
            investment_account=account.type in INVESTMENT_ACCOUNT_TYPES,
        )
        for account, balance in rows
    ]
    return AccountPage(items=items, total=total, limit=pagination.limit, offset=pagination.offset)


@router.post("", response_model=AccountPublic, status_code=201)
def create_account(
    payload: AccountCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AccountPublic:
    _validate_institution(payload.institution_id, current_user, db)
    _validate_account_cipher(payload.model_dump())
    _require_account_cipher(payload.model_dump(), None, current_user)
    account = Account(**payload.model_dump(), user_id=current_user.id)
    # Taxability is server-derived (docs/14 C1): taxable iff brokerage_non_registered.
    account.taxable = account.type == AccountType.brokerage_non_registered
    db.add(account)
    db.commit()
    db.refresh(account)
    today = datetime.now(UTC).date()
    cad, reporting = _account_totals(db, current_user.id, today)
    return _public_for(db, account, cad, reporting, today)


@router.get("/{account_id}", response_model=AccountPublic)
def get_account(
    account_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AccountPublic:
    today = datetime.now(UTC).date()
    cad, reporting = _account_totals(db, current_user.id, today)
    return _public_for(
        db, owned_or_404(Account, account_id, current_user, db), cad, reporting, today
    )


@router.get("/{account_id}/value-history", response_model=AccountValueHistory)
def value_history(
    account_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    granularity: ValueGranularity = "day",
    min_event_value: Annotated[Decimal | None, Query(ge=0)] = None,
) -> AccountValueHistory:
    """Per-account value over time + significant-event markers (M118, docs/08).

    Cash + holdings valued at each bucket END in the reporting currency,
    through docs/05's portfolio-value chain (`services/account_value.py`), with
    markers derived from the account's committed cash flows.

    Defaults: `from` = the account's own floor — the EARLIEST of its
    `created_at` and its first committed row, because `created_at` is when the
    ROW was written and an imported history reaches back further (real account
    731 was created 2026-10-08 with trades to 2025-11-13); an explicit earlier
    `from` is clamped to that floor, so a bucket is never fabricated before the
    account has any data (docs/08 §Defaults). `to` = today; `granularity=day`
    coarsened to the finest level that stays inside the ~400-point roofline
    (docs/08 §Point cap), echoed back as `granularity` next to
    `requested_granularity`; `min_event_value` = 1000 in reporting currency
    (`0` returns EVERY cash flow). `from > to` → `422 invalid_date_range`.

    Read-only and derived from committed rows on every call, so the response
    carries `Cache-Control: private, max-age=60` (docs/08 §Caching).
    """
    account = owned_or_404(Account, account_id, current_user, db)
    window_to = to or datetime.now(UTC).date()
    threshold = (
        min_event_value if min_event_value is not None else account_value.DEFAULT_MIN_EVENT_VALUE
    )
    # The service resolves the window floor, clamps `from` and raises the
    # documented 422 — it is the one place that knows the account's rows.
    response.headers["Cache-Control"] = "private, max-age=60"
    return AccountValueHistory(
        **account_value.account_value_history(db, account, from_, window_to, granularity, threshold)
    )


@router.patch("/{account_id}", response_model=AccountPublic)
def update_account(
    account_id: int,
    payload: AccountUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AccountPublic:
    account = owned_or_404(Account, account_id, current_user, db)
    data = payload.model_dump(exclude_unset=True)
    _validate_account_cipher(data)
    _require_account_cipher(data, account.name_cipher, current_user)
    if "institution_id" in data:
        _validate_institution(data["institution_id"], current_user, db)
    for field, value in data.items():
        setattr(account, field, value)
    # Re-derive on every update so type changes flip taxability (docs/14 C1).
    account.taxable = account.type == AccountType.brokerage_non_registered
    db.commit()
    db.refresh(account)
    today = datetime.now(UTC).date()
    cad, reporting = _account_totals(db, current_user.id, today)
    return _public_for(db, account, cad, reporting, today)


@router.delete("/{account_id}", status_code=204)
def delete_account(
    account_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    account = owned_or_404(Account, account_id, current_user, db)
    # No ON DELETE rules in the schema (docs/29 F3): every referencing table is
    # pre-checked, user_id-scoped, before the parent delete.
    account_in_use_checks = (
        (Transaction, Transaction.account_id),
        (Trade, Trade.account_id),
        (AcbPool, AcbPool.account_id),
        (ImportBatch, ImportBatch.account_id),
        (Debt, Debt.account_id),
        (Goal, Goal.account_id),
        (ReconciliationItem, ReconciliationItem.account_id),
    )
    for _model, column in account_in_use_checks:
        referenced = db.exec(
            select(column).where(
                column == account_id,
                _model.user_id == current_user.id,
            )
        ).first()
        if referenced is not None:
            raise api_error(409, "Account is in use; reassign references first", "account_in_use")
    db.delete(account)
    db.commit()

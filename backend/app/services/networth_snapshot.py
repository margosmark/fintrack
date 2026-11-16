"""Net worth snapshot — drill-down at a specific date.

Returns per-account breakdown of net worth at a given date by summing
committed transactions up to that point. Per-account balances stay in the
account's NATIVE currency (what the user sees on the account page); the
`total_cash` roll-up is in the configured reporting currency, converted with
the same `FxCache` every other report uses (docs/12).
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.config import get_settings
from app.models.accounts import Account
from app.models.import_batches import ImportBatch
from app.models.transactions import Transaction
from app.services.reports import FxCache
from app.services.txn_filters import countable_tx_conditions

_CENT = Decimal("0.01")
_ZERO = Decimal("0")


def networth_snapshot(session: Session, user_id: int, target_date: date) -> dict:
    """Return per-account cash balances at the given date.

    Sums committed transactions by booking `date` (docs/02 — `posting_date`
    is nullable and must never gate period membership). The staging predicate
    (docs/29 F1) filters pending-review batches in the per-account sum
    subquery; accounts with no transactions still appear (zero balance).
    Each balance starts from `Account.starting_balance or 0` (docs/29 F1b),
    matching `api/accounts.py` cash math.

    `total_cash` sums the NATIVE balances 1:1 — a CAD account and a USD account
    were added as if the dollar signs matched. It now converts each balance to
    the reporting currency at the newest cached rate on or before `target_date`
    (docs/12), and EXCLUDES an account whose rate is unresolvable rather than
    adding its raw foreign-currency face value.
    """
    tx_sums = (
        select(
            Transaction.account_id,
            func.sum(Transaction.amount_native).label("total"),
        )
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(
            *countable_tx_conditions(user_id),
            Transaction.date <= target_date,
        )
        .group_by(Transaction.account_id)
        .subquery()
    )

    rows = session.exec(
        select(
            Account.id,
            Account.name,
            Account.currency,
            Account.starting_balance,
            func.coalesce(tx_sums.c.total, 0),
        )
        .outerjoin(tx_sums, tx_sums.c.account_id == Account.id)
        .where(Account.user_id == user_id)
    ).all()

    reporting = get_settings().reporting_currency
    fx = FxCache(session, reporting)

    accounts = []
    total = _ZERO
    for acct_id, name, currency, starting, bal in rows:
        start = Decimal(str(starting)) if starting else _ZERO
        balance = (start + Decimal(str(bal))).quantize(_CENT)
        accounts.append(
            {
                "account_id": acct_id,
                "account_name": name,
                "currency": currency,
                "balance": balance,
            }
        )
        rate = fx.rate(currency, reporting, target_date)
        if rate is not None:  # unresolved → excluded, never fetched
            total += balance * rate

    accounts.sort(key=lambda a: a["balance"], reverse=True)
    return {
        "date": str(target_date),
        "reporting_currency": reporting,
        "accounts": accounts,
        "total_cash": total.quantize(_CENT),
    }

"""Dividend calendar — project upcoming income from historical patterns.

Analyzes historical dividend trades per security, detects payment frequency,
and projects next expected dates and amounts.
"""

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.accounts import Account
from app.models.import_batches import ImportBatch
from app.models.trades import Trade, TradeAction
from app.services import dividend_provenance
from app.services.trade_filters import committed_trade_conditions
from app.services.txn_filters import today_et

_ISO_CURRENCY = re.compile(r"[A-Z]{3}")


def _stated_currency(value: object) -> str | None:
    """Normalize a row/account denomination to the app's ISO-code shape."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if _ISO_CURRENCY.fullmatch(code) else None


def _months_back(d: date, months: int) -> date:
    """First day of the month `months` back from d (calendar month start)."""
    total = d.year * 12 + (d.month - 1) - months
    return date(total // 12, total % 12 + 1, 1)


def _detect_frequency(dates: list[date]) -> str:
    if len(dates) < 2:
        return "unknown"
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    avg_gap = sum(gaps) / len(gaps)
    if avg_gap <= 40:
        return "monthly"
    if avg_gap <= 100:
        return "quarterly"
    return "yearly"


def compute_dividend_calendar(session: Session, user_id: int, today: date | None = None) -> dict:
    today = today or today_et()
    # Trailing 13-month history window — frequency math is meaningless on
    # older payments, and unbounded scans can masquerade stale patterns as
    # upcoming income (docs/29 F1c).
    window_start = _months_back(today, 12)  # first day 12 months ago → 13 months inclusive
    # Fetch dividend/distribution trades in the history window. Committed (or
    # manual) only: the calendar is a LIVE projection, and a staged payment is
    # not history yet (docs/03 staging model).
    rows = session.exec(
        select(Trade)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(
            *committed_trade_conditions(user_id),
            Trade.date >= window_start,
            Trade.action.in_(
                [
                    TradeAction.dividend,
                    TradeAction.distribution,
                    TradeAction.return_of_capital,
                ]
            ),
        )
        .order_by(Trade.date)
    ).all()

    # Group by security
    by_security: dict[int, list[dict]] = defaultdict(list)
    for trade in rows:
        by_security[trade.security_id].append(
            {
                "date": trade.date.date() if isinstance(trade.date, datetime) else trade.date,
                # Cash-only income rows carry the gross CAD amount in
                # price_native (docs/02 conventions) — there is no `price`.
                "amount": trade.price_native,
                "action": trade.action,
                "currency": trade.currency,
                "account_id": trade.account_id,
            }
        )

    # Get security symbols (tenancy-scoped — docs/29 F1c)
    from app.models.securities import Security

    symbols = {
        s.id: s.symbol
        for s in session.exec(
            select(Security).where(Security.user_id == user_id, Security.id.in_(by_security.keys()))
        ).all()
    }
    account_ids = {entry["account_id"] for entries in by_security.values() for entry in entries}
    accounts = {
        account.id: account
        for account in session.exec(
            select(Account).where(Account.user_id == user_id, Account.id.in_(account_ids))
        ).all()
    }

    upcoming = []
    period_names = ("this_month", "this_quarter", "this_year")
    by_currency: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {name: Decimal("0") for name in period_names}
    )
    unresolved_counts = {name: 0 for name in period_names}
    cent = Decimal("0.01")

    for sec_id, entries in by_security.items():
        amounts = [e["amount"] for e in entries]
        dates = sorted(e["date"] for e in entries)
        freq = _detect_frequency(dates)
        avg = sum(amounts) / len(amounts)
        # The sibling cash-flow seam owns this resolver: trade currency first,
        # account currency only for legacy NULLs. Do not infer from listing or FX.
        currencies = {
            _stated_currency(
                entry["currency"] or getattr(accounts.get(entry["account_id"]), "currency", None)
            )
            for entry in entries
        }
        currency = currencies.pop() if len(currencies) == 1 else None
        if currency is not None:
            # A known projected denomination gets a fully zero-filled bucket,
            # even when its next date is outside all three summary windows.
            by_currency[currency]

        multiplier = {"monthly": 12, "quarterly": 4, "yearly": 1}.get(freq, 12)
        annual = (avg * multiplier).quantize(cent, rounding=ROUND_HALF_EVEN)

        # Next expected — preserve the existing one-next-payment schedule.
        last = max(dates)
        offsets = {"monthly": 30, "quarterly": 91, "yearly": 365}
        next_date = last + timedelta(days=offsets.get(freq, 30))

        eligible = {
            "this_month": next_date.month == today.month and next_date.year == today.year,
            # Preserve the legacy <=90 predicate, including dates in the past.
            "this_quarter": (next_date - today).days <= 90,
            "this_year": next_date.year == today.year,
        }
        for period, is_eligible in eligible.items():
            if is_eligible:
                if currency is None:
                    unresolved_counts[period] += 1
                else:
                    # Accumulate raw means and quantize once per bucket.
                    by_currency[currency][period] += avg

        upcoming.append(
            {
                "security_id": sec_id,
                "symbol": symbols.get(sec_id, "?"),
                "frequency": freq,
                "avg_amount": avg.quantize(cent, rounding=ROUND_HALF_EVEN) if currency else None,
                "currency": currency,
                "annual_projection": annual if currency else None,
                "last_payment_date": max(dates),
                "next_expected_date": next_date,
                "num_payments": len(entries),
            }
        )

    upcoming.sort(key=lambda u: u["next_expected_date"])
    recorded = [entry["date"] for entries in by_security.values() for entry in entries]
    newest_payment = max(recorded, default=None)
    provenance_block = dividend_provenance.build(
        [
            dividend_provenance.source_status(
                key="income_trades",
                kind="ledger",
                label="recorded income trades",
                provider=None,
                as_of=newest_payment,
                status=(
                    dividend_provenance.STATUS_OK
                    if newest_payment is not None
                    else dividend_provenance.STATUS_MISSING
                ),
                detail=(
                    f"Projected from {len(recorded)} recorded income payment(s) since "
                    f"{window_start.isoformat()} in your ledger — recorded trades, not a "
                    "market-data feed. Projections are next-payment estimates, not forecasts."
                    if newest_payment is not None
                    else (
                        "No recorded income payments in the trailing 13 months — there is "
                        "nothing to project."
                    )
                ),
            )
        ]
    )
    return {
        "upcoming": upcoming,
        "provenance": provenance_block,
        "summary": {
            # Retired blended scalars stay present and null for every history.
            "this_month": None,
            "this_quarter": None,
            "this_year": None,
            "by_currency": [
                {
                    "currency": currency,
                    **{
                        period: str(
                            by_currency[currency][period].quantize(cent, rounding=ROUND_HALF_EVEN)
                        )
                        for period in period_names
                    },
                }
                for currency in sorted(by_currency)
            ],
            "unresolved_counts": unresolved_counts,
        },
    }

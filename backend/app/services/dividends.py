"""Dividend income analytics (M14, docs/05 + docs/12).

Aggregates the income actions (dividend | distribution | return_of_capital —
cash-only trades per docs/02: quantity=0, price_native = gross cash) into a
yearly overview with trade-date CAD conversion from the fx_rates cache.
Cache-only like every report (docs/03/docs/12): a row whose rate is missing is
counted in `unresolved_fx` and excluded from ALL CAD sums — never fetched.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.config import get_settings
from app.models.fx_rates import FxRate
from app.models.import_batches import ImportBatch
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.services import dividend_provenance as provenance
from app.services.reports import FxCache
from app.services.trade_filters import committed_trade_conditions
from app.tax.acb import cents

INCOME_ACTIONS = (
    TradeAction.dividend,
    TradeAction.distribution,
    TradeAction.return_of_capital,
)

_ZERO = Decimal("0")

_ACTION_KEY = {
    TradeAction.dividend: "dividend_cad",
    TradeAction.distribution: "distribution_cad",
    TradeAction.return_of_capital: "roc_cad",
}


def _report_provenance(
    session: Session,
    rows: list[Trade],
    securities: dict[int, Security],
    year: int,
    reporting: str,
    unresolved_fx: int,
    today: dt.date,
) -> dict[str, object]:
    """Which sources back this report, how fresh each is, stale-or-missing
    (docs/07 §M184, docs/12 §Dividends).

    Two sources, honestly labelled: the tenant's own income trades (ledger —
    recorded facts, never stale) and, only when the report actually converts a
    foreign denomination, the shared trade-date FX cache (market data — it CAN
    be stale, and `unresolved_fx` is exactly the case where it is missing).
    Reads one extra cache query and never fetches (AGENTS §2).
    """
    in_year = [trade for trade in rows if trade.date.year == year]
    recorded = max((trade.date for trade in in_year), default=None)
    sources: list[dict[str, object]] = [
        provenance.source_status(
            key="income_trades",
            kind="ledger",
            label="recorded income trades",
            provider=None,
            as_of=recorded,
            status=provenance.STATUS_OK if recorded is not None else provenance.STATUS_MISSING,
            detail=(
                f"Your committed income trades recorded in {year} — newest payment "
                f"{recorded.isoformat()}. Ledger facts, not a market-data feed."
                if recorded is not None
                else f"No income trades recorded in {year}: nothing to report from your ledger."
            ),
        )
    ]
    bases = sorted(
        {
            security.listing_currency
            for trade in in_year
            if (security := securities.get(trade.security_id)) is not None
            and security.listing_currency != reporting
        }
    )
    if not bases:
        return provenance.build(sources)

    newest_rate = session.exec(
        select(FxRate.date)
        .where(FxRate.quote == reporting, FxRate.base.in_(bases))
        .order_by(FxRate.date.desc())
        .limit(1)
    ).first()
    providers = sorted(
        str(value)
        for value in session.exec(
            select(FxRate.source)
            .where(FxRate.quote == reporting, FxRate.base.in_(bases))
            .distinct()
        ).all()
    )
    codes = ", ".join(f"{base}→{reporting}" for base in bases)
    provider_label = "/".join(providers) if providers else None
    if unresolved_fx:
        status = provenance.STATUS_MISSING
        detail = (
            f"{unresolved_fx} payment(s) in {year} have no cached rate for {codes} and are "
            "excluded from every CAD total. Reports are cache-only: no rate was fetched."
        )
    elif newest_rate is None:
        status = provenance.STATUS_MISSING
        detail = f"{codes} income needs a cached rate, but the FX book holds none."
    else:
        age = provenance.age_days(newest_rate, today)
        stale = age > provenance.FX_STALE_DAYS
        status = provenance.STATUS_STALE if stale else provenance.STATUS_OK
        detail = (
            f"{codes} income converted with cached Bank of Canada rates "
            f"(source {provider_label or 'unknown'}), newest {newest_rate.isoformat()} ({age}d)"
            + (
                f" — older than the {provenance.FX_STALE_DAYS}-day carry-forward window."
                if stale
                else "."
            )
        )
    sources.append(
        provenance.source_status(
            key="trade_date_fx",
            kind="market_data",
            label="trade-date FX cache",
            provider=provider_label,
            as_of=newest_rate,
            status=status,
            detail=detail,
            age_days=provenance.age_days(newest_rate, today) if newest_rate is not None else None,
        )
    )
    return provenance.build(sources)


def dividend_report(session: Session, user_id: int, year: int | None = None) -> dict[str, object]:
    """Yearly dividend income overview for `user_id` (docs/12 §Dividends).

    Committed (or manual) trades only: income staged by a pending investment
    import is not income yet (docs/03 staging model)."""
    rows = list(
        session.exec(
            select(Trade)
            .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
            .where(*committed_trade_conditions(user_id), Trade.action.in_(INCOME_ACTIONS))
            .order_by(Trade.date, Trade.id)
        ).all()
    )
    available_years = sorted({row.date.year for row in rows}, reverse=True)
    if year is None:
        year = available_years[0] if available_years else dt.date.today().year

    securities = {
        security.id: security
        for security in session.exec(select(Security).where(Security.user_id == user_id))
    }
    reporting = get_settings().reporting_currency
    fx = FxCache(session, reporting)
    today = dt.date.today()

    # Per-security accumulators (resolved rows only) keyed by action bucket.
    buckets: dict[int, dict[str, Decimal]] = defaultdict(
        lambda: {"dividend_cad": _ZERO, "distribution_cad": _ZERO, "roc_cad": _ZERO}
    )
    payments: dict[int, int] = defaultdict(int)
    last_payment: dict[int, dt.date] = {}
    by_month = [_ZERO] * 12
    totals = {"dividend_cad": _ZERO, "distribution_cad": _ZERO, "roc_cad": _ZERO}
    unresolved_fx = 0
    ttm_total = _ZERO

    for trade in rows:
        in_year = trade.date.year == year
        in_ttm = today - dt.timedelta(days=365) < trade.date <= today
        if not in_year and not in_ttm:
            continue
        rate = fx.rate(securities[trade.security_id].listing_currency, reporting, trade.date)
        if rate is None:
            # Unresolvable rows are excluded everywhere (docs/03 cache-only);
            # only the selected-year misses surface as `unresolved_fx`.
            if in_year:
                unresolved_fx += 1
            continue
        cad = cents(trade.price_native * rate)
        if in_ttm:
            ttm_total += cad
        if in_year:
            key = _ACTION_KEY[trade.action]
            buckets[trade.security_id][key] += cad
            totals[key] += cad
            by_month[trade.date.month - 1] += cad
            payments[trade.security_id] += 1
            last_payment[trade.security_id] = max(
                last_payment.get(trade.security_id, trade.date), trade.date
            )

    items: list[dict[str, object]] = []
    for security_id, acc in buckets.items():
        security = securities[security_id]
        dividend = cents(acc["dividend_cad"])
        distribution = cents(acc["distribution_cad"])
        roc = cents(acc["roc_cad"])
        items.append(
            {
                "security_id": security_id,
                "symbol": security.symbol,
                "name": security.name,
                "dividend_cad": dividend,
                "distribution_cad": distribution,
                "roc_cad": roc,
                "total_cad": dividend + distribution + roc,
                "payment_count": payments[security_id],
                "last_payment_date": last_payment.get(security_id),
            }
        )
    items.sort(key=lambda item: str(item["symbol"]))

    return {
        "year": year,
        "available_years": available_years,
        "items": items,
        "by_month": [
            {"month": f"{year}-{month:02d}", "total_cad": cents(by_month[month - 1])}
            for month in range(1, 13)
        ],
        "totals": {key: cents(value) for key, value in totals.items()}
        | {"total_cad": sum((cents(v) for v in totals.values()), _ZERO)},
        "ttm_total_cad": cents(ttm_total),
        "unresolved_fx": unresolved_fx,
        "provenance": _report_provenance(
            session, rows, securities, year, reporting, unresolved_fx, today
        ),
    }

"""Tax-loss harvesting alerts — flag unrealized losses in taxable accounts.

Compares current market value vs ACB (book cost) for each non-registered
position and surfaces harvesting opportunities. Respects superficial-loss
rules: warns if buying back within 30 days would trigger denial.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.accounts import Account, AccountType
from app.models.import_batches import ImportBatch
from app.models.trades import Trade, TradeAction
from app.services import valuation
from app.services.trade_filters import committed_trade_conditions

# Minimum unrealized loss (CAD) to surface as a harvesting opportunity.
_MIN_LOSS_THRESHOLD = Decimal("100")


def _taxable_accounts(session: Session, user_id: int) -> set[int]:
    """Account ids that are taxable (brokerage non-registered — docs/02).

    Loaded once per call; `Account.taxable` is true only for
    `brokerage_non_registered`, but the type check makes the tenancy intent
    explicit and keeps registered plans out even if the flag drifts.
    """
    rows = session.exec(
        select(Account).where(
            Account.user_id == user_id,
            Account.type == AccountType.brokerage_non_registered,
        )
    ).all()
    return {a.id for a in rows if a.taxable}


def _superficial_loss_risk(session: Session, user_id: int, security_id: int, today: date) -> bool:
    """Check if there's a buy for this security within ±30 days that would
    trigger the superficial-loss denial rule (docs/06).

    Committed (or manual) buys only — a buyback staged in a pending import
    batch is not a repurchase yet (docs/03 staging model)."""
    from datetime import timedelta

    window_start = today - timedelta(days=30)
    window_end = today + timedelta(days=30)
    buybacks = session.exec(
        select(Trade)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(
            *committed_trade_conditions(user_id),
            Trade.security_id == security_id,
            Trade.action == TradeAction.buy,
            Trade.date >= window_start,
            Trade.date <= window_end,
        )
    ).all()
    return len(buybacks) > 0


def compute_tax_loss_alerts(
    session: Session, user_id: int, today: date | None = None
) -> list[dict]:
    """Return positions at unrealized loss in taxable accounts."""
    today = today or datetime.now(UTC).date()
    positions = valuation.compute_positions(session, user_id, today)
    taxable_accounts = _taxable_accounts(session, user_id)
    alerts = []
    for pos in positions:
        if pos.get("account_id") not in taxable_accounts:
            continue
        qty = pos.get("quantity", Decimal("0"))
        if qty <= 0:
            continue  # only open long positions
        book = pos.get("book_cost_cad")
        mkt = pos.get("market_value_cad")
        if book is None or mkt is None:
            continue
        book = Decimal(str(book))
        mkt = Decimal(str(mkt))
        loss = book - mkt
        if loss < _MIN_LOSS_THRESHOLD:
            continue
        risk = _superficial_loss_risk(session, user_id, pos["security_id"], today)
        # Rough tax benefit at 50% inclusion * 30% marginal rate = 15%
        tax_benefit = (loss * Decimal("0.50") * Decimal("0.30")).quantize(Decimal("0.01"))
        alerts.append(
            {
                "security_id": pos["security_id"],
                "symbol": pos["symbol"],
                "account_id": pos["account_id"],
                "account_name": pos["account_name"],
                "quantity": qty,
                "book_cost_cad": book,
                "market_value_cad": mkt,
                "unrealized_loss_cad": loss,
                "estimated_tax_benefit_cad": tax_benefit,
                "superficial_loss_risk": risk,
            }
        )
    alerts.sort(key=lambda a: a["unrealized_loss_cad"], reverse=True)
    return alerts

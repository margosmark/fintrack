"""Superficial-loss rule (docs/06) — three conditions, both repurchase outcomes.

Deny a loss when ALL of:
1. the sale realizes a loss, AND
2. the same security was acquired within ±30 days of the sale in ANY account
   of the user, AND
3. a positive balance is still held 30 days after the sale.

Outcome by repurchase destination:
- another NON-registered account → denied loss is ADDED to the acquiring pool's
  ACB (deferral; resurfaces when those shares are disposed of);
- a REGISTERED account → denied outright, NO add-back anywhere.

Evaluation always runs over the COMPLETE trade history, so a sale whose window
is still open is provisional and recomputed on every rebuild/report run.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from app.models.trades import Trade, TradeAction

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.tax.acb import Acquisition, Disposal

WINDOW = dt.timedelta(days=30)

OUTCOME_DEFERRAL = "deferral"
OUTCOME_REGISTERED = "registered"


@dataclass(frozen=True)
class SuperficialLossDenial:
    security_id: int
    symbol: str
    sale_trade_id: int
    sale_account_id: int
    sale_date: dt.date
    denied_loss_cad: Decimal
    outcome: str  # "deferral" | "registered"
    acquiring_account_id: int | None


def evaluate_superficial_losses(
    disposals: "Sequence[Disposal]",
    acquisitions: "Sequence[Acquisition]",
    trade_rows: "Sequence[tuple[Trade, object, object]]",
    symbols: dict[int, str],
) -> tuple[list[SuperficialLossDenial], dict[int, Decimal]]:
    """Return (denials, addbacks) where addbacks maps repurchase trade_id → CAD amount."""
    acquisitions_by_security: dict[int, list[Acquisition]] = {}
    for acquisition in acquisitions:
        acquisitions_by_security.setdefault(acquisition.security_id, []).append(acquisition)
    for per_security in acquisitions_by_security.values():
        per_security.sort(key=lambda a: (a.date, a.trade_id))

    denials: list[SuperficialLossDenial] = []
    addbacks: dict[int, Decimal] = {}

    # One pass per security instead of a full trade-history rescan per
    # disposal (docs/29 F10): identical results, O(n log n) per pass.
    holdings = _holdings_by_disposal(trade_rows, disposals)

    for disposal in disposals:
        if disposal.gain_cad >= 0:
            continue  # only losses can be superficial
        window_start = disposal.date - WINDOW
        window_end = disposal.date + WINDOW
        repurchases = [
            a
            for a in acquisitions_by_security.get(disposal.security_id, [])
            if window_start <= a.date <= window_end
        ]
        if not repurchases:
            continue
        if holdings[(disposal.security_id, disposal.date + WINDOW)] <= 0:
            continue

        loss = -disposal.gain_cad
        repurchased_qty = sum((a.quantity for a in repurchases), Decimal("0"))
        # Deny only to the extent of the repurchase.
        denied = (
            loss * min(repurchased_qty, disposal.quantity_sold) / disposal.quantity_sold
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if denied == 0:
            continue

        non_registered = [a for a in repurchases if a.taxable]
        if non_registered:
            target = non_registered[0]  # earliest non-registered repurchase in the window
            outcome = OUTCOME_DEFERRAL
            addbacks[target.trade_id] = addbacks.get(target.trade_id, Decimal("0")) + denied
            acquiring_account_id: int | None = target.account_id
        else:
            outcome = OUTCOME_REGISTERED
            acquiring_account_id = None
        denials.append(
            SuperficialLossDenial(
                security_id=disposal.security_id,
                symbol=disposal.symbol,
                sale_trade_id=disposal.trade_id,
                sale_account_id=disposal.account_id,
                sale_date=disposal.date,
                denied_loss_cad=denied,
                outcome=outcome,
                acquiring_account_id=acquiring_account_id,
            )
        )

    return denials, addbacks


def _holdings_by_disposal(
    trade_rows: "Sequence[tuple[Trade, object, object]]",
    disposals: "Sequence[Disposal]",
) -> dict[tuple[int, dt.date], Decimal]:
    """Shares of one security held across ALL accounts on each disposal's
    (security, sale_date + WINDOW) day — one sorted sweep per security
    (registered counts; identical to the previous per-disposal rescan)."""
    from collections import defaultdict

    trades_by_security: dict[int, list[tuple[dt.date, TradeAction, Decimal, int]]] = defaultdict(
        list
    )
    for trade, account, _security in trade_rows:
        trades_by_security[trade.security_id].append(
            (trade.date, trade.action, trade.quantity, account.id)
        )
    # trade_rows arrives ordered by (date, id) from _load_rows; the sort makes
    # the sweep self-contained for any caller.
    for trades in trades_by_security.values():
        trades.sort(key=lambda t: (t[0],))

    days_by_security: dict[int, set[dt.date]] = defaultdict(set)
    for disposal in disposals:
        days_by_security[disposal.security_id].add(disposal.date + WINDOW)

    holdings: dict[tuple[int, dt.date], Decimal] = {}
    for security_id, days in days_by_security.items():
        per_account: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        total = Decimal("0")
        cursor = 0
        trades = trades_by_security.get(security_id, [])
        for day in sorted(days):
            while cursor < len(trades) and trades[cursor][0] <= day:
                _date, action, quantity, account_id = trades[cursor]
                cursor += 1
                current = per_account[account_id]
                if action in (TradeAction.buy, TradeAction.drip, TradeAction.transfer_in):
                    per_account[account_id] = current + abs(quantity)
                    total += abs(quantity)
                elif action in (TradeAction.sell, TradeAction.transfer_out):
                    per_account[account_id] = current - abs(quantity)
                    total -= abs(quantity)
                elif action == TradeAction.corporate_action_adjustment and quantity > 0:
                    # Split/consolidation replaces the running position.
                    per_account[account_id] = quantity
                    total += quantity - current
            holdings[(security_id, day)] = max(total, Decimal("0"))
    return holdings

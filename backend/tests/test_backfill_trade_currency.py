"""M132 — `scripts/backfill_trade_currency.py` (1359789): the pairing rail.

The M132 migration deliberately backfills NOTHING into `trades.currency`
(docs/02 §trades): the SOURCE FILE is the authority, and the script re-reads
it, lining the file's rows against the stored trades 1:1 on DATE and SYMBOL —
refusing on any doubt (AGENTS §3: strict beats silent for financial data).

The 1359789 fix taught the rail one asymmetry: a symbol-less CASH row (a
Deposit/EFT whose symbol cell is empty) lands on the per-user `CASH` security
(`api/trades.py::_cash_security`), so an empty file symbol must pair with
stored `CASH` — and with nothing else. Before the fix the verbatim compare
refused the FIRST cash row of every real file ("file symbol '' vs
stored 'CASH'"), which is exactly what the real dry run on account 731 hit.

`plan()` is pure given its arguments (file bytes, stored rows, the
security-symbol map) — no session, so these pins need no fixture DB.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.models.accounts import Account, AccountType
from app.models.trades import Trade, TradeAction
from scripts.backfill_trade_currency import AlignmentError, plan

D = Decimal

#: The Wealthsimple 16-field header the legacy parser expects (docs/41).
_WS_HEADER = (
    "effective_date,effective_time,settlement_date,account_id,account_type,"
    "activity_type,activity_sub_type,description,direction,symbol,name,"
    "currency,quantity,unit_price,commission,net_cash_amount"
)


def _deposit_csv(symbol: str = "") -> bytes:
    """One `MoneyMovement/EFT` Deposit row — direction in the description,
    symbol cell left as `symbol` (empty = the symbol-less cash row the fix
    pairs with CASH)."""
    row = f"2026-01-05,00:00:00,,ws-anon,TFSA,MoneyMovement,EFT,Deposit,,{symbol},,CAD,,,,250.00"
    return (_WS_HEADER + "\n" + row + "\n").encode()


def _account() -> Account:
    return Account(user_id=1, name="TFSA", type=AccountType.tfsa, currency="CAD", taxable=True)


def _stored_deposit(security_id: int) -> Trade:
    """The stored twin of `_deposit_csv`: contribution, quantity 0, CAD."""
    return Trade(
        user_id=1,
        account_id=1,
        security_id=security_id,
        action=TradeAction.contribution,
        date=date(2026, 1, 5),
        quantity=D("0"),
        price_native=D("250.00"),
        currency="CAD",
    )


def test_symbol_less_cash_row_pairs_with_the_cash_security() -> None:
    """An empty file symbol pairs with stored `CASH` (1359789).

    The real dry run refused trade 1357 — the file's FIRST cash row — with
    "file symbol '' vs stored 'CASH'". Post-fix the pair is accepted and the
    row needs no change at all (currency, action and amount already agree):
    `plan` returns no changes rather than an `AlignmentError`.
    """
    changes = plan(_account(), _deposit_csv(), [_stored_deposit(42)], {42: "CASH"})
    assert changes == []


def test_symbol_less_cash_row_still_refuses_any_other_stored_symbol() -> None:
    """The pairing stays exact: empty pairs with CASH **and nothing else**.

    A symbol-less file row against a non-CASH security is still a guess the
    script must refuse — the fix widens the CASH case only, it does not turn
    the rail into best-effort matching.
    """
    with pytest.raises(AlignmentError, match="diverges"):
        plan(_account(), _deposit_csv(), [_stored_deposit(42)], {42: "AAPL"})


def test_a_stated_symbol_still_pairs_verbatim() -> None:
    """The ordinary path is untouched: a stated symbol pairs as itself."""
    # Same symbol on both sides → pairs, no changes.
    assert plan(_account(), _deposit_csv("MSFT"), [_stored_deposit(7)], {7: "MSFT"}) == []
    # …and a mismatch still refuses, symbol row or not.
    with pytest.raises(AlignmentError, match="diverges"):
        plan(_account(), _deposit_csv("MSFT"), [_stored_deposit(7)], {7: "CASH"})

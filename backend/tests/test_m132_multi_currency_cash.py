"""M132 — the trade-cash leg keeps each ROW's currency (docs/05 §Portfolio value).

Real account 731 (an anonymized TFSA) holds USD and CAD rows in ONE CAD account. The
pre-M132 leg summed every docs/05 cash effect FACE-VALUE into a single
``cash_native`` and converted once at ``fx(account → reporting)``, so a USD
buy's ``-1,000.00 USD`` landed in a CAD total as ``-1,000.00``. Every figure
below is arithmetic done by hand from the seed, never captured from a run:

    seed (one CAD TFSA, reporting = CAD, USD→CAD 1.4000 cached on 2026-01-28)

      2026-01-05  contribution +1,000.00   currency CAD   → CAD  +1,000.00
      2026-01-08  buy 10 @ 100.00 USD      currency USD   → USD  -1,000.00
      2026-01-10  fee -5.00 (stored neg.)  currency USD   → USD  -5.00
      2026-01-12  transfer_in cash +145.75 currency CAD   → CAD  +145.75

    balances: CAD +1,145.75 · USD -1,005.00
    cash_cad  = _cents(1,145.75 x 1) + _cents(-1,005.00 x 1.4000)
              = 1,145.75 - 1,407.00 = -261.25

    pre-M132 face sum (what this asserts the FIX moved away from)
              = 1,000 - 1,000 - 5 + 145.75 = 140.75, converted once at the
                ACCOUNT's rate (CAD → 1) = 140.75

The last case pins the compatibility contract: a row with ``currency IS NULL``
(a trade written before M132) reads in the ACCOUNT's currency, which is
exactly what every consumer did before the column existed — so a pure-CAD
account's cash line is byte-identical to its pre-M132 value.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlmodel import select
from test_tax_acb import _user, add_trade, make_account, make_security, seed_fx

from app.models.accounts import Account, AccountType
from app.models.fx_rates import FxRate
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade
from app.models.trades import TradeAction as TA
from app.models.users import User
from app.services.account_value import account_value_history, investment_cash_cad

D = Decimal
TODAY = date(2026, 1, 31)


def _cash_security(session: Session, user: User) -> Security:
    security = Security(
        user_id=user.id,
        symbol="CASH",
        name="Cash",
        kind=SecurityKind.stock,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _set_currency(session: Session, trade: Trade, currency: str | None) -> Trade:
    trade.currency = currency
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def _seed(session: Session) -> tuple[User, Account, list[Trade]]:
    user = _user(session)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    cash = _cash_security(session, user)
    msft = make_security(session, user, "MSFT", "USD")
    # Inside the M115 <=7d carry-forward window of TODAY (2026-01-31):
    # a rate older than that is honestly refused, not guessed (docs/07).
    seed_fx(session, "USD", "CAD", "2026-01-28", "1.4000")

    rows = [
        add_trade(session, user, account, cash, TA.contribution, "2026-01-05", "0", "1000.00"),
        add_trade(session, user, account, msft, TA.buy, "2026-01-08", "10", "100.00"),
        add_trade(session, user, account, cash, TA.fee, "2026-01-10", "0", "-5.00"),
        add_trade(session, user, account, cash, TA.transfer_in, "2026-01-12", "0", "145.75"),
    ]
    return user, account, rows


def test_cash_leg_converts_each_row_at_its_own_currency(session: Session) -> None:
    """CAD and USD legs are summed separately, each at its own rate."""
    user, account, rows = _seed(session)
    assert user.id is not None
    for trade, currency in zip(rows, ("CAD", "USD", "USD", "CAD"), strict=True):
        _set_currency(session, trade, currency)

    cash = investment_cash_cad(session, user.id, [account], TODAY)
    # hand-computed in the module docstring: 1,145.75 - 1,407.00
    assert cash == D("-261.25")
    # …and it is NOT the pre-M132 face sum, which is the whole point.
    assert cash != D("140.75")


def test_account_value_history_cash_cell_matches_the_header(session: Session) -> None:
    """The chart's last cash point and the header's cash mean the same thing."""
    _unused_user, account, rows = _seed(session)
    for trade, currency in zip(rows, ("CAD", "USD", "USD", "CAD"), strict=True):
        _set_currency(session, trade, currency)

    history = account_value_history(session, account, date(2025, 1, 1), TODAY, "month", D("1000"))
    assert history["buckets"], "seed must produce at least one bucket"
    assert history["buckets"][-1]["cash_cad"] == D("-261.25")


def test_null_row_currency_reads_as_the_accounts(session: Session) -> None:
    """A pre-M132 row has no currency and behaves exactly as it used to."""
    user, account, rows = _seed(session)
    for trade in rows:
        _set_currency(session, trade, None)

    # Face sum 1,000 - 1,000 - 5 + 145.75 = 140.75, converted once at CAD = 1.
    assert investment_cash_cad(session, user.id, [account], TODAY) == D("140.75")


def test_pure_cad_account_is_byte_identical_without_the_column(session: Session) -> None:
    """Every row already in the account's currency changes nothing at all."""
    user = _user(session)
    account = make_account(session, user, "RRSP", AccountType.rrsp, taxable=False)
    cash = _cash_security(session, user)
    equity = make_security(session, user, "RY", "CAD")

    rows = [
        add_trade(session, user, account, cash, TA.contribution, "2026-01-05", "0", "5000.00"),
        add_trade(session, user, account, equity, TA.buy, "2026-01-08", "10", "100.00"),
        add_trade(session, user, account, cash, TA.fee, "2026-01-10", "0", "-2.50"),
    ]
    assert user.id is not None
    unset = investment_cash_cad(session, user.id, [account], TODAY)
    for trade in rows:
        _set_currency(session, trade, "CAD")
    stamped = investment_cash_cad(session, user.id, [account], TODAY)
    # 5,000 - 1,000 - 2.50 = 3,997.50 either way
    assert unset == stamped == D("3997.50")


def test_unresolved_row_rate_excludes_the_figure_never_a_zero(session: Session) -> None:
    """No cached USD rate in the window → `None`, never a CAD-only half-truth."""
    user, account, rows = _seed(session)
    assert user.id is not None
    for trade, currency in zip(rows, ("CAD", "USD", "USD", "CAD"), strict=True):
        _set_currency(session, trade, currency)
    # Wipe the rate the seed planted so the USD leg cannot be valued.
    for row in session.exec(select(FxRate)).all():
        session.delete(row)
    session.commit()

    assert investment_cash_cad(session, user.id, [account], TODAY) is None


def test_reimbursement_credit_is_money_in(session: Session) -> None:
    """The two `CAD credited` rows account 731 carries are +145.75 each.

    M131 stored them as -145.75 `fee`s, which counted money ARRIVING as money
    LEAVING — 583.00 of the -582.73-vs-WS-0.27 gap. The importer now writes a
    cash `transfer_in`, and the cash leg must credit it.
    """
    user = _user(session)
    assert user.id is not None
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    cash = _cash_security(session, user)
    rows = [
        add_trade(session, user, account, cash, TA.transfer_in, "2025-11-20", "0", "145.75"),
        add_trade(session, user, account, cash, TA.transfer_in, "2026-01-15", "0", "145.75"),
    ]
    for trade in rows:
        _set_currency(session, trade, "CAD")

    assert investment_cash_cad(session, user.id, [account], TODAY) == D("291.50")


def test_cash_cell_rounds_once_across_legs_when_cents_are_fractional(
    session: Session,
) -> None:
    """b9067cb pin: the bucket quantizes the SUM of the legs ONCE (docs/05).

    The header==bucket test above seeds whole-cent legs, where per-leg and
    once-quantization agree — that's why it stays green with the fix reverted.
    This one puts sub-cent fractions in BOTH legs so the two orders diverge:

        native  10.006 x 1       = 10.006  → per-leg _cents 10.01
        foreign  5.005 x 1.4000   =  7.007  → per-leg _cents  7.01
        per-leg sum                        = 17.02   (pre-b9067cb)
        quantize ONCE _cents(17.013)       = 17.01   (the fix, = header)

    `investment_cash_cad` has always summed the legs raw and quantized once,
    so the docs/05 identity (chart's last point == header's cash) holds
    exactly when the bucket does the same — and drifts by 0.01 when it
    doesn't, the real-731 shape (header 0.27, chart 0.26) caught by the
    live probe.
    """
    user = _user(session)
    assert user.id is not None
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    cash = _cash_security(session, user)
    # Inside the M115 <=7d window of the 2026-01-31 bucket end.
    seed_fx(session, "USD", "CAD", "2026-01-28", "1.4000")

    rows = [
        add_trade(session, user, account, cash, TA.contribution, "2026-01-05", "0", "10.006"),
        add_trade(session, user, account, cash, TA.contribution, "2026-01-08", "0", "5.005"),
    ]
    _set_currency(session, rows[0], "CAD")
    _set_currency(session, rows[1], "USD")

    history = account_value_history(session, account, date(2025, 1, 1), TODAY, "month", D("1000"))
    assert history["buckets"], "seed must produce at least one bucket"
    # Hand math above: 17.01 when quantized once, NOT 17.02 per leg.
    assert history["buckets"][-1]["cash_cad"] == D("17.01")
    # docs/05: the chart's last point and the header's cash are the same
    # number by construction — both must answer 17.01 here.
    assert investment_cash_cad(session, user.id, [account], TODAY) == D("17.01")

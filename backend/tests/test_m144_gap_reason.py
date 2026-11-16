"""M144 — the two-reason empty state on the value-history buckets (docs/08).

`valued: false` used to carry one sentence, "no valuation for this period",
for two different facts: a hole in the data, and an account that simply held
nothing yet. Every expectation below is arithmetic from the seed, never
captured from a run:

    account TFSA, created 2025-01-01, reporting CAD, day granularity
      2025-01-10  buy 10 @ 10.00 (CAD security)
      prices:     2025-01-10 = 10.00

    Jan 1 … Jan 9   nothing held at all        -> not_held   (a HOLDINGS gap)
    Jan 10 …        10 x 10.00 = 100.00        -> valued
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session
from test_tax_acb import _user, add_trade, make_account, make_security

from app.models.accounts import Account, AccountType
from app.models.prices import Price, PriceSource
from app.models.trades import TradeAction as TA
from app.models.users import User
from app.services.account_value import (
    DEFAULT_MIN_EVENT_VALUE,
    account_value_history,
    portfolio_value_history,
)

D = Decimal
TODAY = date(2025, 1, 15)


def _account(
    session: Session,
    user: User,
    name: str,
    kind: AccountType,
    *,
    created: str,
    currency: str = "CAD",
) -> Account:
    account = make_account(session, user, name, kind, taxable=False)
    # `created_at` is the row's own birth, and the engine floors the window at
    # it — pinned here so a bucket can sit BEFORE the first money arrives.
    account.created_at = datetime.fromisoformat(f"{created}T00:00:00+00:00")
    account.currency = currency
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _price(session: Session, user: User, security_id: int, day: str, value: str) -> None:
    session.add(
        Price(
            user_id=user.id,
            security_id=security_id,
            date=date.fromisoformat(day),
            price=D(value),
            source=PriceSource.yfinance,
        )
    )
    session.commit()


def _buckets(history: dict[str, object]) -> list[dict[str, object]]:
    return list(history["buckets"])  # type: ignore[arg-type]


def test_a_held_position_without_a_close_is_a_data_gap(session: Session) -> None:
    """The M144 case itself: held, unpriceable -> `no_price`, not `not_held`."""
    user = _user(session)
    account = _account(session, user, "TFSA", AccountType.tfsa, created="2025-01-01")
    security = make_security(session, user, "VUS", "CAD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.buy, "2025-01-10", "10", "10.00")

    history = account_value_history(
        session, account, date(2025, 1, 10), TODAY, "day", DEFAULT_MIN_EVENT_VALUE
    )
    buckets = _buckets(history)
    assert buckets, "the window must produce buckets at all"
    assert all(not bucket["valued"] for bucket in buckets)
    assert {bucket["gap_reason"] for bucket in buckets} == {"no_price"}


def test_leading_days_before_anything_is_held_say_not_held(session: Session) -> None:
    """A $0 opening week is an absence, not a data gap — and the first money
    day is a real valuation again.

    Seed arithmetic: Jan 10 contribution +200, then buy 10 @ 10.00 →
    cash 100.00 + positions 100.00 = 200.00.
    """
    user = _user(session)
    account = _account(session, user, "TFSA", AccountType.tfsa, created="2025-01-01")
    security = make_security(session, user, "VEQT", "CAD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.contribution, "2025-01-10", "0", "200.00")
    add_trade(session, user, account, security, TA.buy, "2025-01-10", "10", "10.00")
    _price(session, user, security.id, "2025-01-10", "10.00")

    history = account_value_history(
        session, account, date(2025, 1, 1), TODAY, "day", DEFAULT_MIN_EVENT_VALUE
    )
    buckets = _buckets(history)
    not_held = [b for b in buckets if b["gap_reason"] == "not_held"]
    valued = [b for b in buckets if b["valued"]]

    # Jan 1 … Jan 9 hold nothing: 9 days, all honestly "not held yet".
    assert len(not_held) == 9
    assert all(b["value_cad"] is None for b in not_held)
    # …and the first day with a position is valued at cash + holdings.
    assert valued and valued[0]["bucket_end"] == date(2025, 1, 10)
    assert valued[0]["value_cad"] == D("200.00")
    assert valued[0]["gap_reason"] is None


def test_a_missing_rate_is_an_fx_gap_not_a_holdings_gap(session: Session) -> None:
    """A USD account with no cached USD→CAD rate is a DATA gap (`fx`)."""
    user = _user(session)
    account = _account(
        session, user, "US RRSP", AccountType.rrsp, created="2025-01-01", currency="USD"
    )
    security = make_security(session, user, "MSFT", "USD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.contribution, "2025-01-02", "0", "500.00")

    history = account_value_history(
        session, account, date(2025, 1, 2), TODAY, "day", DEFAULT_MIN_EVENT_VALUE
    )
    buckets = _buckets(history)
    assert buckets and all(not bucket["valued"] for bucket in buckets)
    assert {bucket["gap_reason"] for bucket in buckets} == {"fx"}


def test_portfolio_merge_treats_not_held_as_zero_not_unknown(session: Session) -> None:
    """One account's empty opening weeks must not blank another's real numbers."""
    user = _user(session)
    assert user.id is not None
    rich = _account(session, user, "TFSA", AccountType.tfsa, created="2025-01-01")
    late = _account(session, user, "RRSP", AccountType.rrsp, created="2025-01-01")
    funded = make_security(session, user, "VEQT", "CAD")
    newcomer = make_security(session, user, "XEQT", "CAD")
    assert funded.id is not None and newcomer.id is not None
    add_trade(session, user, rich, funded, TA.contribution, "2025-01-02", "0", "1000.00")
    add_trade(session, user, late, newcomer, TA.buy, "2025-01-12", "5", "20.00")
    _price(session, user, newcomer.id, "2025-01-12", "20.00")

    portfolio = portfolio_value_history(
        session, user.id, date(2025, 1, 1), TODAY, "day", DEFAULT_MIN_EVENT_VALUE
    )
    merged = _buckets(portfolio)
    early = [b for b in merged if b["bucket_end"] == date(2025, 1, 2)]
    assert early, "the shared window must still draw the days both accounts exist for"
    # The RRSP holds nothing on Jan 2 — that is ZERO, so the portfolio still
    # shows the TFSA's real cash instead of a gap.
    assert early[0]["valued"] is True
    assert early[0]["value_cad"] == D("1000.00")
    assert all(b["gap_reason"] != "not_held" for b in merged)


def test_portfolio_merge_still_propagates_a_real_data_gap(session: Session) -> None:
    """The reason the wall of red appeared at all: one unpriceable holding
    makes the SUM unknown — that honesty rule is unchanged (docs/08)."""
    user = _user(session)
    assert user.id is not None
    healthy = _account(session, user, "TFSA", AccountType.tfsa, created="2025-01-01")
    broken = _account(session, user, "RRSP", AccountType.rrsp, created="2025-01-01")
    fine = make_security(session, user, "VEQT", "CAD")
    unpriced = make_security(session, user, "VUS", "CAD")
    assert fine.id is not None and unpriced.id is not None
    add_trade(session, user, healthy, fine, TA.contribution, "2025-01-02", "0", "1000.00")
    add_trade(session, user, broken, unpriced, TA.buy, "2025-01-03", "10", "50.00")

    portfolio = portfolio_value_history(
        session, user.id, date(2025, 1, 1), TODAY, "day", DEFAULT_MIN_EVENT_VALUE
    )
    merged = _buckets(portfolio)
    gap = [b for b in merged if b["bucket_end"] == date(2025, 1, 3)]
    assert gap and gap[0]["valued"] is False
    assert gap[0]["gap_reason"] == "no_price"


def test_a_later_zero_is_still_a_real_zero(session: Session) -> None:
    """`not_held` only ever runs at the FRONT: an account that bottoms out at
    zero mid-window is valued at zero, not red."""
    user = _user(session)
    account = _account(session, user, "TFSA", AccountType.tfsa, created="2025-01-01")
    security = make_security(session, user, "VEQT", "CAD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.contribution, "2025-01-02", "0", "500.00")
    add_trade(session, user, account, security, TA.withdrawal, "2025-01-12", "0", "-500.00")

    history = account_value_history(
        session, account, date(2025, 1, 1), TODAY, "day", DEFAULT_MIN_EVENT_VALUE
    )
    buckets = _buckets(history)
    # `not_held` only ever runs BEFORE the account's first money day (Jan 1)…
    assert all(b["bucket_end"] < date(2025, 1, 2) for b in buckets if b["gap_reason"] == "not_held")
    # …so the day it legitimately bottoms out at zero is a REAL zero.
    final = buckets[-1]
    assert final["valued"] is True
    assert final["value_cad"] == D("0.00")

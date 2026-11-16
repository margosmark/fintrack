"""Tax-engine contract tests (w4-contract, docs/06) — hand-calculated fixture values.

Every expected number below is computed BY HAND from
backend/tests/fixtures/trades_fixture.json quantities/prices and the seeded
manual fx rates; Decimal equality is exact. No network is touched.

Fixture → DB translation notes (trades table stores canonical forms):
- return_of_capital is a cash-only action: price_native carries the GROSS cash,
  so the fixture's 0.35/share x 150 shares held = 52.50.
- corporate_action_adjustment has no adjustment metadata column; per docs/06 the
  quantity>0 form carries the resulting absolute share count, so the 2:1 split
  on 20 VTI shares is seeded as quantity=40 (ACB preserved).
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.errors import APIError
from app.models.acb_pools import AcbPool
from app.models.accounts import Account, AccountType
from app.models.fx_rates import FxRate, FxSource
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.services import fx as fx_service
from app.tax.acb import compute_user_tax_state, rebuild_user_pools

D = Decimal


def _valet_down(*_args: object, **_kwargs: object) -> dict:
    """Stand in for an unreachable Bank of Canada (docs/07 retry exhaustion)."""
    raise fx_service.ValetUnavailable("simulated: no network in this suite")


def _user(session: Session) -> User:
    user = User(email=f"tax-{id(session)}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def seed_fx(session: Session, base: str, quote: str, day: str, rate: str) -> None:
    session.add(
        FxRate(
            base=base,
            quote=quote,
            date=date.fromisoformat(day),
            rate=D(rate),
            source=FxSource.manual,
        )
    )
    session.flush()


def make_account(
    session: Session, user: User, name: str, kind: AccountType, taxable: bool
) -> Account:
    account = Account(user_id=user.id, name=name, type=kind, currency="CAD", taxable=taxable)
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def make_security(session: Session, user: User, symbol: str, listing_currency: str) -> Security:
    security = Security(
        user_id=user.id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind=SecurityKind.etf if symbol == "VTI" else SecurityKind.stock,
        listing_currency=listing_currency,
        exchange="TSX" if symbol == "RY" else "ARCA",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def add_trade(  # mirrors the trades_fixture.json fields 1:1
    session: Session,
    user: User,
    account: Account,
    security: Security,
    action: TradeAction,
    day: str,
    quantity: str,
    price_native: str,
    commission: str = "0",
    fx_rate_used: str | None = None,
) -> Trade:
    trade = Trade(
        user_id=user.id,
        account_id=account.id,
        security_id=security.id,
        action=action,
        date=date.fromisoformat(day),
        quantity=D(quantity),
        price_native=D(price_native),
        commission=D(commission),
        fx_rate_used=D(fx_rate_used) if fx_rate_used else None,
    )
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


class Scenario:
    """The trades_fixture.json scenario, seeded deterministically."""

    def __init__(self, session: Session, with_usd_fx: bool = True) -> None:
        self.session = session
        self.user = _user(session)
        self.brokerage = make_account(
            session, self.user, "Brokerage", AccountType.brokerage_non_registered, taxable=True
        )
        self.registered = make_account(session, self.user, "RRSP", AccountType.rrsp, taxable=False)
        self.ry = make_security(session, self.user, "RY", "CAD")
        self.vti = make_security(session, self.user, "VTI", "USD")
        if with_usd_fx:
            # Manual deterministic USD/CAD rate on the buy date — deliberately NOT the
            # display value 1.35 stored on the fixture's vti_buy_usd row.
            seed_fx(session, "USD", "CAD", "2024-02-01", "1.36")

    def ry_trade(
        self, action: TradeAction, day: str, qty: str, price: str, comm: str = "0"
    ) -> Trade:
        return add_trade(
            self.session, self.user, self.brokerage, self.ry, action, day, qty, price, comm
        )

    def vti_trade(
        self,
        action: TradeAction,
        day: str,
        qty: str,
        price: str,
        comm: str = "0",
        fx_used: str | None = None,
    ) -> Trade:
        return add_trade(
            self.session,
            self.user,
            self.brokerage,
            self.vti,
            action,
            day,
            qty,
            price,
            comm,
            fx_used,
        )

    def pool(self, security: Security) -> tuple[D, D]:
        state = compute_user_tax_state(self.session, self.user.id)
        key = (self.brokerage.id, security.id)
        p = state.pools.get(key)
        return (p.quantity, p.total_acb_cad) if p else (None, None)


def test_pool_progression_matches_hand_calculation(session: Session) -> None:
    sc = Scenario(session)

    # ry_buy_1: 100 x 120.00 + 9.95 commission (same fx = 1 for CAD-listed)
    sc.ry_trade(TradeAction.buy, "2024-01-15", "100", "120.00", "9.95")
    assert sc.pool(sc.ry) == (D("100"), D("12009.95"))

    # ry_buy_2: +50 x 132.00 + 9.95
    sc.ry_trade(TradeAction.buy, "2024-03-10", "50", "132.00", "9.95")
    assert sc.pool(sc.ry) == (D("150"), D("18619.90"))

    # roc_distribution: gross 0.35/share x 150 held = 52.50 reduces ACB
    sc.ry_trade(TradeAction.return_of_capital, "2024-05-24", "0", "52.50")
    assert sc.pool(sc.ry) == (D("150"), D("18567.40"))

    # ry_sell_partial: proceeds 60x140 = 8400.00; ACB share 18567.40/150;
    # allocated 60/150 x 18567.40 = 7426.96 → gain +973.04
    sell = sc.ry_trade(TradeAction.sell, "2024-06-20", "-60", "140.00", "9.95")
    assert sc.pool(sc.ry) == (D("90"), D("11140.44"))
    disposal = next(
        d for d in compute_user_tax_state(session, sc.user.id).disposals if d.trade_id == sell.id
    )
    assert disposal.proceeds_cad == D("8400.00")
    assert disposal.acb_cad == D("7426.96")
    assert disposal.outlays_commissions_cad == D("9.95")
    assert disposal.gain_cad == D("973.04")

    # ry_superficial_loss_sell: proceeds 10x118 = 1180.00; allocated 11140.44x10/90
    # = 1237.83 → raw loss -57.83
    loss_sale = sc.ry_trade(TradeAction.sell, "2024-11-25", "-10", "118.00")
    assert sc.pool(sc.ry) == (D("80"), D("9902.61"))
    disposal = next(
        d
        for d in compute_user_tax_state(session, sc.user.id).disposals
        if d.trade_id == loss_sale.id
    )
    assert disposal.gain_cad == D("-57.83")

    # ry_superficial_loss_rebuy: +10 x 119.50 = 1195.00 plus the 57.83 denied-loss
    # add-back landing in THIS acquiring pool → docs/06 deferral outcome.
    sc.ry_trade(TradeAction.buy, "2024-12-05", "10", "119.50")
    assert sc.pool(sc.ry) == (D("90"), D("11155.44"))  # 9902.61 + 1195.00 + 57.83
    state = compute_user_tax_state(session, sc.user.id)
    assert len(state.denials) == 1
    denial = state.denials[0]
    assert denial.symbol == "RY"
    assert denial.sale_date == date(2024, 11, 25)
    assert denial.denied_loss_cad == D("57.83")
    assert denial.outcome == "deferral"
    assert denial.acquiring_account_id == sc.brokerage.id

    # vti_buy_usd: 20 x 210.00 x 1.36 (fx_rates table!) + 1.00 x 1.36 commission
    sc.vti_trade(TradeAction.buy, "2024-02-01", "20", "210.00", "1.00", fx_used="1.35")
    assert sc.pool(sc.vti) == (D("20"), D("5713.36"))

    # vti_split_2to1: quantity := 40 (resulting count), total ACB preserved exactly
    sc.vti_trade(TradeAction.corporate_action_adjustment, "2024-09-16", "40", "0")
    assert sc.pool(sc.vti) == (D("40"), D("5713.36"))


def test_fx_rate_used_is_display_only(session: Session) -> None:
    sc = Scenario(session)
    trade = sc.vti_trade(TradeAction.buy, "2024-02-01", "20", "210.00", "1.00", fx_used="1.35")

    # The lying display rate would give 20x210x1.35 + 1x1.35 = 5671.35; the engine
    # must use ONLY the fx_rates table (1.36) → 5713.36.
    assert sc.pool(sc.vti) == (D("20"), D("5713.36"))
    assert trade.fx_rate_used == D("1.35")


def test_missing_fx_rate_blocks_with_domain_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M115: the sweep backfills on a miss, so "genuinely unresolvable" now
    # means the fetch FAILED as well as nothing being cached (docs/07). Pin
    # the outage — otherwise a live BoC answer would make this pass for the
    # wrong reason.
    monkeypatch.setattr(fx_service, "fetch_valet_series", _valet_down)
    sc = Scenario(session, with_usd_fx=False)
    sc.vti_trade(TradeAction.buy, "2024-02-01", "20", "210.00", "1.00")

    with pytest.raises(APIError) as excinfo:
        compute_user_tax_state(session, sc.user.id)
    assert excinfo.value.code == "fx_rate_missing"
    assert "VTI" in excinfo.value.detail
    assert "2024-02-01" in excinfo.value.detail


def test_provisional_window_denial_disappears_without_repurchase(session: Session) -> None:
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-15", "100", "120.00", "9.95")
    sc.ry_trade(TradeAction.buy, "2024-03-10", "50", "132.00", "9.95")
    sc.ry_trade(TradeAction.return_of_capital, "2024-05-24", "0", "52.50")
    sc.ry_trade(TradeAction.sell, "2024-06-20", "-60", "140.00", "9.95")
    sc.ry_trade(TradeAction.sell, "2024-11-25", "-10", "118.00")
    sc.rebuy = sc.ry_trade(TradeAction.buy, "2024-12-05", "10", "119.50")

    state = compute_user_tax_state(session, sc.user.id)
    assert len(state.denials) == 1  # window open ⇒ provisional denial

    session.delete(sc.rebuy)
    session.flush()

    # Full-history re-evaluation over the SAME data with the repurchase gone:
    # condition 2 fails, the loss becomes deductible again, no add-back anywhere.
    state = compute_user_tax_state(session, sc.user.id)
    assert state.denials == []
    assert sc.pool(sc.ry) == (D("80"), D("9902.61"))


def test_roc_excess_above_zero_floor_is_capital_gain(session: Session) -> None:
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-10", "10", "1.00")  # pool ACB 10.00
    sc.ry_trade(TradeAction.return_of_capital, "2024-03-01", "0", "12.50")

    state = compute_user_tax_state(session, sc.user.id)
    assert [(r.excess_gain_cad, r.symbol) for r in state.roc_gains] == [(D("2.50"), "RY")]
    assert sc.pool(sc.ry) == (D("10"), D("0"))  # floored at zero


def test_corporate_action_signed_acb_delta(session: Session) -> None:
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-10", "10", "10.00")  # (10, 100.00)
    # quantity=0 form: price_native is a SIGNED CAD ACB delta applied directly.
    sc.ry_trade(TradeAction.corporate_action_adjustment, "2024-02-01", "0", "-25.00")
    assert sc.pool(sc.ry) == (D("10"), D("75.00"))


def test_invalid_corporate_action_rejected(session: Session) -> None:
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-10", "10", "10.00")
    sc.ry_trade(TradeAction.corporate_action_adjustment, "2024-02-01", "-5", "0")
    with pytest.raises(APIError) as excinfo:
        compute_user_tax_state(session, sc.user.id)
    assert excinfo.value.code == "invalid_corporate_action"


def test_oversell_rejected(session: Session) -> None:
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-10", "10", "10.00")
    sc.ry_trade(TradeAction.sell, "2024-02-01", "-11", "11.00")
    with pytest.raises(APIError) as excinfo:
        compute_user_tax_state(session, sc.user.id)
    assert excinfo.value.code == "oversell"


def test_registered_account_never_gets_a_pool(session: Session) -> None:
    sc = Scenario(session)
    add_trade(
        session,
        sc.user,
        sc.registered,
        sc.ry,
        TradeAction.buy,
        "2024-01-15",
        "100",
        "120.00",
        "9.95",
    )
    rebuild_user_pools(session, sc.user.id)
    rows = session.exec(select(AcbPool).where(AcbPool.user_id == sc.user.id)).all()
    assert rows == []


def _rrsp_trade(sc: Scenario, action: TradeAction, day: str, qty: str, price: str) -> Trade:
    return add_trade(
        session=sc.session,
        user=sc.user,
        account=sc.registered,
        security=sc.ry,
        action=action,
        day=day,
        quantity=qty,
        price_native=price,
    )


def test_registered_sell_and_transfer_out_track_holding_quantity(session: Session) -> None:
    """Registered holdings are replayed for validation; no pool, no disposal.

    buy 10 @ 100.00 + drip 2 @ 25.00 = 12 held; sell 4 and transfer_out 2 leave 6.
    """
    sc = Scenario(session)
    _rrsp_trade(sc, TradeAction.buy, "2024-01-10", "10", "100.00")
    _rrsp_trade(sc, TradeAction.drip, "2024-02-10", "2", "25.00")
    _rrsp_trade(sc, TradeAction.sell, "2024-03-10", "-4", "110.00")
    _rrsp_trade(sc, TradeAction.transfer_out, "2024-04-10", "-2", "0.00")

    state = compute_user_tax_state(session, sc.user.id)
    assert state.pools == {}  # registered accounts never enter acb_pools
    assert state.disposals == []  # no capital gain/loss is reported for them
    assert state.roc_gains == []
    assert [a.taxable for a in state.acquisitions] == [False, False]


def test_registered_oversell_still_rejected(session: Session) -> None:
    sc = Scenario(session)
    _rrsp_trade(sc, TradeAction.buy, "2024-01-10", "10", "100.00")
    _rrsp_trade(sc, TradeAction.sell, "2024-02-01", "-11", "110.00")
    with pytest.raises(APIError) as excinfo:
        compute_user_tax_state(session, sc.user.id)
    assert excinfo.value.code == "oversell"

    _rrsp_trade(sc, TradeAction.transfer_out, "2024-02-02", "-11", "0.00")
    with pytest.raises(APIError) as excinfo:
        compute_user_tax_state(session, sc.user.id)
    assert excinfo.value.code == "oversell"


def test_registered_sell_drains_cross_account_superficial_holdings(session: Session) -> None:
    """A registered sell reduces condition-3 holdings across accounts."""
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-05", "100", "130.00")
    sc.ry_trade(TradeAction.sell, "2024-06-01", "-100", "120.00")  # raw loss -1000
    # Repurchase lands in the RRSP, then is sold inside the window: 0 held 30 days
    # after the sale -> condition 3 fails, so the loss is NOT denied.
    _rrsp_trade(sc, TradeAction.buy, "2024-06-10", "100", "121.00")
    _rrsp_trade(sc, TradeAction.sell, "2024-06-20", "-100", "122.00")

    state = compute_user_tax_state(session, sc.user.id)
    assert state.denials == []


def test_registered_repurchase_denies_loss_with_no_addback(session: Session) -> None:
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-05", "100", "130.00")  # ACB 13000.00
    sc.ry_trade(TradeAction.sell, "2024-06-01", "-100", "120.00")  # raw loss -1000.00
    # Repurchase lands in the RRSP within 30 days → outright denial, no pool exists.
    add_trade(
        session, sc.user, sc.registered, sc.ry, TradeAction.buy, "2024-06-10", "100", "121.00"
    )

    state = compute_user_tax_state(session, sc.user.id)
    assert len(state.denials) == 1
    denial = state.denials[0]
    assert denial.denied_loss_cad == D("1000.00")
    assert denial.outcome == "registered"
    assert denial.acquiring_account_id is None
    # Non-registered side reduced proportionally to nothing; nothing was added back.
    assert sc.pool(sc.ry) == (None, None)


def test_rebuild_user_pools_is_idempotent_and_counts(session: Session) -> None:
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-15", "100", "120.00", "9.95")
    sc.ry_trade(TradeAction.sell, "2024-06-20", "-60", "140.00", "9.95")
    sc.vti_trade(TradeAction.buy, "2024-02-01", "20", "210.00", "1.00", fx_used="1.35")
    sc.vti_trade(TradeAction.corporate_action_adjustment, "2024-09-16", "40", "0")

    first = rebuild_user_pools(session, sc.user.id)
    second = rebuild_user_pools(session, sc.user.id)
    assert first == second == {"pools": 2, "trades": 4}
    assert sc.pool(sc.ry) == (D("40"), D("4803.98"))  # 12009.95 x 40/100


def test_transfer_in_builds_acb_pool_like_buy(session: Session) -> None:
    """transfer_in is an acquisition: shares enter the pool at cost basis."""
    sc = Scenario(session)
    # 10 RY shares transferred in at $25/share → 10 @ ACB 250.00 (CAD, fx=1).
    sc.ry_trade(TradeAction.transfer_in, "2024-03-01", "10", "25.00")
    assert sc.pool(sc.ry) == (D("10"), D("250.00"))


def test_transfer_out_reduces_pool_at_average_cost(session: Session) -> None:
    """transfer_out disposes shares at average cost without realizing a gain."""
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-10", "20", "100.00")  # ACB 2000, qty 20
    sc.ry_trade(TradeAction.transfer_in, "2024-02-10", "10", "50.00")  # +500 → 2500, qty 30
    sc.ry_trade(TradeAction.transfer_out, "2024-03-10", "-12", "0.00")  # -12 → 18 @ 1500
    assert sc.pool(sc.ry) == (D("18"), D("1500.00"))
    # Transfers are not sales: no realized disposal is recorded.
    state = compute_user_tax_state(session, sc.user.id)
    assert state.disposals == []


def test_transfer_does_not_realize_gain_but_sell_does(session: Session) -> None:
    """Regression: buy/sell gain math is unchanged by transfer handling."""
    sc = Scenario(session)
    sc.ry_trade(TradeAction.transfer_in, "2024-01-10", "10", "100.00")  # ACB 1000
    sc.ry_trade(TradeAction.sell, "2024-02-10", "-10", "150.00")  # proceeds 1500, gain 500
    state = compute_user_tax_state(session, sc.user.id)
    assert len(state.disposals) == 1
    assert state.disposals[0].gain_cad == D("500.00")


def test_transfer_in_counts_as_superficial_repurchase(session: Session) -> None:
    """A transfer_in within 30 days of a loss sale is a repurchase (deferral)."""
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-05", "100", "130.00")
    sc.ry_trade(TradeAction.sell, "2024-06-01", "-100", "120.00")  # raw loss -1000
    sc.ry_trade(TradeAction.transfer_in, "2024-06-10", "100", "121.00")  # repurchase
    state = compute_user_tax_state(session, sc.user.id)
    assert len(state.denials) == 1
    assert state.denials[0].denied_loss_cad == D("1000.00")
    assert state.denials[0].outcome == "deferral"


def test_transfer_out_reduces_superficial_holdings(session: Session) -> None:
    """transfer_out drains holdings so condition-3 can fail (no denial)."""
    sc = Scenario(session)
    sc.ry_trade(TradeAction.buy, "2024-01-05", "100", "130.00")
    sc.ry_trade(TradeAction.sell, "2024-06-01", "-100", "120.00")  # raw loss -1000
    sc.ry_trade(TradeAction.buy, "2024-06-10", "100", "121.00")  # repurchase
    sc.ry_trade(TradeAction.transfer_out, "2024-06-15", "-100", "0.00")  # drain → 0 held
    state = compute_user_tax_state(session, sc.user.id)
    # Holdings 30 days after the sale are zero → superficial-loss condition-3 fails.
    assert state.denials == []

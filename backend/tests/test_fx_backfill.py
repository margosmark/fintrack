"""M115 — the ACB sweep backfills and carry-forwards instead of 422-ing (docs/07).

BoC publishes business days only, so a weekend or holiday trade date has no
`fx_rates` row of its own. The exact-date lookup used to refuse those outright
and brick any file touching one (2025-12-26 is Boxing Day). These tests pin the
two-step precedence that replaced it — exact row, then carry-forward ≤7 days —
plus the ONE-batch-per-sweep backfill, the 7-day bound that keeps genuinely
missing data a 422, the docs/11 privacy wording, and the fact that a cash-only
row never drags a Valet call into a sweep.

Every network call is replaced by the `_Valet` stand-in below, so the file is
deterministic and asserts call COUNTS, not just outcomes.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.errors import APIError
from app.models.accounts import Account, AccountType
from app.models.fx_rates import FxRate, FxSource
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.services import fx as fx_service
from app.tax import acb
from app.tax.acb import compute_user_tax_state

D = Decimal

# The canary date: Friday 2025-12-26, Boxing Day — a business-shaped weekday
# BoC does NOT publish. 2025-12-25 (Christmas) is closed too; the last
# publication before both is Wednesday 2025-12-24.
HOLIDAY = date(2025, 12, 26)
CHRISTMAS = date(2025, 12, 25)
LAST_PUBLICATION = date(2025, 12, 24)
PUBLISHED_RATE = D("1.36000000")

_user_seq = itertools.count()


class _Valet:
    """Deterministic BoC stand-in: publishes weekdays minus `closed` days.

    `fail=True` simulates `MAX_RETRIES` exhausted (`ValetUnavailable`), which
    is how a real outage looks to the caller — `get_rates_between` swallows it
    and serves whatever the cache holds.
    """

    def __init__(self) -> None:
        self.closed: set[date] = set()
        self.fail = False
        self.rate = PUBLISHED_RATE
        self.calls: list[tuple[str, date, date]] = []

    def __call__(self, base: str, quote: str, start: date, end: date) -> dict[date, Decimal]:
        self.calls.append((base, start, end))
        if self.fail:
            raise fx_service.ValetUnavailable("simulated: all retries exhausted")
        published: dict[date, Decimal] = {}
        day = start
        while day <= end:
            if day.weekday() < 5 and day not in self.closed:
                published[day] = self.rate
            day += timedelta(days=1)
        return published


@pytest.fixture(name="valet")
def valet_fixture(monkeypatch: pytest.MonkeyPatch) -> _Valet:
    stub = _Valet()
    stub.closed = {CHRISTMAS, HOLIDAY}
    monkeypatch.setattr(fx_service, "fetch_valet_series", stub)
    return stub


@pytest.fixture(name="privacy_on")
def privacy_on_fixture() -> Iterator[None]:
    """docs/11 step 6 for the duration of one test (the ContextVar is sticky)."""
    fx_service.bind_privacy_lock(True)
    yield
    fx_service.bind_privacy_lock(False)


# --- fixtures ----------------------------------------------------------------


def _user(session: Session) -> User:
    user = User(email=f"m115-{next(_user_seq)}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _account(session: Session, user: User, *, taxable: bool) -> Account:
    account = Account(
        user_id=user.id,
        name="Margin" if taxable else "TFSA",
        type=AccountType.brokerage_non_registered if taxable else AccountType.rrsp,
        currency="CAD",
        taxable=taxable,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _security(session: Session, user: User, symbol: str, listing_currency: str) -> Security:
    security = Security(
        user_id=user.id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind=SecurityKind.etf,
        listing_currency=listing_currency,
        exchange="NASDAQ" if listing_currency == "USD" else "TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _trade(
    session: Session,
    user: User,
    account: Account,
    security: Security,
    action: TradeAction,
    day: date,
    quantity: str,
    price: str,
    commission: str = "0",
) -> Trade:
    trade = Trade(
        user_id=user.id,
        account_id=account.id,
        security_id=security.id,
        action=action,
        date=day,
        quantity=D(quantity),
        price_native=D(price),
        commission=D(commission),
    )
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def _seed_rate(session: Session, day: date, rate: str, source: FxSource) -> None:
    session.add(FxRate(base="USD", quote="CAD", date=day, rate=D(rate), source=source))
    session.flush()


def _nvda_buy(
    session: Session, *, taxable: bool = True, day: date = HOLIDAY
) -> tuple[User, Account, Security]:
    user = _user(session)
    account = _account(session, user, taxable=taxable)
    nvda = _security(session, user, "NVDA", "USD")
    _trade(session, user, account, nvda, TradeAction.buy, day, "10", "130.00")
    return user, account, nvda


# --- the canary ---------------------------------------------------------------


def test_holiday_trade_carries_forward_from_the_last_publication(
    session: Session, valet: _Valet, caplog: pytest.LogCaptureFixture
) -> None:
    """2025-12-26 has no BoC row: the sweep backfills 12-24 and prices from it."""
    user, account, nvda = _nvda_buy(session)

    caplog.set_level(logging.INFO, logger="app.tax.acb")
    state = compute_user_tax_state(session, user.id)

    # 10 x 130.00 x 1.36 (the 2025-12-24 publication) + 0 commission.
    assert state.pools[(account.id, nvda.id)].total_acb_cad == D("1768.00")

    # The backfilled row is IN the table, so the next reader needs no fetch.
    cached = session.exec(
        select(FxRate).where(FxRate.base == "USD", FxRate.date == LAST_PUBLICATION)
    ).one()
    assert cached.rate == PUBLISHED_RATE
    assert cached.source == FxSource.boc_valet

    assert len(valet.calls) == 1
    assert valet.calls[0] == ("USD", HOLIDAY - timedelta(days=7), HOLIDAY)

    # The sweep records WHICH publication day priced the holiday.
    assert any(
        "carry-forward" in message and "2025-12-26" in message and "2025-12-24" in message
        for message in caplog.messages
    ), caplog.messages


def test_second_sweep_hits_the_cache_and_never_refetches(session: Session, valet: _Valet) -> None:
    user, account, nvda = _nvda_buy(session)

    first = compute_user_tax_state(session, user.id)
    assert len(valet.calls) == 1

    second = compute_user_tax_state(session, user.id)
    assert len(valet.calls) == 1, "the backfilled rows must satisfy the next sweep"
    assert second.pools[(account.id, nvda.id)].total_acb_cad == D("1768.00")
    assert second.pools == first.pools


def test_one_valet_call_covers_every_missing_date_of_the_sweep(
    session: Session, valet: _Valet
) -> None:
    """Batching: three scattered dates, ONE call — never per-date-by-date."""
    user = _user(session)
    account = _account(session, user, taxable=True)
    nvda = _security(session, user, "NVDA", "USD")
    for day in (LAST_PUBLICATION, HOLIDAY, date(2026, 1, 5)):
        _trade(session, user, account, nvda, TradeAction.buy, day, "1", "100.00")

    state = compute_user_tax_state(session, user.id)

    assert len(valet.calls) == 1
    assert valet.calls[0] == ("USD", date(2025, 12, 17), date(2026, 1, 5))
    assert state.pools[(account.id, nvda.id)].quantity == D("3")


# --- the 7-day bound ----------------------------------------------------------


def test_gap_beyond_the_bound_still_422s(session: Session, valet: _Valet) -> None:
    """Nearest row 8 days back (a 7-day gap) + fetch down = genuinely missing."""
    valet.fail = True
    _seed_rate(session, HOLIDAY - timedelta(days=8), "1.30000000", FxSource.manual)
    user, _account_obj, _nvda = _nvda_buy(session)

    with pytest.raises(APIError) as excinfo:
        compute_user_tax_state(session, user.id)

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "fx_rate_missing"
    assert "NVDA" in excinfo.value.detail
    assert "2025-12-26" in excinfo.value.detail
    assert len(valet.calls) == 1  # it TRIED once, then gave up — not a retry loop


def test_a_row_exactly_seven_days_back_is_carried_forward(session: Session, valet: _Valet) -> None:
    """The boundary: distance ≤ 7 resolves WITHOUT any network call."""
    boundary = HOLIDAY - timedelta(days=acb.CARRY_FORWARD_MAX_DAYS)
    assert boundary == date(2025, 12, 19)
    _seed_rate(session, boundary, "1.30000000", FxSource.manual)
    user, account, nvda = _nvda_buy(session)

    state = compute_user_tax_state(session, user.id)

    assert valet.calls == []
    # 10 x 130.00 x 1.30 — the 2025-12-19 row, seven days back.
    assert state.pools[(account.id, nvda.id)].total_acb_cad == D("1690.00")


def test_fetch_failure_with_nothing_cached_keeps_the_422(session: Session, valet: _Valet) -> None:
    valet.fail = True
    user, _account_obj, _nvda = _nvda_buy(session, day=LAST_PUBLICATION)

    with pytest.raises(APIError) as excinfo:
        compute_user_tax_state(session, user.id)

    assert excinfo.value.code == "fx_rate_missing"
    assert len(valet.calls) == 1


# --- privacy (docs/11 step 6) --------------------------------------------------


def test_privacy_mode_makes_no_fetch_and_explains_itself(
    session: Session, valet: _Valet, privacy_on: None
) -> None:
    user, _account_obj, _nvda = _nvda_buy(session, day=LAST_PUBLICATION)

    with pytest.raises(APIError) as excinfo:
        compute_user_tax_state(session, user.id)

    assert valet.calls == [], "privacy mode must block the fetch, not just its result"
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "fx_rate_missing"
    assert "privacy mode blocks the backfill" in excinfo.value.detail
    assert "add a manual rate" in excinfo.value.detail


def test_privacy_mode_still_reads_cached_and_manual_rows(
    session: Session, valet: _Valet, privacy_on: None
) -> None:
    """Cache reads never reach the seam — an existing rate keeps computing."""
    _seed_rate(session, HOLIDAY, "1.31000000", FxSource.manual)
    user, account, nvda = _nvda_buy(session)

    state = compute_user_tax_state(session, user.id)

    assert valet.calls == []
    # 10 x 130.00 x 1.31 — a manual row is read exactly as a BoC row would be.
    assert state.pools[(account.id, nvda.id)].total_acb_cad == D("1703.00")


# --- scope: only dates the replay actually asks for ----------------------------


def test_cash_only_rows_never_drag_a_fetch_into_the_sweep(session: Session, valet: _Valet) -> None:
    """A USD dividend or split on an uncovered date needs no rate at all."""
    user = _user(session)
    account = _account(session, user, taxable=True)
    nvda = _security(session, user, "NVDA", "USD")
    _trade(session, user, account, nvda, TradeAction.dividend, HOLIDAY, "0", "0.42")
    _trade(
        session,
        user,
        account,
        nvda,
        TradeAction.corporate_action_adjustment,
        HOLIDAY + timedelta(days=30),
        "20",
        "0",
    )

    state = compute_user_tax_state(session, user.id)

    assert valet.calls == []
    # The split still lands; cash-only rows move no cost basis.
    assert state.pools[(account.id, nvda.id)].quantity == D("20")
    assert state.pools[(account.id, nvda.id)].total_acb_cad == D("0")


def test_registered_sells_need_no_rate(session: Session, valet: _Valet) -> None:
    """A TFSA sell `continue`s before the conversion — its date must not fetch."""
    user = _user(session)
    account = _account(session, user, taxable=False)
    nvda = _security(session, user, "NVDA", "USD")
    _seed_rate(session, date(2025, 12, 1), "1.30000000", FxSource.manual)
    _trade(session, user, account, nvda, TradeAction.buy, date(2025, 12, 1), "10", "130.00")
    _trade(session, user, account, nvda, TradeAction.sell, date(2026, 6, 1), "-10", "150.00")

    state = compute_user_tax_state(session, user.id)

    assert valet.calls == []
    assert state.pools == {}  # registered accounts never get pools
    assert state.acquisitions  # chronology still registers for the denial rule


# --- the single-date seam (positions / registered book cost) -------------------


def test_fx_rate_cad_carries_forward_without_touching_the_network(
    session: Session, valet: _Valet
) -> None:
    """`replay_registered_book_cost` reads this — a holiday date must resolve."""
    _seed_rate(session, LAST_PUBLICATION, "1.36000000", FxSource.boc_valet)

    assert acb.fx_rate_cad(session, "USD", HOLIDAY, "NVDA") == PUBLISHED_RATE
    # An exact-date row still wins — same query shape, no regression.
    assert acb.fx_rate_cad(session, "USD", LAST_PUBLICATION, "NVDA") == PUBLISHED_RATE
    assert valet.calls == []


def test_fx_rate_cad_keeps_refusing_beyond_the_bound(session: Session, valet: _Valet) -> None:
    _seed_rate(session, HOLIDAY - timedelta(days=8), "1.30000000", FxSource.manual)

    with pytest.raises(APIError) as excinfo:
        acb.fx_rate_cad(session, "USD", HOLIDAY, "NVDA")

    assert excinfo.value.code == "fx_rate_missing"
    assert valet.calls == []


def test_fx_rate_cad_is_a_no_op_for_cad_listings(session: Session) -> None:
    assert acb.fx_rate_cad(session, "CAD", HOLIDAY, "RY.TO") == D("1")


# --- the cache-only caller (docs/03) ------------------------------------------


def test_allow_fetch_false_keeps_the_seam_shut(session: Session, valet: _Valet) -> None:
    """Ingestion paths (demo seeding) must never open a connection.

    The docs/07 backfill is a per-caller decision, so `allow_fetch=False`
    answers the plain envelope instead of reaching for Valet — this is what
    keeps the demo's fx gaps empty (docs/30 H7) and its rebuild deferring.
    """
    user, _account_obj, _nvda = _nvda_buy(session, day=LAST_PUBLICATION)

    with pytest.raises(APIError) as excinfo:
        compute_user_tax_state(session, user.id, allow_fetch=False)

    assert valet.calls == []
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "fx_rate_missing"
    assert "privacy mode" not in excinfo.value.detail  # this refusal is not privacy


# --- the audit line's destination ----------------------------------------------


def test_the_carry_forward_record_has_a_real_destination() -> None:
    """docs/07: "which publication day priced this trade" must reach the logs.

    uvicorn's LOGGING_CONFIG covers only the `uvicorn*` loggers and never the
    root, so an unhandled `app.*` tree falls through to `lastResort`
    (WARNING-only) and its INFO records vanish — on prod, `app.main`'s own
    "Database connection verified" never appeared in `docker logs`.
    `app.main._configure_app_logging` opens the tree to INFO on stderr, and
    this pins it so the carry-forward record cannot silently go dark again.
    """
    app_logger = logging.getLogger("app")
    assert app_logger.level <= logging.INFO, "app INFO would be filtered before it is built"
    assert app_logger.handlers, "no handler on `app` -> INFO records die at lastResort"

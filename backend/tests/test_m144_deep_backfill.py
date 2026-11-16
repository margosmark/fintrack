"""M144 — the deep price backfill sweep (docs/07 §Backfill depth, §Series validation).

The network seam (`market_data.fetch_history_candidates`) is monkeypatched
throughout: this suite is about WHICH window is fetched, WHICH listing is
cached and WHAT is refused — never about what Yahoo answers today.

The ledger fixture mirrors the measured prod rows (account 459's `VUS`):

    2022-03-04  buy 441.93 @ 79.69 CAD   (the security's own recorded price)
    candidates: bare `VUS`  = a USD listing closing $30.23 — a DIFFERENT
                `VUS.TO`   = the TSX listing closing $79.69 — this one
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session
from sqlmodel import select
from test_tax_acb import _user, add_trade, make_account, make_security

from app.core.errors import api_error
from app.models.accounts import AccountType
from app.models.fx_rates import FxRate, FxSource
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.prices import Price
from app.models.trades import TradeAction as TA
from app.models.watchlist import WatchlistItem
from app.services import deep_backfill, market_data
from app.services.market_data import CandidateSeries

D = Decimal
TODAY = date(2026, 10, 11)


def _series(symbol: str, currency: str, closes: dict[date, Decimal]) -> CandidateSeries:
    return CandidateSeries(symbol, currency, closes)


def _install(monkeypatch: pytest.MonkeyPatch, *answers: CandidateSeries) -> list[dict]:
    """Record every fetch and answer it with the given candidates."""
    seen: list[dict] = []

    def fake(symbol: str, start: date, end: date) -> list[CandidateSeries]:
        seen.append({"symbol": symbol, "start": start, "end": end})
        return list(answers)

    monkeypatch.setattr(market_data, "fetch_history_candidates", fake)
    return seen


def _held(session: Session, security_id: int) -> list[Price]:
    return list(session.exec(select(Price).where(Price.security_id == security_id)).all())


def test_start_bound_is_the_rows_own_need(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2022 trade asks for 2022 — not a hard-coded decade, not 365 days."""
    user = _user(session)
    account = make_account(session, user, "RRSP", AccountType.rrsp, taxable=False)
    security = make_security(session, user, "VUS", "CAD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.buy, "2022-03-04", "10", "79.69")
    seen = _install(monkeypatch, _series("VUS", "CAD", {date(2022, 3, 4): D("79.69")}))

    result = deep_backfill.backfill_security(session, security, TODAY)
    assert result.skipped is None
    assert result.start == date(2022, 3, 4)
    assert seen and seen[0]["start"] == date(2022, 3, 4)


def test_a_watched_only_row_starts_at_its_watch_date(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No trades at all: the watchlist entry is the need (docs/07)."""
    user = _user(session)
    security = make_security(session, user, "MSFT", "USD")
    assert security.id is not None and user.id is not None
    session.add(
        WatchlistItem(
            user_id=user.id,
            security_id=security.id,
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    session.commit()
    seen = _install(monkeypatch, _series("MSFT", "USD", {date(2026, 6, 1): D("500")}))

    result = deep_backfill.backfill_security(session, security, TODAY)
    assert result.skipped is None
    assert seen and seen[0]["start"] == date(2026, 6, 1)


def test_the_asking_listing_wins_when_the_ledger_agrees(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The M144 case: the bare ticker answers as a DIFFERENT instrument, and
    the `.TO` listing is the one the recorded trade price matches."""
    user = _user(session)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    security = make_security(session, user, "VUS", "CAD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.buy, "2022-03-04", "441.93", "79.69")
    # USD→CAD, so the USD candidate is compared in the ledger's own currency.
    session.add(
        FxRate(
            base="USD",
            quote="CAD",
            date=date(2022, 3, 4),
            rate=D("1.2700"),
            source=FxSource.manual,
        )
    )
    session.commit()
    _install(
        monkeypatch,
        _series("VUS", "USD", {date(2022, 3, 4): D("30.23")}),  # Virtus, NYSEArca
        _series("VUS.TO", "CAD", {date(2022, 3, 4): D("75.88")}),  # Vanguard, TSX
    )

    result = deep_backfill.backfill_security(session, security, TODAY)
    assert result.skipped is None
    assert result.series == "VUS.TO"
    cached = {(row.date, row.price) for row in _held(session, security.id)}
    assert cached == {(date(2022, 3, 4), D("75.88"))}, "the wrong listing must not be cached"


def test_a_native_priced_row_is_recognized_even_when_it_reads_as_cad(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured prod VTI/VEA case (M144): a pre-M132 row has no
    `trades.currency`, so its broker-native price READS in the account's CAD,
    but the number itself is the US listing's own close — 329.91 against a
    $329.91 USD close. The fx-converted reading alone would double-convert
    and refuse a series that plainly matches; the tight raw reading accepts
    it."""
    user = _user(session)
    account = make_account(
        session, user, "Margin", AccountType.brokerage_non_registered, taxable=True
    )
    security = make_security(session, user, "VTI", "USD")
    assert security.id is not None
    trade = add_trade(session, user, account, security, TA.buy, "2025-11-13", "10", "329.91")
    assert trade.currency is None, "the fixture row predates M132"
    session.add(
        FxRate(
            base="USD",
            quote="CAD",
            date=date(2025, 11, 13),
            rate=D("1.4018"),
            source=FxSource.manual,
        )
    )
    session.commit()
    # Only the US listing answers; its close IS the recorded price at face
    # value, and 1.4018x converted would be 462.47 — far off.
    _install(monkeypatch, _series("VTI", "USD", {date(2025, 11, 13): D("329.91")}))

    result = deep_backfill.backfill_security(session, security, TODAY)
    assert result.skipped is None
    assert result.series == "VTI"
    cached = {(row.date, row.price) for row in _held(session, security.id)}
    assert cached == {(date(2025, 11, 13), D("329.91"))}


def test_no_listing_agrees_is_a_refusal_not_a_guess(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every candidate contradicts the ledger: cache NOTHING, say why.

    The trade says 100.00 CAD; the bare listing is a USD instrument (33.01 CAD
    at the cached rate) and the `.TO` one closes at 48.10 CAD — both are
    comparable, and neither is the instrument this row recorded.
    """
    user = _user(session)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    security = make_security(session, user, "VGRO", "CAD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.buy, "2022-03-03", "100", "100.00")
    session.add(
        FxRate(
            base="USD",
            quote="CAD",
            date=date(2022, 3, 3),
            rate=D("1.2700"),
            source=FxSource.manual,
        )
    )
    session.commit()
    _install(
        monkeypatch,
        _series("VGRO", "USD", {date(2022, 3, 3): D("25.99")}),
        _series("VGRO.TO", "CAD", {date(2022, 3, 3): D("48.10")}),
    )

    result = deep_backfill.backfill_security(session, security, TODAY)
    assert result.skipped == "no_listing_agrees"
    assert _held(session, security.id) == [], "an honest gap beats a fabricated valuation"


def test_trades_outside_every_candidates_data_keep_the_legacy_order(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is checkable -> the first candidate answers, as it always did."""
    user = _user(session)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    security = make_security(session, user, "VEQT", "CAD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.buy, "2022-03-04", "10", "30.00")
    _install(monkeypatch, _series("VEQT", "CAD", {date(2026, 10, 9): D("62.82")}))

    result = deep_backfill.backfill_security(session, security, TODAY)
    assert result.skipped is None
    assert result.series == "VEQT"


def test_a_dead_symbol_is_skipped_without_a_fetch(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    security = make_security(session, user, "APTX", "CAD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.buy, "2026-06-18", "15", "30.00")
    security.dead_symbol = True
    security.dead_symbol_reason = "Quote not found for symbol: APTX"
    session.add(security)
    session.commit()
    seen = _install(monkeypatch, _series("APTX", "CAD", {}))

    result = deep_backfill.backfill_security(session, security, TODAY)
    assert result.skipped == "dead_symbol"
    assert seen == [], "M141: the automatic sweep never probes a dead row"


def test_the_cash_placeholder_is_never_priced(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row whose trades all move zero shares represents CASH (docs/05), not
    a listed instrument — `CASH` would otherwise fetch Pathward Financial."""
    user = _user(session)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    cash = make_security(session, user, "CASH", "CAD")
    assert cash.id is not None
    add_trade(session, user, account, cash, TA.contribution, "2022-02-28", "0", "1000.00")
    seen = _install(monkeypatch, _series("CASH", "CAD", {date(2022, 2, 28): D("52.55")}))

    result = deep_backfill.backfill_security(session, cash, TODAY)
    assert result.skipped == "cash_placeholder"
    assert seen == []


def test_a_privacy_mode_tenant_is_reported_not_fetched(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam guard raises; the sweep records the refusal and moves on."""
    user = _user(session)
    assert user.id is not None
    user.privacy_mode = True
    session.add(user)
    session.commit()
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    security = make_security(session, user, "MSFT", "USD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.buy, "2026-01-05", "1", "500.00")

    def guarded(symbol: str, start: date, end: date) -> list[CandidateSeries]:
        market_data._guard_external_fetch()
        return []

    monkeypatch.setattr(market_data, "fetch_history_candidates", guarded)
    results = deep_backfill.deep_backfill_all(session, user_id=user.id, today=TODAY)
    assert [r.skipped for r in results] == ["fetch_refused"]
    assert results[0].detail and "privacy" in results[0].detail.lower()
    assert _held(session, security.id) == []


def test_a_re_run_adds_nothing(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotent: the second pass inserts zero rows (docs/07)."""
    user = _user(session)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    security = make_security(session, user, "VEQT", "CAD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.buy, "2026-01-05", "10", "60.00")
    closes = {date(2026, 1, 5): D("60.00"), date(2026, 1, 6): D("61.00")}
    _install(monkeypatch, _series("VEQT", "CAD", closes))

    first = deep_backfill.backfill_security(session, security, TODAY)
    second = deep_backfill.backfill_security(session, security, TODAY)
    assert first.inserted == 2
    assert second.inserted == 0
    assert len(_held(session, security.id)) == 2


def test_a_staged_trade_never_drives_the_sweep(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/03: a pending import batch is invisible to every derived read."""
    user = _user(session)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    security = make_security(session, user, "XEQT", "CAD")
    assert security.id is not None and user.id is not None
    batch = ImportBatch(
        user_id=user.id,
        account_id=account.id,
        institution="T",
        file_name="f.csv",
        status=ImportBatchStatus.pending_review,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    trade = add_trade(session, user, account, security, TA.buy, "2026-01-05", "10", "45.00")
    trade.import_batch_id = batch.id
    session.add(trade)
    session.commit()
    seen = _install(monkeypatch, _series("XEQT", "CAD", {date(2026, 1, 5): D("45.00")}))

    result = deep_backfill.backfill_security(session, security, TODAY)
    assert result.skipped == "not_held_or_watched"
    assert seen == []


def test_the_dry_run_writes_nothing(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user(session)
    assert user.id is not None
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    security = make_security(session, user, "VEQT", "CAD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.buy, "2026-01-05", "10", "60.00")
    _install(monkeypatch, _series("VEQT", "CAD", {date(2026, 1, 5): D("60.00")}))

    results = deep_backfill.deep_backfill_all(session, user_id=user.id, today=TODAY, apply=False)
    assert [r.skipped for r in results] == [None]
    assert results[0].inserted == 1, "the preview reports what WOULD land"
    assert _held(session, security.id) == []


def test_an_exception_from_the_seam_is_a_reported_skip(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep must survive one row's failure and keep going."""
    user = _user(session)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    broken = make_security(session, user, "BROKEN", "CAD")
    fine = make_security(session, user, "VEQT", "CAD")
    assert broken.id is not None and fine.id is not None
    add_trade(session, user, account, broken, TA.buy, "2026-01-05", "1", "10.00")
    add_trade(session, user, account, fine, TA.buy, "2026-01-05", "1", "60.00")

    def flaky(symbol: str, start: date, end: date) -> list[CandidateSeries]:
        if symbol == "BROKEN":
            raise api_error(503, "market data unavailable", "market_data_unavailable")
        return [_series("VEQT", "CAD", {date(2026, 1, 5): D("60.00")})]

    monkeypatch.setattr(market_data, "fetch_history_candidates", flaky)
    results = deep_backfill.deep_backfill_all(session, user_id=user.id, today=TODAY)
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["BROKEN"].skipped == "fetch_refused"
    assert by_symbol["VEQT"].skipped is None

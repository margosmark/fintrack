"""M155 — a standing repair-needed report instead of one-off repairs.

Contract under test (docs/07 §Repair-needed report):

- **Family 1, bare collisions.** A row whose symbol answers TWO listings is
  judged by the LEDGER: the committed trade currencies first (the 294 case —
  a CAD buy against a USD listing), the M144 price evidence when the currency
  cannot say, and "a human must pick" when neither can. The verdict is
  `repair` / `info` / `review` accordingly, never a silent re-point.
- **Family 1, twins.** `X` + `X.TO` under one user is the 293/298 leftover
  `security_dedup` can never merge — reported once, with both row ids.
- **Family 2, live-symbol heuristics.** A stored NAME (`match: search`), a
  listing currency the ledger corroborates, an exchange naming another
  market, a name that belongs to a different instrument, a `dead_symbol`
  verdict the market has since contradicted or confirmed.
- **The report never writes** and always names the command that would.
- **Noise control**: a canary probe suppresses the whole run when market data
  is unreachable (one line, not a wall of `no_listing`), privacy mode costs
  zero packets, and the `CASH` placeholder is never probed.
- **No session survives a provider wait** (docs/01 — the M152/M153 rule),
  pinned from INSIDE the two network seams.

Every network edge is monkeypatched — no external HTTP in CI. The row shapes
are the ones LIVE-PROBED from Yahoo on 2026-10-11 and the ones prod actually
holds (293/294/296/298).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session

import app.services.market_data as market_data
import app.services.repair_report as repair_report
from app.api.deps import get_engine
from app.models.accounts import Account, AccountType
from app.models.securities import QuoteOutcome, Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.models.watchlist import WatchlistItem
from app.services.deep_backfill import TradeEvidence
from app.services.market_data import CandidateSeries
from app.services.repair_report import CANARY_SYMBOL, Check, Severity, scan

# LIVE-PROBED 2026-10-11: `VGRO` is a real US ETF and `VGRO.TO` a real TSX
# ETF — different issuers, different currencies. That is the whole bug.
VGRO_BARE = {
    "shortName": "Virtus Silvant Growth Opportuni",
    "longName": "Virtus Silvant Growth Opportunities ETF",
    "quoteType": "ETF",
    "exchange": "PCX",
    "fullExchangeName": "NYSEArca",
    "currency": "USD",
}
VGRO_TO = {
    "shortName": "VANGUARD GROWTH ETF PORTFOLIO",
    "longName": "Vanguard Growth ETF Portfolio",
    "quoteType": "ETF",
    "exchange": "TOR",
    "fullExchangeName": "Toronto",
    "currency": "CAD",
}
VFV_TO = {
    "shortName": "VANGUARD SP 500 INDEX ETF",
    "longName": "Vanguard S&P 500 Index ETF",
    "quoteType": "ETF",
    "exchange": "TOR",
    "fullExchangeName": "Toronto",
    "currency": "CAD",
}


# --- fixtures ----------------------------------------------------------------


def _user(session: Session, *, privacy_mode: bool = False) -> User:
    import time

    user = User(
        email=f"m155-{time.time_ns()}@example.com",
        hashed_password="x",
        privacy_mode=privacy_mode,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _security(session: Session, user: User, **overrides) -> Security:
    payload = {
        "user_id": user.id,
        "symbol": "VGRO",
        "name": "Virtus Silvant Growth Opportuni",
        "kind": SecurityKind.etf,
        "listing_currency": "USD",
        "exchange": "PCX",
    }
    payload.update(overrides)
    security = Security(**payload)
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _account(session: Session, user: User, currency: str = "CAD") -> Account:
    account = Account(user_id=user.id, name="Questrade", type=AccountType.tfsa, currency=currency)
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _trade(
    session: Session,
    user: User,
    security: Security,
    *,
    price: str,
    currency: str,
    day: date = date(2022, 3, 3),
    quantity: str = "10",
    account: Account | None = None,
) -> Trade:
    account = account or _account(session, user)
    trade = Trade(
        user_id=user.id,
        account_id=account.id,
        security_id=security.id,
        date=day,
        action=TradeAction.buy if Decimal(quantity) else TradeAction.dividend,
        quantity=Decimal(quantity),
        price_native=Decimal(price),
        currency=currency,
    )
    session.add(trade)
    session.commit()
    return trade


def _watch(session: Session, user: User, security: Security) -> None:
    session.add(WatchlistItem(user_id=user.id, security_id=security.id))
    session.commit()


class _FakeTicker:
    """`Ticker.info` that only answers for symbols given at construction."""

    answers: ClassVar[dict[str, dict]] = {}

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    @property
    def info(self) -> dict:
        return self.answers.get(self.symbol, {})


def _patch_ticker(monkeypatch, answers: dict[str, dict]) -> None:
    _FakeTicker.answers = answers
    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", _FakeTicker)


def _patch_search(monkeypatch, rows: list[dict] | None = None) -> list[str]:
    """Record the search seam's queries so a test can prove it did NOT run."""
    calls: list[str] = []

    def fake(query: str) -> list[dict]:
        calls.append(query)
        return [] if rows is None else rows

    monkeypatch.setattr(market_data, "_search_equity_quotes", fake)
    return calls


def _patch_canary(monkeypatch, outcome: QuoteOutcome = QuoteOutcome.ok) -> list[str]:
    asked: list[str] = []

    def fake(symbol: str):
        asked.append(symbol)
        return (outcome, None if outcome is QuoteOutcome.ok else "No public quote found")

    monkeypatch.setattr(market_data, "probe_quote", fake)
    return asked


def _resolve_spy(monkeypatch, answer: dict | None = None) -> list[str]:
    """Record which symbols were resolved — the privacy/cash pins read this."""
    asked: list[str] = []

    def fake(symbol: str, **kwargs):
        asked.append(symbol)
        if answer is not None:
            return answer
        raise AssertionError("resolve_symbol should not have been called")

    monkeypatch.setattr(market_data, "resolve_symbol", fake)
    return asked


def _patch_history(monkeypatch, closes_by_symbol: dict[str, dict[date, Decimal]]) -> list[str]:
    fetched: list[str] = []

    def fake(symbol, start, end):
        fetched.append(symbol)
        closes = closes_by_symbol.get(symbol, {})
        if closes:
            return closes, QuoteOutcome.ok
        return {}, QuoteOutcome.not_found

    monkeypatch.setattr(market_data, "_fetch_one_history", fake)
    return fetched


def _shared_session(monkeypatch, session: Session) -> None:
    """Point `scan`'s session factory at the suite's own (outer-transaction)
    session, WITHOUT letting `scan` close it — fixture rows live in that
    transaction and would be invisible to a fresh connection."""

    class _NoClose:
        def __init__(self, inner: Session) -> None:
            self._inner = inner

        def __enter__(self) -> Session:
            return self._inner

        def __exit__(self, *exc: object) -> bool:
            return False  # the fixture owns this session's lifetime

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    monkeypatch.setattr(repair_report, "Session", lambda *a, **k: _NoClose(session))


def test_names_agree_tolerates_formatting_but_not_a_different_instrument() -> None:
    """The name comparison is where a report's credibility is won or lost:
    Yahoo's casing/punctuation/abbreviation drift must NOT read as a finding,
    and a genuinely different instrument MUST."""
    agree = repair_report._names_agree
    # formatting + case + punctuation
    assert agree("Vanguard Growth ETF Portfolio", "VANGUARD GROWTH ETF PORTFOLIO")
    assert agree("Vanguard S&P 500 Index ETF", "Vanguard SP 500 Index ETF")
    # truncation and abbreviation drift
    assert agree("Virtus Silvant Growth Opportuni", "Virtus Silvant Growth Opportunities ETF")
    assert agree("Vanguard Information Tech ETF", "Vanguard Information Technology ETF")
    # ...and the disagreements the report exists for
    assert not agree("VFV", "VANGUARD SP 500 INDEX ETF")
    assert not agree("Virtus Silvant Growth Opportuni", "Vanguard Growth ETF Portfolio")
    assert not agree("SOME OTHER FUND", "VANGUARD SP 500 INDEX ETF")


def _findings(report, check: Check) -> list:
    return [f for f in report.findings if f.check == check]


# --- family 1: bare collisions ----------------------------------------------


def test_a_collision_the_ledger_contradicts_is_a_repair(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mark's 294: a CAD buy against a USD listing means the row is on the
    wrong side of the collision, and the report says WHICH side to repair to."""
    user = _user(session)
    security = _security(session, user)  # bare VGRO, stored as Virtus/USD
    _trade(session, user, security, price="30.86", currency="CAD")
    _watch(session, user, security)
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    _patch_ticker(monkeypatch, {"VGRO": VGRO_BARE, "VGRO.TO": VGRO_TO})
    _patch_search(monkeypatch)

    report = scan(get_engine(), user_id=user.id)

    collision = _findings(report, Check.collision_wrong_side)
    assert len(collision) == 1
    assert collision[0].severity == Severity.repair
    assert "VGRO.TO" in collision[0].detail and "Virtus" in collision[0].detail
    assert "--symbol VGRO.TO" in (collision[0].suggestion or "")
    assert report.exit_code == 1
    # ...and nothing else was found for this row: one row, one verdict.
    assert [f for f in report.findings if f.security_id == security.id] == collision


def test_a_collision_the_ledger_already_agrees_is_information(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both listings answer, the trades are stated in the bare side's own
    currency: the row is on the side the ledger means, so no action."""
    user = _user(session)
    security = _security(session, user)
    _trade(session, user, security, price="25.50", currency="USD")
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    _patch_ticker(monkeypatch, {"VGRO": VGRO_BARE, "VGRO.TO": VGRO_TO})
    _patch_search(monkeypatch)

    report = scan(get_engine(), user_id=user.id)

    agrees = _findings(report, Check.collision_agrees)
    assert len(agrees) == 1
    assert agrees[0].severity == Severity.info
    assert agrees[0].suggestion is None
    assert report.exit_code == 0


def test_a_collision_the_ledger_cannot_decide_is_a_review(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watched-only row has no trades, so no currency can say which listing
    it means: the report must NOT pick one — it names both and asks."""
    user = _user(session)
    security = _security(session, user)
    _watch(session, user, security)
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    _patch_ticker(monkeypatch, {"VGRO": VGRO_BARE, "VGRO.TO": VGRO_TO})
    _patch_search(monkeypatch)

    report = scan(get_engine(), user_id=user.id)

    undecided = _findings(report, Check.collision_undecided)
    assert len(undecided) == 1
    assert undecided[0].severity == Severity.review
    assert "VGRO (USD" in undecided[0].detail and "VGRO.TO (CAD" in undecided[0].detail
    assert "--symbol <PICK>" in (undecided[0].suggestion or "")
    assert report.exit_code == 0  # a review is not a repair


def test_a_same_currency_collision_is_decided_by_the_recorded_prices(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both candidates quote the same currency (`APTX.TO` + `APTX.NE`, both
    CAD), so the M144 price evidence breaks the tie: the series that explains
    the recorded buys is the listing the row means."""
    user = _user(session)
    security = _security(
        session,
        user,
        symbol="APOTEX",
        name="Apotex Inc.",
        listing_currency="USD",
        exchange="",
    )
    _trade(session, user, security, price="30.80", currency="CAD", day=date(2026, 10, 6))
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    # Both quote probes miss, so resolution falls through to ONE search call
    # that returns two CAD listings — the ambiguous shape.
    _patch_ticker(monkeypatch, {})
    _patch_search(
        monkeypatch,
        [
            {
                "symbol": "APTX.TO",
                "shortname": "APOTEX HEALTH CORP",
                "exchange": "TOR",
                "exchDisp": "Toronto",
                "quoteType": "EQUITY",
            },
            {
                "symbol": "APTX.NE",
                "shortname": "APOTEX HEALTH CORP",
                "exchange": "NEO",
                "exchDisp": "NEO",
                "quoteType": "EQUITY",
            },
        ],
    )
    fetched = _patch_history(
        monkeypatch,
        {
            "APTX.TO": {date(2026, 10, 6): Decimal("30.79")},
            "APTX.NE": {date(2026, 10, 6): Decimal("15.40")},
        },
    )

    report = scan(get_engine(), user_id=user.id)

    # The stored spelling is a NAME, not a ticker, and the ledger picked the
    # side whose closes agree with the recorded buy.
    named = _findings(report, Check.name_stored_as_symbol)
    assert len(named) == 1
    assert named[0].severity == Severity.repair
    assert "recorded trade prices says APTX.TO" in named[0].detail
    assert "--symbol APTX.TO" in (named[0].suggestion or "")
    assert fetched  # the tie-break really fetched


def test_two_rows_for_one_listing_are_reported_as_a_twin(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 293/298 leftover: a repaired `.TO` row and the held bare row are
    two rows for one listing, and `security_dedup` will never merge them."""
    user = _user(session)
    held = _security(
        session, user, symbol="APTX", name="APTX", listing_currency="CAD", exchange="TSX"
    )
    watched = _security(
        session,
        user,
        symbol="APTX.TO",
        name="APOTEX HEALTH CORP",
        listing_currency="CAD",
        exchange="TSX",
    )
    _trade(session, user, held, price="30.80", currency="CAD")
    _watch(session, user, watched)
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    _patch_ticker(monkeypatch, {"APTX.TO": VGRO_TO | {"shortName": "APOTEX HEALTH CORP"}})
    _patch_search(monkeypatch)

    report = scan(get_engine(), user_id=user.id)

    twins = _findings(report, Check.bare_twin)
    assert len(twins) == 1
    assert twins[0].severity == Severity.review
    assert f"{held.id} (APTX)" in twins[0].detail
    assert f"{watched.id} (APTX.TO)" in twins[0].detail
    # ...and the detail says why the automatic merge cannot do it.
    assert "normalized keys" in (twins[0].suggestion or "")


# --- family 2: live-symbol heuristics ----------------------------------------


def test_a_name_stored_as_a_symbol_is_a_repair(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mark's 293: `APOTEX` is a company name, the listing is `APTX.TO`, and
    one search hit means no human pick is needed."""
    user = _user(session)
    security = _security(
        session,
        user,
        symbol="APOTEX",
        name="Apotex Inc.",
        listing_currency="USD",
        exchange="",
    )
    _watch(session, user, security)
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    _patch_ticker(monkeypatch, {})
    _patch_search(
        monkeypatch,
        [
            {
                "symbol": "APTX.TO",
                "shortname": "APOTEX HEALTH CORP",
                "exchange": "TOR",
                "exchDisp": "Toronto",
                "quoteType": "EQUITY",
            }
        ],
    )

    report = scan(get_engine(), user_id=user.id)

    findings = _findings(report, Check.name_stored_as_symbol)
    assert len(findings) == 1
    assert findings[0].severity == Severity.repair
    assert "APTX.TO" in findings[0].detail
    assert f"--security-id {security.id} --apply" in (findings[0].suggestion or "")
    assert "--symbol" not in (findings[0].suggestion or "")  # one hit: no pick needed


def test_a_currency_every_trade_agrees_with_is_a_repair(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mark's 296: the row says USD, the listing quotes CAD and every
    recorded trade is CAD — the FX seams are reading the wrong currency."""
    user = _user(session)
    security = _security(
        session, user, symbol="VFV", name="VFV", listing_currency="USD", exchange="NMS"
    )
    _trade(session, user, security, price="40.10", currency="CAD")
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    # LIVE-PROBED: bare `VFV` answers a venue stub with no name, so the `.TO`
    # side wins outright — one listing, `match: suffix`.
    _patch_ticker(
        monkeypatch, {"VFV": {"quoteType": "ECNQUOTE", "exchange": "NMS"}, "VFV.TO": VFV_TO}
    )
    _patch_search(monkeypatch)

    report = scan(get_engine(), user_id=user.id)

    mismatch = _findings(report, Check.currency_mismatch)
    assert len(mismatch) == 1
    assert mismatch[0].severity == Severity.repair
    assert "every recorded trade agrees" in mismatch[0].detail
    assert mismatch[0].suggestion and "--apply" in mismatch[0].suggestion
    # The typed-symbol name is recorded too, at INFO: cosmetic, not a repair.
    assert _findings(report, Check.name_is_symbol)[0].severity == Severity.info


def test_a_currency_mismatch_the_ledger_does_not_corroborate_is_only_a_review(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live venue heuristic is not gospel: when the trades say something
    else, the report surfaces the disagreement instead of prescribing it."""
    user = _user(session)
    security = _security(
        session,
        user,
        symbol="VFV",
        name="Vanguard S&P 500 Index ETF",
        listing_currency="USD",
        exchange="TSX",  # only the CURRENCY disagrees — nothing else may fire
    )
    _trade(session, user, security, price="40.10", currency="USD")
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    _patch_ticker(
        monkeypatch, {"VFV": {"quoteType": "ECNQUOTE", "exchange": "NMS"}, "VFV.TO": VFV_TO}
    )
    _patch_search(monkeypatch)

    report = scan(get_engine(), user_id=user.id)

    mismatch = _findings(report, Check.currency_mismatch)
    assert len(mismatch) == 1
    assert mismatch[0].severity == Severity.review
    assert mismatch[0].suggestion is None
    assert report.exit_code == 0


def test_a_wrong_exchange_on_the_right_symbol_is_a_repair(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prod 294 AFTER the hand repair: symbol and currency corrected, the
    venue left pointing at NYSEArca. Same symbol, so a repair drops no closes."""
    user = _user(session)
    security = _security(
        session,
        user,
        symbol="VGRO.TO",
        name="VANGUARD GROWTH ETF PORTFOLIO",
        listing_currency="CAD",
        exchange="PCX",
    )
    _trade(session, user, security, price="30.86", currency="CAD")
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    _patch_ticker(monkeypatch, {"VGRO.TO": VGRO_TO})
    _patch_search(monkeypatch)

    report = scan(get_engine(), user_id=user.id)

    mismatch = _findings(report, Check.exchange_mismatch)
    assert len(mismatch) == 1
    assert mismatch[0].severity == Severity.repair
    assert "PCX" in mismatch[0].detail and "TSX" in mismatch[0].detail
    assert "--symbol" not in (mismatch[0].suggestion or "")
    # The identity otherwise matches: no currency/name noise beside it.
    assert not _findings(report, Check.currency_mismatch)


def test_a_row_the_market_disowned_is_a_review_and_a_dead_row_is_information(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`none` means two different things depending on whether M141 already
    booked the verdict — and only the un-booked one is worth a human's time."""
    user = _user(session)
    fresh = _security(
        session, user, symbol="NODATA42M126", name="No data co", listing_currency="CAD"
    )
    judged = _security(
        session,
        user,
        symbol="GONELISTED",
        name="Gone",
        listing_currency="CAD",
        dead_symbol=True,
        quote_miss_count=3,
        dead_symbol_reason="Quote not found for symbol: GONELISTED",
    )
    _watch(session, user, fresh)
    _watch(session, user, judged)
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    _patch_ticker(monkeypatch, {})
    _patch_search(monkeypatch)

    report = scan(get_engine(), user_id=user.id)

    unlisted = _findings(report, Check.no_listing)
    assert len(unlisted) == 1 and unlisted[0].severity == Severity.review
    assert unlisted[0].security_id == fresh.id
    confirmed = _findings(report, Check.dead_symbol_confirmed)
    assert len(confirmed) == 1 and confirmed[0].severity == Severity.info
    assert confirmed[0].security_id == judged.id
    assert confirmed[0].suggestion is None
    assert report.exit_code == 0


def test_a_dead_flag_the_market_contradicted_is_a_review(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prod 298: `APTX` is flagged dead (three misses) while its `.TO`
    sibling answers — the flag is about the old spelling, and a refresh
    (`probe_dead`) is what disproves it."""
    user = _user(session)
    security = _security(
        session,
        user,
        symbol="APTX",
        name="APTX",
        listing_currency="CAD",
        exchange="TSX",
        dead_symbol=True,
        quote_miss_count=3,
        dead_symbol_reason="Quote not found for symbol: APTX",
    )
    _trade(session, user, security, price="30.80", currency="CAD")
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    _patch_ticker(monkeypatch, {"APTX.TO": VGRO_TO | {"shortName": "APOTEX HEALTH CORP"}})
    _patch_search(monkeypatch)

    report = scan(get_engine(), user_id=user.id)

    stale = _findings(report, Check.dead_symbol_stale)
    assert len(stale) == 1
    assert stale[0].severity == Severity.review
    assert f"securities/{security.id}/refresh" in (stale[0].suggestion or "")
    assert report.exit_code == 0


def test_a_name_that_belongs_to_another_instrument_is_a_review(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 294 shape without the currency tell: the row's NAME is the wrong
    instrument's. A rename may have been deliberate, so this is a review."""
    user = _user(session)
    security = _security(
        session,
        user,
        symbol="VFV.TO",
        name="VANGUARD SP 500 INDEX ETF",
        listing_currency="CAD",
        exchange="TSX",
    )
    _trade(session, user, security, price="40.10", currency="CAD")
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    _patch_ticker(monkeypatch, {"VFV.TO": VFV_TO | {"shortName": "SOME OTHER FUND"}})
    _patch_search(monkeypatch)

    report = scan(get_engine(), user_id=user.id)

    mismatch = _findings(report, Check.name_mismatch)
    assert len(mismatch) == 1
    assert mismatch[0].severity == Severity.review
    assert "SOME OTHER FUND" in mismatch[0].detail
    # Same listing otherwise — the tolerant comparison must not fire on a
    # case/punctuation-only difference.
    assert not _findings(report, Check.currency_mismatch)
    assert not _findings(report, Check.exchange_mismatch)


# --- noise control ------------------------------------------------------------


def test_the_cash_placeholder_is_never_probed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CASH` really resolves to Pathward Financial — reporting cash as a
    mis-pricing would be the report crying wolf (docs/07 §Backfill depth)."""
    user = _user(session)
    cash = _security(session, user, symbol="CASH", name="Cash / No Security", exchange="CASH")
    _trade(session, user, cash, price="5.00", currency="CAD", quantity="0")
    real = _security(
        session,
        user,
        symbol="VGRO.TO",
        name="VANGUARD GROWTH ETF PORTFOLIO",
        listing_currency="CAD",
        exchange="TSX",
    )
    _trade(session, user, real, price="30.86", currency="CAD")
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    asked = _resolve_spy(
        monkeypatch,
        {
            "match": "direct",
            "result": {
                "symbol": "VGRO.TO",
                "name": "VANGUARD GROWTH ETF PORTFOLIO",
                "kind": SecurityKind.etf,
                "listing_currency": "CAD",
                "exchange": "TSX",
            },
            "candidates": [],
        },
    )

    report = scan(get_engine(), user_id=user.id)

    assert asked == ["VGRO.TO"]  # CASH was skipped before any probe
    assert report.rows_skipped == {"cash_placeholder": 1}
    assert not _findings(report, Check.no_listing)


def test_a_privacy_owner_costs_zero_packets(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/11: a privacy-mode tenant's rows are refused AND reported, and
    the refusal happens before the first probe, not after it."""
    user = _user(session, privacy_mode=True)
    security = _security(session, user)
    _trade(session, user, security, price="30.86", currency="CAD")
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch)
    asked = _resolve_spy(monkeypatch)

    report = scan(get_engine(), user_id=user.id)

    assert asked == []  # not one probe
    refused = _findings(report, Check.fetch_refused)
    assert len(refused) == 1 and refused[0].severity == Severity.info
    assert "zero packets" in refused[0].detail


def test_an_unreachable_market_is_one_line_not_a_wall_of_no_listing_findings(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canary exists for exactly this: a network blip must not read as
    "every ticker you own was delisted". Nothing is judged, exit code 2."""
    user = _user(session)
    security = _security(session, user)
    _watch(session, user, security)
    _shared_session(monkeypatch, session)
    _patch_canary(monkeypatch, outcome=QuoteOutcome.not_found)
    asked = _resolve_spy(monkeypatch)

    report = scan(get_engine(), user_id=user.id)

    assert asked == []
    assert report.canary_ok is False
    assert report.probe_error and CANARY_SYMBOL in report.probe_error
    assert report.rows_scanned == 1  # the row WAS in scope, just not judged
    assert not _findings(report, Check.no_listing)
    assert report.exit_code == 2
    assert "SCAN INCOMPLETE" in report.summary()


def test_the_canary_is_asked_before_any_row(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session)
    security = _security(session, user)
    _watch(session, user, security)
    _shared_session(monkeypatch, session)
    asked = _patch_canary(monkeypatch)
    _patch_ticker(monkeypatch, {"VGRO": VGRO_BARE, "VGRO.TO": VGRO_TO})
    _patch_search(monkeypatch)

    scan(get_engine(), user_id=user.id)

    assert asked == [CANARY_SYMBOL]


# --- the session discipline (docs/01, M152/M153) -----------------------------


class _SpyFactory:
    """The real session factory, recording what `scan` opens — the pin reads
    these objects' state the moment a provider is awaited."""

    def __init__(self) -> None:
        self.opened: list[Session] = []

    def __call__(self, *args, **kwargs) -> Session:
        session = Session(*args, **kwargs)
        self.opened.append(session)
        return session


#: Scoped to `scan`'s OWN phase-1 statement (`select(Security)` renders with
#: `securities.created_at` first, exactly as M152's incident statement did) so
#: another test's leftovers in this database can never fail the pin.
_IDLE_IN_TRANSACTION = (
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() "
    "AND state = 'idle in transaction' "
    "AND pid <> pg_backend_pid() "
    "AND query LIKE 'SELECT securities.created_at%' "
    "AND query LIKE '%securities.id IN%'"
)


def _idle_backends(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(_IDLE_IN_TRANSACTION)).scalar() or 0)


def test_no_session_survives_a_provider_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinned from INSIDE both network seams, M152's pattern.

    The read runs for real (it is the statement that must be released); only
    its RETURN is replaced with fixture rows, because the suite's rows live in
    an outer transaction this engine's own connections cannot see. What is
    asserted is WHEN each session ends, not which rows come back: while
    `resolve_symbol` and `fetch_history_candidates` wait on the network, no
    session `scan` opened may be in a transaction, and no backend may be
    parked `idle in transaction` — and every session must be closed by the
    time the scan returns.
    """
    real_load = repair_report.load_owned_rows
    rows = [
        repair_report.OwnedRow(
            id=41501,
            user_id=41501,
            symbol="VGRO",
            name="Virtus Silvant Growth Opportuni",
            kind="etf",
            listing_currency="USD",
            exchange="PCX",
            dead_symbol=False,
            quote_miss_count=0,
            dead_symbol_reason=None,
            privacy_mode=False,
            trades=1,
            watched=True,
            # EUR is stated by neither candidate, so the currency reading
            # cannot decide and the price tie-break (the second provider
            # wait) is what has to.
            evidence=(TradeEvidence(date(2026, 10, 6), Decimal("30.86"), "EUR"),),
        ),
        repair_report.OwnedRow(
            id=41502,
            user_id=41501,
            symbol="VEQT",
            name="Vanguard All-Equity ETF Portfolio",
            kind="etf",
            listing_currency="CAD",
            exchange="TSX",
            dead_symbol=False,
            quote_miss_count=0,
            dead_symbol_reason=None,
            privacy_mode=False,
            trades=1,
            watched=False,
            evidence=(),
        ),
    ]

    def _with_fixture_rows(session: Session, user_id: int | None = None):
        real_load(session, user_id)
        return list(rows)

    monkeypatch.setattr(repair_report, "load_owned_rows", _with_fixture_rows)
    factory = _SpyFactory()
    monkeypatch.setattr(repair_report, "Session", factory)

    engine = get_engine()
    busy: list[bool] = []

    def _assert_free() -> None:
        busy.append(
            any(session.in_transaction() for session in factory.opened)
            or _idle_backends(engine) > 0
        )

    def _resolve(symbol: str, **kwargs):
        _assert_free()
        return {
            "match": "ambiguous",
            "result": None,
            "candidates": [
                {
                    "symbol": "VGRO",
                    "name": "Virtus Silvant Growth Opportuni",
                    "kind": SecurityKind.etf,
                    "listing_currency": "USD",
                    "exchange": "PCX",
                    "exchange_display": "NYSEArca",
                },
                {
                    "symbol": "VGRO.TO",
                    "name": "VANGUARD GROWTH ETF PORTFOLIO",
                    "kind": SecurityKind.etf,
                    "listing_currency": "CAD",
                    "exchange": "TSX",
                    "exchange_display": "Toronto",
                },
            ],
        }

    monkeypatch.setattr(market_data, "probe_quote", lambda symbol: (QuoteOutcome.ok, None))
    monkeypatch.setattr(market_data, "resolve_symbol", _resolve)

    def _history(symbol, start, end):
        _assert_free()
        if symbol == "VGRO.TO":
            # Within 1% of the recorded 30.86, so the RAW reading (deep
            # backfill's own first reading) explains the trade — no fx cache
            # needed to make the call.
            return [CandidateSeries("VGRO.TO", "CAD", {date(2026, 10, 6): Decimal("30.80")})]
        return []

    monkeypatch.setattr(market_data, "fetch_history_candidates", _history)

    report = scan(engine)

    assert busy, "no provider wait was observed — the pin never ran"
    assert busy == [False] * len(busy), (
        "repair_report held a session in a transaction while it waited on the "
        "provider — the M152/M153 class this report must not add to"
    )
    assert len(factory.opened) == 2, "phase 1 (read) and phase 3 (score) each own a session"
    assert [s.in_transaction() for s in factory.opened] == [False, False]
    assert report.rows_scanned == 2
    # The EUR evidence makes the currency reading inconclusive, so the price
    # tie-break ran and picked the TSX side.
    wrong_side = _findings(report, Check.collision_wrong_side)
    assert len(wrong_side) == 1 and "--symbol VGRO.TO" in (wrong_side[0].suggestion or "")
    assert "recorded trade prices" in wrong_side[0].detail


# --- the CLI contract the scheduled run depends on ---------------------------


def test_cli_exit_codes_and_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The cron wrapper reads the exit code and `--json`; both are pinned."""
    from scripts import repair_needed_report as cli

    clean = repair_report.ScanReport(canary=CANARY_SYMBOL)
    clean.rows_scanned = 3
    monkeypatch.setattr(cli, "scan", lambda *a, **k: clean)
    assert cli.main([]) == 0
    assert "3 owned row(s)" in capsys.readouterr().out

    needing = repair_report.ScanReport(canary=CANARY_SYMBOL)
    needing.findings = [
        repair_report.Finding(
            security_id=294,
            user_id=343,
            symbol="VGRO",
            check=Check.collision_wrong_side,
            severity=Severity.repair,
            detail="the ledger says VGRO.TO",
            suggestion="--symbol VGRO.TO",
        )
    ]
    monkeypatch.setattr(cli, "scan", lambda *a, **k: needing)
    assert cli.main(["--json", "--user-id", "343"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["repair"] == 1
    assert payload["findings"][0]["check"] == "bare_collision_wrong_side"
    assert payload["findings"][0]["suggestion"] == "--symbol VGRO.TO"

    broken = repair_report.ScanReport(
        canary=CANARY_SYMBOL, probe_error="canary 'AAPL' did not answer"
    )
    monkeypatch.setattr(cli, "scan", lambda *a, **k: broken)
    assert cli.main([]) == 2

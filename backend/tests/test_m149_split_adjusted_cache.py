"""M149 — a split-adjusted price cache against unadjusted ledger quantities (docs/07 §Price frame).

The defect, measured live in prod on 2026-10-11 while M144's deep backfill was
being run against it. Security 86 (`VGT`, USD/PCX) had its whole cached year
written straight from Yahoo's SPLIT-ADJUSTED history, while its trades are the
broker's own unadjusted export:

    2025-11-18  transfer_in  51 @ $732.59
    2025-12-19  buy       0.0434 @ $754.60
    2026-01-08  sell    -51.0434 @ $758.50
    2026-04-21  VGT splits 8:1

Yahoo answers `Close` 91.5737533569336 for 2025-11-18 (and `Adj Close`
91.1711196899414 — a different number again, because `Adj Close` is
DIVIDEND-adjusted too). 51 x 91.57 ≈ $4.7k where the position was worth
$37.4k, on days the YTD table drew as valued. M144's series-validation gate
refused to deep-backfill the row (`no_listing_agrees`: three recorded trades,
none of them anywhere near the ~91 series) — the honest refusal is what
exposed the bug.

Every close below is the PROBE's own reading of yfinance 1.6.0 against prod
(2026-10-11), not a synthetic fixture:

    2025-11-17  Close  92.9625015258789
    2025-11-18  Close  91.5737533569336     <- 51 shares traded at 732.59
    2025-11-19  Close  92.19625091552734
    2025-11-20  Close  89.4625015258789
    2025-12-19  Close  94.44249725341797    <- traded at 754.60
    2026-01-08  Close  94.35124969482422    <- traded at 758.50
    2026-04-16  Close  98.99250030517578
    2026-04-17  Close 100.69750213623047
    2026-04-20  Close 101.14125061035156    <- 1/8 of the 809.13 actually traded
    2026-04-21  Close 101.0199966430664     <- Stock Splits 8.0; real close, unchanged
    2026-04-22  Close 103.02999877929688
    2026-04-23  Close 101.56999969482422

The rule under test: `as_traded(d) = yahoo(d) x Π{ratio : split_date > d}`,
with the split's own effective date already quoted post-split (so the
comparison is STRICTLY before it). Network edges are monkeypatched
throughout; the frames carry the columns yfinance 1.6.0 really returns.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select
from test_tax_acb import _user, add_trade, make_account, make_security

import app.services.market_data as market_data
from app.core.errors import APIError
from app.models.accounts import AccountType
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import TradeAction as TA
from app.models.users import User
from app.services import split_repair
from app.services.account_value import DEFAULT_MIN_EVENT_VALUE, account_value_history
from app.services.deep_backfill import (
    RAW_SERIES_TOLERANCE,
    TradeEvidence,
    _FxLookup,
    choose_series,
    score_series,
)
from app.services.market_data import CandidateSeries

# `scripts/` is a directory of CLIs, not a package — put it on the path the
# same way its own modules do for `app`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from typing import ClassVar

import split_probe

D = Decimal

API = "/api/v1"
SEC_URL = f"{API}/securities"

SPLIT_DAY = date(2026, 4, 21)
#: 2025-11-18 — the day 51 shares were transferred in at $732.59.
TRADE_DAY = date(2025, 11, 18)
#: Yahoo's 2025-11-18 close, live-probed (x8 == 732.5900268554688).
ADJUSTED_TRADE_DAY = D("91.5737533569336")
#: The same close brought back into the ledger's frame.
AS_TRADED_TRADE_DAY = D("732.5900268554688")
#: `prices.price` is `Numeric(20, 8)` (docs/02) — what the COLUMN holds after
#: the round trip, and therefore what any DB comparison has to expect.
STORED_AS_TRADED = D("732.59002686")
STORED_ADJUSTED = D("91.57375336")

# (date, Close, Stock Splits) — the measured VGT frame, yfinance 1.6.0.
FRAME_ROWS: list[tuple[str, str, str]] = [
    ("2025-11-17", "92.9625015258789", "0.0"),
    ("2025-11-18", "91.5737533569336", "0.0"),
    ("2025-11-19", "92.19625091552734", "0.0"),
    ("2025-11-20", "89.4625015258789", "0.0"),
    ("2025-12-19", "94.44249725341797", "0.0"),
    ("2026-01-08", "94.35124969482422", "0.0"),
    ("2026-04-16", "98.99250030517578", "0.0"),
    ("2026-04-17", "100.69750213623047", "0.0"),
    ("2026-04-20", "101.14125061035156", "0.0"),
    ("2026-04-21", "101.0199966430664", "8.0"),
    ("2026-04-22", "103.02999877929688", "0.0"),
    ("2026-04-23", "101.56999969482422", "0.0"),
]


def _frame(rows: list[tuple[str, str, str]] | None = None) -> pd.DataFrame:
    rows = FRAME_ROWS if rows is None else rows
    return pd.DataFrame(
        {
            "Close": [float(close) for _, close, _ in rows],
            "Stock Splits": [float(ratio) for _, _, ratio in rows],
        },
        index=pd.to_datetime([day for day, _, _ in rows]),
    )


class _SplitTicker:
    """`Ticker.history` only — the one seam `_fetch_one_history` uses."""

    frame: pd.DataFrame = _frame()
    #: Every kwargs set `history` was called with, so a test can pin the call.
    calls: ClassVar[list[dict]] = []

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def history(self, **kwargs):
        type(self).calls.append(dict(kwargs))
        return self.frame


@pytest.fixture(autouse=True)
def _patch_market(monkeypatch) -> None:
    """No network anywhere in this module (docs/07 network discipline)."""
    import yfinance

    _SplitTicker.calls = []
    _SplitTicker.frame = _frame()
    monkeypatch.setattr(yfinance, "Ticker", _SplitTicker)
    monkeypatch.setattr(market_data, "_search_equity_quotes", lambda query: [])


def _stored(session: Session, security: Security, day: date) -> Price | None:
    return session.exec(
        select(Price).where(Price.security_id == security.id, Price.date == day)
    ).first()


def _cached(session: Session, security: Security, day: date, price: Decimal) -> None:
    session.add(
        Price(
            user_id=security.user_id,
            security_id=security.id,
            date=day,
            price=price,
            source=PriceSource.yfinance,
        )
    )
    session.commit()


def _vgt(session: Session, user: User, currency: str = "USD") -> Security:
    """The prod row's shape: held 2025-11-18 → 2026-01-08, 51 shares."""
    security = make_security(session, user, "VGT", currency)
    account = make_account(
        session, user, "Brokerage", AccountType.brokerage_non_registered, taxable=True
    )
    account.currency = currency
    session.add(account)
    session.commit()
    add_trade(session, user, account, security, TA.transfer_in, "2025-11-18", "51", "732.59")
    return security


# --- the pure rule ------------------------------------------------------------


def test_the_measured_8_to_1_case_un_adjusts_to_the_broker_price() -> None:
    """The acceptance case in one line: 91.5738 x 8 is the $732.59 that traded."""
    out = market_data.to_as_traded({TRADE_DAY: ADJUSTED_TRADE_DAY}, [(SPLIT_DAY, D("8"))])
    assert out == {TRADE_DAY: AS_TRADED_TRADE_DAY}
    # ...and it lands inside the commission/spread band the broker's own export
    # sits in, which is what makes M144's gate agree instead of refusing.
    assert abs(out[TRADE_DAY] - D("732.59")) <= D("732.59") * RAW_SERIES_TOLERANCE


def test_the_split_date_itself_is_already_post_split() -> None:
    """Dates BEFORE the split scale; the split's own date does not."""
    closes = {
        date(2026, 4, 20): D("101.14125061035156"),
        SPLIT_DAY: D("101.0199966430664"),
        date(2026, 4, 22): D("103.02999877929688"),
    }
    out = market_data.to_as_traded(closes, [(SPLIT_DAY, D("8"))])
    assert out[date(2026, 4, 20)] == D("809.13000488281248")
    assert out[SPLIT_DAY] == D("101.0199966430664")
    assert out[date(2026, 4, 22)] == D("103.02999877929688")


def test_two_splits_multiply_and_a_reverse_split_divides() -> None:
    closes = {date(1980, 1, 1): D("100")}
    splits = [(date(1990, 1, 1), D("2")), (date(2000, 1, 1), D("2"))]
    assert market_data.to_as_traded(closes, splits) == {date(1980, 1, 1): D("400")}
    # Yahoo's 0.1 for a 1:10 reverse split DIVIDES the pre-split closes, so
    # multiplying them back by 0.1 restores the price that actually traded.
    assert market_data.to_as_traded(closes, [(date(1990, 1, 1), D("0.1"))]) == {
        date(1980, 1, 1): D("10")
    }


def test_a_frame_with_no_split_events_is_returned_untouched() -> None:
    closes = {TRADE_DAY: D("732.59")}
    assert market_data.to_as_traded(closes, []) is closes


def test_a_zero_or_nan_split_cell_is_not_an_event() -> None:
    assert market_data._split_ratio(0.0) is None
    assert market_data._split_ratio(float("nan")) is None
    assert market_data._split_ratio(None) is None
    assert market_data._split_ratio("not a number") is None
    assert market_data._split_ratio(8.0) == D("8.0")


# --- the fetch seam -----------------------------------------------------------


def test_fetch_history_returns_the_ledger_frame_and_publishes_the_split() -> None:
    closes = market_data.fetch_history("VGT", TRADE_DAY, SPLIT_DAY)
    assert closes[TRADE_DAY] == AS_TRADED_TRADE_DAY
    assert closes[SPLIT_DAY] == D("101.0199966430664")
    assert market_data.last_splits() == ((SPLIT_DAY, D("8.0")),)


def test_the_fetch_is_one_max_request_not_a_window_plus_a_split_probe() -> None:
    """The un-adjusting must not cost a second call — one request, then a slice."""
    market_data.fetch_history("VGT", TRADE_DAY, date(2025, 11, 19))
    assert _SplitTicker.calls == [{"period": "max", "auto_adjust": False, "raise_errors": True}]


def test_a_window_that_ends_before_the_split_is_still_un_adjusted() -> None:
    """The hole a windowed fetch cannot close.

    A historical window that stops before the split carries no event of its
    own, so reading the window alone would cache 91.5738 as a real close —
    which is exactly what a deep backfill of an old window would have done.
    """
    closes = market_data.fetch_history("VGT", date(2025, 11, 1), date(2025, 12, 31))
    assert closes[TRADE_DAY] == AS_TRADED_TRADE_DAY
    assert market_data.last_splits() == ((SPLIT_DAY, D("8.0")),)


def test_the_window_still_bounds_the_answer() -> None:
    closes = market_data.fetch_history("VGT", TRADE_DAY, date(2025, 11, 19))
    assert set(closes) == {TRADE_DAY, date(2025, 11, 19)}


def test_a_window_yahoo_has_nothing_for_is_still_a_not_found_verdict() -> None:
    closes = market_data.fetch_history("VGT", date(2027, 1, 1), date(2027, 1, 31))
    assert closes == {}
    assert market_data._last_quote_outcome.get() is market_data.QuoteOutcome.not_found
    assert market_data.last_splits() == ()


def test_a_symbol_that_never_split_is_pristine() -> None:
    _SplitTicker.frame = _frame([row for row in FRAME_ROWS if row[2] == "0.0"])
    closes = market_data.fetch_history("VGT", TRADE_DAY, TRADE_DAY)
    assert closes == {TRADE_DAY: ADJUSTED_TRADE_DAY}
    assert market_data.last_splits() == ()


# --- composition with M144's series-validation gate ---------------------------


def _prod_evidence() -> list[TradeEvidence]:
    """Security 86's own three committed trades, as the broker exported them."""
    return [
        TradeEvidence(TRADE_DAY, D("732.59"), "USD"),
        TradeEvidence(date(2025, 12, 19), D("754.60"), "USD"),
        TradeEvidence(date(2026, 1, 8), D("758.50"), "USD"),
    ]


def test_the_m144_gate_stops_refusing_a_split_symbol(session: Session) -> None:
    """`no_listing_agrees` was the honest refusal that exposed this bug.

    The same three trades, scored against the same series, before and after
    the frame fix: the ~91 series explains none of them, the as-traded series
    explains all three, and the deep backfill can deepen the row instead of
    reporting a permanent gap.
    """
    evidence = _prod_evidence()
    fx = _FxLookup(session)

    # The series the daily sweep USED to cache: Yahoo's split-adjusted closes.
    raw = {TRADE_DAY: ADJUSTED_TRADE_DAY, date(2025, 12, 19): D("94.44249725341797")}
    raw[date(2026, 1, 8)] = D("94.35124969482422")
    comparable, agreeing = score_series(CandidateSeries("VGT", "USD", raw), evidence, fx)
    assert (comparable, agreeing) == (3, 0)
    chosen, verdict = choose_series([CandidateSeries("VGT", "USD", raw)], evidence, fx)
    assert verdict == "no_listing_agrees" and chosen is None

    closes = market_data.fetch_history("VGT", TRADE_DAY, date(2026, 1, 8))
    assert closes[TRADE_DAY] == AS_TRADED_TRADE_DAY
    comparable, agreeing = score_series(CandidateSeries("VGT", "USD", closes), evidence, fx)
    assert (comparable, agreeing) == (3, 3)
    chosen, verdict = choose_series([CandidateSeries("VGT", "USD", closes)], evidence, fx)
    assert verdict == "verified" and chosen is not None


# --- the write path -----------------------------------------------------------


def test_the_daily_sweep_caches_as_traded_rows(session: Session) -> None:
    """The writer itself: no split-adjusted row reaches the cache again."""
    user = _user(session)
    security = _vgt(session, user)
    assert security.id is not None
    market_data.backfill_range(session, security, TRADE_DAY, date(2025, 11, 19))
    assert _stored(session, security, TRADE_DAY).price == STORED_AS_TRADED  # type: ignore[union-attr]


# --- the repair ---------------------------------------------------------------


def test_the_repair_fixes_a_window_cached_before_the_split_existed(
    session: Session,
) -> None:
    """Prod security 86, verbatim: 51 shares worth $37.4k read as $4.7k."""
    user = _user(session)
    security = _vgt(session, user)
    assert security.id is not None
    _cached(session, security, TRADE_DAY, ADJUSTED_TRADE_DAY)

    result = split_repair.repair_split_window(session, security, TRADE_DAY, TRADE_DAY)
    assert result.refused is None
    assert result.splits == [(SPLIT_DAY, D("8.0"))]
    assert (result.updated, result.inserted, result.unchanged) == (1, 0, 0)
    assert _stored(session, security, TRADE_DAY).price == STORED_AS_TRADED  # type: ignore[union-attr]


def test_the_repair_is_idempotent_because_it_re_derives_not_scales(
    session: Session,
) -> None:
    """The obvious repair — multiply the rows by the ratio — is NOT idempotent.

    Run twice it would price the window at 64x. Re-deriving through the
    normalizing seam makes the second run a no-op instead.
    """
    user = _user(session)
    security = _vgt(session, user)
    assert security.id is not None
    _cached(session, security, TRADE_DAY, ADJUSTED_TRADE_DAY)

    split_repair.repair_split_window(session, security, TRADE_DAY, TRADE_DAY)
    again = split_repair.repair_split_window(session, security, TRADE_DAY, TRADE_DAY)
    assert (again.updated, again.inserted) == (0, 0)
    assert again.unchanged == 1
    assert _stored(session, security, TRADE_DAY).price == STORED_AS_TRADED  # type: ignore[union-attr]


def test_the_repair_never_touches_a_manual_row(session: Session) -> None:
    """Manual data is the user's, in the user's frame — it wins, always."""
    user = _user(session)
    security = _vgt(session, user)
    assert security.id is not None
    _cached(session, security, TRADE_DAY, D("999.99"))
    row = _stored(session, security, TRADE_DAY)
    assert row is not None
    row.source = PriceSource.manual
    session.add(row)
    session.commit()

    result = split_repair.repair_split_window(session, security, TRADE_DAY, TRADE_DAY)
    assert result.skipped_manual == 1 and result.updated == 0
    assert _stored(session, security, TRADE_DAY).price == D("999.99")  # type: ignore[union-attr]


def test_the_repair_writes_nothing_when_yahoo_answers_nothing(
    session: Session,
) -> None:
    user = _user(session)
    security = _vgt(session, user)
    assert security.id is not None
    _cached(session, security, TRADE_DAY, ADJUSTED_TRADE_DAY)

    _SplitTicker.frame = pd.DataFrame({"Close": [], "Stock Splits": []}, index=pd.to_datetime([]))
    result = split_repair.repair_split_window(session, security, TRADE_DAY, TRADE_DAY)
    assert result.refused == "no_closes" and result.updated == 0
    assert _stored(session, security, TRADE_DAY).price == STORED_ADJUSTED  # type: ignore[union-attr]


def test_the_repair_refuses_an_inverted_window(session: Session) -> None:
    user = _user(session)
    security = _vgt(session, user)
    with pytest.raises(APIError) as excinfo:
        split_repair.repair_split_window(session, security, date(2026, 1, 2), TRADE_DAY)
    assert excinfo.value.code == "invalid_date_range"


def test_a_dry_run_reports_the_counts_and_writes_nothing(session: Session) -> None:
    """`scripts/repair_split.py` without `--apply` previews the exact verdict."""
    user = _user(session)
    security = _vgt(session, user)
    assert security.id is not None
    _cached(session, security, TRADE_DAY, ADJUSTED_TRADE_DAY)

    preview = split_repair.repair_split_window(session, security, TRADE_DAY, TRADE_DAY, apply=False)
    assert preview.refused is None
    assert preview.splits == [(SPLIT_DAY, D("8.0"))]
    assert (preview.updated, preview.inserted) == (1, 0)

    session.expire_all()
    assert _stored(session, security, TRADE_DAY).price == STORED_ADJUSTED  # type: ignore[union-attr]
    assert preview.summary().startswith("REPAIRED VGT")


# --- the endpoint -------------------------------------------------------------


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m149-{datetime.now().timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _create(client: TestClient, headers: dict[str, str], symbol: str) -> dict:
    body = client.post(
        SEC_URL,
        json={
            "symbol": symbol,
            "name": f"{symbol} fund",
            "kind": "etf",
            "listing_currency": "USD",
            "exchange": "ARCA",
        },
        headers=headers,
    )
    assert body.status_code == 201, body.text
    return body.json()


def test_the_repair_endpoint_rewrites_the_window_it_is_pointed_at(
    client: TestClient, session: Session
) -> None:
    headers = _auth(client)
    created = _create(client, headers, "VGTX")
    row = session.get(Security, created["id"])
    assert row is not None
    _cached(session, row, TRADE_DAY, ADJUSTED_TRADE_DAY)

    body = client.post(
        f"{SEC_URL}/{created['id']}/split-repair",
        params={"start_date": TRADE_DAY.isoformat(), "end_date": TRADE_DAY.isoformat()},
        headers=headers,
    )
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["symbol"] == "VGTX"
    assert payload["splits"] == [{"date": SPLIT_DAY.isoformat(), "ratio": "8.0"}]
    assert payload["updated"] == 1 and payload["refused"] is None
    assert _stored(session, row, TRADE_DAY).price == STORED_AS_TRADED  # type: ignore[union-attr]


def test_the_repair_endpoint_is_owner_scoped(client: TestClient, session: Session) -> None:
    mine = _auth(client)
    created = _create(client, mine, "VGTY")
    theirs = _auth(client)
    body = client.post(
        f"{SEC_URL}/{created['id']}/split-repair",
        params={"start_date": TRADE_DAY.isoformat(), "end_date": TRADE_DAY.isoformat()},
        headers=theirs,
    )
    assert body.status_code == 404


def test_the_repair_endpoint_rejects_an_inverted_window(client: TestClient) -> None:
    headers = _auth(client)
    created = _create(client, headers, "VGTZ")
    body = client.post(
        f"{SEC_URL}/{created['id']}/split-repair",
        params={
            "start_date": date(2026, 1, 2).isoformat(),
            "end_date": TRADE_DAY.isoformat(),
        },
        headers=headers,
    )
    assert body.status_code == 422
    assert body.json()["code"] == "invalid_date_range"


# --- acceptance: the held window values at the TRUE price ---------------------


def test_a_pre_split_held_window_values_at_the_true_price_after_the_repair(
    session: Session,
) -> None:
    """51 shares on 2025-11-18 are worth $37.4k, not $4.7k (the M149 acceptance)."""
    user = _user(session)
    account = make_account(session, user, "RRSP", AccountType.rrsp, taxable=False)
    security = make_security(session, user, "VGT", "CAD")
    assert security.id is not None
    add_trade(session, user, account, security, TA.transfer_in, "2025-11-18", "51", "732.59")
    _cached(session, security, TRADE_DAY, ADJUSTED_TRADE_DAY)

    def held_value() -> Decimal:
        history = account_value_history(
            session, account, TRADE_DAY, date(2025, 11, 20), "day", DEFAULT_MIN_EVENT_VALUE
        )
        buckets = [b for b in history["buckets"] if b["valued"]]  # type: ignore[index]
        assert buckets, "the held window must produce a valued bucket"
        return buckets[0]["value_cad"]

    # The reported defect, reproduced: the position read an eighth of its worth.
    assert held_value() == (D("51") * ADJUSTED_TRADE_DAY).quantize(
        D("0.01"), rounding=ROUND_HALF_UP
    )

    split_repair.repair_split_window(session, security, TRADE_DAY, TRADE_DAY)

    expected = (D("51") * AS_TRADED_TRADE_DAY).quantize(D("0.01"), rounding=ROUND_HALF_UP)
    assert expected == D("37362.09")  # hand arithmetic: 51 x 732.5900268554688
    assert held_value() == expected


# --- the read-only re-probe ---------------------------------------------------


def test_the_probe_flags_the_split_and_goes_quiet_after_the_repair(
    session: Session,
) -> None:
    """`scripts/split_probe.py`'s verdict rule, pinned where its numbers come from."""
    # The signature itself: constant, and nowhere near 1.
    assert split_probe.verdict_for([D("8.0000"), D("8.0009"), D("8.0002")]) == ("SPLIT SIGNATURE")
    # Commission/spread scale — measured on every OTHER row of user 343.
    assert split_probe.verdict_for([D("1.0002"), D("1.0032"), D("1.0011")]) == "ok"
    # A split is also constant BELOW 1 (a reverse split the repair missed).
    assert split_probe.verdict_for([D("0.1250"), D("0.1251")]) == "SPLIT SIGNATURE"
    # One date is an anecdote, never a verdict.
    assert split_probe.verdict_for([D("8.0000")]) == "unclear"
    # An inconstant big ratio is noise, not a split.
    assert split_probe.verdict_for([D("8.0000"), D("4.0000")]) == "ok"

    user = _user(session)
    security = _vgt(session, user)
    assert security.id is not None
    _cached(session, security, TRADE_DAY, ADJUSTED_TRADE_DAY)
    # The cached close and the trade price are in the SAME currency here
    # (USD/USD), so nothing but the split can explain the ratio below.
    assert fx_rate_identity(session, security) == D("1")

    ratios, unclear = split_probe.probe_security(session, security)
    assert (len(ratios), unclear) == (1, 0)
    assert split_probe.verdict_for([ratios[0].quantize(D("0.0001"))]) == "unclear"
    # An UNDER-valued position reads the reciprocal — 1/8, not 8 — which is
    # why `verdict_for` checks both sides of 1.
    assert ratios[0] < D("0.13"), ratios[0]

    split_repair.repair_split_window(session, security, TRADE_DAY, TRADE_DAY)
    # After the repair the row is EXPLAINED — the two-reading rule drops it
    # out of the vote entirely, so the probe has nothing left to report.
    ratios, unclear = split_probe.probe_security(session, security)
    assert (len(ratios), unclear) == (0, 0)
    assert split_probe.verdict_for(ratios) == "unclear"
    stored = _stored(session, security, TRADE_DAY)
    assert stored is not None
    assert abs(stored.price / D("732.59") - 1) < D("0.01"), stored.price


def fx_rate_identity(session: Session, security: Security) -> Decimal | None:
    """The probe's fx step for a row whose listing and trade agree on currency."""
    return _FxLookup(session).rate((security.listing_currency or "USD").upper(), "USD", TRADE_DAY)

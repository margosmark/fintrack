"""M143 — a bare ticker that exists in TWO markets is a choice, not a coin flip.

Contract under test (docs/07 §Candidate resolution, §Unresolved symbols;
docs/08 `GET /securities/lookup` + `POST /securities`; docs/09 §Candidate
picker):

- the bare and `.TO` candidates are BOTH probed, and when both name a real
  listing the resolver answers `ambiguous` — the chooser, never bare-first
  (the silent bare-first pick is what stored *Virtus Silvant Growth
  Opportunities* on security 294 instead of Vanguard's `VGRO.TO`);
- a chooser row is create-ready (symbol, name, kind, currency, venue), so
  applying a pick never re-asks the resolver the question it just failed;
- a single-market symbol resolves with NO chooser — no UX tax;
- a `.info` body that names no security (the live `VFV` ECN stub) is a MISS,
  so nothing is ever synthesized out of a failure;
- the CSV import's own currency cell disambiguates the collision instead of
  losing the row's venue to the fallback heuristic;
- the repair refuses the collision (the chooser IS the refusal), applies an
  explicit pick, drops the closes fetched for the listing it left, and keeps
  the row's watchlist/trades attachments;
- a create with no currency is an UNRESOLVED row: typed name, no invented
  venue, no backfill queued.

Every network edge is monkeypatched — no external HTTP in CI. The row shapes
are the ones LIVE-PROBED from Yahoo on 2026-10-11.
"""

from datetime import date
from decimal import Decimal
from typing import ClassVar

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

import app.services.market_data as market_data
from app.models.accounts import Account, AccountType
from app.models.prices import Price, PriceSource
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.models.watchlist import WatchlistItem
from app.services.symbol_repair import repair_symbol

SEC_URL = "/api/v1/securities"

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
# LIVE-PROBED: `VFV` bare answers a venue stub with NO name at all.
VFV_STUB = {"quoteType": "ECNQUOTE", "exchange": "NMS", "fullExchangeName": "NasdaqGS"}
VFV_TO = {
    "shortName": "VANGUARD SP 500 INDEX ETF",
    "longName": "Vanguard S&P 500 Index ETF",
    "quoteType": "ETF",
    "exchange": "TOR",
    "fullExchangeName": "Toronto",
    "currency": "CAD",
}


def _auth(client: TestClient) -> dict[str, str]:
    import time

    email = f"m143-{time.time_ns()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _user(session: Session) -> User:
    import time

    user = User(email=f"m143-{id(session)}-{time.time_ns()}@example.com", hashed_password="x")
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


def _patch_search(monkeypatch, rows: list[dict] | None = None):
    """Record the search seam's queries so a test can prove it did NOT run."""
    calls: list[str] = []

    def fake(query: str) -> list[dict]:
        calls.append(query)
        return [] if rows is None else rows

    monkeypatch.setattr(market_data, "_search_equity_quotes", fake)
    return calls


def _patch_history(monkeypatch, closes_by_symbol: dict[str, dict[date, Decimal]]) -> list[str]:
    from app.models.securities import QuoteOutcome

    fetched: list[str] = []

    def fake(symbol, start, end):
        fetched.append(symbol)
        closes = closes_by_symbol.get(symbol, {})
        if closes:
            return closes, QuoteOutcome.ok
        return {}, QuoteOutcome.not_found

    monkeypatch.setattr(market_data, "_fetch_one_history", fake)
    return fetched


# --- resolution: the collision itself ----------------------------------------


def test_a_dual_listed_ticker_is_a_choice_not_a_coin_flip(monkeypatch) -> None:
    """BOTH candidates are probed; both real -> `ambiguous`, never bare-first.

    The old resolver returned on the first hit, so `VGRO` silently became
    Virtus Silvant (USD) and `VGRO.TO` was never asked — security 294.
    """
    _patch_ticker(monkeypatch, {"VGRO": VGRO_BARE, "VGRO.TO": VGRO_TO})
    calls = _patch_search(monkeypatch)

    resolution = market_data.resolve_symbol("VGRO")

    assert resolution["match"] == "ambiguous"
    assert resolution["result"] is None  # nothing to persist without a pick
    assert calls == []  # the search never runs when the quotes answered
    assert resolution["candidates"] == [
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
    ]
    # The single-result seam cannot express "pick one" (docs/07).
    assert market_data.lookup_symbol("VGRO") is None


def test_one_real_candidate_still_resolves_with_no_chooser(monkeypatch) -> None:
    """No UX tax on the unambiguous case: ONE real candidate = one answer.

    A bare ticker whose `.TO` sibling does not answer -> `direct`; a symbol
    typed WITH the suffix -> one candidate -> `direct`. Neither shows a
    chooser.

    The bare example is deliberately a symbol the mock leaves sibling-less,
    because on live Yahoo **bare `AAPL` is not single-market**: `AAPL.TO` is
    a real TSX listing (`APPLE CDR (CAD HEDGED)`, CAD, probed 2026-10-11), so
    it asks exactly as `VGRO` does. The no-chooser path a user actually hits
    is the suffix-typed symbol — `SHOP.TO`, `RY.TO` — asserted below.
    """
    _patch_search(monkeypatch)

    _patch_ticker(monkeypatch, {"AQN": VGRO_BARE | {"shortName": "Algonquin Power & Utilities"}})
    resolution = market_data.resolve_symbol("AQN")
    assert resolution["match"] == "direct"
    assert resolution["candidates"] == []
    assert resolution["result"]["name"] == "Algonquin Power & Utilities"

    _patch_ticker(monkeypatch, {"SHOP.TO": VGRO_TO | {"shortName": "SHOPIFY INC"}})
    resolution = market_data.resolve_symbol("SHOP.TO")
    assert resolution["match"] == "direct"
    assert resolution["candidates"] == []
    assert resolution["result"]["symbol"] == "SHOP.TO"


def test_a_suffix_typed_symbol_resolves_the_suffix_side(monkeypatch) -> None:
    """`VGRO.TO` names the TSX listing outright — one candidate, no chooser."""
    _patch_ticker(monkeypatch, {"VGRO": VGRO_BARE, "VGRO.TO": VGRO_TO})
    _patch_search(monkeypatch)

    resolution = market_data.resolve_symbol("VGRO.TO")
    assert resolution["match"] == "direct"
    assert resolution["result"]["symbol"] == "VGRO.TO"
    assert resolution["result"]["listing_currency"] == "CAD"
    assert resolution["result"]["exchange"] == "TSX"


def test_disambiguate_false_keeps_the_old_bare_first_order(monkeypatch) -> None:
    """A caller with NO picker (a background fetch) must not be handed one."""
    _patch_ticker(monkeypatch, {"VGRO": VGRO_BARE, "VGRO.TO": VGRO_TO})
    _patch_search(monkeypatch)

    resolution = market_data.resolve_symbol("VGRO", disambiguate=False)
    assert resolution["match"] == "direct"
    assert resolution["result"]["name"] == "Virtus Silvant Growth Opportuni"
    assert resolution["result"]["listing_currency"] == "USD"


def test_the_csv_currency_cell_disambiguates_the_collision(monkeypatch) -> None:
    """The import's own currency cell is the user's answer, not a guess."""
    _patch_ticker(monkeypatch, {"VGRO": VGRO_BARE, "VGRO.TO": VGRO_TO})
    _patch_search(monkeypatch)

    cad = market_data.lookup_symbol("VGRO", prefer_currency="CAD")
    assert cad is not None
    assert cad["listing_currency"] == "CAD" and cad["exchange"] == "TSX"

    usd = market_data.lookup_symbol("VGRO", prefer_currency="USD")
    assert usd is not None
    assert usd["listing_currency"] == "USD" and usd["exchange"] == "PCX"

    # A hint that fits neither side disambiguates nothing -> the chooser.
    assert market_data.lookup_symbol("VGRO", prefer_currency="EUR") is None
    assert market_data.lookup_symbol("VGRO") is None


# --- the failure path must never synthesize identity -------------------------


def test_a_venue_stub_that_names_no_security_is_a_miss(monkeypatch) -> None:
    """LIVE-PROBED: bare `VFV` answers `{"quoteType": "ECNQUOTE", ...}` — a
    stub with no short/long name. It used to pass as a real quote, so the
    payload fell back to the typed string for the name and the currency
    heuristic answered USD: "VFV, USD" out of a body that never mentioned
    a security (security 296). Now it is a miss and the REAL listing wins.
    """
    _patch_ticker(monkeypatch, {"VFV": VFV_STUB, "VFV.TO": VFV_TO})
    calls = _patch_search(monkeypatch)

    assert market_data._is_real_quote(VFV_STUB) is False
    assert market_data._is_real_quote(VGRO_TO) is True

    resolution = market_data.resolve_symbol("VFV")
    assert calls == []  # a miss on one candidate is not a search trigger
    assert resolution["match"] == "suffix"
    assert resolution["result"]["symbol"] == "VFV"  # typed spelling kept (docs/07)
    assert resolution["result"]["name"] == "VANGUARD SP 500 INDEX ETF"
    assert resolution["result"]["listing_currency"] == "CAD"
    assert resolution["result"]["exchange"] == "TSX"


# --- the API surface ---------------------------------------------------------


def test_lookup_endpoint_surfaces_the_collision_chooser(client: TestClient, monkeypatch) -> None:
    _patch_ticker(monkeypatch, {"VGRO": VGRO_BARE, "VGRO.TO": VGRO_TO})
    _patch_search(monkeypatch)

    response = client.get(f"{SEC_URL}/lookup", params={"symbol": "VGRO"}, headers=_auth(client))
    body = response.json()

    assert response.status_code == 200
    assert body["match"] == "ambiguous"
    assert body["symbol"] is None and body["listing_currency"] is None
    assert [(row["symbol"], row["listing_currency"]) for row in body["candidates"]] == [
        ("VGRO", "USD"),
        ("VGRO.TO", "CAD"),
    ]
    # Every candidate is create-ready, so applying a pick needs no 2nd lookup.
    assert all(row["kind"] == "etf" for row in body["candidates"])
    assert {row["exchange"] for row in body["candidates"]} == {"PCX", "TSX"}


def test_creating_without_a_currency_creates_an_unresolved_row(
    client: TestClient, monkeypatch
) -> None:
    """No quote + no search hit -> an honest UNRESOLVED row, never a fake one.

    The form used to pre-fill "CAD", so an unidentified symbol was
    indistinguishable from a resolved one. An empty `listing_currency` is
    now the marker: the typed name survives, no venue/currency is invented,
    M141's dead-symbol guard is set up front and no backfill is queued.
    """
    headers = _auth(client)
    created = client.post(
        SEC_URL,
        json={
            "symbol": "ZZUNLISTED",
            "name": "ZZUNLISTED",
            "kind": "stock",
            "listing_currency": "",
            "exchange": "",
        },
        headers=headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "ZZUNLISTED"  # the typed string, not a market name
    assert body["listing_currency"] == ""  # no invented currency
    assert body["dead_symbol"] is True
    assert "No market data" in body["dead_symbol_reason"]

    # ...and the resolved path is untouched: no dead flag, backfill queued.
    _patch_ticker(monkeypatch, {"AAPL": VGRO_BARE | {"shortName": "Apple Inc."}})
    _patch_search(monkeypatch)
    resolved = client.post(
        SEC_URL,
        json={
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "kind": "stock",
            "listing_currency": "USD",
            "exchange": "NMS",
        },
        headers=headers,
    )
    assert resolved.status_code == 201
    assert resolved.json()["dead_symbol"] is False
    assert resolved.json()["listing_currency"] == "USD"


# --- the row repair ----------------------------------------------------------


def test_repair_refuses_a_collision_until_the_operator_picks(session: Session, monkeypatch) -> None:
    """The repair path surfaces the SAME chooser the add form does: with no
    explicit `pick` a collision refuses instead of keeping whichever side
    answered first."""
    user = _user(session)
    security = _security(session, user)
    _patch_ticker(monkeypatch, {"VGRO": VGRO_BARE, "VGRO.TO": VGRO_TO})
    _patch_search(monkeypatch)

    refused = repair_symbol(session, security, backfill=False)
    session.rollback()

    assert refused.changed is False
    assert "pick one" in refused.reason
    assert security.symbol == "VGRO"  # untouched
    assert security.listing_currency == "USD"


def test_repair_applies_the_pick_and_drops_the_wrong_instrument_closes(
    session: Session, monkeypatch
) -> None:
    """Mark's 294: same row id, watchlist + trades intact, symbol corrected,
    and the three *Virtus* closes (25.00-25.99) are NOT left behind next to
    Vanguard prices."""
    user = _user(session)
    security = _security(session, user)
    account = Account(user_id=user.id, name="Questrade", type=AccountType.tfsa, currency="CAD")
    session.add(account)
    session.flush()
    session.add(WatchlistItem(user_id=user.id, security_id=security.id))
    session.add(
        Trade(
            user_id=user.id,
            account_id=account.id,
            security_id=security.id,
            date=date(2026, 10, 1),
            action=TradeAction.buy,
            quantity=Decimal("10"),
            price_native=Decimal("25.50"),
            currency="CAD",
        )
    )
    # The wrong instrument's bars (pre-repair) plus one hand-entered close.
    for day, value in (
        (date(2026, 10, 8), Decimal("25.10")),
        (date(2026, 10, 9), Decimal("25.99")),
    ):
        session.add(
            Price(
                user_id=user.id,
                security_id=security.id,
                date=day,
                price=value,
                source=PriceSource.yfinance,
            )
        )
    session.add(
        Price(
            user_id=user.id,
            security_id=security.id,
            date=date(2026, 10, 7),
            price=Decimal("25.00"),
            source=PriceSource.manual,
        )
    )
    session.commit()
    security_id = security.id

    _patch_ticker(monkeypatch, {"VGRO": VGRO_BARE, "VGRO.TO": VGRO_TO})
    _patch_search(monkeypatch)
    # 1,159 true closes in the wide window; a short one for the daily bars.
    true_closes = {
        date(2026, 10, 8): Decimal("48.00"),
        date(2026, 10, 9): Decimal("48.10"),
        date(2022, 3, 1): Decimal("26.78"),
    }
    _patch_history(monkeypatch, {"VGRO.TO": true_closes})

    repair = repair_symbol(session, security, pick="VGRO.TO", backfill=True)
    session.commit()

    assert repair.changed is True
    assert repair.before["symbol"] == "VGRO" and repair.after["symbol"] == "VGRO.TO"
    assert repair.prices_deleted == 2  # the yfinance bars, not the manual one
    assert repair.prices_backfilled == 3

    rows = session.exec(select(Price).where(Price.security_id == security_id)).all()
    assert {(r.date, r.price, r.source) for r in rows} == {
        (date(2026, 10, 7), Decimal("25.00"), PriceSource.manual),
        (date(2026, 10, 8), Decimal("48.00"), PriceSource.yfinance),
        (date(2026, 10, 9), Decimal("48.10"), PriceSource.yfinance),
        (date(2022, 3, 1), Decimal("26.78"), PriceSource.yfinance),
    }
    # The attachments survive in place — the row was corrected, not replaced.
    assert session.get(Security, security_id) is not None
    assert session.exec(select(WatchlistItem).where(WatchlistItem.security_id == security_id)).one()
    assert session.exec(select(Trade).where(Trade.security_id == security_id)).one()
    assert security.symbol == "VGRO.TO"
    assert security.listing_currency == "CAD"
    assert security.exchange == "TSX"
    assert security.name == "VANGUARD GROWTH ETF PORTFOLIO"


def test_a_name_only_repair_keeps_its_price_history(session: Session, monkeypatch) -> None:
    """Same symbol + same venue = the closes already belong to the listing
    the row points at; dropping them would trade a cosmetic fix for lost
    history (the class of row the sweep finds: `XEQT` stored under its own
    typed name, priced correctly all along)."""
    user = _user(session)
    security = _security(
        session,
        user,
        symbol="XEQT",
        name="XEQT",
        kind=SecurityKind.etf,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(
        Price(
            user_id=user.id,
            security_id=security.id,
            date=date(2026, 10, 9),
            price=Decimal("46.16"),
            source=PriceSource.yfinance,
        )
    )
    session.commit()

    _patch_ticker(
        monkeypatch,
        {"XEQT": {}, "XEQT.TO": VGRO_TO | {"shortName": "ISHARES CORE EQUITY ETF"}},
    )
    _patch_search(monkeypatch)
    _patch_history(monkeypatch, {})

    repair = repair_symbol(session, security, backfill=True)
    session.commit()

    assert repair.changed is True
    assert repair.prices_deleted == 0
    assert security.symbol == "XEQT"  # typed spelling kept (docs/07)
    assert security.name == "ISHARES CORE EQUITY ETF"
    kept = session.exec(select(Price).where(Price.security_id == security.id)).one()
    assert kept.price == Decimal("46.16")


def test_dropped_history_is_refetched_over_the_dropped_span(session: Session, monkeypatch) -> None:
    """A 5-year series drops and comes back as a 5-year series — the refetch
    never stops at the default 365-day window and silently truncates it."""
    from datetime import timedelta

    user = _user(session)
    security = _security(session, user)
    oldest = date.today() - timedelta(days=1200)
    session.add(
        Price(
            user_id=user.id,
            security_id=security.id,
            date=oldest,
            price=Decimal("25.00"),
            source=PriceSource.yfinance,
        )
    )
    session.commit()

    _patch_ticker(monkeypatch, {"VGRO": VGRO_BARE, "VGRO.TO": VGRO_TO})
    _patch_search(monkeypatch)
    _patch_history(monkeypatch, {})

    spans: list[tuple[date, date]] = []
    real_backfill = market_data.backfill_range

    def capture(session_, security_, start, end, *, probe_dead=False):
        spans.append((start, end))
        return 0

    monkeypatch.setattr(market_data, "backfill_range", capture)
    repair = repair_symbol(session, security, pick="VGRO.TO", backfill=True)
    session.add(security)  # keep the ORM state consistent after the rollback-free run
    assert repair.prices_deleted == 1
    assert spans and spans[0][0] <= oldest
    monkeypatch.setattr(market_data, "backfill_range", real_backfill)


# --- the import seam ---------------------------------------------------------


def test_import_binds_a_bare_cell_to_its_repaired_twin(session: Session) -> None:
    """After `VGRO` -> `VGRO.TO` the NEXT import of the same file must find
    the row: without the twin match it would insert a second security and
    split one holding across two rows (docs/02)."""
    from app.api.trades import _find_existing_security

    user = _user(session)
    twin = _security(
        session,
        user,
        symbol="VGRO.TO",
        name="VANGUARD GROWTH ETF PORTFOLIO",
        listing_currency="CAD",
        exchange="TSX",
    )

    # The file's CAD cell binds to the repaired `.TO` row.
    assert _find_existing_security(session, user.id, "VGRO", "CAD") is twin
    # A USD cell must NOT latch onto a CAD listing (that is the collision).
    assert _find_existing_security(session, user.id, "VGRO", "USD") is None
    # And an exact cell still wins outright.
    assert _find_existing_security(session, user.id, "VGRO.TO", "CAD") is twin

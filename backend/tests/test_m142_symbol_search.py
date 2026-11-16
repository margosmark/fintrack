"""M142 — the Yahoo search fallback: a typed NAME resolves to a ticker.

Contract under test (docs/07 §Symbol search fallback, docs/08 `GET
/securities/lookup`, docs/09 §New security suggestion chip):

- both quote candidates 404 -> ONE `v1/finance/search` call;
- exactly one equity hit -> the CANONICAL symbol is adopted (`APOTEX` ->
  `APTX.TO`), with the `.TO`/venue currency heuristic behind the FX seams;
- several hits -> a pick-one chooser, never a silent guess;
- no hit -> the dead-row state M141's guard consumes (404 `lookup_unavailable`);
- a TRUE ticker resolves with NO search call at all;
- the row repair corrects symbol/currency/name IN PLACE (same id, watchlist
  intact, no duplicate row) and refuses to merge on its own.

Every network edge is monkeypatched — no external HTTP in CI. The row shapes
are the ones LIVE-PROBED from Yahoo on 2026-10-10.
"""

import time
from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

import app.services.market_data as market_data
from app.models.accounts import Account, AccountType
from app.models.prices import Price, PriceSource
from app.models.securities import QuoteOutcome, Security, SecurityKind
from app.models.users import User
from app.models.watchlist import WatchlistItem
from app.services.symbol_repair import repair_symbol

SEC_URL = "/api/v1/securities"

# Live Yahoo search payload for q=Apotex (2026-10-10): two listings, so the
# real user flow is the CHOOSER; the single-hit shape is derived from row 1.
APOTEX_SEARCH_ROWS = [
    {
        "symbol": "APTX.TO",
        "shortname": "APOTEX HEALTH CORP",
        "longname": "Apotex Health Corp.",
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
]
ONE_HIT = APOTEX_SEARCH_ROWS[:1]


def _auth(client: TestClient) -> dict[str, str]:
    import time

    email = f"m142-{time.time_ns()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _user(session: Session) -> User:
    user = User(email=f"m142-{id(session)}-{time.time_ns()}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _security(session: Session, user: User, **overrides) -> Security:
    payload = {
        "user_id": user.id,
        "symbol": "APOTEX",
        "name": "APOTEX",
        "kind": SecurityKind.stock,
        "listing_currency": "USD",
        "exchange": "UNKNOWN",
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


def _patch_search(monkeypatch, rows: list[dict] | None):
    """Record the search seam's queries so tests can prove it ran (or didn't)."""
    calls: list[str] = []

    def fake(query: str) -> list[dict]:
        calls.append(query)
        return [] if rows is None else rows

    monkeypatch.setattr(market_data, "_search_equity_quotes", fake)
    return calls


# --- resolution unit level ----------------------------------------------------


def test_search_adopts_the_canonical_symbol_for_a_typed_name(monkeypatch) -> None:
    _patch_ticker(monkeypatch, {})  # APOTEX and APOTEX.TO both 404
    calls = _patch_search(monkeypatch, ONE_HIT)

    resolution = market_data.resolve_symbol("APOTEX")
    assert resolution["match"] == "search"
    assert calls == ["APOTEX"]  # exactly ONE search call
    assert resolution["result"] == {
        "symbol": "APTX.TO",  # canonical, NOT the typed name
        "name": "APOTEX HEALTH CORP",
        "kind": SecurityKind.stock,
        "listing_currency": "CAD",  # `.TO` heuristic -> the M116 FX seam
        "exchange": "TSX",  # TOR normalized to the app's convention
        "exchange_display": "Toronto",
    }
    # lookup_symbol keeps its single-result contract and gets the same answer.
    assert market_data.lookup_symbol("APOTEX") == resolution["result"]


def test_search_multiple_hits_surface_a_chooser_never_a_guess(monkeypatch) -> None:
    _patch_ticker(monkeypatch, {})
    _patch_search(monkeypatch, APOTEX_SEARCH_ROWS)

    resolution = market_data.resolve_symbol("APOTEX")
    assert resolution["match"] == "ambiguous"
    assert resolution["result"] is None  # nothing to persist without a pick
    # M143: a candidate is create-ready — kind + listing currency ride along,
    # so applying a pick never re-asks the resolver (docs/07).
    assert resolution["candidates"] == [
        {
            "symbol": "APTX.TO",
            "name": "APOTEX HEALTH CORP",
            "kind": SecurityKind.stock,
            "listing_currency": "CAD",
            "exchange": "TSX",
            "exchange_display": "Toronto",
        },
        {
            "symbol": "APTX.NE",
            "name": "APOTEX HEALTH CORP",
            "kind": SecurityKind.stock,
            "listing_currency": "CAD",
            "exchange": "NEO",
            "exchange_display": "NEO",
        },
    ]
    # The single-result seam cannot express "pick one" — it degrades to None,
    # which is what the trade-form autofill and the sub-fund creator consume.
    assert market_data.lookup_symbol("APOTEX") is None


def test_search_zero_hits_are_the_dead_row_state(monkeypatch) -> None:
    _patch_ticker(monkeypatch, {})
    calls = _patch_search(monkeypatch, None)

    resolution = market_data.resolve_symbol("PRIVCO-NOT-LISTED")
    assert resolution == {"match": "none", "result": None, "candidates": []}
    assert calls == ["PRIVCO-NOT-LISTED"]
    assert market_data.lookup_symbol("PRIVCO-NOT-LISTED") is None


def test_a_junk_info_body_is_a_miss_not_a_quote(monkeypatch) -> None:
    """yfinance answers an unlisted symbol with a NON-EMPTY junk `.info`
    (LIVE-PROBED 2026-10-10, M141): `{"trailingPegRatio": None}`. So "the
    payload is non-empty" is not evidence of a quote — the resolver keeps
    going, and a string nothing real resolves to ends in the dead-row state
    instead of a fabricated row named after what was typed (which is exactly
    the row Mark ended up with)."""
    junk = {"trailingPegRatio": None}
    _patch_ticker(
        monkeypatch,
        {"APOTEX": junk, "APOTEX.TO": junk, "PRIVCO": junk, "PRIVCO.TO": junk},
    )
    calls = _patch_search(monkeypatch, ONE_HIT)

    resolution = market_data.resolve_symbol("APOTEX")
    assert resolution["match"] == "search"  # junk did not count as a hit
    assert resolution["result"] is not None
    assert resolution["result"]["symbol"] == "APTX.TO"
    assert calls == ["APOTEX"]

    _patch_search(monkeypatch, None)  # and nothing real answers PRIVCO
    assert market_data.resolve_symbol("PRIVCO")["match"] == "none"


def test_a_true_ticker_resolves_with_no_search_call(monkeypatch) -> None:
    """`APTX` is a ticker, not a name: the `.TO` candidate answers and the
    search seam must not be touched (one probe set, no extra call)."""
    _patch_ticker(
        monkeypatch,
        {"APTX.TO": {"quoteType": "EQUITY", "shortName": "APOTEX HEALTH CORP", "exchange": "TOR"}},
    )
    calls = _patch_search(monkeypatch, APOTEX_SEARCH_ROWS)

    resolution = market_data.resolve_symbol("APTX")
    assert resolution["match"] == "suffix"
    assert calls == []  # the search never ran
    assert resolution["result"] is not None
    assert resolution["result"]["symbol"] == "APTX"  # the typed spelling is kept
    assert resolution["result"]["listing_currency"] == "CAD"
    assert resolution["result"]["exchange"] == "TSX"


def test_listing_currency_heuristic_covers_the_canadian_venues() -> None:
    assert market_data._listing_currency("APTX.TO") == "CAD"
    assert market_data._listing_currency("APTX.NE", "NEO") == "CAD"  # venue, no .TO
    assert market_data._listing_currency("AAPL", "NMS") == "USD"
    assert market_data._listing_currency("RY", "TOR") == "CAD"


def test_search_quote_filter_drops_non_equity_rows(monkeypatch) -> None:
    """A name matching only an INDEX/NEWS row is NOT a trackable listing."""
    import httpx

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "quotes": [
                    {"symbol": "^APOTEX", "quoteType": "INDEX", "shortname": "Apotex idx"},
                    {"symbol": "APTX.TO", "quoteType": "EQUITY", "shortname": "APOTEX HEALTH CORP"},
                    {"not": "a quote"},
                ]
            }

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _Response())
    assert [row["symbol"] for row in market_data._search_equity_quotes("ap otox")] == ["APTX.TO"]


def test_search_degrades_to_no_hit_when_yahoo_is_unreachable(monkeypatch) -> None:
    import httpx

    def boom(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "get", boom)
    _patch_ticker(monkeypatch, {})
    assert market_data._search_equity_quotes("APOTEX") == []
    assert market_data.resolve_symbol("APOTEX")["match"] == "none"


def test_privacy_mode_blocks_the_search_seam(monkeypatch) -> None:
    """The guard runs BEFORE a packet leaves the machine (docs/11 step 6)."""
    from app.core.errors import APIError

    calls: list[str] = []
    monkeypatch.setattr(
        market_data, "_http_search_json", lambda query: calls.append(query) or {"quotes": ONE_HIT}
    )
    market_data.bind_privacy_lock(True)
    try:
        # A lookup RAISES the documented 403 rather than quietly answering
        # "not found" — privacy mode is a refusal, not a miss.
        with pytest.raises(APIError) as raised:
            market_data.resolve_symbol("APOTEX")
        assert raised.value.code == "privacy_mode_blocks_external_api"
        assert calls == []  # guarded BEFORE any packet left the machine
    finally:
        market_data.bind_privacy_lock(False)


# --- fetch_history heal -------------------------------------------------------


def test_fetch_history_heals_a_name_typed_row(monkeypatch, caplog) -> None:
    """A row stored as a NAME gets its closes from the canonical ticker."""
    fetched: list[str] = []

    def fake_history(symbol: str, start, end) -> tuple[dict[date, Decimal], QuoteOutcome]:
        fetched.append(symbol)
        if symbol == "APTX.TO":
            return {date(2026, 10, 9): Decimal("30.54"), date(2026, 10, 10): Decimal("30.79")}, (
                QuoteOutcome.ok
            )
        return {}, QuoteOutcome.not_found

    monkeypatch.setattr(market_data, "_fetch_one_history", fake_history)
    _patch_search(monkeypatch, ONE_HIT)

    with caplog.at_level("INFO", logger="app.services.market_data"):
        closes = market_data.fetch_history("APOTEX", date(2026, 10, 1), date(2026, 10, 10))

    assert closes == {date(2026, 10, 9): Decimal("30.54"), date(2026, 10, 10): Decimal("30.79")}
    assert fetched == ["APOTEX", "APOTEX.TO", "APTX.TO"]  # candidates first, then the heal
    resolutions = [r for r in caplog.records if "healed the price fetch" in r.message]
    assert len(resolutions) == 1  # logged once, not once per candidate
    assert "APTX.TO" in resolutions[0].getMessage()


def test_fetch_history_never_guesses_between_two_listings(monkeypatch) -> None:
    fetched: list[str] = []

    def fake_history(symbol: str, start, end) -> tuple[dict[date, Decimal], QuoteOutcome]:
        fetched.append(symbol)
        return {}, QuoteOutcome.not_found

    monkeypatch.setattr(market_data, "_fetch_one_history", fake_history)
    _patch_search(monkeypatch, APOTEX_SEARCH_ROWS)

    assert market_data.fetch_history("APOTEX", date(2026, 10, 1), date(2026, 10, 10)) == {}
    assert fetched == ["APOTEX", "APOTEX.TO"]  # no third (guessed) attempt


# --- the API surface ----------------------------------------------------------


def test_lookup_endpoint_adopts_a_resolved_name(client: TestClient, monkeypatch) -> None:
    _patch_ticker(monkeypatch, {})
    _patch_search(monkeypatch, ONE_HIT)

    response = client.get(f"{SEC_URL}/lookup", params={"symbol": "APOTEX"}, headers=_auth(client))
    assert response.status_code == 200
    assert response.json() == {
        "symbol": "APTX.TO",
        "name": "APOTEX HEALTH CORP",
        "kind": "stock",
        "listing_currency": "CAD",
        "exchange": "TSX",
        "exchange_display": "Toronto",
        "match": "search",
        "candidates": [],
    }


def test_lookup_endpoint_returns_the_picker_payload(client: TestClient, monkeypatch) -> None:
    _patch_ticker(monkeypatch, {})
    _patch_search(monkeypatch, APOTEX_SEARCH_ROWS)

    response = client.get(f"{SEC_URL}/lookup", params={"symbol": "APOTEX"}, headers=_auth(client))
    body = response.json()
    assert response.status_code == 200
    assert body["match"] == "ambiguous"
    assert body["symbol"] is None and body["kind"] is None  # nothing resolved
    assert [row["symbol"] for row in body["candidates"]] == ["APTX.TO", "APTX.NE"]


def test_lookup_endpoint_still_404s_when_nothing_matches(client: TestClient, monkeypatch) -> None:
    _patch_ticker(monkeypatch, {})
    _patch_search(monkeypatch, None)

    response = client.get(f"{SEC_URL}/lookup", params={"symbol": "PRIVCO"}, headers=_auth(client))
    assert response.status_code == 404
    assert response.json()["code"] == "lookup_unavailable"


def test_resolved_payload_creates_a_cad_security_for_the_fx_seams(
    client: TestClient, monkeypatch
) -> None:
    """The M116 trading-currency seam: the adopted `.TO` symbol stores CAD,
    which is what every FX conversion downstream keys off (docs/06)."""
    _patch_ticker(monkeypatch, {})
    _patch_search(monkeypatch, ONE_HIT)
    headers = _auth(client)

    lookup = client.get(f"{SEC_URL}/lookup", params={"symbol": "APOTEX"}, headers=headers).json()
    created = client.post(SEC_URL, json=lookup, headers=headers)
    assert created.status_code == 201
    assert created.json()["symbol"] == "APTX.TO"
    assert created.json()["listing_currency"] == "CAD"
    assert created.json()["exchange"] == "TSX"


# --- the row repair -----------------------------------------------------------


def test_repair_corrects_the_row_in_place(session: Session, monkeypatch, caplog) -> None:
    """Mark's APOTEX row: same id, watchlist intact, no duplicate, CAD."""
    user = _user(session)
    security = _security(session, user)
    watch = WatchlistItem(user_id=user.id, security_id=security.id)
    session.add(watch)
    manual_close = Price(
        user_id=user.id,
        security_id=security.id,
        date=date(2026, 10, 9),
        price=Decimal("30.54"),
        source=PriceSource.manual,
    )
    session.add(manual_close)
    session.commit()

    # Resolution is the ONE network seam the repair opens.
    monkeypatch.setattr(
        market_data,
        "resolve_symbol",
        lambda symbol, **kwargs: {
            "match": "search",
            "result": {
                "symbol": "APTX.TO",
                "name": "APOTEX HEALTH CORP",
                "kind": SecurityKind.stock,
                "listing_currency": "CAD",
                "exchange": "TSX",
                "exchange_display": "Toronto",
            },
            "candidates": [],
        },
    )

    with caplog.at_level("INFO"):
        repair = repair_symbol(session, security, backfill=False)
    session.commit()

    assert repair.changed is True
    assert repair.before["symbol"] == "APOTEX" and repair.after["symbol"] == "APTX.TO"
    # Same row, corrected — not deleted and re-created.
    assert session.get(Security, security.id) is security
    assert security.symbol == "APTX.TO"
    assert security.listing_currency == "CAD"
    assert security.name == "APOTEX HEALTH CORP"
    assert security.kind == SecurityKind.stock
    rows = session.exec(select(Security).where(Security.user_id == user.id)).all()
    assert len(rows) == 1  # no duplicate securities row
    assert session.exec(select(WatchlistItem).where(WatchlistItem.security_id == security.id)).one()
    # A hand-entered close survives untouched (manual wins, docs/07).
    kept = session.exec(select(Price).where(Price.security_id == security.id)).one()
    assert kept.source == PriceSource.manual and kept.price == Decimal("30.54")


def test_repair_refuses_when_the_target_key_is_taken(session: Session, monkeypatch) -> None:
    user = _user(session)
    security = _security(session, user, symbol="APOTEX", exchange="UNKNOWN")
    _security(session, user, symbol="APTX.TO", exchange="TSX", name="already there")
    monkeypatch.setattr(
        market_data,
        "resolve_symbol",
        lambda symbol, **kwargs: {
            "match": "search",
            "result": {
                "symbol": "APTX.TO",
                "name": "APOTEX HEALTH CORP",
                "kind": SecurityKind.stock,
                "listing_currency": "CAD",
                "exchange": "TSX",
                "exchange_display": "Toronto",
            },
            "candidates": [],
        },
    )

    repair = repair_symbol(session, security, backfill=False)
    assert repair.changed is False
    assert "merge the duplicates first" in repair.reason
    assert security.symbol == "APOTEX"  # untouched


def test_repair_refuses_to_pick_between_listings(session: Session, monkeypatch) -> None:
    user = _user(session)
    security = _security(session, user)
    monkeypatch.setattr(
        market_data,
        "resolve_symbol",
        lambda symbol, **kwargs: {
            "match": "ambiguous",
            "result": None,
            "candidates": [{"symbol": "APTX.TO"}, {"symbol": "APTX.NE"}],
        },
    )

    repair = repair_symbol(session, security, backfill=False)
    assert repair.changed is False
    assert "pick one" in repair.reason
    assert security.symbol == "APOTEX"


def test_the_operator_pick_resolves_the_canonical_listing(session: Session, monkeypatch) -> None:
    """`APOTEX` matches two listings, so the repair waits for the human's
    pick — and then applies exactly what that canonical symbol resolves to."""
    user = _user(session)
    security = _security(session, user)
    resolved = {
        "symbol": "APTX.TO",
        "name": "APOTEX HEALTH CORP",
        "kind": SecurityKind.stock,
        "listing_currency": "CAD",
        "exchange": "TSX",
        "exchange_display": "Toronto",
    }

    def fake_resolve(symbol: str, **kwargs) -> dict:
        if symbol == "APOTEX":  # the stored NAME — two listings, refuse
            return {
                "match": "ambiguous",
                "result": None,
                "candidates": [{"symbol": "APTX.TO"}, {"symbol": "APTX.NE"}],
            }
        assert symbol == "APTX.TO"  # the pick is resolved, not trusted
        return {"match": "direct", "result": resolved, "candidates": []}

    monkeypatch.setattr(market_data, "resolve_symbol", fake_resolve)

    refused = repair_symbol(session, security, backfill=False)
    assert refused.changed is False and "pick one" in refused.reason

    # Case-insensitive, like every other symbol input in the app.
    applied = repair_symbol(session, security, pick="aptx.to", backfill=False)
    session.commit()
    assert applied.changed is True
    assert security.symbol == "APTX.TO"
    assert security.listing_currency == "CAD"
    assert security.name == "APOTEX HEALTH CORP"
    assert len(session.exec(select(Security).where(Security.user_id == user.id)).all()) == 1


def test_repair_backfills_closes_with_the_normal_refresh_machinery(
    session: Session, monkeypatch
) -> None:
    user = _user(session)
    security = _security(session, user)
    monkeypatch.setattr(
        market_data,
        "resolve_symbol",
        lambda symbol, **kwargs: {
            "match": "search",
            "result": {
                "symbol": "APTX.TO",
                "name": "APOTEX HEALTH CORP",
                "kind": SecurityKind.stock,
                "listing_currency": "CAD",
                "exchange": "TSX",
                "exchange_display": "Toronto",
            },
            "candidates": [],
        },
    )
    monkeypatch.setattr(
        market_data,
        "_fetch_one_history",
        lambda symbol, start, end: (
            (
                {date(2026, 10, 10): Decimal("30.79")},
                QuoteOutcome.ok,
            )
            if symbol == "APTX.TO"
            else ({}, QuoteOutcome.not_found)
        ),
    )

    repair = repair_symbol(session, security, backfill=True)
    assert repair.prices_backfilled == 1
    close = session.exec(select(Price).where(Price.security_id == security.id)).one()
    assert close.price == Decimal("30.79") and close.source == PriceSource.yfinance


def test_repair_is_a_no_op_when_the_row_is_already_canonical(session: Session, monkeypatch) -> None:
    user = _user(session)
    security = _security(
        session,
        user,
        symbol="APTX.TO",
        exchange="TSX",
        name="APOTEX HEALTH CORP",
        listing_currency="CAD",
    )
    monkeypatch.setattr(
        market_data,
        "resolve_symbol",
        lambda symbol, **kwargs: {
            "match": "direct",
            "result": {
                "symbol": "APTX.TO",
                "name": "APOTEX HEALTH CORP",
                "kind": SecurityKind.stock,
                "listing_currency": "CAD",
                "exchange": "TSX",
                "exchange_display": "Toronto",
            },
            "candidates": [],
        },
    )

    repair = repair_symbol(session, security, backfill=False)
    assert repair.changed is False
    assert repair.prices_backfilled == 0


def test_import_autofill_adopts_the_resolved_metadata(session: Session, monkeypatch) -> None:
    """`_resolve_security` (docs/05) keeps storing the CSV's own symbol cell —
    a canonical adopt must not split a Questrade `VEQT` into a second row."""
    user = _user(session)
    monkeypatch.setattr(
        market_data,
        "resolve_symbol",
        lambda symbol, **kwargs: {
            "match": "suffix",
            "result": {
                "symbol": "VEQT",  # typed spelling preserved by design
                "name": "Vanguard All-Equity ETF",
                "kind": SecurityKind.etf,
                "listing_currency": "CAD",
                "exchange": "TSX",
                "exchange_display": "Toronto",
            },
            "candidates": [],
        },
    )
    from app.api.trades import _resolve_security

    account = Account(user_id=user.id, name="Questrade", type=AccountType.tfsa, currency="CAD")
    session.add(account)
    session.commit()

    first = _resolve_security(session, user.id, "veqt", "Vanguard All-Equity ETF", "CAD")
    session.flush()
    second = _resolve_security(session, user.id, "VEQT", "Vanguard All-Equity ETF", "CAD")
    assert first.id == second.id  # one row, matched on the normalized cell
    assert second.symbol == "VEQT"
    assert second.listing_currency == "CAD"
    assert second.kind == SecurityKind.etf

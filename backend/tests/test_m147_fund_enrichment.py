"""Fund composition auto-enrichment + fallback chain (M147, docs/05/07/09).

Covers: the issuer-CSV parser (iShares shape pinned live), the provider
chain (issuer full CSV first, yfinance top-holdings fallback), the single
write path (provider rows replaced together, manual rows permanent,
holding_security_id linked, kind stock→etf repaired), auto-enrichment
firing on a committed trade batch, graceful privacy-mode degradation, the
static country map's deterministic geography fallback, and the
unclassified_funds honesty attribution in the breakdown response.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

import app.services.market_data as market_data
from app.models.etf_holdings import EtfHolding, EtfHoldingSource
from app.models.securities import Security, SecurityKind
from app.models.users import User
from app.services import fund_enrichment
from app.services.company_country_map import COUNTRY_BY_SYMBOL, country_for_symbol

API = "/api/v1"
SEC_URL = f"{API}/securities"

# Captured BEFORE the autouse offline fixture swaps module attributes, so the
# privacy test can exercise the real guarded seam.
_REAL_FETCH_ISSUER = market_data.fetch_issuer_holdings

# IVV's live `latest-holdings.csv` head (trimmed to three rows, 2026-10-10):
# a fund-facts preamble, then the pinned header, then quoted CSV lines.
_IVV_CSV_SNIPPET = (
    "iShares Core S&P 500 ETF\n"
    'Fund Holdings as of,"Oct 08, 2026"\n'
    'Inception Date,"May 15, 2000"\n'
    'Stock,"-"\n'
    'Bond,"-"\n\n'
    "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,"
    "Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date\n"
    '"NVDA","NVIDIA","Information Technology","Equity","74,146,212,999.84","8.32",'
    '"74,146,212,999.84","321,703,458.00","230.48","United States","NASDAQ","USD",'
    '"1.00","USD","2026-10-08"\n'
    '"AAPL","APPLE","Information Technology","Equity","66,044,265,997.28","7.41",'
    '"66,044,265,997.28","194,008,184.00","340.42","United States","NASDAQ","USD",'
    '"1.00","USD","2026-10-08"\n'
    '"CASH","GOLDMAN SACHS FINANCIAL SQUARE GOVERNMENT FUND","-","Cash",'
    '"1,000,000.00","0.11","1,000,000.00","1,000,000.00","1.00","United States",'
    '"NA","USD","1.00","USD","2026-10-08"\n'
)


@pytest.fixture(autouse=True)
def offline_market(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Default no-ops keep tests offline (docs/07 network discipline)."""

    def fake_yf_rows(symbol: str) -> list[dict[str, object]] | None:
        return [
            {
                "holding_symbol": "NVDA",
                "holding_name": "NVIDIA Corp",
                "weight_pct": Decimal("7.55"),
            },
            {"holding_symbol": "AAPL", "holding_name": "Apple Inc", "weight_pct": Decimal("7.04")},
        ]

    def no_issuer(symbol: str) -> list[dict[str, object]] | None:
        return None

    def no_lookup(symbol: str) -> dict[str, object] | None:
        return None

    def no_close(symbol: str, today: date | None = None) -> None:
        return None

    def cached_price(session: Any, security: Any, today: Any, **kwargs: object) -> Any:
        """The M129 seam contract: return the cached row the position is
        valued at — stubbing it to None would blank every market value."""
        from app.models.prices import Price

        return session.exec(
            select(Price).where(Price.security_id == security.id).order_by(Price.date.desc())
        ).first()

    def no_ensure(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(market_data, "fetch_fund_holdings", fake_yf_rows)
    monkeypatch.setattr(market_data, "fetch_issuer_holdings", no_issuer)
    # M147-followup: VEQT/VCN/... are live Vanguard registry keys, so the chain
    # must not reach the network here. Tests that exercise the Vanguard step
    # monkeypatch `fetch_vanguard_holdings` themselves (see the new module).
    monkeypatch.setattr(market_data, "fetch_vanguard_holdings", no_issuer)
    monkeypatch.setattr(market_data, "lookup_symbol", no_lookup)
    monkeypatch.setattr(market_data, "fetch_latest_close", no_close)
    monkeypatch.setattr(market_data, "ensure_recent_price", cached_price)
    monkeypatch.setattr(market_data, "ensure_fundamentals", no_ensure)
    return {}


def _auth(client: TestClient) -> tuple[dict[str, str], str]:
    email = f"m147-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}, email


def _uid(session: Session, email: str) -> int:
    return session.exec(select(User).where(User.email == email)).one().id


def _security(
    session: Session,
    user_id: int,
    symbol: str,
    kind: SecurityKind = SecurityKind.etf,
) -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind=kind,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


# --- issuer CSV parser (docs/07 pinned shape) ---------------------------------


def test_issuer_csv_parses_pinned_ishares_shape() -> None:
    rows = market_data._parse_issuer_csv(_IVV_CSV_SNIPPET)
    assert rows is not None and len(rows) == 3
    nvda = rows[0]
    assert nvda["holding_symbol"] == "NVDA"
    assert nvda["holding_name"] == "NVIDIA"
    assert nvda["weight_pct"] == Decimal("8.32")  # already a percent of NAV
    assert nvda["sector"] == "Information Technology"
    assert nvda["country"] == "United States"
    assert nvda["asset_class"] == "stock"
    cash = rows[2]
    assert cash["asset_class"] == "cash"
    assert cash.get("sector") is None  # "-" means unstated, not a sector named -
    assert cash["country"] == "United States"


def test_issuer_csv_error_page_is_none_and_junk_rows_skipped() -> None:
    assert market_data._parse_issuer_csv("<!doctype html><html>Failed to fetch</html>") is None
    junk = (
        "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Location\n"
        '""," ","Equity","Equity","1","0.00","United States"\n'
    )
    assert market_data._parse_issuer_csv(junk) is None


def test_provider_chain_prefers_issuer_then_yfinance(monkeypatch: pytest.MonkeyPatch) -> None:
    def issuer(symbol: str) -> list[dict[str, object]] | None:
        if symbol.upper() == "IVV":
            return [{"holding_name": "NVIDIA", "weight_pct": Decimal("8.32")}]
        return None

    monkeypatch.setattr(market_data, "fetch_issuer_holdings", issuer)
    chain = market_data.fetch_fund_composition("IVV")
    assert chain is not None and chain[0] == EtfHoldingSource.issuer
    chain = market_data.fetch_fund_composition("VEQT")
    assert chain is not None and chain[0] == EtfHoldingSource.yfinance

    def no_yf(symbol: str) -> None:
        return None

    monkeypatch.setattr(market_data, "fetch_fund_holdings", no_yf)
    monkeypatch.setattr(market_data, "fetch_issuer_holdings", lambda symbol: None)
    assert market_data.fetch_fund_composition("VEQT") is None


# --- the single write path ----------------------------------------------------


def test_provider_chain_retries_the_to_form_for_stored_bare_tickers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prod seed's catch: Canadian funds are stored BARE (VEQT) while
    yfinance answers only VEQT.TO — the bare probe runs first, the suffix
    is a FALLBACK after it answered nothing."""
    probed: list[str] = []

    def yf(symbol: str) -> list[dict[str, object]] | None:
        probed.append(symbol)
        if symbol == "VEQT.TO":
            return [{"holding_name": "Sub fund", "weight_pct": Decimal("100")}]
        return None

    monkeypatch.setattr(market_data, "fetch_fund_holdings", yf)
    monkeypatch.setattr(market_data, "fetch_issuer_holdings", lambda symbol: None)
    chain = market_data.fetch_fund_composition("VEQT")
    assert chain is not None and chain[0] == EtfHoldingSource.yfinance
    assert probed == ["VEQT", "VEQT.TO"]


def test_import_fund_composition_replaces_provider_keeps_manual_repairs_kind(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email="m147-write@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    fund = _security(session, user.id, "VEQT", kind=SecurityKind.stock)  # import-created default
    msft = _security(session, user.id, "MSFT", kind=SecurityKind.stock)
    stale = EtfHolding(
        user_id=user.id,
        security_id=fund.id,
        holding_name="Stale Inc",
        weight_pct=Decimal("5"),
        source=EtfHoldingSource.yfinance,
    )
    manual = EtfHolding(
        user_id=user.id,
        security_id=fund.id,
        holding_name="Hand Curated",
        weight_pct=Decimal("3"),
        source=EtfHoldingSource.manual,
    )
    session.add_all([stale, manual])
    session.commit()

    def chain(symbol: str):
        return (
            EtfHoldingSource.yfinance,
            [
                {
                    "holding_symbol": "MSFT",
                    "holding_name": "Microsoft",
                    "weight_pct": Decimal("60"),
                },
                {"holding_symbol": "NVDA", "holding_name": "Nvidia", "weight_pct": Decimal("40")},
            ],
        )

    monkeypatch.setattr(market_data, "fetch_fund_composition", chain)
    rows = market_data.import_fund_composition(session, fund)

    assert rows is not None and len(rows) == 2
    stored = session.exec(select(EtfHolding).where(EtfHolding.security_id == fund.id)).all()
    assert len(stored) == 3  # 2 fresh + the manual row; the stale one is gone
    assert {row.source for row in stored} == {EtfHoldingSource.yfinance, EtfHoldingSource.manual}
    msft_row = next(row for row in stored if row.holding_symbol == "MSFT")
    assert msft_row.holding_security_id == msft.id
    session.refresh(fund)
    assert fund.kind == SecurityKind.etf  # provider proved the ticker is a fund


def test_import_fund_composition_none_provider_returns_none(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email="m147-none@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    fund = _security(session, user.id, "ZZZ")

    monkeypatch.setattr(market_data, "fetch_fund_composition", lambda symbol: None)
    assert market_data.import_fund_composition(session, fund) is None


# --- auto-enrichment on a committed trade batch -------------------------------


def test_auto_enrichment_fires_on_trade_batch_commit(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed trade batch queues enrichment for its zero-composition
    funds (the trigger), and the queued task's write path imports their
    rows (the body). The body is driven on the TEST session directly —
    the suite's rollback-per-test outer transaction is invisible to a
    background task's own session (the house pattern, docs/13)."""
    queued: list[list[int]] = []
    real_enqueue = fund_enrichment.enqueue

    def spy_enqueue(db, background_tasks, user_id, security_ids):
        result = real_enqueue(db, background_tasks, user_id, security_ids)
        queued.append([fund.id for fund in result])
        return result

    monkeypatch.setattr(fund_enrichment, "enqueue", spy_enqueue)
    headers, email = _auth(client)
    account = client.post(
        f"{API}/accounts",
        json={"name": "TFSA", "type": "tfsa", "currency": "CAD"},
        headers=headers,
    ).json()
    # Pre-existing fund row (zero composition) — exactly Mark's state.
    fund = _security(session, _uid(session, email), "VEQT", kind=SecurityKind.etf)

    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "investment_sample.csv"
    with open(fixture, "rb") as handle:
        upload = client.post(
            f"{API}/trades/import",
            files={"file": ("investment_sample.csv", handle, "text/csv")},
            data={"account_id": account["id"]},
            headers=headers,
        )
    assert upload.status_code == 201, upload.text
    commit = client.post(f"{API}/trades/import/{upload.json()['batch_id']}/commit", headers=headers)
    assert commit.status_code == 200, commit.text

    # The trigger: commit queued exactly the batch's funds with no rows —
    # the pre-created VEQT plus the two the CSV introduced (XEQT, ZGRO).
    assert queued and fund.id in queued[0] and len(queued[0]) == 3

    # The body: the same write path the background task calls.
    written = market_data.import_fund_composition(session, fund)
    assert written is not None
    rows = session.exec(select(EtfHolding).where(EtfHolding.security_id == fund.id)).all()
    assert rows, "the enrichment write path must import composition rows"
    assert {row.holding_symbol for row in rows} == {"NVDA", "AAPL"}


def test_auto_enrichment_skips_funds_that_already_have_rows(
    client: TestClient, session: Session
) -> None:
    _headers, email = _auth(client)
    user_id = _uid(session, email)
    fund = _security(session, user_id, "HAVE", kind=SecurityKind.etf)
    session.add(
        EtfHolding(
            user_id=user_id,
            security_id=fund.id,
            holding_name="Manual Row",
            weight_pct=Decimal("100"),
            source=EtfHoldingSource.manual,
        )
    )
    session.commit()
    targets = fund_enrichment.funds_needing_enrichment(session, user_id, [fund.id])
    assert targets == []


# --- privacy mode degrades gracefully (docs/11 step 6) ------------------------


def test_privacy_mode_blocks_enrichment_without_crashing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email="m147-privacy@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    fund = _security(session, user.id, "VEQT")
    # Swap the offline fixture's fake back to the REAL guarded seam.
    monkeypatch.setattr(market_data, "fetch_issuer_holdings", _REAL_FETCH_ISSUER)
    market_data.bind_privacy_lock(True)
    try:
        # The write path refuses under the lock (docs/11 step 6)...
        with pytest.raises(Exception) as excinfo:
            market_data.import_fund_composition(session, fund)
        # ...and the background task body swallows exactly that.
        fund_enrichment._run_enrichment(fund.id)  # must not raise
    finally:
        market_data.bind_privacy_lock(False)
    assert "privacy" in str(excinfo.value).lower()
    rows = session.exec(select(EtfHolding).where(EtfHolding.security_id == fund.id)).all()
    assert rows == []


def test_privacy_guard_raises_on_real_issuer_seam() -> None:
    market_data.bind_privacy_lock(True)
    try:
        with pytest.raises(Exception) as excinfo:
            _REAL_FETCH_ISSUER("IVV")
    finally:
        market_data.bind_privacy_lock(False)
    detail = str(excinfo.value)
    assert "privacy" in detail.lower()


# --- static country map (deterministic last resort) ---------------------------

_MANDATED_TOP_20 = (
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "GOOG",
    "AMZN",
    "META",
    "TSLA",
    "AVGO",
    "BRK.B",
    "JPM",
    "V",
    "MA",
    "LLY",
    "XOM",
    "UNH",
    "ORCL",
    "COST",
    "JNJ",
    "HD",
)


def test_static_country_map_covers_top_20_mega_caps_deterministically() -> None:
    for ticker in _MANDATED_TOP_20:
        assert COUNTRY_BY_SYMBOL.get(ticker) == "United States", ticker
    assert country_for_symbol("MSFT") == "United States"
    assert country_for_symbol("ry.to") == "Canada"  # case-insensitive + suffix-stripped
    assert country_for_symbol("ASML.AS") == "Netherlands"
    assert country_for_symbol("  nvda  ") == "United States"
    assert country_for_symbol("NOT_A_REAL_TICKER_XYZ") is None
    assert country_for_symbol(None) is None
    assert country_for_symbol("") is None


def test_geography_falls_back_to_static_map_for_countryless_rows(
    client: TestClient, session: Session
) -> None:
    headers, email = _auth(client)
    user_id = _uid(session, email)
    _seed_position(session, user_id, "VEQT", SecurityKind.etf)
    fund_id = (
        session.exec(select(Security).where(Security.user_id == user_id, Security.symbol == "VEQT"))
        .one()
        .id
    )
    session.add(
        EtfHolding(
            user_id=user_id,
            security_id=fund_id,
            holding_symbol="MSFT",
            holding_name="Microsoft",
            weight_pct=Decimal("100"),
            country=None,  # yfinance rows never carry country
            source=EtfHoldingSource.yfinance,
        )
    )
    session.commit()
    body = client.get(f"{API}/portfolio/breakdown?dimension=geography", headers=headers)
    assert body.status_code == 200, body.text
    keys = {item["key"]: item for item in body.json()["items"]}
    assert "United States" in keys, keys
    assert "Unclassified" not in keys


# --- honesty attribution: unclassified_funds ---------------------------------


def _seed_position(
    session: Session, user_id: int, symbol: str, kind: SecurityKind, quantity: str = "10"
) -> None:
    from app.models.accounts import Account, AccountType
    from app.models.prices import Price, PriceSource
    from app.models.trades import Trade, TradeAction

    account = session.exec(select(Account).where(Account.user_id == user_id)).first()
    if account is None:
        account = Account(
            user_id=user_id,
            name="TFSA",
            type=AccountType.tfsa,
            currency="CAD",
        )
        session.add(account)
        session.commit()
        session.refresh(account)
    security = session.exec(
        select(Security).where(Security.user_id == user_id, Security.symbol == symbol)
    ).first()
    if security is None:
        security = _security(session, user_id, symbol, kind=kind)
    today = datetime.now(UTC).date()
    session.add(
        Trade(
            user_id=user_id,
            account_id=account.id,
            security_id=security.id,
            action=TradeAction.buy,
            date=today,
            quantity=Decimal(quantity),
            price_native=Decimal("40.00"),
            commission=Decimal("0"),
        )
    )
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=today,
            price=Decimal("40.00"),
            source=PriceSource.manual,
        )
    )
    session.commit()


def test_breakdown_marks_top_slice_only_funds_as_partial(
    client: TestClient, session: Session
) -> None:
    """A fund whose rows sum to < 100 (the provider's top slice) reports
    reason=partial with what DID import — the honesty copy for the
    residual that no Import click can fix."""
    headers, email = _auth(client)
    user_id = _uid(session, email)
    _seed_position(session, user_id, "VTT", SecurityKind.etf)
    fund_id = (
        session.exec(select(Security).where(Security.user_id == user_id, Security.symbol == "VTT"))
        .one()
        .id
    )
    session.add(
        EtfHolding(
            user_id=user_id,
            security_id=fund_id,
            holding_symbol="MSFT",
            holding_name="Microsoft",
            weight_pct=Decimal("33.50"),
            country=None,
            source=EtfHoldingSource.yfinance,
        )
    )
    session.commit()
    payload = client.get(f"{API}/portfolio/breakdown?dimension=geography", headers=headers).json()
    entry = next(f for f in payload["unclassified_funds"] if f["symbol"] == "VTT")
    assert entry["reason"] == "partial"
    assert Decimal(entry["imported_weight_pct"]) == Decimal("33.50")
    assert Decimal(entry["market_value_cad"]) == Decimal("400.00")
    buckets = {item["key"]: item for item in payload["items"]}
    # denominator max(33.5, 100): MSFT lands, the 66.5 residual is honest
    assert Decimal(buckets["United States"]["market_value_cad"]) == Decimal("134.00")
    assert Decimal(buckets["Unclassified"]["market_value_cad"]) == Decimal("266.00")

    # A near-complete import (>= 99%) is NOT "partial" — a sub-1% residual
    # is noise; the fund must not be named in the note (XEQT's 99.81 live).
    session.add(
        EtfHolding(
            user_id=user_id,
            security_id=fund_id,
            holding_name="Second sleeve",
            weight_pct=Decimal("66.31"),
            source=EtfHoldingSource.yfinance,
        )
    )
    session.commit()
    payload = client.get(f"{API}/portfolio/breakdown?dimension=geography", headers=headers).json()
    assert all(f["symbol"] != "VTT" for f in payload["unclassified_funds"])


def test_breakdown_names_funds_whose_composition_is_missing(
    client: TestClient, session: Session
) -> None:
    headers, email = _auth(client)
    user_id = _uid(session, email)
    _seed_position(session, user_id, "VGRO", SecurityKind.etf)
    body = client.get(f"{API}/portfolio/breakdown?dimension=geography", headers=headers)
    assert body.status_code == 200, body.text
    payload = body.json()
    funds = payload["unclassified_funds"]
    assert funds and funds[0]["symbol"] == "VGRO"
    assert funds[0]["name"] == "VGRO long name"
    assert Decimal(funds[0]["market_value_cad"]) == Decimal("400.00")

    # Once composition exists, the honesty list empties (the UI copy is
    # only for genuinely-not-imported funds).
    fund_id = (
        session.exec(select(Security).where(Security.user_id == user_id, Security.symbol == "VGRO"))
        .one()
        .id
    )
    session.add(
        EtfHolding(
            user_id=user_id,
            security_id=fund_id,
            holding_name="Hand Curated",
            weight_pct=Decimal("100"),
            source=EtfHoldingSource.manual,
        )
    )
    session.commit()
    payload = client.get(f"{API}/portfolio/breakdown?dimension=geography", headers=headers).json()
    assert payload["unclassified_funds"] == []

"""M166 — sector classification: one vocabulary, the cross-fund index, and the
published fund-mix estimate (docs/16 §Sector chain, docs/05 §Sector estimation).

No provider publishes a sector for every sub-holding: the iShares CSVs and the
Vanguard CANADIAN gateway do, the Vanguard US profile JSON does not. Before
M166 that left 62.77% of a real portfolio's sector pie Unclassified while the
same companies were already labelled elsewhere in the same cache.

Covers, in order: the canonical-vocabulary rule (two spellings must never split
one pie), the cross-fund index built from the tenant's own labelled rows
(including the bare-ticker disagreement that MUST refuse), the static ticker and
fund-mix tables, the engine's sector chain, the M19 no-inflation invariant under
fund-mix inheritance, and the `estimated` flag that rides the payload to the
legend.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

import app.services.fx as fx_service
import app.services.market_data as market_data
from app.models.accounts import Account, AccountType
from app.models.etf_holdings import EtfAssetClass, EtfHolding, EtfHoldingSource
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.services.sector_map import (
    SECTOR_ALIASES,
    SECTOR_NON_SECTORS,
    SectorMaps,
    build_sector_index,
    canonical_sector,
    fund_sector_mix,
    static_sector_for_symbol,
)

API = "/api/v1"
BREAKDOWN_URL = f"{API}/portfolio/breakdown"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Breakdown is pure aggregation over cached rows — any fetch attempt
    fails loudly (docs/07 cache-only convention for derived views)."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network fetch attempted during breakdown")

    monkeypatch.setattr(market_data, "fetch_latest_close", forbidden)
    monkeypatch.setattr(market_data, "fetch_history", forbidden)
    monkeypatch.setattr(market_data, "fetch_fundamentals", forbidden)
    monkeypatch.setattr(fx_service, "fetch_valet_series", forbidden)


@pytest.fixture
def sector_maps(session: Session) -> SectorMaps:
    """The two static families, loaded from `provider_fund_registry` (M168).

    The tables moved to the DB, so the lookups take the snapshot as an
    argument instead of reading a module global; loading it through the
    test's own session keeps the read inside the transaction the suite rolls
    back, exactly like every other table these tests touch.
    """
    maps = SectorMaps.load(session)
    assert maps.static_rows and maps.mix_rows, "migration 0050 seeded no static rows"
    return maps


def _today() -> date:
    return datetime.now(UTC).date()


class _Row:
    """Minimal stand-in for an ``EtfHolding`` in the pure-function tests."""

    def __init__(
        self,
        symbol: str | None,
        sector: str | None,
        country: str | None = None,
        asset_class: EtfAssetClass = EtfAssetClass.stock,
        weight: str = "100",
    ) -> None:
        self.holding_symbol = symbol
        self.holding_name = symbol or "unnamed"
        self.sector = sector
        self.country = country
        self.region = None
        self.asset_class = asset_class
        self.weight_pct = Decimal(weight)
        self.source = EtfHoldingSource.manual
        self.holding_security_id = None


# --- 1. one vocabulary ---------------------------------------------------------


@pytest.mark.parametrize(
    ("provider_label", "expected"),
    [
        ("Information Technology", "Technology"),  # iShares CSV / Vanguard GICS
        ("Financials", "Financial Services"),
        ("Health Care", "Healthcare"),
        ("Consumer Discretionary", "Consumer Cyclical"),
        ("Consumer Staples", "Consumer Defensive"),
        ("Materials", "Basic Materials"),
        ("Telecommunications", "Communication Services"),
        ("Communication", "Communication Services"),
        # the canonical names themselves — canonicalisation is idempotent
        ("Technology", "Technology"),
        ("Financial Services", "Financial Services"),
        ("Consumer Cyclical", "Consumer Cyclical"),
        ("Real Estate", "Real Estate"),
        ("Industrials", "Industrials"),
    ],
)
def test_canonical_sector_maps_every_provider_spelling(provider_label: str, expected: str) -> None:
    assert canonical_sector(provider_label) == expected


def test_canonical_sector_is_case_insensitive_and_trims() -> None:
    assert canonical_sector("  information technology ") == "Technology"
    assert canonical_sector("HEALTH CARE") == "Healthcare"


@pytest.mark.parametrize(
    "label",
    # sorted(): the frozenset spreads in hash order, which differs per
    # xdist worker (PYTHONHASHSEED), and xdist requires identical
    # collection order on every worker.
    [None, "", "   ", *sorted(SECTOR_NON_SECTORS)],
)
def test_canonical_sector_drops_blank_and_non_sector_labels(label: str | None) -> None:
    """A cash leg and an empty GICS cell are NOT sectors — they must not grow
    a pie slice nothing else can be compared against."""
    assert canonical_sector(label) is None


def test_canonical_sector_passes_an_unknown_label_through_unchanged() -> None:
    """An unmapped word is not a reason to drop a value the provider published
    (the value would silently shrink the pie); it is a reason to add a mapping
    entry, which is what the alias table's comment asks for."""
    assert canonical_sector("Quantum Widgets") == "Quantum Widgets"


def test_every_alias_value_is_a_canonical_name_it_maps_onto() -> None:
    """No alias may introduce a slice spelling of its own."""
    for alias, canonical in SECTOR_ALIASES.items():
        assert canonical_sector(canonical) == canonical
        assert canonical == canonical_sector(alias)


# --- 2. the cross-fund sector index -------------------------------------------


def test_index_resolves_a_ticker_another_of_the_tenant_funds_labelled() -> None:
    """The whole point of M166: IVV's issuer-CSV rows label S&P 500 names that
    VTI's Vanguard rows leave blank, and the label answers for both."""
    rows = [
        _Row("RY.TO", "Financials", country="Canada"),  # labelled by fund A
        _Row("RY.TO", None, country="Canada"),  # blank in fund B (held)
    ]
    index = build_sector_index(rows)  # type: ignore[arg-type]
    assert index.lookup("RY.TO", "Canada") == "Financial Services"


def test_index_matches_the_symbol_case_insensitively() -> None:
    index = build_sector_index([_Row("NVDA", "Information Technology", country="United States")])  # type: ignore[arg-type]
    assert index.lookup("nvda", "United States") == "Technology"


def test_index_refuses_a_bare_ticker_whose_rows_disagree() -> None:
    """``SU`` is Suncor in Canada and Sogefi in Italy. A bare lookup that
    resolved either one would be the M143 collision all over again, so the
    disagreement is refused outright and only the exact pair answers."""
    rows = [
        _Row("SU", "Energy", country="Canada"),
        _Row("SU", "Industrials", country="France"),
    ]
    index = build_sector_index(rows)  # type: ignore[arg-type]
    assert index.lookup("SU", None) is None
    assert index.lookup("SU", "United States") is None
    assert index.lookup("SU", "Canada") == "Energy"
    assert index.lookup("SU", "France") == "Industrials"


def test_index_ignores_rows_without_a_sector_or_symbol() -> None:
    rows = [
        _Row("AAPL", None, country="United States"),
        _Row(None, "Technology", country="United States"),
        _Row("", "Technology"),
    ]
    index = build_sector_index(rows)  # type: ignore[arg-type]
    assert index.lookup("AAPL", "United States") is None
    assert len(index) == 0


def test_index_is_built_only_from_the_rows_it_is_given() -> None:
    """Tenancy: the index never sees another tenant's labels."""
    mine = build_sector_index([_Row("SHOP", "Technology", country="Canada")])  # type: ignore[arg-type]
    theirs = build_sector_index([])  # type: ignore[arg-type]
    assert mine.lookup("SHOP", "Canada") == "Technology"
    assert theirs.lookup("SHOP", "Canada") is None


# --- 3a. the static ticker map ------------------------------------------------


def test_static_sector_map_resolves_exact_slash_and_suffixed_forms(
    sector_maps: SectorMaps,
) -> None:
    assert static_sector_for_symbol("BRK/B", sector_maps) == "Financial Services"
    assert static_sector_for_symbol("2330", sector_maps) == "Technology"
    assert static_sector_for_symbol("2330.TW", sector_maps) == "Technology"
    assert static_sector_for_symbol("OR.PA", sector_maps) == "Consumer Defensive"


def test_static_sector_map_strips_a_known_exchange_suffix(sector_maps: SectorMaps) -> None:
    """One entry answers both spellings, exactly like the country map."""
    assert static_sector_for_symbol("SAP.DE", sector_maps) == static_sector_for_symbol(
        "SAP", sector_maps
    )
    assert static_sector_for_symbol("NVO", sector_maps) == static_sector_for_symbol(
        "NOVO-B.CO", sector_maps
    )


def test_static_sector_map_read_as_dot_for_a_slash_share_class(
    sector_maps: SectorMaps,
) -> None:
    """Vanguard writes ``BRK/B`` where the country map already knows ``BRK.B``
    — the one normalisation the map does."""
    assert static_sector_for_symbol("BRK/B", sector_maps) == sector_maps.static_rows["BRK.B"]


@pytest.mark.parametrize("symbol", [None, "", "   ", "ZZZZQ", "ZZZZQ.TO"])
def test_static_sector_map_unknown_is_none_never_a_guess(
    symbol: str | None, sector_maps: SectorMaps
) -> None:
    assert static_sector_for_symbol(symbol, sector_maps) is None


def test_static_sector_map_values_are_canonical(sector_maps: SectorMaps) -> None:
    for symbol, sector in sector_maps.static_rows.items():
        assert canonical_sector(sector) == sector, symbol


# --- 3b. the published fund mix -----------------------------------------------


def test_fund_mix_returns_a_copy_so_a_caller_cannot_corrupt_the_table(
    sector_maps: SectorMaps,
) -> None:
    mix = fund_sector_mix("VTI", sector_maps)
    assert mix is not None
    mix.pop("Technology")
    assert fund_sector_mix("VTI", sector_maps) is not None
    assert "Technology" in fund_sector_mix("VTI", sector_maps)  # type: ignore[operator]


def test_fund_mix_resolves_a_venue_suffixed_listing(sector_maps: SectorMaps) -> None:
    assert fund_sector_mix("VOO.TO", sector_maps) == fund_sector_mix("VOO", sector_maps)


@pytest.mark.parametrize("symbol", [None, "", "VXUS", "ZZZZQ", "ZZZZQ.TO"])
def test_fund_mix_unknown_or_unpublished_fund_is_none(
    symbol: str | None, sector_maps: SectorMaps
) -> None:
    """Vanguard reports `isavailable: false` for every non-US-index product,
    so VXUS/VT/VEA/VWO must NOT be estimated from a number nobody published."""
    assert fund_sector_mix(symbol, sector_maps) is None


def test_every_published_mix_is_positive_and_canonical(sector_maps: SectorMaps) -> None:
    for symbol, mix in sector_maps.mix_rows.items():
        assert sum(mix.values()) > 0, symbol
        for sector, weight in mix.items():
            assert weight > 0, f"{symbol}: {sector}"
            assert canonical_sector(sector) == sector, f"{symbol}: {sector}"


# --- the engine ---------------------------------------------------------------


def _new_user(client: TestClient) -> tuple[dict[str, str], str]:
    email = f"m166-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}, email


def _uid(session: Session, email: str) -> int:
    return session.exec(select(User).where(User.email == email)).one().id


def _account(session: Session, user_id: int, name: str) -> Account:
    account = Account(
        user_id=user_id,
        name=name,
        type=AccountType.brokerage_non_registered,
        currency="CAD",
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _security(
    session: Session, user_id: int, symbol: str, kind: SecurityKind | str = SecurityKind.stock
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


def _buy(
    session: Session,
    user_id: int,
    account: Account,
    security: Security,
    quantity: str,
    price: str,
) -> None:
    today = _today()
    session.add(
        Trade(
            user_id=user_id,
            account_id=account.id,
            security_id=security.id,
            action=TradeAction.buy,
            date=today,
            quantity=Decimal(quantity),
            price_native=Decimal(price),
            commission=Decimal("0"),
        )
    )
    from app.models.prices import Price, PriceSource

    if (
        session.exec(
            select(Price).where(Price.security_id == security.id, Price.date == today)
        ).first()
        is None
    ):
        session.add(
            Price(
                user_id=user_id,
                security_id=security.id,
                date=today,
                price=Decimal(price),
                source=PriceSource.manual,
            )
        )
    session.commit()


def _rows(
    session: Session,
    user_id: int,
    fund: Security,
    rows: list[dict],
    source: EtfHoldingSource = EtfHoldingSource.manual,
) -> None:
    for row in rows:
        session.add(
            EtfHolding(
                user_id=user_id,
                security_id=fund.id,
                holding_symbol=row.get("holding_symbol"),
                holding_name=row.get("holding_name", row.get("holding_symbol", "unnamed")),
                asset_class=row.get("asset_class", EtfAssetClass.stock),
                country=row.get("country"),
                sector=row.get("sector"),
                weight_pct=Decimal(row["weight_pct"]),
                source=source,
            )
        )
    session.commit()


def _buckets(payload: dict) -> dict[str, str]:
    return {item["key"]: item["market_value_cad"] for item in payload["items"]}


def test_composition_row_resolves_its_sector_from_the_tenant_other_funds(
    client: TestClient, session: Session
) -> None:
    """Fund A's issuer-CSV rows label the company; fund B (the one held) has
    the same ticker with no sector. The label answers for both — and the
    pie reports it as sourced, not estimated."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")

    labellel = _security(session, user_id, "IVV", SecurityKind.etf)
    _rows(
        session,
        user_id,
        labellel,
        [
            {
                "holding_symbol": "RY.TO",
                "sector": "Financials",
                "weight_pct": "100",
                "country": "Canada",
            }
        ],
    )
    held = _security(session, user_id, "VTI", SecurityKind.etf)
    _rows(
        session,
        user_id,
        held,
        [{"holding_symbol": "RY.TO", "weight_pct": "100", "country": "Canada"}],
    )
    _buy(session, user_id, account, held, "1", "500")

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    assert _buckets(payload) == {"Financial Services": "500.00"}
    assert payload["estimated_cad"] == "0.00"
    assert payload["items"][0]["estimated"] is False


def test_two_provider_spellings_land_in_one_slice_not_two(
    client: TestClient, session: Session
) -> None:
    """yfinance says ``Technology``, the iShares CSV says ``Information
    Technology``. One company, one slice."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    from app.models.fundamentals import Fundamentals, FundamentalsSource

    fund = _security(session, user_id, "XTF", SecurityKind.etf)
    _rows(
        session,
        user_id,
        fund,
        [
            {"holding_symbol": "AAPL", "sector": "Information Technology", "weight_pct": "50"},
            {"holding_symbol": "MSFT", "weight_pct": "50"},
        ],
    )
    msft = _security(session, user_id, "MSFT")
    session.add(
        Fundamentals(
            user_id=user_id,
            security_id=msft.id,
            sector="Technology",
            source=FundamentalsSource.manual,
            fetched_at=_today(),
        )
    )
    session.commit()
    _buy(session, user_id, account, fund, "1", "1000")

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    assert _buckets(payload) == {"Technology": "1000.00"}


def test_bare_ticker_disagreement_stays_unclassified(client: TestClient, session: Session) -> None:
    """Two funds label the same bare ticker differently. Refusing beats
    guessing (M143); only the exact (symbol, country) pair may answer."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    labellel = _security(session, user_id, "LAB", SecurityKind.etf)
    _rows(
        session,
        user_id,
        labellel,
        [
            {"holding_symbol": "SU", "sector": "Energy", "country": "Canada", "weight_pct": "50"},
            {
                "holding_symbol": "SU",
                "sector": "Industrials",
                "country": "France",
                "weight_pct": "50",
            },
        ],
    )
    held = _security(session, user_id, "HLD", SecurityKind.etf)
    _rows(session, user_id, held, [{"holding_symbol": "SU", "weight_pct": "100"}])
    _buy(session, user_id, account, held, "1", "300")

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    assert _buckets(payload) == {"Unclassified": "300.00"}
    assert payload["estimated_cad"] == "0.00"  # refused, never estimated


def test_static_map_resolves_a_name_no_provider_labelled(
    client: TestClient, session: Session
) -> None:
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "GLO", SecurityKind.etf)
    _rows(session, user_id, fund, [{"holding_symbol": "2330", "weight_pct": "100"}])
    _buy(session, user_id, account, fund, "1", "100")

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    assert _buckets(payload) == {"Technology": "100.00"}
    assert payload["estimated_cad"] == "0.00"  # a static fact, not an estimate


def test_fund_mix_inherits_only_for_equity_and_is_labelled_estimated(
    client: TestClient, session: Session, sector_maps: SectorMaps
) -> None:
    """VOO's rows carry no sector and no other fund in the cache labels them,
    but Vanguard publishes VOO's own GICS mix. The unattributable share is
    split across that mix and every cent of it is reported ``estimated``."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "VOO", SecurityKind.etf)
    _rows(
        session,
        user_id,
        fund,
        [
            {"holding_symbol": "ZZEQ", "weight_pct": "60"},  # equity, no sector anywhere
            {"holding_symbol": "ZZCASH", "asset_class": EtfAssetClass.cash, "weight_pct": "40"},
        ],
    )
    _buy(session, user_id, account, fund, "1", "1000")

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    buckets = _buckets(payload)
    mix = sector_maps.mix_rows["VOO"]
    mix_total = sum(mix.values())
    # the 600 of equity is split across VOO's published mix; the 400 of cash
    # has no GICS sector and stays Unclassified.
    assert buckets["Unclassified"] == "400.00"
    expected_technology = Decimal("600") * mix["Technology"] / mix_total
    assert Decimal(buckets["Technology"]) == expected_technology.quantize(Decimal("0.01"))
    assert sum(Decimal(value) for value in buckets.values()) == Decimal("1000.00")

    assert Decimal(payload["estimated_cad"]) == Decimal("600.00")
    technology = next(i for i in payload["items"] if i["key"] == "Technology")
    assert technology["estimated"] is True
    assert Decimal(technology["estimated_cad"]) == expected_technology.quantize(Decimal("0.01"))
    unclassified = next(i for i in payload["items"] if i["key"] == "Unclassified")
    assert unclassified["estimated"] is False
    assert unclassified["estimated_cad"] == "0.00"


def test_fund_mix_absorbs_the_m19_residual_without_inflating(
    client: TestClient,
    session: Session,
    sector_maps: SectorMaps,
) -> None:
    """The M19 denominator rule holds under inheritance: a nested fund that
    inherits a mix and is then distributed inside a parent that has one must
    not inflate. Every value stays bounded by the position, and the sum is the
    position."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")

    inner = _security(session, user_id, "VOO", SecurityKind.etf)
    # rows sum to 94 — the provider published only that much, so 6% is the
    # M19 residual, which the inner fund's mix must absorb.
    _rows(session, user_id, inner, [{"holding_symbol": "ZZEQ", "weight_pct": "94"}])
    outer = _security(session, user_id, "VTI", SecurityKind.etf)
    _rows(session, user_id, outer, [{"holding_symbol": "VOO", "weight_pct": "100"}])
    _buy(session, user_id, account, outer, "1", "1000")

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    buckets = _buckets(payload)
    total = Decimal("1000")
    assert sum(Decimal(value) for value in buckets.values()) == total
    assert Decimal(payload["total_cad"]) == total
    for key, value in buckets.items():
        assert Decimal(value) <= total, key
        # every sector is VOO's (the inner fund resolved the leaf), never a
        # VTI-weighted second helping of the same money
        assert key in sector_maps.mix_rows["VOO"], key
    assert Decimal(payload["estimated_cad"]) == total


def test_fund_without_a_published_mix_stays_unclassified(
    client: TestClient, session: Session
) -> None:
    """No published number, no estimate — the honesty rule the static tables
    are built on."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "ZZRO", SecurityKind.etf)
    _rows(session, user_id, fund, [{"holding_symbol": "ZZEQ", "weight_pct": "100"}])
    _buy(session, user_id, account, fund, "1", "100")

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    assert _buckets(payload) == {"Unclassified": "100.00"}
    assert payload["estimated_cad"] == "0.00"


def test_bond_and_commodity_rows_never_inherit_an_equity_mix(
    client: TestClient, session: Session
) -> None:
    """A bond has no GICS sector. Inheriting an equity fund's mix would
    invent one."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "VOO", SecurityKind.etf)
    _rows(
        session,
        user_id,
        fund,
        [
            {
                "holding_symbol": "ZZBOND",
                "asset_class": EtfAssetClass.bond,
                "weight_pct": "100",
            }
        ],
    )
    _buy(session, user_id, account, fund, "1", "250")

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    assert _buckets(payload) == {"Unclassified": "250.00"}
    assert payload["estimated_cad"] == "0.00"


def test_issuer_csv_sector_column_reaches_the_pie_in_one_vocabulary(
    client: TestClient, session: Session
) -> None:
    """M147's importer already maps the iShares `Sector` column; M166 pins the
    half nobody asserted — that the CSV's GICS spelling lands in the SAME
    slice a yfinance fundamentals row would."""
    csv = (
        "iShares Core S&P 500 ETF\n"
        'Fund Holdings as of,"Oct 08, 2026"\n'
        "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Location\n"
        '"NVDA","NVIDIA","Information Technology","Equity","1","8.32","United States"\n'
        '"AAPL","APPLE","Information Technology","Equity","1","7.41","United States"\n'
    )
    rows = market_data._parse_issuer_csv(csv)
    assert rows is not None
    assert [row["sector"] for row in rows] == ["Information Technology", "Information Technology"]
    assert [canonical_sector(row["sector"]) for row in rows] == ["Technology", "Technology"]

    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "IVV", SecurityKind.etf)
    _rows(
        session,
        user_id,
        fund,
        [
            {
                "holding_symbol": row["holding_symbol"],
                "holding_name": row["holding_name"],
                "sector": row["sector"],
                "country": row["country"],
                "weight_pct": "50",
            }
            for row in rows
        ],
        source=EtfHoldingSource.issuer,
    )
    _buy(session, user_id, account, fund, "1", "1000")

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    assert _buckets(payload) == {"Technology": "1000.00"}


def test_estimated_flag_is_zero_on_every_other_dimension(
    client: TestClient, session: Session
) -> None:
    """The estimate is a sector-only story; geography, asset class and company
    stay exact and must say so."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "VOO", SecurityKind.etf)
    _rows(session, user_id, fund, [{"holding_symbol": "ZZEQ", "weight_pct": "100"}])
    _buy(session, user_id, account, fund, "1", "100")

    for dimension in ("geography", "asset_class", "company"):
        payload = client.get(BREAKDOWN_URL, params={"dimension": dimension}, headers=headers).json()
        assert payload["estimated_cad"] == "0.00", dimension
        assert all(item["estimated"] is False for item in payload["items"]), dimension


def test_static_map_miss_and_null_stay_unclassified(client: TestClient, session: Session) -> None:
    """The long tail of international small-caps stays honestly Unclassified —
    no silent guessing anywhere in the chain."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "TAIL", SecurityKind.etf)
    _rows(session, user_id, fund, [{"holding_symbol": "ZZZZQ", "weight_pct": "100"}])
    _buy(session, user_id, account, fund, "1", "42")

    payload = client.get(BREAKDOWN_URL, params={"dimension": "sector"}, headers=headers).json()
    assert _buckets(payload) == {"Unclassified": "42.00"}

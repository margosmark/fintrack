"""M168 — the coverage registries are DATA: discovery, negative cache, tenancy.

The card this file answers is Mark's "is what we added for getting details on
sector/geography/underlying securities done generically?" (docs/07 §Provider
registry, docs/16 §Sector chain). Before M168 the answer was "the engines are
generic; the five tables that feed them were literals in Python", which meant
a Vanguard Canada listing added after the seed — or any fund family nobody had
hand-listed — answered nothing until somebody edited code.

Six things are pinned here:

1. **The move was a move.** Migration 0050's seed matches the five tables it
   replaced byte for byte, so the exposure pipeline's answers cannot have
   changed (the card's "coverage tables moved, not altered" test).
2. **Discovery resolves a fund nobody registered, with zero code change** —
   one probe for the US endpoint, the issuer's own catalogue for Canada, the
   issuer's own screener for iShares. Each writes the registry row it earned.
3. **The negative case**: an unknown fund is recorded ONCE and never probed
   again; a transient failure records NOTHING, so an outage cannot retire a
   real fund.
4. **Tenancy**: registry rows are operator-level (no `user_id` anywhere);
   holdings stay strictly per-user.
5. **The maintenance surface**: `fintrackerctl provider-funds` lists, edits,
   verifies and sweeps — coverage grows without a deploy.
6. **The pattern retired**: `git grep VANGUARD_US_FUNDS backend/app` is empty.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlmodel import Session, select

import app.services.market_data as market_data
from app.models.etf_holdings import EtfAssetClass, EtfHolding, EtfHoldingSource
from app.models.provider_registry import (
    ProviderFundFamily,
    ProviderFundRegistry,
    ProviderFundStatus,
)
from app.models.securities import Security, SecurityKind
from app.models.users import User
from app.services import provider_registry
from app.services.sector_map import SectorMaps

BACKEND = Path(__file__).resolve().parents[1]
_FROZEN = Path(__file__).parent / "fixtures" / "m168_registry_seed.json"


def _frozen() -> dict:
    return json.loads(_FROZEN.read_text())


# --- 1. the move was a move -------------------------------------------------


def test_migration_seeded_exactly_the_five_tables(session: Session) -> None:
    """132 rows, the counts the literals had, and nothing else.

    A discovery row in here would mean some earlier test's verdict survived
    the suite's registry rollback — the exact pollution this suite refuses.
    """
    frozen = _frozen()
    families = {
        family: provider_registry.load_family(family, session=session)
        for family in ProviderFundFamily
    }
    assert {str(f): len(rows) for f, rows in families.items()} == {
        "vanguard_us": len(frozen["vanguard_us"]),
        "vanguard_ca": len(frozen["vanguard_ca"]),
        "ishares_us": len(frozen["ishares_us"]),
        "static_sector": len(frozen["static_sector"]),
        "fund_sector_mix": len(frozen["fund_sector_mix"]),
    }
    assert len(families[ProviderFundFamily.vanguard_us]) == 50
    assert len(families[ProviderFundFamily.vanguard_ca]) == 42

    for ticker in frozen["vanguard_us"]:
        row = families[ProviderFundFamily.vanguard_us][ticker]
        assert row.status is ProviderFundStatus.verified
        assert row.verified_by_run == "seed:0050"

    for ticker, payload in frozen["vanguard_ca"].items():
        row = families[ProviderFundFamily.vanguard_ca][ticker]
        assert (row.port_id, row.venue_suffix) == (payload["port_id"], payload["venue_suffix"])

    for ticker, stem in frozen["ishares_us"].items():
        assert families[ProviderFundFamily.ishares_us][ticker].endpoint == stem

    # every row of the seed carries the migration as its writer, so the
    # quarterly sweep knows where `verified_at` came from.
    all_rows = provider_registry.list_rows(session=session)
    assert len(all_rows) == 132
    assert {row.verified_by_run for row in all_rows} == {"seed:0050"}


def test_the_static_families_are_byte_identical_to_the_literals(
    session: Session,
) -> None:
    """The card's byte-identity test for the DATA half.

    The exposure pipeline reads these two families at breakdown time; if the
    strings or the decimal TEXT differ from what the old dicts held, some
    slice would round differently. They must be equal, not merely similar.
    """
    frozen = _frozen()
    maps = SectorMaps.load(session)
    assert maps.static_rows == frozen["static_sector"]
    assert {
        ticker: {sector: str(weight) for sector, weight in mix.items()}
        for ticker, mix in maps.mix_rows.items()
    } == frozen["fund_sector_mix"]
    # and the Decimals are the literals' Decimals, not float round-trips
    assert maps.mix_rows["VOO"]["Technology"] == Decimal("37.90")
    assert maps.mix_rows["VTI"]["Technology"] == Decimal("40.90")


def test_the_hardcoded_pattern_is_gone_from_the_app_package() -> None:
    """The CI grep-assert the card asks for, as a test so it cannot be skipped.

    `git grep VANGUARD_US_FUNDS backend/app` must return nothing — the code
    no longer knows a single ticker, which is the whole point of moving the
    tables.
    """
    retired = (
        "VANGUARD_US_FUNDS",
        "VANGUARD_CA_FUNDS",
        "ISHARES_US_HOLDINGS_CSV",
        "STATIC_SECTOR_BY_SYMBOL",
        "FUND_SECTOR_MIX_BY_SYMBOL",
    )
    hits: list[str] = []
    for path in (BACKEND / "app").rglob("*.py"):
        text = path.read_text()
        for name in retired:
            if name in text:
                hits.append(f"{path.relative_to(BACKEND)}: {name}")
    assert hits == []


# --- 2. discovery -----------------------------------------------------------


class _Response:
    def __init__(self, status: int, payload: object, text: str | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self._text = text if text is not None else json.dumps(payload)

    def json(self) -> Any:
        return self._payload

    @property
    def text(self) -> str:
        return self._text


def _screener_html(ticker: str, product_id: str, slug: str) -> str:
    return (
        "<table><tr>"
        f'<td class="links"><a href="/us/products/{product_id}/{slug}">{ticker}</a></td>'
        f'<td class="links"><a href="/us/products/{product_id}/{slug}">Some Fund</a></td>'
        "</tr></table>"
    )


def _model_json(port_ids: str) -> dict:
    return {"site-config": {"portIds": port_ids}}


def _catalogue(entries: list[tuple[str, str, str]]) -> dict:
    """(portId, ticker, MIC) triples -> the FundCatalogue envelope."""
    return {
        "data": {
            "funds": [
                {
                    "portId": port_id,
                    "profile": {
                        "fundFullName": f"Fund {ticker}",
                        "listings": [
                            {
                                "stockExchangeMarketIdentifierCode": mic,
                                "identifiers": [{"altId": "Ticker - Canada", "altIdValue": ticker}],
                            }
                        ],
                    },
                }
                for port_id, ticker, mic in entries
            ]
        }
    }


def _holdings(items: list[dict]) -> dict:
    return {
        "data": {
            "borHoldings": [
                {"holdings": {"items": items, "totalHoldings": len(items), "lastItemKey": None}}
            ]
        }
    }


@pytest.fixture
def issuer(monkeypatch: pytest.MonkeyPatch):
    """A fake issuer layer: routes by URL, records every call.

    Seeded so that three funds OUTSIDE the migration's seed exist in the
    issuer's own catalogues and nowhere in our code — the "zero code change"
    case the card asks for.
    """
    state = {"get": [], "post": []}
    routes: dict[str, _Response] = {
        # iShares: a product the seed's single IVV row never covered
        "etf-investments": _Response(
            200, {}, _screener_html("ITOT", "239724", "ishares-core-sp-total")
        ),
        "/us/products/239724/ishares-core-sp-total/latest-holdings.csv": _Response(
            200,
            {},
            "iShares Core S&P Total US Stock Market ETF\n"
            "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Location\n"
            '"NVDA","NVIDIA","Information Technology","Equity","1","8.32","United States"\n',
        ),
        # Vanguard US: a fund the 50-row literal never covered
        "NEWUS-AdditionalFundData": _Response(
            200,
            {
                "holdingDetails": {
                    "equityHoldings": [
                        {
                            "securityType": "EQ.STOCK",
                            "securityLongDescription": "Example Corp",
                            "marketValuePercentage": "10.00%",
                            "ticker": "EXMPL",
                            "isin": "US0000000000",
                        }
                    ]
                }
            },
        ),
        # Vanguard CA: a listing the 42-row literal never covered
        "product.model.json": _Response(200, _model_json("1234")),
    }

    def fake_get(url: object = "", **kwargs: object) -> _Response:
        target = str(url)
        state["get"].append(target)
        for key, response in routes.items():
            if key in target:
                return response
        if "AdditionalFundData" in target:
            # An unrouted fund-profile URL is exactly what the live endpoint
            # answers for a ticker it does not know: 404, a verdict.
            return _Response(404, {"message": "Fund not found"})
        raise httpx.ConnectError(f"unrouted {target}")

    def fake_post(url: object = "", **kwargs: object) -> _Response:
        state["post"].append(kwargs)
        body = kwargs.get("json") or {}
        if body.get("operationName") == "FundCatalogue":
            return _Response(200, _catalogue([("1234", "ZNEW", "XTSE")]))
        variables = body.get("variables") or {}
        if variables.get("portIds") == ["1234"]:
            return _Response(
                200,
                _holdings(
                    [
                        {
                            "securityType": "EQ.ETF",
                            "securityLongDescription": "ZNEW holding",
                            "marketValuePercentage": 12.5,
                            "ticker": "VCN",
                            "bloombergIsoCountry": "CA",
                            "gicsSectorDescription": "Financials",
                        }
                    ]
                ),
            )
        return _Response(200, {"data": {"borHoldings": []}})  # unknown portId

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    market_data._reset_discovery_cache()
    yield state
    market_data._reset_discovery_cache()


def _clean(session: Session, family: ProviderFundFamily, ticker: str) -> None:
    provider_registry.delete(family, ticker, session=session)


def test_a_vanguard_us_fund_outside_the_seed_joins_the_registry_by_probe(
    session: Session, issuer
) -> None:
    """NEWUS answers 200 and parses; no literal names it, no code changed."""
    _clean(session, ProviderFundFamily.vanguard_us, "NEWUS")
    assert provider_registry.get(ProviderFundFamily.vanguard_us, "NEWUS", session=session) is None

    rows = market_data.fetch_vanguard_holdings("NEWUS")
    assert rows and rows[0]["holding_symbol"] == "EXMPL"

    row = provider_registry.get(ProviderFundFamily.vanguard_us, "NEWUS", session=session)
    assert row is not None
    assert row.status is ProviderFundStatus.verified
    assert row.verified_by_run == "discovery:vanguard_us_probe"
    assert row.verified_at is not None


def test_a_vanguard_ca_fund_outside_the_seed_resolves_through_the_issuers_catalogue(
    session: Session, issuer
) -> None:
    """ZNEW is in no literal: the catalogue names its portId, the gateway
    answers for it, and the registry row carries BOTH — portId and the `.TO`
    the MIC says, derived rather than hand-typed."""
    _clean(session, ProviderFundFamily.vanguard_ca, "ZNEW")
    rows = market_data.fetch_vanguard_holdings("ZNEW.TO")
    assert rows, "the chain must reach the gateway for a fund nobody registered"
    assert rows[0]["holding_symbol"] == "VCN.TO"  # fund leg, suffixed from the registry

    row = provider_registry.get(ProviderFundFamily.vanguard_ca, "ZNEW", session=session)
    assert row is not None and row.status is ProviderFundStatus.verified
    assert (row.port_id, row.venue_suffix) == ("1234", ".TO")
    assert row.verified_by_run == "discovery:vanguard_ca_catalogue"


def test_an_ishares_product_outside_the_seed_resolves_through_the_screener(
    session: Session, issuer
) -> None:
    """The fund-id lookup the card asks about EXISTS — iShares' own product
    screener names the product id — so iShares coverage automates too."""
    _clean(session, ProviderFundFamily.ishares_us, "ITOT")
    rows = market_data.fetch_issuer_holdings("ITOT")
    assert rows and rows[0]["holding_symbol"] == "NVDA"
    assert rows[0]["sector"] == "Information Technology"

    row = provider_registry.get(ProviderFundFamily.ishares_us, "ITOT", session=session)
    assert row is not None and row.status is ProviderFundStatus.verified
    assert row.endpoint == "https://www.ishares.com/us/products/239724/ishares-core-sp-total"
    assert row.verified_by_run == "discovery:ishares_csv"


def test_the_whole_chain_books_a_new_fund_with_source_vanguard(session: Session, issuer) -> None:
    """End to end through `fetch_fund_composition`: the chain's answer for a
    fund outside the seed is the vanguard provider, not the yfinance slice."""
    _clean(session, ProviderFundFamily.vanguard_us, "NEWUS")
    _clean(session, ProviderFundFamily.ishares_us, "NEWUS")
    _clean(session, ProviderFundFamily.vanguard_ca, "NEWUS")
    chain = market_data.fetch_fund_composition("NEWUS")
    assert chain is not None
    source, rows = chain
    assert source is EtfHoldingSource.vanguard
    assert rows and rows[0]["holding_symbol"] == "EXMPL"


# --- 3. the negative case ---------------------------------------------------


def test_an_unknown_fund_is_recorded_once_and_never_probed_again(session: Session, issuer) -> None:
    """A ticker no catalogue names: ONE probe, one `miss` row per family,
    and every later encounter answered by the row with zero requests."""
    for family in ProviderFundFamily:
        _clean(session, family, "NOSUCH")

    assert market_data.fetch_issuer_holdings("NOSUCH") is None
    assert market_data.fetch_vanguard_holdings("NOSUCH") is None
    first = len(issuer["get"])
    # iShares screener + the US fund-profile probe + the CA catalogue
    assert first >= 3, f"a first encounter must ask the issuers, saw {first}"

    us = provider_registry.get(ProviderFundFamily.vanguard_us, "NOSUCH", session=session)
    assert us is not None and us.status is ProviderFundStatus.miss
    assert us.miss_count == 1
    assert us.last_probe_at is not None
    # a miss row keeps no endpoint payload but keeps its audit trail
    assert us.verified_by_run == "discovery:vanguard_us_probe"
    assert (
        provider_registry.get(ProviderFundFamily.ishares_us, "NOSUCH", session=session).status
        is ProviderFundStatus.miss
    )

    for _ in range(3):
        assert market_data.fetch_issuer_holdings("NOSUCH") is None
        assert market_data.fetch_vanguard_holdings("NOSUCH") is None
    assert len(issuer["get"]) == first, "a miss row must stop every further probe"


def test_a_failed_catalogue_is_not_a_verdict(
    session: Session, issuer: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outage writes NOTHING: no row, no miss — the next import asks again.

    The M141 discipline applied to coverage instead of quotes: only the
    issuer's own answer about the TICKER may retire the TICKER. A registry
    that recorded "SPY is not a Vanguard fund" because the issuer was down
    for ten minutes would be a coverage bug with no way to notice it.
    """
    _clean(session, ProviderFundFamily.vanguard_us, "FLAKY")

    def down(url: object = "", **kwargs: object) -> _Response:
        raise httpx.ConnectError("issuer down")

    monkeypatch.setattr(httpx, "get", down)
    assert market_data.fetch_vanguard_holdings("FLAKY") is None
    assert (
        provider_registry.get(ProviderFundFamily.vanguard_us, "FLAKY", session=session) is None
    ), "a network failure must not be recorded as a verdict about the fund"

    # The issuer comes back and the SAME ticker is picked up after all —
    # which is only possible because nothing was written to stop the probe.
    payload = {
        "holdingDetails": {
            "equityHoldings": [
                {
                    "securityType": "EQ.STOCK",
                    "securityLongDescription": "Recovered Corp",
                    "marketValuePercentage": "5.00%",
                    "ticker": "RCVR",
                    "isin": "US0000000001",
                }
            ]
        }
    }
    monkeypatch.setattr(httpx, "get", lambda url="", **kw: _Response(200, payload))
    assert market_data.fetch_vanguard_holdings("FLAKY")
    row = provider_registry.get(ProviderFundFamily.vanguard_us, "FLAKY", session=session)
    assert row is not None and row.status is ProviderFundStatus.verified


# --- 4. tenancy -------------------------------------------------------------


def test_registry_rows_are_operator_level_and_holdings_stay_per_user(
    session: Session, issuer
) -> None:
    """The card's tenancy test: the registry has NO `user_id` — one verdict
    answers every tenant — while `etf_holdings` stays strictly per-user."""
    assert "user_id" not in ProviderFundRegistry.__table__.c, (
        "provider_fund_registry is operator-level data like fx_rates: a "
        "shared verdict must not be duplicated per tenant"
    )

    def _user(tag: str) -> User:
        user = User(
            email=f"m168-{tag}-{datetime.now(UTC).timestamp()}@example.com", hashed_password="x"
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    alice, bob = _user("alice"), _user("bob")
    for user, symbol in ((alice, "AAA"), (bob, "BBB")):
        fund = Security(
            user_id=user.id,
            symbol=symbol,
            name=f"{symbol} fund",
            kind=SecurityKind.etf,
            listing_currency="CAD",
            exchange="TSX",
        )
        session.add(fund)
        session.commit()
        session.refresh(fund)
        session.add(
            EtfHolding(
                user_id=user.id,
                security_id=fund.id,
                holding_symbol="RY.TO",
                holding_name="Royal Bank",
                asset_class=EtfAssetClass.stock,
                weight_pct=Decimal("100"),
                source=EtfHoldingSource.manual,
            )
        )
    session.commit()

    # Alice's import discovers a fund; the verdict lands once, unattributed…
    _clean(session, ProviderFundFamily.vanguard_us, "NEWUS")
    assert market_data.fetch_vanguard_holdings("NEWUS") is not None
    # …and BOTH tenants read exactly that one row.
    assert provider_registry.get(ProviderFundFamily.vanguard_us, "NEWUS") is not None

    alice_rows = session.exec(select(EtfHolding).where(EtfHolding.user_id == alice.id)).all()
    bob_rows = session.exec(select(EtfHolding).where(EtfHolding.user_id == bob.id)).all()
    assert {row.holding_symbol for row in alice_rows} == {"RY.TO"}
    assert {row.holding_symbol for row in bob_rows} == {"RY.TO"}
    assert all(row.security_id != r.security_id for row in alice_rows for r in bob_rows), (
        "holdings are per-user; only the coverage verdict is shared"
    )


# --- 5. the maintenance surface --------------------------------------------


def test_cli_lists_sets_and_removes_rows(session: Session, capsys) -> None:
    from app import cli

    assert cli.main(["provider-funds", "list", "--family", "vanguard_ca", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert any(row["ticker"] == "VCN" and row["port_id"] == "9561" for row in listed)

    assert (
        cli.main(
            [
                "provider-funds",
                "set",
                "static_sector",
                "ZZCLI",
                "--sector",
                "Technology",
            ]
        )
        == 0
    )
    row = provider_registry.get(ProviderFundFamily.static_sector, "ZZCLI", session=session)
    assert row is not None and row.sector == "Technology"
    assert row.verified_by_run == "cli:set"

    # …and the reader sees it through the SectorMaps the breakdown uses.
    maps = SectorMaps.load(session)
    assert maps.static_sector("ZZCLI") == "Technology"

    assert cli.main(["provider-funds", "remove", "static_sector", "ZZCLI"]) == 0
    assert provider_registry.get(ProviderFundFamily.static_sector, "ZZCLI", session=session) is None
    assert cli.main(["provider-funds", "remove", "static_sector", "ZZCLI"]) == 1


def test_cli_reverify_only_touches_stale_verified_rows(session: Session, issuer) -> None:
    """The quarterly sweep: rows inside the window are left alone, rows past
    it are re-probed, and classification rows (no endpoint) are skipped."""
    from app import cli

    fresh = provider_registry.get(ProviderFundFamily.vanguard_us, "VXUS", session=session)
    assert fresh is not None
    # age the seed row past the window
    provider_registry.upsert(
        ProviderFundFamily.vanguard_us,
        "VXUS",
        status=ProviderFundStatus.verified,
        run="seed:0050",
        session=session,
    )
    session.execute(
        ProviderFundRegistry.__table__.update()
        .where(ProviderFundRegistry.__table__.c.ticker == "VXUS")
        .values(verified_at=datetime.now(UTC) - timedelta(days=200))
    )
    session.commit()

    stale = provider_registry.stale(older_than_days=180, session=session)
    assert ("vanguard_us", "VXUS") in {(str(r.family), r.ticker) for r in stale}
    assert all(str(r.family) in {"vanguard_us", "vanguard_ca", "ishares_us"} for r in stale), (
        "a classification row has no endpoint to re-probe"
    )

    assert (
        cli.main(
            [
                "provider-funds",
                "reverify",
                "--older-than-days",
                "180",
                "--family",
                "vanguard_us",
                "--dry-run",
            ]
        )
        == 0
    )

    # nothing stale in-window: the sweep says so and exits 0
    assert cli.main(["provider-funds", "reverify", "--older-than-days", "100000"]) == 0


def test_cli_verify_reasks_and_books_the_verdict(session: Session, issuer) -> None:
    """`verify` deletes the row first — asking AGAIN is the whole point — and
    the probe's verdict is what the next import will read."""
    from app import cli

    _clean(session, ProviderFundFamily.vanguard_us, "NEWUS")
    assert cli.main(["provider-funds", "verify", "vanguard_us", "NEWUS"]) == 0
    row = provider_registry.get(ProviderFundFamily.vanguard_us, "NEWUS", session=session)
    assert row is not None and row.status is ProviderFundStatus.verified
    assert row.verified_by_run == "cli:verify"

    assert cli.main(["provider-funds", "verify", "static_sector", "NEWUS"]) == 2

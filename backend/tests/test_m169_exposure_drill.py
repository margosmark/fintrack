"""M169 — the exposure drill-down: click a bucket, see what made it (docs/16).

Mark's ask on the back of M167's geography view: "neat would be to drill down
what security or fund is from that place and in the case of fund what holding(s)
if possible." The engine already attributed every dollar through M19's recursive
look-through; the browser just received the aggregate with the trail dropped.

Covers, in order: the wire contract (a `drill` block that appears ONLY when
asked, so pre-M169 consumers are byte-identical), the direct-stock case (the
position IS its own leaf), the 3-level chain (VEQT → VXUS → Microsoft, with
`via` naming the path), the totals identity at EVERY level (the anti-fabrication
pin — the drill must reconcile to the bucket it drills), the bounded drill
(top N + an honest `other`), the M166 estimate propagating into drill rows, the
404 envelope for a bucket that does not exist, and tenancy.
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
from app.services.sector_map import SectorMaps

API = "/api/v1"
BREAKDOWN_URL = f"{API}/portfolio/breakdown"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drill is aggregation over the SAME cached rows (docs/07) — any fetch
    attempt fails loudly."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network fetch attempted during breakdown drill")

    monkeypatch.setattr(market_data, "fetch_latest_close", forbidden)
    monkeypatch.setattr(market_data, "fetch_history", forbidden)
    monkeypatch.setattr(market_data, "fetch_fundamentals", forbidden)
    monkeypatch.setattr(fx_service, "fetch_valet_series", forbidden)


def _today() -> date:
    return datetime.now(UTC).date()


def _new_user(client: TestClient) -> tuple[dict[str, str], str]:
    email = f"m169-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
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
    session: Session,
    user_id: int,
    symbol: str,
    kind: SecurityKind | str = SecurityKind.stock,
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


def _fundamentals(client: TestClient, headers: dict, security_id: int, **fields) -> None:
    body = client.put(
        f"{API}/securities/{security_id}/fundamentals",
        json={"source": "manual", **fields},
        headers=headers,
    )
    assert body.status_code == 200, body.text


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
                holding_security_id=row.get("holding_security_id"),
                asset_class=row.get("asset_class", EtfAssetClass.stock),
                country=row.get("country"),
                sector=row.get("sector"),
                region=row.get("region"),
                weight_pct=Decimal(row["weight_pct"]),
                source=source,
            )
        )
    session.commit()


def _drill(client: TestClient, headers: dict, dimension: str, key: str) -> dict:
    body = client.get(
        BREAKDOWN_URL,
        params={"dimension": dimension, "drill": key},
        headers=headers,
    )
    assert body.status_code == 200, body.text
    return body.json()["drill"]


def _contribution(drill: dict, symbol: str) -> dict:
    return next(row for row in drill["contributions"] if row["symbol"] == symbol)


def _holding(contribution: dict, symbol: str) -> dict:
    return next(row for row in contribution["holdings"] if row["symbol"] == symbol)


# --- the wire contract ---------------------------------------------------------


def test_no_drill_param_leaves_the_payload_shape_untouched(
    client: TestClient, session: Session
) -> None:
    """Pre-M169 consumers must not see a new key: the drill is opt-in, so a
    caller that never asks for it gets the exact payload it always got."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    stock = _security(session, user_id, "SHOP")
    _buy(session, user_id, account, stock, "10", "50")

    plain = client.get(BREAKDOWN_URL, params={"dimension": "geography"}, headers=headers).json()
    assert set(plain) == {
        "dimension",
        "items",
        "total_cad",
        "unclassified_cad",
        "estimated_cad",
        "unclassified_funds",
    }
    assert "drill" not in plain


def test_drill_block_carries_the_bucket_identity_it_drills(
    client: TestClient, session: Session
) -> None:
    """The drill names the bucket it describes and repeats the bucket's OWN
    figures — the client renders the server's numbers, never re-adds rows."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    stock = _security(session, user_id, "SHOP")
    _buy(session, user_id, account, stock, "10", "50")  # mv 500
    _fundamentals(client, headers, stock.id, sector="Technology", country="Canada")

    drill = _drill(client, headers, "geography", "Canada")
    assert drill["key"] == "Canada"
    assert drill["dimension"] == "geography"
    assert drill["bucket_market_value_cad"] == "500.00"
    assert drill["contributions_cad"] == "500.00"
    assert drill["bucket_weight_pct"] == 100.0
    assert drill["depth_cap"] == 3


def test_unknown_drill_key_is_a_404_envelope_not_an_empty_drill(
    client: TestClient,
) -> None:
    """An empty drill would read as "nothing holds this" — a different, and
    false, claim from "that bucket is not in this dimension"."""
    headers, _ = _new_user(client)
    body = client.get(
        BREAKDOWN_URL,
        params={"dimension": "geography", "drill": "Narnia"},
        headers=headers,
    )
    assert body.status_code == 404, body.text
    assert body.json()["code"] == "drill_bucket_not_found"


def test_drill_key_must_exist_in_its_own_dimension(client: TestClient, session: Session) -> None:
    """`stock` is an asset-class bucket; drilling geography for it is the same
    404, because (dimension, key) is the address — not the key alone."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    stock = _security(session, user_id, "SHOP")
    _buy(session, user_id, account, stock, "10", "50")

    assert (
        client.get(
            BREAKDOWN_URL,
            params={"dimension": "geography", "drill": "stock"},
            headers=headers,
        ).status_code
        == 404
    )
    assert _drill(client, headers, "asset_class", "stock")["key"] == "stock"


# --- direct stock: the position IS the leaf ------------------------------------


def test_direct_stock_drills_to_itself_with_no_chain(client: TestClient, session: Session) -> None:
    """A stock you hold directly is one contribution that IS its own leaf:
    no `via`, no nested holdings (nesting it under itself would print the
    same dollar twice)."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    stock = _security(session, user_id, "SHOP")
    _buy(session, user_id, account, stock, "10", "50")
    _fundamentals(client, headers, stock.id, sector="Technology", country="Canada")

    drill = _drill(client, headers, "geography", "Canada")
    assert len(drill["contributions"]) == 1
    row = drill["contributions"][0]
    assert row["position_kind"] == "direct"
    assert row["symbol"] == "SHOP"
    assert row["security_id"] == stock.id
    assert row["market_value_cad"] == "500.00"
    assert row["weight_pct"] == 100.0
    assert row["via"] == []
    assert row["depth"] == 0
    assert row["holdings"] == []
    assert row["holdings_other"] is None
    # A position has no composition row, so it has no provider provenance.
    assert row["provenance_source"] is None
    assert row["native_weight_share"] is None
    assert row["estimated"] is False
    assert row["unattributed"] is False


def test_direct_stock_drill_names_the_accounts_holding_it(
    client: TestClient, session: Session
) -> None:
    """One security in two accounts is ONE contribution with both accounts —
    the position was grouped exactly once (M19's rule), and the drill says
    where the money sits."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    tfsa = _account(session, user_id, "TFSA")
    rrsp = _account(session, user_id, "RRSP")
    stock = _security(session, user_id, "SHOP")
    _buy(session, user_id, tfsa, stock, "5", "50")
    _buy(session, user_id, rrsp, stock, "5", "50")
    _fundamentals(client, headers, stock.id, sector="Technology", country="Canada")

    drill = _drill(client, headers, "geography", "Canada")
    assert len(drill["contributions"]) == 1
    row = drill["contributions"][0]
    assert row["account_ids"] == sorted([tfsa.id, rrsp.id])
    assert row["account_count"] == 2
    assert row["market_value_cad"] == "500.00"


# --- the 3-level chain ---------------------------------------------------------


def _three_level_portfolio(client: TestClient, session: Session) -> tuple[dict, Security]:
    """VEQT 1000 → VXUS 60% + RY 30% (residual 10%); VXUS → Microsoft 50% +
    cash 50%. Microsoft is reached TWO funds down — the chain case Mark asked
    about by name."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    veqt = _security(session, user_id, "VEQT", kind=SecurityKind.etf)
    _buy(session, user_id, account, veqt, "10", "100")  # mv 1000
    _rows(
        session,
        user_id,
        veqt,
        [
            {"holding_symbol": "VXUS", "holding_name": "VXUS Intl", "weight_pct": "60"},
            {
                "holding_symbol": "RY",
                "holding_name": "Royal Bank",
                "country": "Canada",
                "weight_pct": "30",
            },
        ],
    )
    vxus = _security(session, user_id, "VXUS", kind=SecurityKind.etf)
    _rows(
        session,
        user_id,
        vxus,
        [
            {
                "holding_symbol": "MSFT",
                "holding_name": "Microsoft",
                "country": "United States",
                "weight_pct": "50",
            },
            {"holding_symbol": "ZZCASH", "asset_class": EtfAssetClass.cash, "weight_pct": "50"},
        ],
    )
    return headers, veqt


def test_three_level_chain_names_the_path_not_just_the_leaf(
    client: TestClient, session: Session
) -> None:
    """VEQT → VXUS → Microsoft must read `via: ["VEQT", "VXUS"]`. The leaf is
    named AND the path is named, so the payload can say "your $x reaches
    Microsoft of the USA via VEQT→VXUS" instead of implying you hold it."""
    headers, _ = _three_level_portfolio(client, session)

    usa = _drill(client, headers, "geography", "United States")
    assert len(usa["contributions"]) == 1
    veqt = usa["contributions"][0]
    assert veqt["symbol"] == "VEQT"
    assert veqt["position_kind"] == "fund"
    assert veqt["via"] == []
    assert veqt["depth"] == 0

    msft = _holding(veqt, "MSFT")
    assert msft["via"] == ["VEQT", "VXUS"]
    assert msft["depth"] == 2
    assert msft["position_kind"] == "direct"
    assert msft["market_value_cad"] == "300.00"  # 1000 x 60% x 50%
    assert msft["native_weight_share"] == "50.00"  # the weight VXUS published

    canada = _drill(client, headers, "geography", "Canada")
    ry = _holding(_contribution(canada, "VEQT"), "RY")
    assert ry["via"] == ["VEQT"]  # ONE hop — held through the fund directly
    assert ry["depth"] == 1
    assert ry["market_value_cad"] == "300.00"  # 1000 x 30%
    assert ry["native_weight_share"] == "30.00"


def test_depth_cap_is_disclosed_and_the_deepest_fund_is_a_sub_fund(
    client: TestClient, session: Session
) -> None:
    """At MAX_DEPTH the walk stops and names the fund it stopped at — the
    level-4 fund is a `sub_fund` leaf, not a company, and `depth_cap` says
    how far the walk goes so the UI's "3 levels max" copy has a source."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    chain = {
        name: _security(session, user_id, name, kind=SecurityKind.etf)
        for name in ("L1", "L2", "L3", "L4")
    }
    _buy(session, user_id, account, chain["L1"], "10", "100")  # mv 1000
    for outer, inner in (("L1", "L2"), ("L2", "L3"), ("L3", "L4")):
        _rows(
            session,
            user_id,
            chain[outer],
            [{"holding_symbol": inner, "weight_pct": "100"}],
        )
    _rows(
        session,
        user_id,
        chain["L4"],
        [{"holding_symbol": "DDD", "country": "Canada", "weight_pct": "100"}],
    )

    drill = _drill(client, headers, "company", "L4")
    assert drill["depth_cap"] == 3
    # L1 is the position; L4 is the LEAF the walk stopped at, three funds
    # down. `via` names every fund the money passed through.
    l1 = drill["contributions"][0]
    assert l1["symbol"] == "L1"
    assert l1["position_kind"] == "fund"
    assert l1["via"] == []
    leaf = l1["holdings"][0]
    assert leaf["symbol"] == "L4"
    assert leaf["position_kind"] == "sub_fund"  # a FUND, not a company
    assert leaf["via"] == ["L1", "L2", "L3"]
    assert leaf["depth"] == 3
    assert leaf["market_value_cad"] == "1000.00"


def test_a_fund_the_walk_stopped_at_is_named_sub_fund_not_company(
    client: TestClient, session: Session
) -> None:
    """A back-edge is a leaf (M19's cycle guard) — and the payload calls it a
    `sub_fund`, because the money stopped at a FUND, not at a company. The
    `via` still names the path it took to get there."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "XCYC", kind=SecurityKind.etf)
    sub = _security(session, user_id, "SUBC", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "10", "100")  # mv 1000
    _rows(session, user_id, fund, [{"holding_symbol": "SUBC", "weight_pct": "60"}])
    _rows(
        session,
        user_id,
        sub,
        [
            {"holding_symbol": "XCYC", "weight_pct": "50"},
            {"holding_symbol": "XXX", "country": "Canada", "weight_pct": "30"},
        ],
    )

    drill = _drill(client, headers, "company", "XCYC")
    # The position put 600 into SUBC; SUBC's rows sum to 80 against a
    # denominator of 100, so the back-edge carries 600 x 50/100 = 300.
    contribution = drill["contributions"][0]
    assert contribution["symbol"] == "XCYC"
    assert contribution["position_kind"] == "fund"
    assert contribution["market_value_cad"] == "300.00"

    leaf = contribution["holdings"][0]
    assert leaf["symbol"] == "XCYC"
    assert leaf["position_kind"] == "sub_fund"  # a FUND stopped here, not a company
    # The money really did go XCYC → SUBC → back to XCYC; `via` names the
    # path it took, including the hop that closed the loop.
    assert leaf["via"] == ["XCYC", "SUBC"]
    assert leaf["depth"] == 2
    assert leaf["native_weight_share"] == "50.00"
    assert leaf["market_value_cad"] == "300.00"


def test_totals_identity_at_every_level(client: TestClient, session: Session) -> None:
    """The anti-fabrication pin (M91-grade): Σ contributions == the bucket,
    Σ a fund's holdings == that fund's contribution, and each leaf's value is
    the share of its parent the walk actually computed. The drill is built
    from the SAME trails as the bucket, so this holds by construction — the
    test is what keeps it true after the next refactor."""
    headers, _ = _three_level_portfolio(client, session)

    for dimension, key in (
        ("geography", "Canada"),
        ("geography", "United States"),
        ("geography", "Unclassified"),
        ("company", "MSFT"),
        ("asset_class", "cash"),
    ):
        drill = _drill(client, headers, dimension, key)
        bucket = drill["bucket_market_value_cad"]
        # Level 1: contributions + the folded tail == the bucket, in cents.
        shown = sum(Decimal(row["market_value_cad"]) for row in drill["contributions"])
        if drill["other"]:
            shown += Decimal(drill["other"]["market_value_cad"])
        assert shown == Decimal(bucket), f"{dimension}/{key} contributions"

        # Level 2: each fund's holdings + its tail == that fund's contribution.
        for contribution in drill["contributions"]:
            nested = sum(Decimal(row["market_value_cad"]) for row in contribution["holdings"])
            if contribution["holdings_other"]:
                nested += Decimal(contribution["holdings_other"]["market_value_cad"])
            assert nested == Decimal(contribution["market_value_cad"]), (
                f"{dimension}/{key} holdings of {contribution['symbol']}"
            )

        # Every percentage on the wire describes the bucket it sits in.
        for row in drill["contributions"]:
            expected = (
                float(
                    (Decimal(row["market_value_cad"]) / Decimal(bucket) * 100).quantize(
                        Decimal("0.01")
                    )
                )
                if Decimal(bucket) > 0
                else 0.0
            )
            assert row["weight_pct"] == expected, f"{dimension}/{key} weight"


def test_the_drill_never_changes_the_aggregate_it_drills(
    client: TestClient, session: Session
) -> None:
    """Asking for a drill must not move a single cent of the breakdown: the
    items and totals are identical with and without it."""
    headers, _ = _three_level_portfolio(client, session)

    for dimension in ("geography", "sector", "asset_class", "company"):
        plain = client.get(BREAKDOWN_URL, params={"dimension": dimension}, headers=headers).json()
        drilled = client.get(
            BREAKDOWN_URL, params={"dimension": dimension, "drill": "Unclassified"}, headers=headers
        ).json()
        assert plain["items"] == drilled["items"], dimension
        assert plain["total_cad"] == drilled["total_cad"], dimension
        assert plain["unclassified_cad"] == drilled["unclassified_cad"], dimension
        assert plain["estimated_cad"] == drilled["estimated_cad"], dimension


# --- bounded drill --------------------------------------------------------------


def test_drill_is_bounded_with_an_honest_other_tail(client: TestClient, session: Session) -> None:
    """A drill must not infinite-scroll: the top DRILL_N stay, and the tail is
    ONE block carrying its exact cent-sum and its count — M167's truncation
    language, on the wire."""
    from app.services.exposure_breakdown import DRILL_N

    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "XBIG", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "100", "100")  # mv 10000
    # 30 Canadian holdings, $1000 apart, so the sort order is unambiguous.
    _rows(
        session,
        user_id,
        fund,
        [
            {
                "holding_symbol": f"C{i:02d}",
                "holding_name": f"Canadian Co {i}",
                "country": "Canada",
                "weight_pct": "1",
            }
            for i in range(30)
        ],
    )

    # 30 rows x 1% against a denominator of max(30, 100) = 100 → $100 each,
    # so Canada holds $3,000 of the $10,000 position.
    drill = _drill(client, headers, "geography", "Canada")
    assert drill["bucket_market_value_cad"] == "3000.00"
    contribution = drill["contributions"][0]
    assert contribution["symbol"] == "XBIG"
    assert contribution["market_value_cad"] == "3000.00"
    assert len(contribution["holdings"]) == DRILL_N
    assert contribution["holdings"][0]["symbol"] == "C29"  # value desc, ties by symbol
    assert contribution["holdings_other"] == {
        "count": 30 - DRILL_N,
        "market_value_cad": "500.00",
        "weight_pct": 16.67,
    }
    # The head and the tail still add up to the contribution exactly.
    head = sum(Decimal(row["market_value_cad"]) for row in contribution["holdings"])
    assert head + Decimal(contribution["holdings_other"]["market_value_cad"]) == Decimal(
        contribution["market_value_cad"]
    )


def test_a_drill_that_fits_reports_no_tail(client: TestClient, session: Session) -> None:
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "XSM", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "1", "1000")
    _rows(
        session,
        user_id,
        fund,
        [{"holding_symbol": "RY", "country": "Canada", "weight_pct": "100"}],
    )

    drill = _drill(client, headers, "geography", "Canada")
    assert drill["other"] is None
    assert drill["contributions"][0]["holdings_other"] is None


# --- provenance -----------------------------------------------------------------


def test_every_drill_leaf_carries_its_row_source(client: TestClient, session: Session) -> None:
    """M149's provenance, one level down: the UI's "issuer CSV" / "top
    holdings" badge has a per-row source to read, taken from the row that
    actually supplied the weights."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "XPROV", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "1", "1000")
    _rows(
        session,
        user_id,
        fund,
        [
            {"holding_symbol": "RY", "country": "Canada", "weight_pct": "60"},
            {"holding_symbol": "TD", "country": "Canada", "weight_pct": "40"},
        ],
        source=EtfHoldingSource.issuer,
    )

    contribution = _drill(client, headers, "geography", "Canada")["contributions"][0]
    for symbol in ("RY", "TD"):
        assert _holding(contribution, symbol)["provenance_source"] == "issuer"


# --- M166 estimate propagation ---------------------------------------------------


def test_the_published_mix_estimate_propagates_into_the_drill_rows(
    client: TestClient, session: Session
) -> None:
    """VOO's rows name no sector, so its unattributable share is split across
    Vanguard's PUBLISHED mix (M166). The drill must carry that flag on the row
    that speaks for it — the fund itself, since no holding does."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "VOO", kind=SecurityKind.etf)
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

    # M168: the published mixes are rows in provider_fund_registry now, so the
    # snapshot is loaded through the test's own session instead of read from a
    # module constant that no longer exists.
    mix = SectorMaps.load(session).mix_rows["VOO"]
    mix_total = sum(mix.values())
    technology = (Decimal("600") * mix["Technology"] / mix_total).quantize(Decimal("0.01"))

    drill = _drill(client, headers, "sector", "Technology")
    row = drill["contributions"][0]
    assert row["symbol"] == "VOO"
    assert row["position_kind"] == "fund"
    assert row["estimated"] is True
    assert Decimal(row["estimated_cad"]) == technology
    # The estimate is a slice OF the contribution, so it is part of the total.
    assert Decimal(row["market_value_cad"]) == technology
    # …and it reconciles to the bucket the item above it reports.
    assert drill["bucket_market_value_cad"] == str(technology)
    # The row that speaks for the estimate is the FUND — no holding produced it.
    assert len(row["holdings"]) == 1
    estimate = row["holdings"][0]
    assert estimate["symbol"] == "VOO"
    assert estimate["estimated"] is True
    assert estimate["provenance_source"] is None
    assert estimate["native_weight_share"] is None
    # The cash leg has no GICS sector and never reaches the mix.
    assert drill["contributions"][0]["estimated_cad"] == str(technology)


def test_the_residual_is_reported_as_unattributed_not_dropped(
    client: TestClient, session: Session
) -> None:
    """The denominator rule leaves a real residual in Unclassified. The drill
    must show it as an `unattributed` slice of the fund that produced it, so
    drilling Unclassified still reconciles to the cent."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "XRES", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "1", "1000")
    _rows(
        session,
        user_id,
        fund,
        [
            {"holding_symbol": "RY", "country": "Canada", "weight_pct": "60"},
            {"holding_symbol": "TD", "country": "Canada", "weight_pct": "30"},
        ],
    )  # rows sum to 90 → the remaining 10% lands in Unclassified

    drill = _drill(client, headers, "geography", "Unclassified")
    row = drill["contributions"][0]
    assert row["symbol"] == "XRES"
    assert row["position_kind"] == "fund"
    assert row["unattributed"] is True
    assert row["market_value_cad"] == "100.00"
    # The residual has no ROW behind it, so the row that speaks for it is the
    # fund itself — and it says why it is not a holding.
    assert len(row["holdings"]) == 1
    residual = row["holdings"][0]
    assert residual["symbol"] == "XRES"
    assert residual["unattributed"] is True
    assert residual["estimated"] is False
    assert residual["provenance_source"] is None
    assert residual["market_value_cad"] == "100.00"
    # The named holdings went to Canada, not here.
    assert (
        _drill(client, headers, "geography", "Canada")["contributions"][0]["holdings"][0]["symbol"]
        == "RY"
    )


def test_a_fund_with_no_rows_reports_its_whole_position_as_unattributed(
    client: TestClient, session: Session
) -> None:
    """M147's story, one level down: a fund with NO composition rows has
    nothing to drill into, so the drill says so instead of showing a hole."""
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    fund = _security(session, user_id, "XEMPTY", kind=SecurityKind.etf)
    _buy(session, user_id, account, fund, "1", "1000")

    drill = _drill(client, headers, "geography", "Unclassified")
    row = drill["contributions"][0]
    assert row["symbol"] == "XEMPTY"
    assert row["position_kind"] == "fund"
    assert row["unattributed"] is True
    assert row["market_value_cad"] == "1000.00"
    assert row["holdings"] == []


# --- tenancy --------------------------------------------------------------------


def test_a_drill_is_scoped_to_its_own_tenant(client: TestClient, session: Session) -> None:
    headers, email = _new_user(client)
    user_id = _uid(session, email)
    account = _account(session, user_id, "Brokerage")
    stock = _security(session, user_id, "SHOP")
    _buy(session, user_id, account, stock, "10", "50")
    _fundamentals(client, headers, stock.id, sector="Technology", country="Canada")

    other_headers, other_email = _new_user(client)
    other_user = _uid(session, other_email)
    other_account = _account(session, other_user, "Other Brokerage")
    other_stock = _security(session, other_user, "RY")
    _buy(session, other_user, other_account, other_stock, "1", "150")
    _fundamentals(client, other_headers, other_stock.id, sector="Financials", country="Canada")

    mine = _drill(client, headers, "geography", "Canada")
    assert [row["symbol"] for row in mine["contributions"]] == ["SHOP"]
    theirs = _drill(client, other_headers, "geography", "Canada")
    assert [row["symbol"] for row in theirs["contributions"]] == ["RY"]

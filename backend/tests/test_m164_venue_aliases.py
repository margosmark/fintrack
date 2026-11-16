"""M164 — one venue, one spelling (docs/07 §Venue aliases, docs/02 §securities).

`uq_securities_symbol UNIQUE (user_id, symbol, exchange)` makes a second
spelling of ONE venue a second row for ONE listing — and the provider really
does spell a venue two ways. LIVE-PROBED 2026-10-11 from the api container:

* `600900.SS` answered `SHH`/"Shanghai" on one call and the prod row 2894
  `603986.SS` was stored under `SHSE`; `PNDORA.CO` answered `CPH`/"Copenhagen"
  while prod row 1721 stores `OCSE`;
* a bare `7182` answers `TOKY1` "Japan Post Bank Co Ltd" while `7182.T` answers
  `JPX`/"Tokyo" for the SAME company (`2454`/`2454.T`, `1299`/`1299.HK` the
  same way).

Those twins are invisible to `security_dedup`'s raw-key grouping, so they
survive forever and — because `security_symbol_index` counts two rows sharing
a stem as two LISTINGS — every bare-stem fund leg behind them stays
`holding_security_id = NULL` on an ambiguity that is not one (prod: tenant
343's `600900` and `PNDORA` legs, the 10 this card exists for).

`services/venue_codes.py` owns the table; this module pins:

* the table is per PAIR of venue codes and is never derived from a symbol —
  `9503` bare is SAUDI "Baazeem Trading Co" while `9503.T` is JPX "KANSAI
  ELECTRIC POWER", so an alias of one numeric stem would merge two companies;
* the dedup merges alias twins with the machinery it already had (keeper =
  most price history, ACB refusal) and rewrites the survivor to the canonical
  code, idempotently;
* the operator relink (`services/holding_relink.py`, the M154 SQL made
  re-runnable) links the legs those twins were blocking — and still never
  guesses a genuinely shared stem.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlmodel import select

from app.models.acb_pools import AcbPool
from app.models.etf_holdings import EtfHolding, EtfHoldingSource
from app.models.prices import Price, PriceSource
from app.models.securities import Security, SecurityKind
from app.models.users import User
from app.services import holding_relink
from app.services.holding_relink import relink_holdings
from app.services.security_dedup import repair_securities
from app.services.venue_codes import VENUE_ALIASES, canonical_exchange

API = "/api/v1"
D = Decimal


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m164-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _user_and_account(client: TestClient, session) -> tuple[User, int]:
    headers = _auth(client)
    uid = client.get(f"{API}/users/me", headers=headers).json()["id"]
    user = session.get(User, uid)
    assert user is not None
    account = client.post(
        f"{API}/accounts",
        json={"name": "Margin", "type": "brokerage_non_registered", "currency": "CAD"},
        headers=headers,
    )
    assert account.status_code == 201, account.text
    return user, account.json()["id"]


def _seed_security(
    session, user: User, symbol: str, exchange: str, *, name: str | None = None
) -> Security:
    row = Security(
        user_id=user.id,
        symbol=symbol,
        name=name or symbol,
        kind=SecurityKind.stock,
        listing_currency="USD",
        exchange=exchange,
    )
    session.add(row)
    session.flush()
    return row


def _seed_price(session, user: User, security: Security, day: date, value: str) -> None:
    session.add(
        Price(
            user_id=user.id,
            security_id=security.id,
            date=day,
            price=D(value),
            source=PriceSource.yfinance,
        )
    )


def _seed_leg(session, user: User, fund: Security, symbol: str, weight: str) -> EtfHolding:
    row = EtfHolding(
        user_id=user.id,
        security_id=fund.id,
        holding_symbol=symbol,
        holding_name=symbol,
        weight_pct=D(weight),
        source=EtfHoldingSource.vanguard,
    )
    session.add(row)
    session.flush()
    return row


# --- the ruling: the table --------------------------------------------------


def test_the_alias_table_is_per_pair_and_carries_its_evidence() -> None:
    """Every alias maps to ONE canonical code, and the falsifying pairs map to
    DIFFERENT canonicals — which is what keeps `9503`/SAUDI and `9503.T`/JPX
    (Baazeem vs Kansai Electric) apart without any symbol reasoning."""
    # the pairs this card measured (prod rows 2555/2705 and 1721/2502)
    assert canonical_exchange("SHSE") == "SHH"
    assert canonical_exchange("OCSE") == "CPH"
    # the bare-ticker microsite codes the provider answers for the same listing
    assert canonical_exchange("TOKY1") == "JPX"
    assert canonical_exchange("JASDQ") == "JPX"
    assert canonical_exchange("HONGK") == "HKG"
    # M142's Canadian convention, moved into the same table unchanged
    assert {alias for alias, canonical in VENUE_ALIASES.items() if canonical == "TSX"} == {
        "TOR",
        "TSE",
        "XTSE",
        "XTSX",
        "TSXV",
    }
    # normalization still happens here, and an unknown code is never guessed at
    assert canonical_exchange(" shh ") == "SHH"
    assert canonical_exchange("SHH") == "SHH"
    assert canonical_exchange("") == ""
    assert canonical_exchange(None) == ""
    for unknown in ("NMS", "NYQ", "SAUDI", "JSD", "CHIX", "CPH", "JPX"):
        assert canonical_exchange(unknown) == unknown


def test_provider_payloads_are_written_with_the_canonical_code() -> None:
    """The write seam: `lookup_symbol`'s payload must carry the spelling the
    table calls canonical, or the next provider run creates the twin again."""
    import app.services.market_data as market_data

    assert market_data._normalize_exchange("SHSE") == "SHH"
    assert market_data._normalize_exchange("ocse") == "CPH"
    assert market_data._normalize_exchange("TOKY1") == "JPX"
    # M142 behaviour, unchanged
    assert market_data._normalize_exchange("TOR") == "TSX"
    assert market_data._normalize_exchange("nms") == "NMS"


# --- the repair ------------------------------------------------------------


def test_alias_twins_merge_repoint_and_keep_the_canonical_code(client: TestClient, session) -> None:
    """Prod shape: one `600900.SS` row spelled `SHH`, one spelled `SHSE`."""
    user, _account_id = _user_and_account(client, session)
    shh = _seed_security(session, user, "600900.SS", "SHH", name="CHINA YANGTZE POWER CO")
    shse = _seed_security(session, user, "600900.SS", "SHSE", name="China Yangtze Power Co Ltd")
    # The SHSE twin carries the price history, so it is the keeper by the
    # rule that has always chosen one — the spelling is not what wins.
    _seed_price(session, user, shse, date(2026, 10, 8), "28.41")
    _seed_price(session, user, shse, date(2026, 10, 9), "28.90")
    _seed_price(session, user, shse, date(2026, 10, 10), "29.02")
    _seed_price(session, user, shh, date(2026, 10, 10), "29.01")
    session.flush()

    report = repair_securities(session)

    assert report.merged == [(shse.id, (shh.id,))]
    assert report.canonicalized == [(shse.id, "SHSE", "SHH")]
    assert session.get(Security, shh.id) is None
    survivor = session.get(Security, shse.id)
    assert survivor is not None
    assert survivor.exchange == "SHH"  # the canonical spelling, on the keeper
    assert survivor.symbol == "600900.SS"
    # Keeper's close wins the contested date; the loser's copy is dropped.
    prices = {
        row.date: row.price
        for row in session.exec(select(Price).where(Price.security_id == shse.id)).all()
    }
    assert prices == {
        date(2026, 10, 8): D("28.41"),
        date(2026, 10, 9): D("28.90"),
        date(2026, 10, 10): D("29.02"),
    }

    # Idempotent: the survivor now owns the canonical code, so nothing collides.
    again = repair_securities(session)
    assert again.done, again.summary()
    assert again.merged == [] and again.canonicalized == []


def test_the_canonical_rewrite_only_touches_a_group_that_merged(
    client: TestClient, session
) -> None:
    """A lone row with an unusual venue code is the user's own statement of
    venue — the repair normalizes its case and never re-spells it."""
    user, _account_id = _user_and_account(client, session)
    lone = _seed_security(session, user, " 1299 ", "hongk", name="AIA Group Ltd")

    report = repair_securities(session)

    assert report.merged == [] and report.canonicalized == []
    assert (lone.symbol, lone.exchange) == ("1299", "HONGK")
    assert report.normalized == 1


def test_two_genuinely_different_venues_are_never_merged(client: TestClient, session) -> None:
    """The card's danger case plus the falsifying probe: the alias map is per
    pair of venue codes, so a shared SYMBOL (or stem) merges nothing."""
    user, _account_id = _user_and_account(client, session)
    # SHOP on two real venues — the card's own example
    shop_nyse = _seed_security(session, user, "SHOP", "NYSE", name="Shopify Inc.")
    shop_tsx = _seed_security(session, user, "SHOP", "TSX", name="SHOPIFY INC")
    # the live probe: 9503 bare is SAUDI Baazeem, 9503.T is JPX Kansai Electric
    saudi = _seed_security(session, user, "9503", "SAUDI", name="Baazeem Trading Co")
    kansai = _seed_security(session, user, "9503.T", "JPX", name="KANSAI ELECTRIC POWER CO INC")
    # a Canadian fund and its US-listed namesake (docs/05 twin rule)
    viu = _seed_security(session, user, "VIU", "NMS", name="Vanguard FTSE Developed Asia")
    viu_to = _seed_security(session, user, "VIU.TO", "TSX", name="BMO VIU")

    report = repair_securities(session)

    assert report.merged == []
    assert report.canonicalized == []
    for row in (shop_nyse, shop_tsx, saudi, kansai, viu, viu_to):
        assert session.get(Security, row.id) is not None


def test_the_acb_refusal_still_wins_over_an_alias_merge(client: TestClient, session) -> None:
    """Two ACB pools on one account+security mean cost basis was accumulated
    twice — an alias proves the rows are one listing, it does not make choosing
    a winner safe (docs/06)."""
    user, account_id = _user_and_account(client, session)
    shh = _seed_security(session, user, "600900.SS", "SHH")
    shse = _seed_security(session, user, "600900.SS", "SHSE")
    for security in (shh, shse):
        session.add(
            AcbPool(
                user_id=user.id,
                account_id=account_id,
                security_id=security.id,
                quantity=D("10"),
                total_acb_cad=D("100"),
            )
        )
    session.flush()

    report = repair_securities(session)

    assert report.merged == []
    assert len(report.skipped) == 1
    assert "acb_pools" in report.skipped[0][2]
    # Neither row is touched — no delete, and no canonical rewrite either.
    assert report.canonicalized == []
    assert session.get(Security, shh.id) is not None
    assert session.get(Security, shse.id) is not None


# --- the relink ------------------------------------------------------------


def test_a_bare_leg_links_once_the_alias_twin_is_merged(client: TestClient, session) -> None:
    """THE card pin: tenant 343's `600900` legs are NULL behind an ambiguity
    that is only two spellings of Shanghai. The repair removes the twin, the
    stem becomes unambiguous, the relink links the leg."""
    user, _account_id = _user_and_account(client, session)
    fund = _seed_security(session, user, "XEC.TO", "TSX", name="iShares Core MSCI Emerging Mkts")
    shh = _seed_security(session, user, "600900.SS", "SHH", name="CHINA YANGTZE POWER CO")
    shse = _seed_security(session, user, "600900.SS", "SHSE", name="China Yangtze Power Co Ltd")
    leg = _seed_leg(session, user, fund, "600900", "6.4428")
    _seed_price(session, user, shse, date(2026, 10, 10), "29.02")
    session.flush()

    # Before: two rows share the stem, so the index refuses to guess.
    before = relink_holdings(session)
    assert before.linked == []
    assert leg.holding_security_id is None

    repair_securities(session)

    after = relink_holdings(session, apply=True)
    assert after.linked == [(leg.id, "600900", shse.id, "600900.SS")]
    session.refresh(leg)
    assert leg.holding_security_id == shse.id
    assert leg.weight_pct == D("6.4428")  # linking sets an FK, never a weight
    assert session.get(Security, shh.id) is None

    # Idempotent: the leg is linked, so a second pass has nothing to do.
    assert relink_holdings(session).done


def test_the_relink_never_guesses_a_shared_stem_and_a_dry_run_writes_nothing(
    client: TestClient, session
) -> None:
    """M154's rule, applied by the operator half: the EXACT spelling wins, an
    unambiguous stem answers, an ambiguous one never does."""
    user, _account_id = _user_and_account(client, session)
    fund = _seed_security(session, user, "VEQT.TO", "TSX")
    # exact spelling beats the suffixed twin (docs/05 twin rule)
    bare = _seed_security(session, user, "VUN", "NMS")
    qualified = _seed_security(session, user, "VUN.TO", "TSX")
    # one unambiguous suffixed row answers for its own bare stem
    samsung = _seed_security(session, user, "005930.KS", "KSC")
    kq = _seed_security(session, user, "005930.KQ", "KOE")
    exact_leg = _seed_leg(session, user, fund, "VUN", "1.0000")
    stem_leg = _seed_leg(session, user, fund, "RY", "2.0000")
    ry_to = _seed_security(session, user, "RY.TO", "TSX")
    ambiguous_leg = _seed_leg(session, user, fund, "005930", "3.0000")
    nonsense_leg = _seed_leg(session, user, fund, "NOSUCH", "4.0000")
    session.flush()

    dry = relink_holdings(session)

    assert dry.linked == [
        (exact_leg.id, "VUN", bare.id, "VUN"),
        (stem_leg.id, "RY", ry_to.id, "RY.TO"),
    ]
    # A dry run writes NOTHING.
    for leg in (exact_leg, stem_leg, ambiguous_leg, nonsense_leg):
        session.refresh(leg)
        assert leg.holding_security_id is None

    applied = relink_holdings(session, apply=True)
    assert applied.linked == dry.linked
    session.refresh(exact_leg)
    session.refresh(stem_leg)
    session.refresh(ambiguous_leg)
    assert exact_leg.holding_security_id == bare.id  # exact spelling wins
    assert stem_leg.holding_security_id == ry_to.id
    assert ambiguous_leg.holding_security_id is None  # .KS + .KQ = two listings
    assert session.get(Security, qualified.id) is not None
    assert session.get(Security, kq.id) is not None
    assert session.get(Security, samsung.id) is not None


def test_the_operator_script_exists_and_is_the_service_thin_cli() -> None:
    """`scripts/relink_holdings.py` is the re-runnable half of the M154 relink
    (it used to be ad-hoc SQL nobody could run twice)."""
    from pathlib import Path

    script = Path(holding_relink.__file__).parents[2] / "scripts" / "relink_holdings.py"
    text = script.read_text()
    assert "relink_holdings" in text
    assert "--apply" in text
    assert "weight_pct" in text  # the "never touch a weight" promise is stated


def test_migration_0049_runs_the_repair(client: TestClient, session, monkeypatch) -> None:
    """The wrapper must actually invoke the service against alembic's own
    connection — a no-op revision would leave the alias merge as dead code."""
    import importlib.util
    from pathlib import Path

    migration = (
        Path(__file__).parent.parent / "alembic" / "versions" / "0049_merge_venue_aliases.py"
    )
    spec = importlib.util.spec_from_file_location("m164_0049", migration)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class _StubOp:
        @staticmethod
        def get_bind():
            return session.connection()

    monkeypatch.setattr(module, "op", _StubOp)
    user, _account_id = _user_and_account(client, session)
    keeper = _seed_security(session, user, "PNDORA.CO", "CPH", name="Pandora A/S")
    loser = _seed_security(session, user, "PNDORA.CO", "OCSE", name="Pandora AS")
    session.flush()

    module.upgrade()

    # A fresh SELECT, not `session.get`: the migration ran its DELETE as a
    # Core statement on its own Session, so this session's identity map still
    # holds the row it loaded — the ROW is what must be gone.
    survivors = session.exec(
        select(Security).where(Security.user_id == user.id).order_by(Security.id)
    ).all()
    assert [row.id for row in survivors] == [keeper.id]
    assert survivors[0].exchange == "CPH"
    assert loser.id not in [row.id for row in survivors]

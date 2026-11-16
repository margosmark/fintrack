"""M117 — securities repair pass (docs/02 §securities, docs/06 §ACB safety).

`uq_securities_symbol UNIQUE (user_id, symbol, exchange)` only holds if both
parts are normalized on the way in. The investment-CSV import used to store
the raw cell, so a file reading `veqt` could sit beside the `VEQT` row created
through the API — two rows for one listing, splitting its trades, prices and
ACB pool across a stale twin.

`app.services.security_dedup.repair_securities` merges those groups and is
idempotent; migration `0045_merge_dup_securities` is the thin wrapper that
runs it on deploy (and is executed — against an empty table — by every suite
setup, which runs `alembic upgrade head`).
"""

import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import select

from app.models.acb_pools import AcbPool
from app.models.etf_holdings import EtfHolding, EtfHoldingSource
from app.models.fundamentals import Fundamentals, FundamentalsSource
from app.models.prices import Price, PriceSource
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.models.watchlist import WatchlistItem
from app.services.security_dedup import repair_securities

API = "/api/v1"
D = Decimal

MIGRATION = Path(__file__).parent.parent / "alembic" / "versions" / "0045_merge_dup_securities.py"


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m117-{date.today().isoformat()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _seed_security(session, user: User, symbol: str, exchange: str) -> Security:
    row = Security(
        user_id=user.id,
        symbol=symbol,
        name=f"{symbol} fund",
        kind=SecurityKind.etf,
        listing_currency="CAD",
        exchange=exchange,
    )
    session.add(row)
    session.flush()
    return row


def _seed_price(session, user: User, security: Security, day: date, value: str) -> Price:
    row = Price(
        user_id=user.id,
        security_id=security.id,
        date=day,
        price=D(value),
        source=PriceSource.yfinance,
    )
    session.add(row)
    session.flush()
    return row


def _user_and_account(client: TestClient, session) -> tuple[dict[str, str], User, int]:
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
    return headers, user, account.json()["id"]


# --- merge -------------------------------------------------------------------


def test_merges_case_variants_repoints_references_and_normalizes(
    client: TestClient, session
) -> None:
    _headers, user, account_id = _user_and_account(client, session)

    # A: canonical-looking row but only two closes. B: legacy lowercase row
    # carrying three — the keeper rule must pick B on price history, not the
    # prettier spelling.
    a = _seed_security(session, user, "VEQT", "TSX")
    b = _seed_security(session, user, "veqt", "tsx")
    _seed_price(session, user, a, date(2026, 1, 5), "40.00")
    _seed_price(session, user, a, date(2026, 1, 8), "111.11")
    _seed_price(session, user, b, date(2026, 1, 6), "41.00")
    _seed_price(session, user, b, date(2026, 1, 7), "42.00")
    _seed_price(session, user, b, date(2026, 1, 8), "999.99")

    for security in (a, b):
        session.add(
            Trade(
                user_id=user.id,
                account_id=account_id,
                security_id=security.id,
                action=TradeAction.buy,
                date=date(2026, 1, 5),
                quantity=D("1"),
                price_native=D("40"),
            )
        )
    # Watch the loser only: the watch must survive onto the keeper.
    session.add(WatchlistItem(user_id=user.id, security_id=a.id))
    session.add(
        Fundamentals(
            user_id=user.id,
            security_id=a.id,
            source=FundamentalsSource.manual,
            fetched_at=date(2026, 1, 5),
            sector="Financial Services",
        )
    )
    session.add(
        EtfHolding(
            user_id=user.id,
            security_id=a.id,
            holding_name="Top 10",
            weight_pct=D("10.0000"),
            source=EtfHoldingSource.manual,
        )
    )
    session.flush()

    report = repair_securities(session)

    assert report.merged == [(b.id, (a.id,))]
    # Only the LOSER's trade moves; the keeper's is already on the keeper.
    assert report.repointed == {
        "trades": 1,
        "prices": 1,
        "watchlist": 1,
        "fundamentals": 1,
        "etf_holdings": 1,
    }
    assert report.deleted == {"prices": 1}
    assert report.skipped == []
    # The surviving row keeps the keeper's id but gets the normalized spelling.
    assert report.normalized == 1
    assert (b.symbol, b.exchange) == ("VEQT", "TSX")
    assert b.id is not None

    # The loser is gone and nothing still points at it.
    assert session.get(Security, a.id) is None
    remaining = session.exec(select(Security).where(Security.user_id == user.id)).all()
    assert [row.id for row in remaining] == [b.id]

    trade_targets = set(
        session.exec(select(Trade.security_id).where(Trade.user_id == user.id)).all()
    )
    assert trade_targets == {b.id}

    # Keeper's close for the contested date wins; the loser's copy is dropped.
    prices = {
        row.date: row for row in session.exec(select(Price).where(Price.security_id == b.id)).all()
    }
    assert sorted(prices) == [
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
        date(2026, 1, 8),
    ]
    assert prices[date(2026, 1, 8)].price == D("999.99")

    watches = session.exec(select(WatchlistItem).where(WatchlistItem.user_id == user.id)).all()
    assert [row.security_id for row in watches] == [b.id]

    fundamentals = session.exec(select(Fundamentals).where(Fundamentals.user_id == user.id)).all()
    assert [row.security_id for row in fundamentals] == [b.id]

    holdings = session.exec(select(EtfHolding).where(EtfHolding.user_id == user.id)).all()
    assert [row.security_id for row in holdings] == [b.id]

    # Idempotent: a second pass has nothing left to do.
    again = repair_securities(session)
    assert again.done, again.summary()
    assert again.normalized == 0


def test_refuses_a_merge_that_would_corrupt_acb(client: TestClient, session) -> None:
    """Two ACB pools on one account+security mean cost basis was accumulated
    twice — the merge must stop rather than silently drop one (docs/06)."""
    _headers, user, account_id = _user_and_account(client, session)
    a = _seed_security(session, user, "CASH", "CASH")
    b = _seed_security(session, user, "cash", "cash")
    for security in (a, b):
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
    survivors = {
        sid: (symbol, exchange)
        for sid, symbol, exchange in session.exec(
            select(Security.id, Security.symbol, Security.exchange).where(
                Security.user_id == user.id
            )
        ).all()
    }
    assert set(survivors) == {a.id, b.id}
    # The refused merge also blocks normalization of the loser: rewriting
    # `cash`/`cash` would collide with the survivor's `CASH`/`CASH` key.
    assert survivors[a.id] == ("CASH", "CASH")
    assert survivors[b.id] == ("cash", "cash")
    assert len(report.unnormalized) == 1
    assert report.unnormalized[0][1] == b.id


def test_normalizes_rows_that_are_not_part_of_a_duplicate(client: TestClient, session) -> None:
    _headers, user, _account_id = _user_and_account(client, session)
    lone = _seed_security(session, user, "  ry ", "nms")

    first = repair_securities(session)
    assert first.normalized == 1
    assert first.merged == []
    assert (lone.symbol, lone.exchange) == ("RY", "NMS")

    second = repair_securities(session)
    assert second.done, second.summary()


def test_leaves_distinct_exchange_listings_alone(client: TestClient, session) -> None:
    """`VT` on TSX and `VT` on NYSE are two securities by design (docs/02) —
    only case/whitespace variants of the same normalized key merge."""
    _headers, user, _account_id = _user_and_account(client, session)
    tsx = _seed_security(session, user, "VT", "TSX")
    nyse = _seed_security(session, user, "VT", "NYSE")

    report = repair_securities(session)

    assert report.merged == []
    assert session.get(Security, tsx.id) is not None
    assert session.get(Security, nyse.id) is not None
    # Both still normalized (they already were).
    assert report.normalized == 0


# --- migration wrapper ------------------------------------------------------


def test_migration_upgrade_runs_the_repair(client: TestClient, session, monkeypatch) -> None:
    """`0045_merge_dup_securities.upgrade()` must actually invoke the service
    against alembic's connection — a wrapper that quietly no-ops would leave
    the repair as dead code."""
    spec = importlib.util.spec_from_file_location("m117_0045", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class _StubOp:
        @staticmethod
        def get_bind():
            return session.connection()

    monkeypatch.setattr(module, "op", _StubOp)

    _headers, user, _account_id = _user_and_account(client, session)
    keeper = _seed_security(session, user, "XGRO", "TSX")
    _loser = _seed_security(session, user, "xgro", "tsx")
    session.flush()

    module.upgrade()

    # Read through a column select: the migration's own Session deleted the
    # loser, so the test session's identity map would raise ObjectDeletedError
    # on a `get()` for a row that no longer exists.
    survivors = dict(
        session.exec(select(Security.id, Security.symbol).where(Security.user_id == user.id)).all()
    )
    assert set(survivors) == {keeper.id}
    assert survivors[keeper.id] == "XGRO"

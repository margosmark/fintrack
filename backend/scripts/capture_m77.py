"""Capture the M77 golden vectors (docs/12 §Sankey cash-flow, docs/05 §Total return).

Writes ``frontend/tests/fixtures/m77-vectors.json``: the frozen inputs the
client engines ran over plus the SERVER's own JSON for each captured range.
``frontend/tests/m77Parity.test.ts`` required both engines to reproduce every
``expected`` value byte for byte — that reader was deleted by M160/P2-5 and the
fixture is kept as a frozen server-route pin (replay wiring: card t_62afc8a5).

Run it against this slice's own isolated database (never the shared demo one):

    source ./ft_m77_env.sh && cd backend && \\
      SEED_DEMO_ACCOUNT=false uv run --no-sync python scripts/capture_m77.py

The script wipes and re-migrates ``$DATABASE_URL`` first, so point it at a
throwaway database. Regenerate only alongside a deliberate contract change,
and update ``tests/fixtures/README.md`` in the same commit.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

# Must happen before `app.core.config` is imported anywhere: an import-time
# snapshot of the settings would seed the demo tenant into this fixture, and
# `create_app` builds `Settings` at import time. Same guards as
# `tests/conftest.py` — this is a capture harness, never a production process.
os.environ.setdefault("SEED_DEMO_ACCOUNT", "false")
os.environ.setdefault("JWT_SECRET", "test-secret-not-default-0123456789abcd")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ["NOTIFY_WORKER_ENABLED"] = "false"

# ruff: noqa: E402 — the settings guard above must run before `app` is imported.

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlmodel import Session as SQLModelSession
from test_tax_acb import add_trade

from alembic import command
from app.api.deps import get_engine
from app.core.config import get_settings
from app.core.security import create_access_token
from app.main import app
from app.models.accounts import Account, AccountType
from app.models.categories import Category
from app.models.fx_rates import FxRate, FxSource
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.prices import Price, PriceSource
from app.models.securities import Security, SecurityKind
from app.models.trades import TradeAction as TA
from app.models.transactions import Transaction
from app.models.users import User

D = Decimal
OUT = REPO / "frontend" / "tests" / "fixtures" / "m77-vectors.json"

# Ranges captured for both reports. `empty` has no activity at all, so it pins
# the empty-Sankey and `status: "no_data"` shapes (M65: no confident zeros).
RANGES: dict[str, tuple[str, str]] = {
    "full": ("2025-01-01", "2025-12-31"),
    "q2": ("2025-04-01", "2025-06-30"),
    "empty": ("2024-01-01", "2024-12-31"),
}


# --- database reset ----------------------------------------------------------


def reset_database() -> None:
    """Drop every object in the target database and re-run all migrations."""
    # Drop the TARGET database's schema (not the `postgres` maintenance db!)
    # and re-run every migration, so the fixture is built from a known schema.
    target = create_engine(make_url(get_settings().database_url))
    with target.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    target.dispose()

    cfg = Config(str(BACKEND / "alembic.ini"))
    command.upgrade(cfg, "head")


# --- seed --------------------------------------------------------------------


def _account(
    session: Session,
    user: User,
    name: str,
    kind: AccountType,
    currency: str = "CAD",
    taxable: bool = False,
    starting_balance: str = "0.00",
) -> Account:
    account = Account(
        user_id=user.id,
        name=name,
        type=kind,
        currency=currency,
        taxable=taxable,
        starting_balance=D(starting_balance),
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _security(session: Session, user: User, symbol: str, listing: str) -> Security:
    security = Security(
        user_id=user.id,
        symbol=symbol,
        name=f"{symbol} long name",
        kind=SecurityKind.stock,
        listing_currency=listing,
        exchange="TSX" if listing == "CAD" else "ARCA",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _category(session: Session, user: User, name: str, parent: Category | None = None) -> Category:
    cat = Category(user_id=user.id, name=name, parent_id=parent.id if parent else None)
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def _batch(
    session: Session, user: User, account: Account, status: ImportBatchStatus
) -> ImportBatch:
    batch = ImportBatch(
        user_id=user.id,
        account_id=account.id,
        institution="Test Bank",
        file_name="stmt.csv",
        status=status,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def _txn(
    session: Session,
    user: User,
    account: Account,
    batch: ImportBatch | None,
    day: str,
    amount: str,
    category: Category | None,
    currency: str = "CAD",
    resolve_cad: bool = True,
) -> Transaction:
    txn = Transaction(
        user_id=user.id,
        account_id=account.id,
        date=dt.date.fromisoformat(day),
        raw_description=f"raw {day}",
        normalized_payee="Payee",
        amount_native=D(amount),
        currency=currency,
        cad_amount=D(amount) if resolve_cad else None,
        category_id=category.id if category else None,
        import_batch_id=batch.id if batch else None,
        dedup_hash=f"{account.id}-{day}-{amount}-{batch.id if batch else 'n'}",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _price(session: Session, user: User, security: Security, day: str, price: str) -> None:
    session.add(
        Price(
            user_id=user.id,
            security_id=security.id,
            date=dt.date.fromisoformat(day),
            price=D(price),
            source=PriceSource.manual,
        )
    )
    session.commit()


def seed(session: Session) -> User:
    user = User(email="m77-golden@example.com", hashed_password="x")  # noqa: S106 -- capture fixture, never authenticates
    session.add(user)
    session.commit()
    session.refresh(user)

    salary = _category(session, user, "Salary")
    food = _category(session, user, "Food")
    groceries = _category(session, user, "Groceries", parent=food)
    dining = _category(session, user, "Dining", parent=food)
    transport = _category(session, user, "Transport")

    chequing = _account(session, user, "Chequing", AccountType.chequing)
    cash = _account(session, user, "Cash", AccountType.savings, starting_balance="250.00")
    cad_brokerage = _account(
        session,
        user,
        "CAD Brokerage",
        AccountType.brokerage_non_registered,
        taxable=True,
        starting_balance="500.00",
    )
    usd_brokerage = _account(session, user, "USD Brokerage", AccountType.rrsp, currency="USD")
    rrsp = _account(session, user, "RRSP", AccountType.rrsp, starting_balance="0.00")

    ry = _security(session, user, "RY", "CAD")
    vti = _security(session, user, "VTI", "USD")

    committed = _batch(session, user, chequing, ImportBatchStatus.committed)
    pending = _batch(session, user, chequing, ImportBatchStatus.pending_review)
    pending_trades = _batch(session, user, cad_brokerage, ImportBatchStatus.pending_review)

    # --- transactions: 2 income sources, 4 expense buckets, 1 unresolved, 1 staged
    _txn(session, user, chequing, committed, "2025-01-05", "3000.00", salary)
    _txn(session, user, chequing, committed, "2025-01-08", "-120.00", groceries)
    _txn(session, user, chequing, committed, "2025-02-03", "-60.00", dining)
    _txn(session, user, chequing, committed, "2025-02-14", "-40.00", transport)
    _txn(session, user, chequing, committed, "2025-03-02", "-12.50", None)
    _txn(session, user, chequing, committed, "2025-05-11", "-85.25", groceries)
    _txn(session, user, cash, committed, "2025-04-19", "-310.00", transport)
    # unresolved CAD (no cached fx on a USD row) — excluded from both sides
    _txn(
        session,
        user,
        usd_brokerage,
        committed,
        "2025-06-01",
        "40.00",
        None,
        currency="USD",
        resolve_cad=False,
    )
    # staged rows never reach any report (docs/03)
    _txn(session, user, chequing, pending, "2025-03-05", "-999.00", groceries)

    # --- fx + prices (cache-only; the engine never fetches)
    # `fx_rate_cad` is EXACT-date (docs/06) — every non-CAD trade date below
    # needs its own row or `GET /accounts` 422s with `fx_rate_missing` before
    # the capture can paginate anything. The three anchors still exist so the
    # on-or-before readers (`PriceCache`, the report engines) span the year.
    for day, rate in [
        ("2025-01-01", "1.4400"),
        ("2025-02-07", "1.4450"),
        ("2025-04-01", "1.4300"),
        ("2025-05-09", "1.3900"),
        ("2025-07-01", "1.3600"),
        ("2025-07-04", "1.3650"),
        ("2025-09-05", "1.3800"),
    ]:
        session.add(
            FxRate(
                base="USD",
                quote="CAD",
                date=dt.date.fromisoformat(day),
                rate=D(rate),
                source=FxSource.manual,
            )
        )
    session.commit()
    for day, price in [
        ("2025-01-10", "140.00"),
        ("2025-03-14", "146.50"),
        ("2025-06-13", "151.00"),
        ("2025-09-12", "155.75"),
    ]:
        _price(session, user, ry, day, price)
    for day, price in [
        ("2025-02-07", "260.00"),
        ("2025-05-09", "271.00"),
        ("2025-08-08", "268.50"),
    ]:
        _price(session, user, vti, day, price)

    # --- trades: income, buys/sells with commission, dividends + DRIP, fee,
    #     contributions/withdrawals and both kinds of transfer
    add_trade(session, user, cad_brokerage, ry, TA.contribution, "2025-01-10", "0", "1000.00")
    add_trade(session, user, cad_brokerage, ry, TA.buy, "2025-01-10", "4", "140.00", "6.95")
    add_trade(session, user, cad_brokerage, ry, TA.dividend, "2025-03-14", "0", "45.00")
    add_trade(session, user, cad_brokerage, ry, TA.sell, "2025-03-14", "-1", "146.50", "6.95")
    add_trade(session, user, cad_brokerage, ry, TA.fee, "2025-04-02", "0", "12.50")
    add_trade(session, user, cad_brokerage, ry, TA.buy, "2025-06-13", "2", "151.00", "6.95")
    add_trade(session, user, cad_brokerage, ry, TA.drip, "2025-09-12", "0.288000", "155.75", "0")
    add_trade(session, user, cad_brokerage, ry, TA.withdrawal, "2025-08-15", "0", "200.00")
    add_trade(session, user, usd_brokerage, vti, TA.contribution, "2025-02-07", "0", "2000.00")
    add_trade(session, user, usd_brokerage, vti, TA.buy, "2025-02-07", "5", "260.00", "0")
    add_trade(session, user, usd_brokerage, vti, TA.dividend, "2025-05-09", "0", "18.00")
    add_trade(session, user, usd_brokerage, vti, TA.transfer_out, "2025-07-04", "0", "150.00")
    add_trade(session, user, usd_brokerage, vti, TA.transfer_in, "2025-09-05", "1", "268.50")
    add_trade(session, user, rrsp, ry, TA.transfer_in, "2025-05-20", "0", "500.00")
    # staged trade — invisible to every derived reader (docs/03)
    staged = add_trade(session, user, cad_brokerage, ry, TA.buy, "2025-05-21", "10", "150.00", "0")
    staged.import_batch_id = pending_trades.id
    session.add(staged)
    session.commit()

    return user


# --- capture -----------------------------------------------------------------


def paginate(client: TestClient, path: str, token: str, **params: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        query = {"limit": "200", "offset": str(offset), **params}
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        res = client.get(f"{path}?{qs}", headers={"Authorization": f"Bearer {token}"})
        if res.status_code >= 400:
            # `raise_for_status()` alone hides WHICH field 422'd — the capture
            # harness has no other way to see the envelope.
            raise SystemExit(f"{path}?{qs} -> {res.status_code} {res.text}")
        page = res.json()
        rows.extend(page["items"])
        offset += len(page["items"])
        if not page["items"] or offset >= page["total"]:
            return rows


def main() -> None:
    reset_database()

    engine = get_engine()
    with SQLModelSession(bind=engine) as session:
        # read the id BEFORE the session closes — a detached instance would
        # raise DetachedInstanceError on attribute access.
        user_id = seed(session).id
    token = create_access_token(user_id)

    with TestClient(app) as client:
        inputs = {
            "accounts": paginate(client, "/api/v1/accounts", token),
            "transactions": paginate(client, "/api/v1/transactions", token),
            "trades": paginate(client, "/api/v1/trades", token, committed_only="true"),
            "securities": paginate(client, "/api/v1/securities", token),
            "categories": paginate(client, "/api/v1/categories", token),
            "prices": paginate(client, "/api/v1/prices", token),
        }
        fx_rows = client.get(
            "/api/v1/fx?base=USD&quote=CAD&start_date=2000-01-01&end_date=2026-12-31&fetch=false",
            headers={"Authorization": f"Bearer {token}"},
        )
        fx_rows.raise_for_status()
        inputs["fx"] = fx_rows.json()["items"]

        sankey: dict[str, object] = {}
        total_return: dict[str, object] = {}
        for name, (start, end) in RANGES.items():
            for path, store in (
                ("/api/v1/reports/cashflow-sankey", sankey),
                ("/api/v1/reports/total-return", total_return),
            ):
                res = client.get(
                    path,
                    params={"from": start, "to": end},
                    headers={"Authorization": f"Bearer {token}"},
                )
                res.raise_for_status()
                store[name] = res.json()

    payload = {
        "captured_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "base_commit": _head(),
        "reporting_currency": get_settings().reporting_currency,
        "ranges": {k: {"from": v[0], "to": v[1]} for k, v in RANGES.items()},
        **inputs,
        "sankey": sankey,
        "total_return": total_return,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)} ({OUT.stat().st_size} bytes)")


def _head() -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607 -- git resolved from PATH
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


if __name__ == "__main__":
    main()

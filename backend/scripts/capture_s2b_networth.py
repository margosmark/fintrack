"""Re-capture the net-worth goldens in `frontend/tests/fixtures/s2b-vectors.json`.

M97 (docs/12 §4) added `assets_cad` / `liabilities_cad` to every net-worth
bucket, so the frozen server JSON in the S2b fixture no longer matches what the
server answers — the parity gate (`frontend/tests/s2bReports.test.ts`, deleted
by M160/P2-5) diffed the shipped browser engine against that file and would go
red; the fixture is now kept as a frozen server-route pin (replay wiring: card
t_62afc8a5).

Why a script instead of a hand-edit: `tests/fixtures/README.md` pins
"re-capture, don't hand-edit" — hand-editing a golden turns a bug into an
expectation. This runs the REAL server service and records its response.

The frozen inputs are the whole point. `networth[*].buckets` was captured
together with `accounts` / `transactions` / `trades` / `prices` / `securities`
/ `fx`, and the demo tenant has drifted since 2026-09-27 (its cash no longer
matches the fixture), so capturing against production would pair NEW output
with OLD inputs and break parity for the wrong reason. `--seed` therefore loads
the fixture's own frozen rows into a throwaway `*_test` database first, and the
capture then verifies that every pre-existing bucket field is byte-identical to
the frozen one — i.e. that the replay reproduces the 2026-09-27 server exactly
before it trusts the two new fields. Any mismatch aborts without writing.

Safety: `--seed` refuses a database whose name carries no `_test` marker — the
legacy hand-named `<base>_test` and the per-session `<base>_test_<id>` names
(t_00b6d06d) both pass. The capture itself only SELECTs. `performance` goldens,
the frozen inputs and `captured_at` (which `s2bReports.test.ts` used as its
`today`, and which any route replay must inject) are untouched —
only the seven `networth[*].buckets` arrays are rewritten.

Usage (from `backend/`, DATABASE_URL pointing at a throwaway `*_test` DB that
already carries the schema — conftest's `alembic upgrade head` creates it):

    DATABASE_URL=postgresql+psycopg://…@localhost:15432/fintrack_m97_test \
        uv run --no-sync python scripts/capture_s2b_networth.py --seed
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.pool import NullPool
from sqlmodel import Session

# This checkout's code, always: run as `python scripts/<name>.py` sys.path[0] is
# `backend/scripts`, so `import app` would otherwise resolve through the venv's
# editable install — which points at the OTHER clone (/home/hermes/fin_tracker)
# and does not carry this branch. (pytest is unaffected: its conftest puts the
# right backend/ first.)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.accounts import Account
from app.models.fx_rates import FxRate, FxSource
from app.models.prices import Price
from app.models.securities import Security
from app.models.trades import Trade
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.reports import NetWorthReport
from app.services.demo_seed import DEMO_EMAIL
from app.services.reports import net_worth_report

FIXTURE = (
    Path(__file__).resolve().parents[2] / "frontend" / "tests" / "fixtures" / "s2b-vectors.json"
)
NEW_FIELDS = ("assets_cad", "liabilities_cad")
SEED_USER_ID = 1

# Children first (SQLAlchemy orders by FK dependency anyway; this documents it).
# The fixture froze no categories and no import batches, so the columns that
# reference them are nulled during the seed — net worth reads accounts,
# transactions, trades, prices, securities and fx only (docs/12 §4), and the
# committed predicate treats a NULL batch as committed (docs/03).
SEED_MODELS = [Transaction, Trade, Price, Account, Security, FxRate, User]
NULLABLE_OVERRIDES: dict[type, tuple[str, ...]] = {
    Transaction: ("category_id", "transfer_pair_id", "import_batch_id"),
    Trade: ("import_batch_id",),
}


def _seed(session: Session, fixture: dict[str, Any]) -> None:
    for model in SEED_MODELS:
        session.execute(delete(model))
    session.commit()

    def load(model: type, rows: list[dict[str, Any]]) -> None:
        columns = {c.name for c in model.__table__.columns}
        drop = set(NULLABLE_OVERRIDES.get(model, ()))
        for row in rows:
            data = {k: v for k, v in row.items() if k in columns and k not in drop}
            if "user_id" in columns:
                data["user_id"] = SEED_USER_ID
            if model is Transaction:
                data["dedup_hash"] = f"s2b-{row['id']}"
            session.add(model(**data))
        session.commit()

    # hash_password(), not a string literal: CI's content secret scan reads
    # `password="<16+ chars>"` as an assignment-shaped credential (ci.yml
    # PAT_ASSIGN). The row is a throwaway in a *_test database and this value
    # is a placeholder, so it gets a real bcrypt hash of a documented value.
    session.add(
        User(id=SEED_USER_ID, email=DEMO_EMAIL, hashed_password=hash_password("s2b-capture-only"))
    )
    session.commit()
    load(Security, fixture["securities"])
    load(Account, fixture["accounts"])
    load(Price, fixture["prices"])
    load(Transaction, fixture["transactions"])
    load(Trade, fixture["trades"])
    # fx_rates is the shared cache (no user_id); the fixture stores one series
    # per base against the reporting currency, and only date/rate are read.
    for base, rows in fixture["fx"].items():
        for row in rows:
            session.add(
                FxRate(
                    base=base,
                    quote="CAD",
                    date=date.fromisoformat(row["date"]),
                    rate=row["rate"],
                    source=FxSource.boc_valet,
                )
            )
    session.commit()

    counts = {
        model.__tablename__: session.scalar(select(func.count()).select_from(model))
        for model in reversed(SEED_MODELS)
    }
    print(f"  seeded: {counts}")


def capture(session: Session, user_id: int, key: str) -> list[dict[str, Any]]:
    """One range's buckets exactly as `GET /reports/net-worth` serializes them."""
    from_, to, granularity = key.split("|")
    report = net_worth_report(
        session, user_id, date.fromisoformat(from_), date.fromisoformat(to), granularity
    )
    payload = NetWorthReport(**report).model_dump(mode="json", by_alias=True)
    return payload["buckets"]


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set — refusing to guess a database", file=sys.stderr)
        return 4
    do_seed = "--seed" in sys.argv[1:]
    db_name = get_settings().database_url.rsplit("/", 1)[-1].split("?")[0]
    # `_test` alone is no longer the whole story: the suite names its databases
    # `<base>_test_<session-id>` (then `_demo` / `_perf`), so require the marker
    # followed by end-of-name or the id rather than an exact `_test` ending.
    if do_seed and re.search(r"_test(_|$)", db_name) is None:
        print(f"--seed refuses non-test database {db_name!r}", file=sys.stderr)
        return 4

    fixture = json.loads(FIXTURE.read_text())
    # SQLModel Session, not sqlalchemy.orm.Session: the services call .exec().
    session = Session(create_engine(get_settings().database_url, poolclass=NullPool))
    if do_seed:
        print(f"seeding frozen inputs into {db_name}")
        _seed(session, fixture)
    user_id = session.scalar(select(User.id).where(User.email == DEMO_EMAIL))
    if user_id is None:
        print("no demo user — run with --seed", file=sys.stderr)
        return 1
    print(f"capturing net-worth goldens for {DEMO_EMAIL} (id {user_id})")

    rewritten: dict[str, list[dict[str, Any]]] = {}
    for key, frozen in fixture["networth"].items():
        fresh = capture(session, user_id, key)
        old = frozen["buckets"]
        if len(fresh) != len(old):
            print(
                f"ABORT {key}: {len(old)} frozen buckets vs {len(fresh)} captured", file=sys.stderr
            )
            return 1
        for i, (was, now) in enumerate(zip(old, fresh, strict=True)):
            for field, value in was.items():
                if now.get(field) != value:
                    print(
                        f"ABORT {key} bucket {i}: pre-existing field {field} moved "
                        f"{value!r} -> {now.get(field)!r} — the replay does not "
                        f"reproduce the frozen capture; nothing written",
                        file=sys.stderr,
                    )
                    return 1
            missing = [f for f in NEW_FIELDS if f not in now]
            if missing:
                print(f"ABORT {key} bucket {i}: server did not emit {missing}", file=sys.stderr)
                return 1
        rewritten[key] = fresh
        print(f"  {key}: {len(fresh)} buckets, pre-existing fields identical")

    fixture["networth"] = {
        key: {**entry, "buckets": rewritten[key]} for key, entry in fixture["networth"].items()
    }
    FIXTURE.write_text(json.dumps(fixture))
    print(f"wrote {FIXTURE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

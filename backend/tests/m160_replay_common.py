"""M160 server-route replay helpers (docs/33 §Server-first rule 5).

M158 Decision 2 (option c) retired the client report mirrors, keeping
``capture*`` + the golden fixtures as SERVER-ROUTE REGRESSION PINS: the
captured live-stack JSON is replayed against the mounted routes instead of
diffed against a browser engine. This module is the shared machinery for
those pins, on the pattern `tests/test_search_goldens.py` established for
the s5 vectors (seed the captured INPUTS into a throwaway tenant, replay the
captured REQUEST, diff the answer against the frozen GOLDEN).

Two invariants the helpers encode:

* **Ids never survive a re-seed.**  The suite database assigns its own
  serials, so every seeded row returns an ``IdMap`` (captured id -> suite
  id) and every captured id in a query or a golden body is translated
  through it before comparison — the same order-isomorphic translation
  `test_search_goldens.py` performs for transaction ids. A translation miss
  raises (KeyError) rather than silently passing.
* **``fx_rates`` is the one SHARED table** (docs/02's tenancy exception).
  Seeding is insert-if-absent: a rate another test already committed for the
  same ``(base, quote, date)`` is left alone, and if that ever disagrees with
  the captured value the replay surfaces it as a honest golden diff instead
  of a unique-constraint crash.

Nothing here mutates anything outside the suite database, and every tenant
created for a case is scoped by its own ``user_id`` (the suite's
leftover-tenant guard reads only the ``users`` table, which the
function-scoped session rolls back).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.accounts import Account
from app.models.categories import Category
from app.models.fx_rates import FxRate, FxSource
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.tags import Tag
from app.models.trades import Trade
from app.models.transaction_tags import TransactionTag
from app.models.transactions import Transaction
from app.models.users import User

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "frontend" / "tests" / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """The frozen `frontend/tests/fixtures/<name>` golden (never re-captured)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_user(session: Session, tag: str) -> User:
    """A bare tenant (no register — no default categories, no API writes).

    Every seeded row is derived from the FIXTURE's own datasets, so the
    tenant carries exactly the captured population and nothing the register
    path would have added.
    """
    digest = hashlib.sha1(f"{tag}-{id(session)}".encode()).hexdigest()[:12]  # noqa: S324 -- email slug, not security
    user = User(email=f"m160-replay-{digest}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


class IdMap:
    """captured id -> suite id for one tenant."""

    def __init__(self) -> None:
        self._map: dict[int, int] = {}

    def add(self, old: int, new: int) -> None:
        self._map[int(old)] = int(new)

    def __getitem__(self, old: int) -> int:
        return self._map[int(old)]  # KeyError = a seeding bug, never a pass

    def translate(self, value: Any) -> Any:
        if value is None:
            return None
        return self._map[int(value)]

    def merge(self, other: IdMap) -> None:
        """Union a second tenant's captured ids (household: two trees)."""
        self._map.update(other._map)


# --- seeders -----------------------------------------------------------------


def seed_categories(session: Session, user: User, rows: list[dict]) -> IdMap:
    """Insert categories in captured-id order (parents precede children in
    every fixture here), translating `parent_id` through the same map."""
    mapping = IdMap()
    for row in sorted(rows, key=lambda r: r["id"]):
        parent = None if row.get("parent_id") is None else mapping[row["parent_id"]]
        cat = Category(user_id=user.id, name=row["name"], parent_id=parent)
        session.add(cat)
        session.flush()
        mapping.add(row["id"], cat.id)
    session.commit()
    return mapping


def seed_accounts(session: Session, user: User, rows: list[dict]) -> IdMap:
    """The captured accounts. `taxable` comes from the fixture when it carries
    it (every dataset here does) — the tax engine reads the COLUMN, and the
    API's type-derived derivation never runs on a direct row insert (docs/02:
    taxability is derived server-side; s4's pools depend on it)."""
    mapping = IdMap()
    for row in sorted(rows, key=lambda r: r["id"]):
        account = Account(
            user_id=user.id,
            name=row["name"],
            type=row["type"],
            currency=row.get("currency") or "CAD",
            starting_balance=Decimal(str(row.get("starting_balance") or "0")),
            taxable=(
                bool(row["taxable"])
                if "taxable" in row
                else row["type"] == "brokerage_non_registered"
            ),
        )
        session.add(account)
        session.flush()
        mapping.add(row["id"], account.id)
    session.commit()
    return mapping


def ensure_accounts(session: Session, user: User, dataset: dict) -> IdMap:
    """The dataset's accounts when it carries them (s2a/s4), else one stub
    account per distinct captured account id — matrix's and s3's captures
    never stored an ``accounts[]`` dataset, but `transactions.account_id` is
    still a foreign key the replay must satisfy. No report in these slices
    reads an account field, so a stub cannot move a golden."""
    if dataset.get("accounts"):
        return seed_accounts(session, user, dataset["accounts"])
    accounts = IdMap()
    for old in sorted({row["account_id"] for row in dataset.get("transactions") or []}):
        account = Account(
            user_id=user.id,
            name=f"replay-{old}",
            type="chequing",
            currency="CAD",
        )
        session.add(account)
        session.flush()
        accounts.add(old, account.id)
    session.commit()
    return accounts


def seed_securities(session: Session, user: User, rows: list[dict]) -> IdMap:
    """The captured securities. `kind` and `exchange` are NOT NULL in today's
    schema but the capture predates both — no golden in these slices reads
    either (capital-gains items carry security/symbol only; the ACB page
    prints the symbol), so the two columns are derived, never asserted:
    `.TO` is TSX, everything else trades on ARCA (tests/test_tax_acb.py's
    own convention)."""
    mapping = IdMap()
    for row in sorted(rows, key=lambda r: r["id"]):
        sec = Security(
            user_id=user.id,
            symbol=row["symbol"],
            name=row.get("name") or row["symbol"],
            kind="etf" if row["symbol"] in ("VTI", "VXUS") else "stock",
            listing_currency=row.get("listing_currency") or "CAD",
            exchange="TSX" if row["symbol"].endswith(".TO") else "ARCA",
        )
        session.add(sec)
        session.flush()
        mapping.add(row["id"], sec.id)
    session.commit()
    return mapping


def seed_transactions(
    session: Session,
    user: User,
    rows: list[dict],
    accounts: IdMap,
    categories: IdMap,
    tags_by_name: dict[str, Tag] | None = None,
) -> IdMap:
    """The captured ledger, one batched commit, returning the txn IdMap
    (rule-preview items carry transaction ids). Rows are manual (no import
    batch) — the committed-only predicate treats manual rows as committed
    (docs/03), which is what `test_search_goldens.py` relies on too."""
    txns = IdMap()
    created: list[Transaction] = []
    for index, row in enumerate(sorted(rows, key=lambda r: r["id"])):
        txn = Transaction(
            user_id=user.id,
            account_id=accounts[row["account_id"]],
            date=date.fromisoformat(row["date"]),
            posting_date=None,
            raw_description=row.get("raw_description") or row["normalized_payee"],
            normalized_payee=row["normalized_payee"],
            amount_native=Decimal(str(row["amount_native"])),
            currency=row.get("currency") or "CAD",
            cad_amount=(None if row.get("cad_amount") is None else Decimal(str(row["cad_amount"]))),
            category_id=(
                None if row.get("category_id") is None else categories[row["category_id"]]
            ),
            dedup_hash=hashlib.sha256(f"m160-{row['id']}-{index}".encode()).hexdigest(),
        )
        created.append(txn)
        session.add(txn)
    session.flush()
    ordered = sorted(rows, key=lambda r: r["id"])
    for row, txn in zip(ordered, created, strict=True):
        txns.add(row["id"], txn.id)
    if tags_by_name is not None:
        for row, txn in zip(ordered, created, strict=True):
            for tag_ref in row.get("tags") or []:
                tag = tags_by_name.get(tag_ref["name"])
                if tag is not None:
                    session.add(TransactionTag(transaction_id=txn.id, tag_id=tag.id))
    session.commit()
    return txns


def seed_tags(session: Session, user: User, rows: list[dict]) -> dict[str, Tag]:
    """Tags by NAME (fixture ids do not survive; names are the identity the
    matrix golden also prints)."""
    by_name: dict[str, Tag] = {}
    for row in sorted(rows, key=lambda r: r["id"]):
        if row["name"] in by_name:
            continue
        tag = Tag(user_id=user.id, name=row["name"])
        session.add(tag)
        session.flush()
        by_name[row["name"]] = tag
    session.commit()
    return by_name


def collect_tags(*datasets: dict) -> list[dict]:
    """Every `{id, name}` any transaction row references, deduped by name."""
    seen: dict[str, dict] = {}
    for dataset in datasets:
        for row in dataset.get("transactions") or []:
            for tag in row.get("tags") or []:
                seen.setdefault(tag["name"], tag)
    return sorted(seen.values(), key=lambda t: t["id"])


def seed_trades(
    session: Session,
    user: User,
    rows: list[dict],
    accounts: IdMap,
    securities: IdMap,
) -> None:
    for row in sorted(rows, key=lambda r: r["id"]):
        session.add(
            Trade(
                user_id=user.id,
                account_id=accounts[row["account_id"]],
                security_id=securities[row["security_id"]],
                action=row["action"],
                date=date.fromisoformat(row["date"]),
                quantity=Decimal(str(row["quantity"])),
                price_native=Decimal(str(row["price_native"])),
                commission=Decimal(str(row.get("commission") or "0")),
                currency=row.get("currency"),
            )
        )
    session.commit()


def seed_prices(session: Session, user: User, rows: list[dict], securities: IdMap) -> None:
    for row in rows:
        session.add(
            Price(
                user_id=user.id,
                security_id=securities[row["security_id"]],
                date=date.fromisoformat(row["date"]),
                price=Decimal(str(row["price"])),
                source=PriceSource(row.get("source") or "yfinance"),
            )
        )
    session.commit()


def seed_fx(session: Session, fx: dict[str, Any] | None) -> int:
    """The captured cache-only fx range into the SHARED ``fx_rates`` table.

    Insert-if-absent on (base, quote, date): another test's committed row for
    the same day wins its own value, and a genuine disagreement surfaces as a
    golden diff (honest) rather than a constraint crash.

    Row counts are deliberately NOT asserted: psycopg3 reports rowcount -1
    ("unknown") for INSERT .. ON CONFLICT DO NOTHING, so the RETURNING tells
    us nothing about whether the row landed.
    """
    if not fx:
        return 0
    for base, rows in fx.items():
        for row in rows:
            stmt = (
                pg_insert(FxRate)
                .values(
                    base=base,
                    quote="CAD",
                    rate=Decimal(str(row["rate"])),
                    date=date.fromisoformat(row["date"]),
                    source=FxSource.manual,
                )
                .on_conflict_do_nothing(index_elements=["base", "quote", "date"])
            )
            session.execute(stmt)
    session.commit()
    return len(fx)


# --- replay + comparison -----------------------------------------------------


def auth(user: User) -> dict[str, str]:
    """A bearer header for a directly-seeded user (no login round trip)."""
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def replay_path(path: str, maps: dict[str, IdMap]) -> str:
    """Rewrite captured ids inside a query string (drill filters carry
    `category_id=<captured id>`), leaving every other param untouched."""
    parts = urlsplit(path)
    params = parse_qs(parts.query, keep_blank_values=True)
    rewritten: list[tuple[str, str]] = []
    for key, values in params.items():
        id_map = maps.get(key)
        for value in values:
            translated = (
                value
                if id_map is None or not value.lstrip("-").isdigit()
                else str(id_map[int(value)])
            )
            rewritten.append((key, translated))
    return f"{parts.path}?{urlencode(rewritten)}"


def translate_body(value: Any, maps: dict[str, IdMap]) -> Any:
    """Recursively translate captured ids in a golden body.

    Only the keys in `maps` are treated as ids (never a blanket int walk —
    `limit`, `payment_count` and friends are counts, not foreign keys), and
    an unmapped id raises instead of passing silently.
    """
    if isinstance(value, dict):
        return {
            key: (maps[key].translate(item) if key in maps else translate_body(item, maps))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [translate_body(item, maps) for item in value]
    return value


def assert_golden(actual: Any, expected: Any, *, context: str) -> None:
    """Byte-level equality with a readable failure (the whole body on diff)."""
    assert actual == expected, (
        f"{context}: replay diverged from the frozen golden\n"
        f"  actual   = {json.dumps(actual, sort_keys=True)[:2000]}\n"
        f"  expected = {json.dumps(expected, sort_keys=True)[:2000]}"
    )


def dataset_of(fixture: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    return fixture["datasets"][case["dataset"]]


def cases_of(fixture: dict[str, Any], methods: tuple[str, ...] = ("GET", "POST")) -> list[dict]:
    return [c for c in fixture["cases"] if c["request"]["method"] in methods]


def ids_of(cases: list[dict]) -> list[str]:
    return [c["id"] for c in cases]


def seed_budgets(session: Session, user: User, rows: list[dict], categories: IdMap) -> IdMap:
    """Budget definitions from the captured GET /budgets payload (definitions
    only — the server's own progress fields are stripped by the caller, same
    shape `budgets_progress` consumes). Returns the budget IdMap, assigned in
    captured order (the route answers ORDER BY Category.name regardless)."""
    from app.models.budgets import Budget

    mapping = IdMap()
    for row in sorted(rows, key=lambda r: r["id"]):
        budget = Budget(
            user_id=user.id,
            category_id=categories[row["category_id"]],
            amount=Decimal(str(row["amount"])),
            period=row["period"],
            carry=row.get("carry") or "off",
        )
        session.add(budget)
        session.flush()
        mapping.add(row["id"], budget.id)
    session.commit()
    return mapping

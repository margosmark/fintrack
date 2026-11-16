"""M124 — tag-based grouping and the Category x Tag matrix (docs/12 §1, docs/09).

Hand-calculated on small deterministic seeded datasets, same discipline as
`test_reports_spending_cashflow.py`: every expected number below is derived by
hand from the seed rows printed in each docstring, never read back from the
code under test. No network is touched.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from conftest import latency_budget_blocker
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_reports_spending_cashflow import (  # fixtures + helpers
    _batch,
    _category,
    _headers,
    _txn,
)
from test_tax_acb import _user, make_account

from app.models.accounts import AccountType
from app.models.import_batches import ImportBatchStatus
from app.models.tags import Tag
from app.models.transaction_tags import TransactionTag
from app.models.users import User

D = Decimal

RANGE = {"from": "2025-01-01", "to": "2025-03-31"}
JAN, FEB, MAR = "2025-01-01", "2025-02-01", "2025-03-01"


# --- seed -------------------------------------------------------------------


def _tag(session: Session, user: User, name: str) -> Tag:
    tag = Tag(user_id=user.id, name=name)
    session.add(tag)
    session.commit()
    session.refresh(tag)
    return tag


def _other_user(session: Session) -> User:
    """A second tenant — `_user` derives its email from the session id, so a
    second call in the same test would collide on the unique email."""
    user = User(email=f"m124-other-{id(session)}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _tag_txn(session: Session, txn_id: int | None, *tags: Tag) -> None:
    assert txn_id is not None  # a committed+refreshed row always has one
    for tag in tags:
        session.add(TransactionTag(transaction_id=txn_id, tag_id=tag.id))
    session.commit()


def _seed_tagged(session: Session) -> tuple[User, dict[str, int]]:
    """Dataset B — a Food > {Groceries, Dining} tree with tags on the spend.

        2025-01-05  -50.00  Groceries  tags vacation+work  (committed)
        2025-01-07 -999.00  Dining     tags vacation       (PENDING batch -> out)
        2025-01-10  -10.00 USD -> -13.60 CAD
                            Groceries  tags vacation
        2025-01-12   -5.00 USD, cad NULL
                            Groceries  tags vacation       (unresolved -> out)
        2025-02-10  -25.50  Dining     NO tags             -> Untagged
        2025-02-14  -12.25  (none)     tags work
        2025-02-20 +2000.00 income                         (positive -> out)

    Once-counted spend = 50.00 + 13.60 + 25.50 + 12.25 = **101.35**
    Tag blocks (the two-tag Jan 5 row counts in BOTH):
        vacation = 50.00 + 13.60 = **63.60**
        work     = 50.00 + 12.25 = **62.25**
        Untagged = **25.50**
        block sum = **151.35**  — over by exactly the 50.00 double count.
    """
    user = _user(session)
    food = _category(session, user, "Food")
    groceries = _category(session, user, "Groceries", parent=food)
    dining = _category(session, user, "Dining", parent=food)

    chequing = make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    usd = make_account(session, user, "USD", AccountType.savings, taxable=False)
    usd.currency = "USD"
    session.add(usd)
    session.commit()

    b_ok = _batch(session, user, chequing, ImportBatchStatus.committed)
    b_usd = _batch(session, user, usd, ImportBatchStatus.committed)
    b_pending = _batch(session, user, chequing, ImportBatchStatus.pending_review)

    vacation = _tag(session, user, "vacation")
    work = _tag(session, user, "work")

    t1 = _txn(session, user, chequing, b_ok, "2025-01-05", "-50.00", "-50.00", groceries)
    _txn(session, user, chequing, b_pending, "2025-01-07", "-999.00", "-999.00", dining)
    t3 = _txn(
        session, user, usd, b_usd, "2025-01-10", "-10.00", "-13.60", groceries, currency="USD"
    )
    t4 = _txn(session, user, usd, b_usd, "2025-01-12", "-5.00", None, groceries, currency="USD")
    _txn(session, user, chequing, b_ok, "2025-02-10", "-25.50", "-25.50", dining)
    t6 = _txn(session, user, chequing, b_ok, "2025-02-14", "-12.25", "-12.25", None)
    _txn(session, user, chequing, b_ok, "2025-02-20", "2000.00", "2000.00", None)

    _tag_txn(session, t1.id, vacation, work)
    _tag_txn(session, t3.id, vacation)
    _tag_txn(session, t4.id, vacation)  # unresolved CAD: tagged but never counted
    _tag_txn(session, t6.id, work)

    ids = {"food": food.id, "groceries": groceries.id, "dining": dining.id}
    ids.update({"vacation": vacation.id, "work": work.id})
    assert all(isinstance(value, int) for value in ids.values())  # committed + refreshed
    return user, ids  # type: ignore[return-value]


# --- R1: group_by=tag --------------------------------------------------------


def test_tag_monthly_series_matches_hand_sum(client: TestClient, session: Session) -> None:
    """The tag path buckets and zero-fills exactly like the category path."""
    user, cats = _seed_tagged(session)
    body = client.get(
        "/api/v1/reports/spending", params={**RANGE, "group_by": "tag"}, headers=_headers(user)
    ).json()

    assert (body["group_by"], body["total_cad"], body["spend_total_cad"]) == (
        "tag",
        "151.35",
        "101.35",
    )
    items = [
        (i["bucket"], i["tag_id"], i["tag_name"], D(str(i["total_cad"]))) for i in body["items"]
    ]
    assert items == [
        (JAN, cats["vacation"], "vacation", D("63.60")),
        (JAN, cats["work"], "work", D("50.00")),
        (FEB, cats["work"], "work", D("12.25")),
        (FEB, None, "Untagged", D("25.50")),  # no tags at all -> its own bucket
        (MAR, None, "Untagged", D("0")),  # zero-filled quiet bucket
    ]


def test_category_path_is_byte_identical_with_the_new_fields(
    client: TestClient, session: Session
) -> None:
    """R1's default is R0's behaviour: same items, same order, same numbers."""
    user, cats = _seed_tagged(session)
    body = client.get("/api/v1/reports/spending", params=RANGE, headers=_headers(user)).json()

    assert body["group_by"] == "category"
    items = [
        (i["bucket"], i["category_id"], i["category_name"], D(str(i["total_cad"])))
        for i in body["items"]
    ]
    assert items == [
        (JAN, cats["groceries"], "Groceries", D("63.60")),
        (FEB, cats["dining"], "Dining", D("25.50")),
        (FEB, None, "Uncategorized", D("12.25")),
        (MAR, None, "Uncategorized", D("0")),
    ]
    # With one dimension per transaction, blocks and spend agree exactly.
    assert D(str(body["total_cad"])) == D(str(body["spend_total_cad"])) == D("101.35")


def test_tag_breakdown_slices_and_honest_denominator(client: TestClient, session: Session) -> None:
    user, cats = _seed_tagged(session)
    body = client.get(
        "/api/v1/reports/spending/categories",
        params={**RANGE, "group_by": "tag"},
        headers=_headers(user),
    ).json()

    items = [(i["tag_id"], i["name"], D(str(i["cad_total"]))) for i in body["items"]]
    assert items == [
        (cats["vacation"], "vacation", D("63.60")),
        (cats["work"], "work", D("62.25")),
        (None, "Untagged", D("25.50")),
    ]
    assert D(str(body["total_cad"])) == D("151.35")
    assert D(str(body["spend_total_cad"])) == D("101.35")


def test_category_breakdown_still_rolls_up_to_roots(client: TestClient, session: Session) -> None:
    user, cats = _seed_tagged(session)
    body = client.get(
        "/api/v1/reports/spending/categories", params=RANGE, headers=_headers(user)
    ).json()
    items = [(i["category_id"], i["name"], D(str(i["cad_total"]))) for i in body["items"]]
    assert items == [(cats["food"], "Food", D("89.10")), (None, "Uncategorized", D("12.25"))]


def test_tag_csv_columns_follow_the_dimension(client: TestClient, session: Session) -> None:
    """`format=csv` headers are pinned to the JSON fields actually emitted."""
    user, cats = _seed_tagged(session)
    resp = client.get(
        "/api/v1/reports/spending",
        params={**RANGE, "group_by": "tag", "format": "csv"},
        headers=_headers(user),
    )
    assert resp.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == ["bucket", "tag_id", "tag_name", "total_cad"]
    assert ("2025-01-01", str(cats["vacation"]), "vacation", "63.60") in [
        tuple(r) for r in rows[1:]
    ]

    resp = client.get(
        "/api/v1/reports/spending/categories",
        params={**RANGE, "group_by": "tag", "format": "csv"},
        headers=_headers(user),
    )
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == ["tag_id", "name", "cad_total"]


def test_invalid_group_by_is_a_422_envelope(client: TestClient, session: Session) -> None:
    user, _ = _seed_tagged(session)
    resp = client.get(
        "/api/v1/reports/spending", params={**RANGE, "group_by": "colour"}, headers=_headers(user)
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


# --- R2: the drill filter ---------------------------------------------------


def test_drill_into_one_category_zero_fills_that_category(
    client: TestClient, session: Session
) -> None:
    user, cats = _seed_tagged(session)
    body = client.get(
        "/api/v1/reports/spending",
        params={**RANGE, "category_id": str(cats["groceries"])},
        headers=_headers(user),
    ).json()

    items = [
        (i["bucket"], i["category_id"], i["category_name"], D(str(i["total_cad"])))
        for i in body["items"]
    ]
    # Only Groceries' two resolved rows survive (50.00 + 13.60 = 63.60), and
    # the quiet months are Groceries-at-zero, not an "Uncategorized" red herring.
    assert items == [
        (JAN, cats["groceries"], "Groceries", D("63.60")),
        (FEB, cats["groceries"], "Groceries", D("0")),
        (MAR, cats["groceries"], "Groceries", D("0")),
    ]
    assert D(str(body["total_cad"])) == D("63.60")


def test_drill_on_a_parent_covers_its_descendants(client: TestClient, session: Session) -> None:
    """Food's drill reproduces the donut slice: children fold in (docs/12 §2)."""
    user, cats = _seed_tagged(session)
    body = client.get(
        "/api/v1/reports/spending",
        params={**RANGE, "category_id": str(cats["food"])},
        headers=_headers(user),
    ).json()

    items = [(i["bucket"], i["category_id"], D(str(i["total_cad"]))) for i in body["items"]]
    # The quiet month zero-fills to FOOD — the category actually drilled —
    # not to "Uncategorized", which would be a different question entirely.
    assert items == [
        (JAN, cats["groceries"], D("63.60")),
        (FEB, cats["dining"], D("25.50")),
        (MAR, cats["food"], D("0")),
    ]
    # 63.60 + 25.50 = 89.10 — the exact "Food" slice of the donut above it.
    assert D(str(body["total_cad"])) == D("89.10")


def test_drill_uncategorized_token(client: TestClient, session: Session) -> None:
    user, _cats = _seed_tagged(session)
    body = client.get(
        "/api/v1/reports/spending",
        params={**RANGE, "category_id": "uncategorized"},
        headers=_headers(user),
    ).json()

    items = [(i["bucket"], i["category_id"], D(str(i["total_cad"]))) for i in body["items"]]
    assert items == [(JAN, None, D("0")), (FEB, None, D("12.25")), (MAR, None, D("0"))]
    assert D(str(body["total_cad"])) == D("12.25")


def test_drill_composes_with_tag_grouping(client: TestClient, session: Session) -> None:
    """`group_by=tag&category_id=Groceries` — the card's motivating question."""
    user, cats = _seed_tagged(session)
    body = client.get(
        "/api/v1/reports/spending",
        params={**RANGE, "group_by": "tag", "category_id": str(cats["groceries"])},
        headers=_headers(user),
    ).json()

    items = [
        (i["bucket"], i["tag_id"], i["tag_name"], D(str(i["total_cad"]))) for i in body["items"]
    ]
    assert items == [
        (JAN, cats["vacation"], "vacation", D("63.60")),
        (JAN, cats["work"], "work", D("50.00")),
        (FEB, None, "Untagged", D("0")),
        (MAR, None, "Untagged", D("0")),
    ]
    assert D(str(body["spend_total_cad"])) == D("63.60")


def test_invalid_category_filter_is_a_422_envelope(client: TestClient, session: Session) -> None:
    user, _ = _seed_tagged(session)
    for bad in ("banana", "0", "-3"):
        resp = client.get(
            "/api/v1/reports/spending", params={**RANGE, "category_id": bad}, headers=_headers(user)
        )
        assert resp.status_code == 422, bad
        assert resp.json()["code"] == "invalid_category"


def test_drill_csv_carries_the_same_rows(client: TestClient, session: Session) -> None:
    user, cats = _seed_tagged(session)
    resp = client.get(
        "/api/v1/reports/spending",
        params={**RANGE, "category_id": str(cats["groceries"]), "format": "csv"},
        headers=_headers(user),
    )
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == ["bucket", "category_id", "category_name", "total_cad"]
    assert [tuple(r) for r in rows[1:]] == [
        (JAN, str(cats["groceries"]), "Groceries", "63.60"),
        (FEB, str(cats["groceries"]), "Groceries", "0.00"),
        (MAR, str(cats["groceries"]), "Groceries", "0.00"),
    ]


# --- R3: the Category x Tag matrix ------------------------------------------


def test_matrix_cells_rows_and_columns(client: TestClient, session: Session) -> None:
    """Long form: rows are RAW categories, columns the largest tags + Untagged."""
    user, cats = _seed_tagged(session)
    body = client.get(
        "/api/v1/reports/category-tag-matrix", params=RANGE, headers=_headers(user)
    ).json()

    assert [(c["tag_id"], c["tag_name"]) for c in body["columns"]] == [
        (cats["vacation"], "vacation"),  # 63.60 across categories
        (cats["work"], "work"),  # 62.25
        (None, "Untagged"),  # always last, never truncated away
    ]
    assert [(r["category_id"], r["category_name"]) for r in body["rows"]] == [
        (cats["groceries"], "Groceries"),  # 63.60
        (cats["dining"], "Dining"),  # 25.50
        (None, "Uncategorized"),  # 12.25
    ]
    cells = [(c["category_name"], c["tag_name"], D(str(c["cad_total"]))) for c in body["items"]]
    assert cells == [
        ("Groceries", "vacation", D("63.60")),
        ("Groceries", "work", D("50.00")),
        ("Dining", "Untagged", D("25.50")),
        ("Uncategorized", "work", D("12.25")),
    ]
    # The once-counted total, NOT the cell sum (138.35) — 50.00 is in two cells.
    assert D(str(body["total_cad"])) == D("101.35")


def test_matrix_rows_are_raw_not_root_rolled(client: TestClient, session: Session) -> None:
    """Groceries is a child of Food; rolling up would erase the asked-for row."""
    user, _cats = _seed_tagged(session)
    body = client.get(
        "/api/v1/reports/category-tag-matrix", params=RANGE, headers=_headers(user)
    ).json()
    names = [r["category_name"] for r in body["rows"]]
    assert "Groceries" in names and "Food" not in names


def test_matrix_columns_truncate_at_eight_with_an_other_bucket(
    client: TestClient, session: Session
) -> None:
    """Past the cut, spend becomes ONE labelled column instead of vanishing."""
    user = _user(session)
    food = _category(session, user, "Food")
    account = make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    batch = _batch(session, user, account, ImportBatchStatus.committed)
    tags = [_tag(session, user, f"t{i:02d}") for i in range(10)]

    # Each tag t00..t09 gets (10 - i) dollars on Food, so the ranking is
    # t00 > t01 > ... > t09 and the top-8 cut lands between t07 and t08.
    for i, tag in enumerate(tags):
        txn = _txn(
            session,
            user,
            account,
            batch,
            "2025-01-05",
            f"-{10 - i}.00",
            f"-{10 - i}.00",
            food,
        )
        _tag_txn(session, txn.id, tag)

    body = client.get(
        "/api/v1/reports/category-tag-matrix", params=RANGE, headers=_headers(user)
    ).json()
    names = [c["tag_name"] for c in body["columns"]]
    assert names[:8] == [f"t{i:02d}" for i in range(8)]
    assert names[8:] == ["Other tags", "Untagged"]  # t08 + t09 folded, Untagged last

    cells = {c["tag_name"]: D(str(c["cad_total"])) for c in body["items"]}
    # t08 (2.00) + t09 (1.00) = 3.00, not silently dropped.
    assert cells["Other tags"] == D("3.00")
    assert "t08" not in cells and "t09" not in cells


def test_matrix_csv_is_the_same_rows_long_form(client: TestClient, session: Session) -> None:
    user, cats = _seed_tagged(session)
    resp = client.get(
        "/api/v1/reports/category-tag-matrix",
        params={**RANGE, "format": "csv"},
        headers=_headers(user),
    )
    assert resp.headers["content-type"].startswith("text/csv")
    assert "category-tag-matrix-2025-01-01-2025-03-31.csv" in resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == ["category_id", "category_name", "tag_id", "tag_name", "cad_total"]
    assert [tuple(r) for r in rows[1:]] == [
        (str(cats["groceries"]), "Groceries", str(cats["vacation"]), "vacation", "63.60"),
        (str(cats["groceries"]), "Groceries", str(cats["work"]), "work", "50.00"),
        (str(cats["dining"]), "Dining", "", "Untagged", "25.50"),
        ("", "Uncategorized", str(cats["work"]), "work", "12.25"),
    ]


def test_matrix_is_tenant_scoped(client: TestClient, session: Session) -> None:
    """One user's tags and categories never leak into another's matrix."""
    _seed_tagged(session)
    other = _other_user(session)
    other_account = make_account(session, other, "Chequing", AccountType.chequing, taxable=False)
    other_batch = _batch(session, other, other_account, ImportBatchStatus.committed)
    other_cat = _category(session, other, "Food")
    other_tag = _tag(session, other, "vacation")  # SAME NAME as user 1's tag
    txn = _txn(
        session, other, other_account, other_batch, "2025-02-01", "-7.00", "-7.00", other_cat
    )
    _tag_txn(session, txn.id, other_tag)

    body = client.get(
        "/api/v1/reports/category-tag-matrix", params=RANGE, headers=_headers(other)
    ).json()
    assert [(r["category_id"], r["category_name"]) for r in body["rows"]] == [
        (other_cat.id, "Food")
    ]
    assert [(c["tag_id"], c["tag_name"]) for c in body["columns"]] == [
        (other_tag.id, "vacation"),
        (None, "Untagged"),
    ]
    assert [(c["category_id"], c["tag_id"]) for c in body["items"]] == [
        (other_cat.id, other_tag.id)
    ]
    assert body["total_cad"] == "7.00"


def test_matrix_requires_no_granularity_and_rejects_bad_range(
    client: TestClient, session: Session
) -> None:
    user, _ = _seed_tagged(session)
    resp = client.get(
        "/api/v1/reports/category-tag-matrix",
        params={"from": "2025-03-31", "to": "2025-01-01"},
        headers=_headers(user),
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_date_range"


# --- the tag helpers themselves ---------------------------------------------


def _counting_session(session: Session) -> tuple[list[tuple[str, object]], callable]:
    """`(statements, uninstall)` — records every SQL the engine runs, WITH its
    bound parameters (the EXPLAIN replay needs both).

    One listener for the whole probe and an explicit uninstall, rather than a
    per-case listener: `event.remove` needs the SAME callable it registered,
    and a factory that hands back a fresh closure each time silently fails.
    """
    from sqlalchemy import event

    statements: list[tuple[str, object]] = []
    engine = session.get_bind()

    def _record(_conn, _cursor, statement, parameters, *_args):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", _record)
    return statements, lambda: event.remove(engine, "before_cursor_execute", _record)


def test_tags_by_txn_is_one_statement_for_the_whole_range(session: Session) -> None:
    """docs/34 no-N+1: the tag lookup must not scale with the row count."""
    from app.services import reports as reports_service

    user, _ = _seed_tagged(session)
    statements, uninstall = _counting_session(session)
    try:
        reports_service._tags_by_txn(
            session,
            user.id,
            date(2025, 1, 1),
            date(2025, 3, 31),  # type: ignore[arg-type]
        )
    finally:
        uninstall()

    tag_statements = [s for s, _params in statements if "transaction_tags" in s]
    assert len(tag_statements) == 1, statements
    assert len(statements) <= 2, statements  # +1 for the token's user lookup


def test_tag_series_endpoints_are_query_bounded(client: TestClient, session: Session) -> None:
    """Tag grouping adds LOOKUPS, never per-row queries (docs/34).

    The dataset has 5 spend rows and 6 tag links. If the implementation ever
    fell back to a per-transaction query, the count would track those numbers;
    it must instead be a small constant that does not move when the ledger
    grows. So: measure at 1 ledger, then again after 4 more tagged rows.
    """
    user, cats = _seed_tagged(session)
    from sqlmodel import select as _select

    from app.models.accounts import Account

    # Resolved BEFORE the listener goes on: a statement this test itself runs
    # would otherwise be counted as the endpoint's.
    account = session.exec(_select(Account).where(Account.user_id == user.id)).first()
    assert account is not None  # _seed_tagged created one

    def _measure() -> dict[str, int]:
        """Statements per endpoint, counting only the REPORT tables.

        The `users` row is the token's auth lookup, which is cached after the
        first request and so appears in one measurement but not the other; it
        is not what this test is guarding. Everything that touches
        categories/tags/transaction_tags/transactions IS the endpoint.
        """
        statements, uninstall = _counting_session(session)
        counts: dict[str, int] = {}
        try:
            for label, path, params in (
                ("spending-tag", "/api/v1/reports/spending", {**RANGE, "group_by": "tag"}),
                (
                    "categories-tag",
                    "/api/v1/reports/spending/categories",
                    {**RANGE, "group_by": "tag"},
                ),
                ("matrix", "/api/v1/reports/category-tag-matrix", RANGE),
            ):
                before = len(statements)
                resp = client.get(path, params=params, headers=_headers(user))
                assert resp.status_code == 200, resp.text
                run = [s for s, _params in statements[before:]]
                counts[label] = len(
                    [
                        s
                        for s in run
                        if any(
                            table in s
                            for table in (
                                "categories",
                                "tags",
                                "transaction_tags",
                                "transactions",
                            )
                        )
                    ]
                )
        finally:
            uninstall()
        return counts

    first = _measure()

    # Grow the ledger by 4 tagged rows in the same range.
    batch = _batch(session, user, account, ImportBatchStatus.committed)
    vacation = Tag(user_id=user.id, name="extra")
    session.add(vacation)
    session.commit()
    session.refresh(vacation)
    for day in ("2025-01-20", "2025-01-21", "2025-01-22", "2025-01-23"):
        txn = _txn(session, user, account, batch, day, "-3.00", "-3.00", None)
        _tag_txn(session, txn.id, vacation)

    second = _measure()

    # THE invariant: the statement count is flat in the ledger size.
    assert first == second, (first, second)
    # And it is a small constant, not a per-row multiple of the 7 spend rows.
    assert first["spending-tag"] <= 4, first  # auth + categories + tags + links + txns
    assert cats["vacation"] > 0


# --- perf: 10k rows, indexed joins (M124 acceptance) -------------------------


_PERF_ROWS = 10_000
# A second tenant of the SAME size. Without it `user_id = X` matches every row
# in the table, Postgres correctly prefers a seq scan, and the plan assertions
# below would be testing nothing but a degenerate fixture.
_PERF_FILLER_ROWS = 10_000


def _seed_perf_tenant(session: Session, user: User, rows: int, prefix: str) -> int:
    """`rows` spend rows for one tenant, across 40 categories and 24 tags.

    Raw SQL on purpose: 10k ORM round-trips would measure the fixture, not the
    endpoint. Every row is committed, has a resolved CAD amount and exactly one
    tag — except every 7th, which carries a second, so the multi-tag
    duplication rule is exercised at scale and not just in the small seeds.

    Tag ids are derived arithmetically from a per-tenant base rather than
    shipped as a 10k-row parameter list, and the base is passed in so two
    tenants can never collide on `uq_tags_user_name`.
    """
    from sqlalchemy import text

    account = make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    batch = _batch(session, user, account, ImportBatchStatus.committed)
    category_ids = [_category(session, user, f"{prefix} Cat {i:02d}").id for i in range(40)]
    tag_ids = [_tag(session, user, f"{prefix} tag {i:02d}").id for i in range(24)]
    assert all(isinstance(cid, int) for cid in category_ids)
    assert all(isinstance(tid, int) for tid in tag_ids)
    tag_base = min(tag_ids)
    assert tag_ids == [tag_base + i for i in range(24)]

    now = "2026-01-01T00:00:00"
    session.execute(
        text(
            """
            INSERT INTO transactions
              (user_id, account_id, date, raw_description, normalized_payee,
               amount_native, currency, cad_amount, category_id, import_batch_id,
               dedup_hash, is_split, created_at, updated_at)
            SELECT :uid, :aid,
                   date '2025-01-01' + ((g % 365))::int,
                   'row ' || g, 'PAYEE',
                   -10.00, 'CAD', -10.00,
                   :cat_base + (g % 40), :bid,
                   :prefix || g, false,
                   CAST(:now AS timestamp), CAST(:now AS timestamp)
            FROM generate_series(0, :rows - 1) AS g
            """
        ),
        {
            "uid": user.id,
            "aid": account.id,
            "bid": batch.id,
            "cat_base": min(category_ids),
            "prefix": f"{prefix}-{user.id}-",
            "now": now,
            "rows": rows,
        },
    )
    session.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('transactions','id'),"
            " (SELECT MAX(id) FROM transactions))"
        )
    )
    link_count = session.execute(
        text(
            """
            INSERT INTO transaction_tags (transaction_id, tag_id, created_at, updated_at)
            SELECT t.id,
                   :tag_base + (t.id % 12) + CASE WHEN t.id % 7 = 0 THEN 12 ELSE 0 END,
                   now(), now()
            FROM transactions t
            WHERE t.user_id = :uid AND t.dedup_hash LIKE :prefix
            """
        ),
        {"uid": user.id, "tag_base": tag_base, "prefix": f"{prefix}-{user.id}-%"},
    ).rowcount
    assert link_count >= rows  # every row got at least one tag
    return rows


# The seed's stat refresh is part of the SEED, not a nicety: the EXPLAIN plans
# below are the acceptance, and a planner looking at an un-analyzed (or freshly
# emptied) `transactions` picks a seq scan honestly — so the assertions would
# then be pinning the stats, not the indexes.
#
# `ANALYZE` takes SHARE UPDATE EXCLUSIVE, which conflicts with the lock an
# autovacuum worker holds while it looks at the same table — and conftest sets
# a database-wide `lock_timeout = '500ms'` (AGENTS §5, BUG-3) so such a wait is
# CANCELLED rather than ridden out. `categories` is the highest-churn table in
# this database (every seeded registration writes ten rows into it), so while
# two full suites share one box the worker can hold it past 500 ms: one of two
# simultaneous full runs reded HERE on 2026-10-11 — `LockNotAvailable:
# canceling statement due to lock timeout` on `ANALYZE categories` — while the
# other ran clean (t_428d4b99). A stat refresh is not the thing under test, so
# wait for the transience (the `drop_database_force` idiom) instead of failing.
_ANALYZE_DEADLINE_S = 20.0
_ANALYZE_LOCK_TIMEOUT = "canceling statement due to lock timeout"


def _analyze(session: Session, table: str) -> None:
    """`ANALYZE <table>` on THIS transaction, retried while the lock is transient.

    It has to run on the caller's transaction: the session fixture's outer
    transaction is rolled back at teardown, so the 20k seeded rows are visible
    to this connection only, and stats computed anywhere else would describe an
    empty table.

    `begin_nested()` scopes each attempt to a SAVEPOINT, because a cancelled
    statement leaves the transaction aborted and only rolling back to a
    savepoint clears that WITHOUT losing the rows seeded before it. A blocker
    that never leaves raises after the deadline rather than hanging until the
    dispatcher reclaims the run.
    """
    import time

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    expires = time.monotonic() + _ANALYZE_DEADLINE_S
    delay = 0.25
    while True:
        try:
            with session.begin_nested():
                session.execute(text(f"ANALYZE {table}"))
            return
        except OperationalError as exc:
            if _ANALYZE_LOCK_TIMEOUT not in str(exc):
                raise
            if time.monotonic() >= expires:
                raise RuntimeError(
                    f"ANALYZE {table} waited {_ANALYZE_DEADLINE_S:.0f}s for a lock and never"
                    " got it — that is longer than autovacuum's transience: look at"
                    " pg_stat_activity for who holds it"
                ) from exc
        time.sleep(min(delay, max(expires - time.monotonic(), 0.0)))
        delay = min(delay * 2, 2.0)


def _seed_perf_ledger(session: Session) -> User:
    """Two tenants of `_PERF_ROWS` each, then ANALYZE both tables.

    The filler tenant is the point, not padding: with one tenant `user_id = X`
    is true of every row, so Postgres picks a seq scan and the plan assertions
    below would prove nothing. Two equal tenants is what makes the index the
    right answer — the same shape production is in from its second user on.
    """
    filler = _other_user(session)
    measured = _user(session)
    assert isinstance(filler.id, int) and isinstance(measured.id, int)
    _seed_perf_tenant(session, filler, _PERF_FILLER_ROWS, "filler")
    _seed_perf_tenant(session, measured, _PERF_ROWS, "measured")
    for table in ("transactions", "transaction_tags", "categories", "tags"):
        _analyze(session, table)
    session.commit()
    return measured


def _explain_plans(session: Session, statements: list[tuple[str, object]]) -> list[str]:
    """Replay each captured statement under EXPLAIN, same bound parameters.

    `exec_driver_sql` rather than `text()`: the captured SQL carries the
    driver's own `%(name)s` placeholders, so it has to go back out at driver
    level with the parameters SQLAlchemy captured alongside it.
    """
    plans: list[str] = []
    for statement, parameters in statements:
        body = "EXPLAIN " + statement.strip().rstrip(";")
        rows = session.connection().exec_driver_sql(body, parameters or ()).all()
        plans.append("\n".join(str(row[0]) for row in rows))
    return plans


def test_matrix_and_drill_are_indexed_at_10k_rows(client: TestClient, session: Session) -> None:
    """M124 perf cap: <1.5s first paint at 10k rows, on INDEXED joins.

    The wall-clock assertion is the acceptance number; the EXPLAIN assertions
    are what make it MEAN something — a plan that seq-scans `transactions`
    passes today on a fast disk and fails in production, so the plan shape is
    pinned here rather than left to the stopwatch. The clock itself is asked
    LAST and only when the box can answer it (`latency_budget_blocker`, docs/34),
    because AGENTS §5 runs suites concurrently: the plan and the functional
    counts must hold on every run, loaded or not (t_428d4b99).
    """
    import time

    from app.services import reports as reports_service

    user = _seed_perf_ledger(session)
    assert isinstance(user.id, int)

    # The two aggregates the matrix is built from, verbatim from the service.
    statements, uninstall = _counting_session(session)
    try:
        started = time.perf_counter()
        matrix = reports_service.category_tag_matrix(
            session, user.id, date(2025, 1, 1), date(2025, 12, 31)
        )
        matrix_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        drill = reports_service.spending_report(
            session,
            user.id,
            date(2025, 1, 1),
            date(2025, 12, 31),
            "month",
            group_by="tag",
            category_id=1,  # any real id; the point is the plan, not the slice
        )
        drill_ms = (time.perf_counter() - started) * 1000
        assert drill["items"], "the drill returned nothing at 10k rows"
    finally:
        uninstall()

    aggregating = [
        (s, params)
        for s, params in statements
        if "GROUP BY" in s.upper() and "transactions" in s and "users" not in s.split("FROM")[0]
    ]
    assert aggregating, statements  # the matrix really did run its aggregates here

    # --- the plans, not just the clock -------------------------------------
    plans = _explain_plans(session, aggregating)
    for plan in plans:
        assert "Seq Scan on transactions" not in plan, plan
        assert "ix_transactions_user_id_date" in plan or "Index" in plan, plan

    # Sanity: the seed really did land, so "fast" cannot mean "empty".
    assert len(matrix["columns"]) >= 9  # 8 leading tags + Untagged
    assert len(matrix["rows"]) == 40
    # Called at service level, so total_cad is a Decimal, not the JSON string:
    # 10000 rows x 10.00, each counted ONCE despite the two-tag rows.
    assert matrix["total_cad"] == D("100000.00")
    print(f"\nPERF matrix={matrix_ms:.1f}ms drill={drill_ms:.1f}ms rows={_PERF_ROWS}")

    # --- the acceptance number ---------------------------------------------
    # Everything above is enforced on EVERY run — plan shape, the functional
    # counts, the printed timings. The clock is the one claim this box cannot
    # always answer: AGENTS §5 lets suites run concurrently on one CPU pool, so
    # the same two queries took 2355 ms in one of two simultaneous full suites
    # at load 11/4 cores (t_428d4b99) while this whole file ran green in
    # 6.75 s solo. It is the PLAN above that catches a seq-scan in production,
    # not the stopwatch — docs/34's rule, through conftest's helper: latency is
    # reported, never enforced, on a shared machine.
    blocker = latency_budget_blocker()
    if blocker is not None:
        pytest.skip(
            "1.5s matrix/drill budget not measurable on this box: "
            f"{blocker}; measured matrix={matrix_ms:.1f}ms drill={drill_ms:.1f}ms"
            f" at {_PERF_ROWS} rows (plan + functional assertions above"
            " enforced regardless)"
        )
    assert matrix_ms < 1500, f"matrix took {matrix_ms:.0f}ms at {_PERF_ROWS} rows"
    assert drill_ms < 1500, f"drill took {drill_ms:.0f}ms at {_PERF_ROWS} rows"


def test_stat_refresh_rides_out_a_transient_table_lock(session: Session) -> None:
    """The seed's `ANALYZE` waits out a lock holder instead of dying on the 500 ms timeout.

    The red this reproduces on purpose: with two full suites on the box, an
    autovacuum worker held `categories` past the suite-wide `lock_timeout` and
    `_seed_perf_ledger` failed on `LockNotAvailable` — one of two simultaneous
    runs, green in the other (t_428d4b99). A decoy holds ACCESS EXCLUSIVE for
    1.2 s (more than two timeouts) and the refresh must return once it leaves,
    still on the caller's transaction — nothing in the perf path is committed,
    so a refresh that had to restart the seed would be the wrong fix.
    """
    import threading
    import time

    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    from app.api.deps import get_engine

    decoy_engine = create_engine(get_engine().url, poolclass=NullPool)
    locked = threading.Event()

    def hold() -> None:
        with decoy_engine.connect() as conn:
            # A LOCK is held to the end of its TRANSACTION, so this must NOT be
            # an autocommit connection: SQLAlchemy's autobegin keeps the
            # transaction (and the lock) open until the connection closes.
            conn.execute(text("LOCK TABLE categories IN ACCESS EXCLUSIVE MODE"))
            locked.set()
            time.sleep(1.2)

    holder = threading.Thread(target=hold, name="categories-lock-decoy")
    holder.start()
    try:
        assert locked.wait(timeout=10), "the decoy never took the lock"
        started = time.perf_counter()
        _analyze(session, "categories")
        waited = time.perf_counter() - started
    finally:
        holder.join(timeout=30)
        decoy_engine.dispose()
    assert waited >= 1.0, (
        f"the decoy held the lock 1.2s but the refresh returned in {waited:.2f}s —"
        " it cannot have waited, so it cannot ride out autovacuum either"
    )


def test_saved_view_accepts_group_by_tag() -> None:
    """M108 P2's saved views must be able to restore the new lens (docs/02)."""
    from app.schemas.users import _validate_saved_state

    assert _validate_saved_state({"group_by": "tag"}) == {"group_by": "tag"}
    assert _validate_saved_state({"group_by": "owner"}) == {"group_by": "owner"}
    try:
        _validate_saved_state({"group_by": "colour"})
    except ValueError as exc:
        assert "category|tag|owner" in str(exc)
    else:  # pragma: no cover — the validator must reject an unknown lens
        raise AssertionError("an unknown group_by must be a ValueError")

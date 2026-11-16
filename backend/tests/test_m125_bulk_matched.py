"""M125 — bulk over a MATCHED set: one request, the page's own filter.

Specs pinned here (docs/08 §Bulk over a matched set, docs/09 §Bulk selection
toolbar §Select-all-matched, docs/02 §transaction_events, docs/19):

* **filter equivalence, proven server-side** — for every filter shape the page
  can build, the JSON `filter` body and the equivalent `GET /transactions`
  query select the SAME rows, and the bulk run touches exactly those rows and
  no others (checked by re-listing with the same filter afterwards, not by
  trusting the returned count);
* **the matched set is the whole set** — a filter spanning three pages applies
  to every matched row, and `matched` equals the pagination's `total`;
* **exceptions are subtracted, not ignored** — an `exclude_ids` row survives
  and leaves the count;
* **the delete gate is count-aware** — a body whose `expected_count`
  disagrees with what the filter now matches writes NOTHING
  (`409 bulk_count_mismatch`);
* **partial success still reports per row** — a split child the single-row
  routes refuse is named in `skipped` with the server's own `{detail, code}`
  while the rest of the batch lands;
* **receipts** — one per affected row, never two, never zero, and a re-run that
  changes nothing writes none (M120);
* **the filter's ownership and `q` guards answer exactly as `GET` does**, and
  `/bulk/*` is registered ahead of `/{transaction_id}/*` (a swallowed route
  would answer 422 on `int("bulk")`).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, update
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.transaction_events import TransactionEvent, TransactionEventKind
from app.models.transaction_tags import TransactionTag
from app.models.transactions import Transaction

API = "/api/v1"
BULK = f"{API}/transactions/bulk"
PASSWORD = "m125-bulk-pass-1"


# --- helpers -----------------------------------------------------------------


def _register(client: TestClient) -> tuple[dict[str, str], int]:
    """(headers, user_id) — a fresh tenant per test (rolled back at teardown)."""
    email = f"m125-{uuid.uuid4().hex[:12]}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    body = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert body.status_code == 200, body.text
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    me = client.get(f"{API}/users/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, int(me.json()["id"])


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def _account(client: TestClient, headers: dict[str, str], name: str) -> int:
    response = client.post(
        f"{API}/accounts",
        headers=headers,
        json={"name": name, "type": "chequing", "currency": "CAD"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _category(client: TestClient, headers: dict[str, str], name: str | None = None) -> int:
    response = client.post(
        f"{API}/categories", headers=headers, json={"name": name or _uniq("cat")}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _tag(client: TestClient, headers: dict[str, str], name: str | None = None) -> int:
    response = client.post(f"{API}/tags", headers=headers, json={"name": name or _uniq("tag")})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _seed(
    session: Session,
    *,
    user_id: int,
    account_id: int,
    count: int,
    payee_prefix: str = "Merchant",
    start_day: date = date(2026, 3, 1),
    **overrides: Any,
) -> list[int]:
    """`count` rows through the SESSION (fast) — one distinct payee each, so
    the dedup unique index never trips. Month rolls over every 28 rows, and
    the sign ALTERNATES so both `direction` filters have something to select."""
    rows: list[Transaction] = []
    for index in range(count):
        payee = f"{payee_prefix} {index:04d}"
        day = date(start_day.year, start_day.month + (index // 28), 1 + (index % 28))
        sign = "-" if index % 2 == 0 else "+"
        rows.append(
            Transaction(
                user_id=user_id,
                account_id=account_id,
                date=day,
                raw_description=payee,
                normalized_payee=payee,
                amount_native=Decimal(f"{sign}{10 + (index % 7)}.00"),
                currency="CAD",
                dedup_hash=hashlib.sha256(f"{payee}-{day}-{account_id}".encode()).hexdigest(),
                **overrides,
            )
        )
    session.add_all(rows)
    session.commit()
    return [int(row.id) for row in rows if row.id is not None]


def _list(client: TestClient, headers: dict[str, str], params: dict[str, Any]) -> dict:
    response = client.get(f"{API}/transactions", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(page: dict) -> list[int]:
    return [int(item["id"]) for item in page["items"]]


def _code(response) -> str:
    return response.json().get("code", "")


def _events(session: Session, kind: TransactionEventKind, ids: list[int]) -> int:
    return int(
        session.exec(
            select(func.count())
            .select_from(TransactionEvent)
            .where(TransactionEvent.event_kind == kind, TransactionEvent.tx_id.in_(ids))
        ).one()
    )


def _reload(session: Session, transaction_id: int) -> Transaction:
    row = session.get(Transaction, transaction_id)
    assert row is not None, f"transaction {transaction_id} vanished"
    session.refresh(row)
    return row


def _reset_categories(
    session: Session, user_id: int, categorized: list[int], category: int
) -> None:
    """Restore the seeded category state between equivalence shapes.

    Every shape recategorizes its own match set to one target category; without
    this the PREVIOUS shape's rows would still carry it and the "nothing
    outside the set moved" assertion would be measuring leftovers, not the run.
    """
    session.execute(
        update(Transaction).where(Transaction.user_id == user_id).values(category_id=None)
    )
    if categorized:
        session.execute(
            update(Transaction).where(Transaction.id.in_(categorized)).values(category_id=category)
        )
    session.commit()
    session.expire_all()


# --- 1. the filter body IS the page's filter ---------------------------------


def _filter_shapes(
    account_id: int, other_account_id: int, category_id: int, tag_id: int
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """(label, JSON `filter`, the SAME filter as `GET` query params).

    Two spellings of ONE filter — `category_id` is the string `"null"` in JSON
    and on a query string, an int in both, the rest field-identical — so a
    divergence between the two parsers cannot hide behind a matching count.
    """
    return [
        ("no filter", {}, {}),
        ("account", {"account_id": account_id}, {"account_id": account_id}),
        ("category id", {"category_id": category_id}, {"category_id": str(category_id)}),
        ("uncategorized", {"category_id": "null"}, {"category_id": "null"}),
        ("from", {"from": "2026-04-01"}, {"from": "2026-04-01"}),
        ("to", {"to": "2026-06-30"}, {"to": "2026-06-30"}),
        (
            "date range",
            {"from": "2026-04-01", "to": "2026-06-30"},
            {"from": "2026-04-01", "to": "2026-06-30"},
        ),
        ("payee", {"payee": "Merchant 000"}, {"payee": "Merchant 000"}),
        ("amount min", {"amount_min": "-14"}, {"amount_min": "-14"}),
        ("amount max", {"amount_max": "-13"}, {"amount_max": "-13"}),
        (
            "amount range",
            {"amount_min": "-14", "amount_max": "-13"},
            {"amount_min": "-14", "amount_max": "-13"},
        ),
        ("direction paid", {"direction": "paid"}, {"direction": "paid"}),
        ("direction received", {"direction": "received"}, {"direction": "received"}),
        ("tag", {"tag_id": tag_id}, {"tag_id": str(tag_id)}),
        ("tag_ids x1", {"tag_ids": [tag_id]}, {"tag_ids": [str(tag_id)]}),
        ("reviewed", {"reviewed": True}, {"reviewed": "true"}),
        (
            "account + category + range",
            {
                "account_id": account_id,
                "category_id": category_id,
                "from": "2026-04-01",
                "amount_max": "-13",
                "direction": "paid",
            },
            {
                "account_id": account_id,
                "category_id": str(category_id),
                "from": "2026-04-01",
                "amount_max": "-13",
                "direction": "paid",
            },
        ),
        ("other account", {"account_id": other_account_id}, {"account_id": other_account_id}),
    ]


def test_bulk_filter_body_selects_exactly_the_rows_the_page_filter_renders(
    client: TestClient, session: Session
) -> None:
    """The equivalence check (docs/09): same filter, two spellings, SAME rows.

    For every shape: read the page's ids and `total`, run the bulk action with
    the JSON body, then RE-LIST with the same query filter and prove the row
    set is byte-identical. A count alone could agree while the SET differed.
    """
    headers, user_id = _register(client)
    account_a = _account(client, headers, _uniq("CIBC"))
    account_b = _account(client, headers, _uniq("Savings"))
    category = _category(client, headers)
    tag = _tag(client, headers)

    _seed(session, user_id=user_id, account_id=account_a, count=60, payee_prefix="Merchant")
    _seed(session, user_id=user_id, account_id=account_b, count=25, payee_prefix="Other")

    a_ids = [
        int(row.id)
        for row in session.exec(
            select(Transaction).where(
                Transaction.account_id == account_a, Transaction.user_id == user_id
            )
        ).all()
    ]
    categorized: list[int] = []
    for index, tx_id in enumerate(a_ids):
        if index % 2 == 0:
            _reload(session, tx_id).category_id = category
            categorized.append(tx_id)
        if index % 10 == 0:
            session.add(TransactionTag(transaction_id=tx_id, tag_id=tag))
        if index % 5 == 0:
            _reload(session, tx_id).reviewed_at = datetime(2026, 1, 2, tzinfo=UTC)
    session.commit()
    session.expire_all()

    target = _category(client, headers, "Bulk target")
    shapes = _filter_shapes(account_a, account_b, category, tag)
    assert len(shapes) >= 18

    for label, body_filter, query in shapes:
        expected_page = _list(client, headers, {**query, "limit": 200})
        expected_ids = _ids(expected_page)
        assert expected_ids, f"{label}: the shape selected nothing — the check is vacuous"

        response = client.post(
            f"{BULK}/recategorize",
            headers=headers,
            json={"filter": body_filter, "exclude_ids": [], "category_id": target},
        )
        assert response.status_code == 200, f"{label}: {response.text}"
        body = response.json()
        assert body["matched"] == len(expected_ids), f"{label}: matched"
        assert body["matched"] == expected_page["total"], f"{label}: total"
        assert body["updated"] == body["matched"], f"{label}: updated"
        assert body["skipped"] == [], f"{label}: skipped"

        # The set that MOVED is exactly the set the page would have rendered…
        moved = _ids(_list(client, headers, {**query, "category_id": str(target), "limit": 200}))
        assert sorted(moved) == sorted(expected_ids), label
        # …and nothing outside it moved.
        outsiders = [
            int(row.id)
            for row in session.exec(
                select(Transaction).where(
                    Transaction.user_id == user_id, Transaction.category_id == target
                )
            ).all()
            if int(row.id) not in set(expected_ids)
        ]
        assert outsiders == [], f"{label}: touched {len(outsiders)} out-of-set rows"

        _reset_categories(session, user_id, categorized, category)


def test_bulk_filter_accepts_q_and_acts_on_the_ranked_set(
    client: TestClient, session: Session
) -> None:
    """M137 (docs/33 §Server-first computing): `q` is a FIRST-CLASS filter.

    A bulk run over a ranked set touches exactly the rows `GET /transactions?q=`
    ranked — the same `_ledger_scope` predicate, so select-all-matched's counter
    and the page it mirrors cannot disagree. (Before M137 both routes refused
    `q` with 422 `q_removed`.)"""
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    _seed(session, user_id=user_id, account_id=account, count=3, payee_prefix="Merchant")
    _seed(session, user_id=user_id, account_id=account, count=2, payee_prefix="FreshCo")

    get_q = client.get(f"{API}/transactions", headers=headers, params={"q": "Merchant"})
    assert get_q.status_code == 200, get_q.text
    matched = [row["id"] for row in get_q.json()["items"]]
    assert len(matched) == 3, "only the Merchant family ranks"

    response = client.post(
        f"{BULK}/recategorize",
        headers=headers,
        json={
            "filter": {"q": "Merchant"},
            "exclude_ids": [],
            "category_id": None,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["matched"] == 3
    assert response.json()["updated"] == 3

    # The FreshCo rows are outside the ranked set and untouched.
    fresh = [
        row.id
        for row in session.exec(
            select(Transaction).where(
                Transaction.user_id == user_id, Transaction.normalized_payee.startswith("FreshCo")
            )
        ).all()
    ]
    assert len(fresh) == 2
    category = _category(client, headers)
    client.post(
        f"{BULK}/recategorize",
        headers=headers,
        json={"filter": {"q": "Merchant"}, "exclude_ids": [], "category_id": category},
    )
    for row in session.exec(select(Transaction).where(Transaction.id.in_(fresh))).all():
        assert row.category_id != category, "outside the ranked set must not move"


def test_bulk_filter_answers_the_same_ownership_404s_as_get(
    client: TestClient, session: Session
) -> None:
    """A foreign id in the FILTER is a 404 before any row is read — the same
    guard, in the same place, for the body as for the query string."""
    headers, _user_id = _register(client)
    account = _account(client, headers, _uniq("A"))

    other_headers, _other_id = _register(client)
    foreign_account = _account(client, other_headers, _uniq("Foreign"))
    foreign_category = _category(client, other_headers)
    foreign_tag = _tag(client, other_headers)

    for body_filter in [
        {"account_id": foreign_account},
        {"category_id": foreign_category},
        {"tag_id": foreign_tag},
        {"tag_ids": [foreign_tag]},
    ]:
        response = client.post(
            f"{BULK}/review",
            headers=headers,
            json={"filter": body_filter, "exclude_ids": [], "reviewed": True},
        )
        assert response.status_code == 404, body_filter
        assert _code(response) == "not_found"

    # A foreign TARGET category (not a filter id) is refused too.
    response = client.post(
        f"{BULK}/recategorize",
        headers=headers,
        json={
            "filter": {"account_id": account},
            "exclude_ids": [],
            "category_id": foreign_category,
        },
    )
    assert response.status_code == 404 and _code(response) == "not_found"

    # An unknown filter key is a validation error, not a silent no-op.
    response = client.post(
        f"{BULK}/review",
        headers=headers,
        json={"filter": {"nonsense": 1}, "exclude_ids": [], "reviewed": True},
    )
    assert response.status_code == 422 and _code(response) == "validation_error"


# --- 2. the matched set is the WHOLE set -------------------------------------


def test_select_all_matched_recategorizes_a_set_that_spans_several_pages(
    client: TestClient, session: Session
) -> None:
    """121 rows behind a 50-row page: ONE request moves all of them, and
    `matched` is the pagination's `total` — the number the counter printed."""
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("CIBC Chequing"))
    decoy = _account(client, headers, _uniq("Savings"))
    matched_ids = _seed(session, user_id=user_id, account_id=account, count=121)
    _seed(session, user_id=user_id, account_id=decoy, count=30, payee_prefix="Decoy")

    page_one = _list(client, headers, {"account_id": account, "limit": 50, "offset": 0})
    page_last = _list(client, headers, {"account_id": account, "limit": 50, "offset": 100})
    assert page_one["total"] == 121
    assert len(page_last["items"]) == 21  # genuinely three pages

    target = _category(client, headers, "Matched target")
    response = client.post(
        f"{BULK}/recategorize",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "category_id": target},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"matched": 121, "updated": 121, "skipped": []}

    moved = session.exec(
        select(Transaction).where(Transaction.user_id == user_id, Transaction.category_id == target)
    ).all()
    assert {int(row.id) for row in moved} == set(matched_ids)
    # The decoy account was never part of the selection.
    assert all(row.account_id != decoy for row in moved)


def test_reading_one_page_then_mutating_the_rest_keeps_the_count_right(
    client: TestClient, session: Session
) -> None:
    """E (docs/09): the counter's number is the SERVER's, so the set can be
    mutated from rows the page never showed without the count going stale.

    The exact sequence the acceptance names — read ONE page of a MIXED set,
    mutate the OTHER pages' rows, return. The page path cannot even name those
    rows (it only ever holds the 50 ids in front of it); the matched path
    names them by filter plus the 50 ids it did see, as exceptions.
    """
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("CIBC Chequing"))
    category = _category(client, headers)
    tag = _tag(client, headers)
    all_ids = _seed(session, user_id=user_id, account_id=account, count=121)

    # MIXED: a third categorized, a tenth tagged, a fifth already reviewed —
    # the three filter dimensions have to hold across the pages too.
    for index, tx_id in enumerate(all_ids):
        if index % 3 == 0:
            _reload(session, tx_id).category_id = category
        if index % 10 == 0:
            session.add(TransactionTag(transaction_id=tx_id, tag_id=tag))
        if index % 5 == 0:
            _reload(session, tx_id).reviewed_at = datetime(2026, 1, 2, tzinfo=UTC)
    session.commit()
    session.expire_all()

    def reviewed_by_id(ids: list[int]) -> dict[int, bool]:
        return {tx_id: _reload(session, tx_id).reviewed_at is not None for tx_id in ids}

    # 1. read ONE page — 121 total, 50 ids, nothing else fetched over the API.
    page_one = _list(client, headers, {"account_id": account, "limit": 50, "offset": 0})
    assert page_one["total"] == 121
    seen = _ids(page_one)
    assert len(seen) == 50
    unseen = [tx_id for tx_id in all_ids if tx_id not in set(seen)]
    assert len(unseen) == 71  # pages 2 and 3
    seen_before = reviewed_by_id(seen)

    # 2. mutate those other pages' rows — matched by FILTER, minus the ids the
    #    page DID show. `matched` is 71 because the exceptions were honoured.
    response = client.post(
        f"{BULK}/review",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": seen, "reviewed": True},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"matched": 71, "updated": 71, "skipped": []}
    assert all(reviewed_by_id(unseen).values()), "the unseen pages did not move"
    assert reviewed_by_id(seen) == seen_before, "the page that was read moved"

    # 3. return: the page is byte-identical, and its count still describes the
    #    WHOLE set — not 50 (what was read) and not 71 (what was mutated).
    again = _list(client, headers, {"account_id": account, "limit": 50, "offset": 0})
    assert again["total"] == 121
    assert _ids(again) == seen
    final = client.post(
        f"{BULK}/review",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "reviewed": True},
    )
    assert final.status_code == 200, final.text
    assert final.json() == {"matched": 121, "updated": 121, "skipped": []}


def test_exclude_ids_are_subtracted_from_the_matched_set(
    client: TestClient, session: Session
) -> None:
    """A row unchecked out of the matched set survives the run AND leaves the
    count — the exceptions list is honoured, not decorative."""
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    ids = _seed(session, user_id=user_id, account_id=account, count=10)
    kept = ids[3]

    target = _category(client, headers)
    response = client.post(
        f"{BULK}/recategorize",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [kept], "category_id": target},
    )
    assert response.status_code == 200, response.text
    assert response.json()["matched"] == 9

    assert _reload(session, kept).category_id != target
    moved = session.exec(
        select(Transaction).where(Transaction.user_id == user_id, Transaction.category_id == target)
    ).all()
    assert {int(row.id) for row in moved} == set(ids) - {kept}


def test_exclude_ids_beyond_the_documented_cap_are_refused(
    client: TestClient, session: Session
) -> None:
    """5001 exceptions is past any human click-path — refused, not truncated."""
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    _seed(session, user_id=user_id, account_id=account, count=2)
    response = client.post(
        f"{BULK}/review",
        headers=headers,
        json={"filter": {}, "exclude_ids": list(range(1, 5002)), "reviewed": True},
    )
    assert response.status_code == 422
    assert _code(response) == "validation_error"


# --- 3. the count-aware delete gate ------------------------------------------


def test_bulk_delete_refuses_a_count_that_is_not_the_confirmed_one(
    client: TestClient, session: Session
) -> None:
    """`expected_count` is checked BEFORE the first row: a set that moved
    writes nothing (`409 bulk_count_mismatch`)."""
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    other = _account(client, headers, _uniq("B"))
    ids = _seed(session, user_id=user_id, account_id=account, count=10)
    _seed(session, user_id=user_id, account_id=other, count=4, payee_prefix="Keep")

    wrong = client.post(
        f"{BULK}/delete",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "expected_count": 9},
    )
    assert wrong.status_code == 409
    assert _code(wrong) == "bulk_count_mismatch"
    assert "10" in wrong.json()["detail"]
    # Nothing at all was written.
    total = session.exec(
        select(func.count()).select_from(Transaction).where(Transaction.user_id == user_id)
    ).one()
    assert int(total) == 14

    right = client.post(
        f"{BULK}/delete",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "expected_count": 10},
    )
    assert right.status_code == 200, right.text
    assert right.json() == {"matched": 10, "updated": 10, "skipped": []}

    surviving = {
        int(row.id)
        for row in session.exec(select(Transaction).where(Transaction.user_id == user_id)).all()
    }
    assert surviving.isdisjoint(set(ids))
    assert len(surviving) == 4  # the other account's four rows


def test_bulk_delete_honours_the_exceptions_list_in_the_count(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    ids = _seed(session, user_id=user_id, account_id=account, count=6)
    kept = ids[0]

    response = client.post(
        f"{BULK}/delete",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [kept], "expected_count": 5},
    )
    assert response.status_code == 200, response.text
    assert response.json()["matched"] == 5
    session.expire_all()
    remaining = session.exec(select(Transaction).where(Transaction.user_id == user_id)).all()
    assert [int(row.id) for row in remaining] == [kept]


# --- 4. partial success names the row it refused -----------------------------


def test_bulk_delete_skips_a_split_child_and_says_so(client: TestClient, session: Session) -> None:
    """The same refusal the single-row `DELETE` gives (`split_leg_immutable`),
    reported in the M96 conflict shape while the rest of the batch lands."""
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    ordinary = _seed(session, user_id=user_id, account_id=account, count=3, payee_prefix="Plain")
    parent = client.post(
        f"{API}/transactions",
        headers=headers,
        json={
            "account_id": account,
            "date": "2026-07-04",
            "raw_description": "COSTCO",
            "amount_native": "-187.42",
            "currency": "CAD",
        },
    )
    assert parent.status_code == 201, parent.text
    parent_id = int(parent.json()["id"])
    split = client.post(
        f"{API}/transactions/{parent_id}/split",
        headers=headers,
        json={"children": [{"amount_native": "-120.00"}, {"amount_native": "-67.42"}]},
    )
    assert split.status_code == 201, split.text
    child_ids = [int(child["id"]) for child in split.json()["children"]]

    response = client.post(
        f"{BULK}/delete",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "expected_count": 5},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # LEDGER: the parent drops out of the population, its two children stay in.
    assert body["matched"] == 5
    assert body["updated"] == 3
    assert {int(row["id"]) for row in body["skipped"]} == set(child_ids)
    for row in body["skipped"]:
        assert row["code"] == "split_leg_immutable"
        assert row["detail"]  # the server's own sentence
        assert row["label"]  # NAMED, not `#4711`

    session.expire_all()
    surviving = {
        int(row.id)
        for row in session.exec(select(Transaction).where(Transaction.user_id == user_id)).all()
    }
    assert surviving == set(child_ids) | {parent_id}
    assert all(int(i) not in surviving for i in ordinary)


# --- 5. receipts: one per affected row, and none on a re-run -----------------


def test_bulk_recategorize_writes_exactly_one_receipt_per_row_that_moved(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    ids = _seed(session, user_id=user_id, account_id=account, count=12)
    # Four rows already carry the target: PATCH writes no receipt for a no-op,
    # and neither may this.
    target = _category(client, headers, "Already there")
    for tx_id in ids[:4]:
        _reload(session, tx_id).category_id = target
    session.commit()
    session.expire_all()

    response = client.post(
        f"{BULK}/recategorize",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "category_id": target},
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 12
    assert _events(session, TransactionEventKind.categorized, ids) == 8

    # Re-run: nothing moves, so nothing is logged.
    again = client.post(
        f"{BULK}/recategorize",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "category_id": target},
    )
    assert again.status_code == 200
    assert again.json()["updated"] == 12
    assert _events(session, TransactionEventKind.categorized, ids) == 8


def test_bulk_review_is_idempotent_and_matches_the_single_row_route(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    ids = _seed(session, user_id=user_id, account_id=account, count=7)

    first = client.post(
        f"{BULK}/review",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "reviewed": True},
    )
    assert first.status_code == 200 and first.json()["matched"] == 7
    assert _events(session, TransactionEventKind.reviewed, ids) == 7
    assert all(_reload(session, tx_id).reviewed_at is not None for tx_id in ids)

    # Re-running the SAME state writes no receipt (M106's idempotence rule).
    second = client.post(
        f"{BULK}/review",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "reviewed": True},
    )
    assert second.status_code == 200
    assert second.json() == {"matched": 7, "updated": 7, "skipped": []}
    assert _events(session, TransactionEventKind.reviewed, ids) == 7

    # And unreviewing is the same route with the flag flipped.
    back = client.post(
        f"{BULK}/review",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "reviewed": False},
    )
    assert back.status_code == 200
    assert all(_reload(session, tx_id).reviewed_at is None for tx_id in ids)
    assert _events(session, TransactionEventKind.reviewed, ids) == 14


def test_bulk_add_tag_is_idempotent_and_needs_no_per_row_route(
    client: TestClient, session: Session
) -> None:
    """`POST /transactions/bulk/tags` must NOT be swallowed by the later
    `POST /transactions/{transaction_id}/tags` (registration order — a
    swallowed route answers 422 on `int("bulk")`)."""
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    ids = _seed(session, user_id=user_id, account_id=account, count=6)
    tag = _tag(client, headers, "vacation")

    first = client.post(
        f"{BULK}/tags",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "tag_id": tag},
    )
    assert first.status_code == 200, first.text
    assert first.json()["matched"] == 6
    assert _events(session, TransactionEventKind.tagged, ids) == 6
    linked = session.exec(
        select(TransactionTag).where(TransactionTag.transaction_id.in_(ids))
    ).all()
    assert len(linked) == 6

    second = client.post(
        f"{BULK}/tags",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "tag_id": tag},
    )
    assert second.status_code == 200
    assert _events(session, TransactionEventKind.tagged, ids) == 6  # no duplicates


def test_bulk_payee_renames_the_matched_set_and_receipts_each_row(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    ids = _seed(session, user_id=user_id, account_id=account, count=5)

    response = client.post(
        f"{BULK}/payee",
        headers=headers,
        json={
            "filter": {"account_id": account},
            "exclude_ids": [],
            "normalized_payee": "Unified Payee",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"matched": 5, "updated": 5, "skipped": []}
    assert _events(session, TransactionEventKind.edited, ids) == 5
    session.expire_all()
    rows = session.exec(select(Transaction).where(Transaction.user_id == user_id)).all()
    assert {row.normalized_payee for row in rows} == {"Unified Payee"}


def test_bulk_delete_writes_one_deleted_receipt_per_row(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    ids = _seed(session, user_id=user_id, account_id=account, count=4)

    response = client.post(
        f"{BULK}/delete",
        headers=headers,
        json={"filter": {"account_id": account}, "exclude_ids": [], "expected_count": 4},
    )
    assert response.status_code == 200, response.text
    # The receipt outlives the row (`transaction_events.tx_id` has no FK).
    assert _events(session, TransactionEventKind.deleted, ids) == 4
    session.expire_all()
    surviving = session.exec(select(Transaction).where(Transaction.user_id == user_id)).all()
    assert surviving == []


# --- 6. the shape of the request ---------------------------------------------


def test_bulk_requires_an_explicit_filter_and_rejects_unknown_keys(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    _seed(session, user_id=user_id, account_id=account, count=2)

    missing = client.post(f"{BULK}/review", headers=headers, json={"reviewed": True})
    assert missing.status_code == 422 and _code(missing) == "validation_error"

    extra = client.post(
        f"{BULK}/review",
        headers=headers,
        json={"filter": {}, "exclude_ids": [], "reviewed": True, "surprise": 1},
    )
    assert extra.status_code == 422 and _code(extra) == "validation_error"

    # `category_id` inside the FILTER is a filter; the run's own target is the
    # payload-level `category_id`. A wrong target type is still 422.
    bad = client.post(
        f"{BULK}/recategorize",
        headers=headers,
        json={"filter": {}, "exclude_ids": [], "category_id": "not-an-id"},
    )
    assert bad.status_code == 422 and _code(bad) == "validation_error"


def test_bulk_routes_are_not_shadowed_by_the_transaction_id_routes(
    client: TestClient, session: Session
) -> None:
    """Every `/bulk/*` path answers its OWN handler — a shadowed route would
    answer 422 `validation_error` with `loc: path.transaction_id` instead."""
    headers, user_id = _register(client)
    account = _account(client, headers, _uniq("A"))
    _seed(session, user_id=user_id, account_id=account, count=2)
    tag = _tag(client, headers)
    category = _category(client, headers)

    for path, payload in [
        ("review", {"filter": {}, "exclude_ids": [], "reviewed": True}),
        ("tags", {"filter": {}, "exclude_ids": [], "tag_id": tag}),
        ("payee", {"filter": {}, "exclude_ids": [], "normalized_payee": "Renamed"}),
        ("recategorize", {"filter": {}, "exclude_ids": [], "category_id": category}),
        ("delete", {"filter": {}, "exclude_ids": [], "expected_count": 2}),
    ]:
        response = client.post(f"{BULK}/{path}", headers=headers, json=payload)
        assert response.status_code == 200, f"{path}: {response.text}"
        loc = str(response.json().get("loc"))
        assert "transaction_id" not in loc, path

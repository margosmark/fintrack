"""Search (docs/33 §Server-first computing, M137): /transactions/similar and
`GET /transactions?q=` re-mounted, /search blind-index token + fuzzy groups,
direction filter on /transactions.

M48-S3/S5 removed these as content scans; M137 puts the matcher back because
the database owns it already (pg_trgm at 0.3, `_search_helpers`, the
`ix_trgm_transactions_payee` index). The client's pg_trgm port stays as the
E2EE/offline fallback — its vectors now live in `test_search_goldens.py`."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api._search_helpers import SCORE_THRESHOLD
from app.models.categories import Category
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transactions import Transaction
from app.models.users import User
from tests.test_cat_common import auth, create_account, make_transaction

PASSWORD = "correct-horse-battery"


def _register(client: TestClient, session: Session, email: str) -> tuple[dict, int]:
    """Register + login a fresh user; returns (headers, user_id)."""
    client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    headers = auth(token)
    user_id = session.exec(select(User).where(User.email == email)).one().id
    return headers, user_id


def _category(session: Session, name: str, user_id: int) -> Category:
    return session.exec(
        select(Category).where(Category.name == name, Category.user_id == user_id)
    ).one()


def _seed_metro(
    client: TestClient,
    session: Session,
    email: str,
) -> tuple[dict, int, list[int]]:
    """Two categorized Metro payees (Grocery negative, Pharmacy positive);
    returns (headers, account_id, [grocery_txn_id, pharmacy_txn_id])."""
    headers, user_id = _register(client, session, email)
    account_id = create_account(client, headers)
    grocery = make_transaction(
        session,
        user_id,
        account_id,
        "Metro Grocery",
        "-82.50",
        category_id=_category(session, "groceries", user_id).id,
    )
    pharmacy = make_transaction(
        session,
        user_id,
        account_id,
        "Metro Pharmacy",
        "25.00",
        category_id=_category(session, "health", user_id).id,
    )
    return headers, account_id, [grocery.id, pharmacy.id]


def _stage_batch(session: Session, user_id: int, account_id: int, status: ImportBatchStatus) -> int:
    """Create a staging batch so transactions can be attached to it."""
    batch = ImportBatch(
        user_id=user_id,
        account_id=account_id,
        institution="test",
        file_name="stage.qfx",
        status=status,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    assert batch.id is not None
    return batch.id


# --- GET /transactions/similar -------------------------------------------------


def test_similar_route_ranks_the_payee_family(client: TestClient, session: Session) -> None:
    """M137 (docs/04, docs/33): `GET /transactions/similar` is RE-MOUNTED.

    Same contract the TypeScript port reproduced: `score desc, id desc` over
    `greatest(similarity, word_similarity) >= 0.3`, uncategorized siblings
    included (M119), staged rows and other tenants' rows never reachable.
    """
    email = "sim-ranked@example.com"
    headers, account_id, (grocery_id, pharmacy_id) = _seed_metro(client, session, email)
    user_id = session.exec(select(User.id).where(User.email == email)).one()
    fresh_id = make_transaction(session, user_id, account_id, "FreshCo Oakville", "-3.00").id
    pending_id = _stage_batch(session, user_id, account_id, ImportBatchStatus.pending_review)
    staged = make_transaction(
        session, user_id, account_id, "Metro Staged", "-6.00", import_batch_id=pending_id
    )

    resp = client.get("/api/v1/transactions/similar", params={"q": "metro"}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [row["id"] for row in body["items"]]

    assert set(ids) == {grocery_id, pharmacy_id}
    assert body["total"] == 2
    assert body["withheld"] == 0
    assert all(row["score"] >= SCORE_THRESHOLD for row in body["items"])
    scores = [row["score"] for row in body["items"]]
    assert scores == sorted(scores, reverse=True), "score desc"
    assert fresh_id not in ids, "below threshold never surfaces"
    assert staged.id not in ids, "staged rows never surface (docs/03)"

    # `exclude_id`: the row asking about its own family is never in it (M119).
    without_anchor = client.get(
        "/api/v1/transactions/similar",
        params={"q": "metro", "exclude_id": grocery_id},
        headers=headers,
    ).json()
    assert [row["id"] for row in without_anchor["items"]] == [pharmacy_id]
    assert without_anchor["total"] == 1

    # Tenancy: a foreign Metro row never reaches this response.
    _headers_b, account_b, _ = _seed_metro(client, session, "sim-ranked-b@example.com")
    make_transaction(
        session,
        session.exec(select(User.id).where(User.email == "sim-ranked-b@example.com")).one(),
        account_b,
        "Metro Foreign",
        "-9.00",
    )
    scoped = client.get(
        "/api/v1/transactions/similar", params={"q": "metro"}, headers=headers
    ).json()
    assert all(row["normalized_payee"] != "Metro Foreign" for row in scoped["items"])


def test_similar_hides_encrypted_rows_only_when_asked(client: TestClient, session: Session) -> None:
    """The M119 honesty substrate, server-side (docs/33 §Server-first).

    A cipher payee is plaintext on the server, so it ALWAYS matches; a locked
    client is what withholds it, and it asks with `hide_encrypted` — the
    response then carries `withheld`, the count the `partial` copy prints
    instead of inventing.
    """
    email = "sim-enc@example.com"
    headers, account_id, (grocery_id, pharmacy_id) = _seed_metro(client, session, email)
    user_id = session.exec(select(User.id).where(User.email == email)).one()
    make_transaction(
        session,
        user_id,
        account_id,
        "Metro Encrypted",
        "-4.00",
        payee_cipher="ciphertext-opaque",
    )

    open_resp = client.get(
        "/api/v1/transactions/similar", params={"q": "metro"}, headers=headers
    ).json()
    assert open_resp["total"] == 3
    assert open_resp["withheld"] == 0
    flagged = {row["id"]: row["encrypted"] for row in open_resp["items"]}
    assert list(flagged.values()).count(True) == 1

    locked_resp = client.get(
        "/api/v1/transactions/similar",
        params={"q": "metro", "hide_encrypted": "true"},
        headers=headers,
    ).json()
    assert locked_resp["total"] == 2
    assert locked_resp["withheld"] == 1
    assert locked_resp["total"] + locked_resp["withheld"] == open_resp["total"]
    assert {row["id"] for row in locked_resp["items"]} == {grocery_id, pharmacy_id}
    assert all(row["encrypted"] is False for row in locked_resp["items"])


def test_search_payee_token_is_exact_and_scoped(client: TestClient, session: Session) -> None:
    """The blind-index token ranks FIRST, then the pg_trgm half fills the
    window (M137, docs/33 §Server-first computing).

    Tenancy + docs/03 staging apply to both halves, and uncategorized rows
    still count (search always included them, unlike /similar).
    """
    headers_a, account_a, _ = _seed_metro(client, session, "tok-a@example.com")
    headers_b, _, _ = _seed_metro(client, session, "tok-b@example.com")
    user_a = session.exec(select(User.id).where(User.email == "tok-a@example.com")).one()
    user_b = session.exec(select(User.id).where(User.email == "tok-b@example.com")).one()

    tok = "idx_metro_tok"
    # Same token everywhere — only user A's committed rows may come back.
    live = make_transaction(session, user_a, account_a, "Metro Live", "-5.00", payee_token=tok)
    pending_id = _stage_batch(session, user_a, account_a, ImportBatchStatus.pending_review)
    staged = make_transaction(
        session,
        user_a,
        account_a,
        "Metro Staged",
        "-6.00",
        import_batch_id=pending_id,
        payee_token=tok,
    )
    account_b = create_account(client, headers_b)
    foreign = make_transaction(
        session, user_b, account_b, "Metro Foreign", "-7.00", payee_token=tok
    )

    body = client.get(
        "/api/v1/search",
        params={"q": "metro", "payee_token": tok, "limit": 10},
        headers=headers_a,
    ).json()
    found = {t["id"] for t in body["transactions"]}
    assert live.id in found, "committed own-user rows match an exact token"
    # Token hits lead the group at score 1.0; the trigram half follows.
    assert body["transactions"][0]["id"] == live.id
    assert body["transactions"][0]["score"] == 1.0
    assert staged.id not in found, "staged rows never surface (docs/03)"
    assert foreign.id not in found, "token lookups are user-scoped"

    # Without a token the transaction group still answers — from the pg_trgm
    # matcher (M137), which the _seed_metro rows (Metro Grocery / Metro
    # Pharmacy) reach on their plaintext payee alone.
    bare = client.get("/api/v1/search", params={"q": "metro"}, headers=headers_a).json()
    bare_payees = {t["normalized_payee"] for t in bare["transactions"]}
    assert {"Metro Grocery", "Metro Pharmacy"} <= bare_payees
    assert bare["withheld"] == 0


# --- GET /search ---------------------------------------------------------------


def test_search_excludes_pending_batch_transactions(client: TestClient, session: Session) -> None:
    """Staged (pending_review) rows must not surface in global search results.

    docs/03: batch-scoped reads filter `import_batches.status = 'committed'`.
    A user who has not yet reviewed an import must not see its payees in
    search, and a discarded batch's rows must never appear either.
    """
    headers, account_id, ids = _seed_metro(client, session, "search-staged@example.com")
    user_id = session.exec(select(User.id).where(User.email == "search-staged@example.com")).one()

    committed_id = _stage_batch(session, user_id, account_id, ImportBatchStatus.committed)
    pending_id = _stage_batch(session, user_id, account_id, ImportBatchStatus.pending_review)
    discarded_id = _stage_batch(session, user_id, account_id, ImportBatchStatus.discarded)

    tok = "idx_staged"
    live = make_transaction(
        session,
        user_id,
        account_id,
        "Metro Bakery",
        amount="-10.00",
        import_batch_id=committed_id,
        payee_token=tok,
    )
    staged = make_transaction(
        session,
        user_id,
        account_id,
        "Metro Deli",
        amount="-11.00",
        import_batch_id=pending_id,
        payee_token=tok,
    )
    dropped = make_transaction(
        session,
        user_id,
        account_id,
        "Metro Depot",
        amount="-12.00",
        import_batch_id=discarded_id,
        payee_token=tok,
    )
    # Manual (import_batch_id IS NULL) rows carry the same token: unaffected.
    for txn_id in ids:
        txn = session.get(Transaction, txn_id)
        assert txn is not None
        txn.payee_token = tok
        session.add(txn)
    session.commit()

    body = client.get(
        "/api/v1/search",
        params={"q": "metro", "payee_token": tok, "limit": 10},
        headers=headers,
    ).json()
    found = {t["id"] for t in body["transactions"]}
    assert live.id in found, "committed import rows stay searchable"
    assert staged.id not in found, "pending_review rows must be excluded"
    assert dropped.id not in found, "discarded rows must be excluded"
    assert {t["normalized_payee"] for t in body["transactions"]} >= {
        "Metro Grocery",
        "Metro Pharmacy",
    }


def test_search_returns_all_groups_and_is_tenant_scoped(
    client: TestClient, session: Session
) -> None:
    headers_a, _, _ = _seed_metro(client, session, "search-a@example.com")
    headers_b, _, _ = _seed_metro(client, session, "search-b@example.com")
    # Distinctive account names so `groc` hits exactly one account per user.
    create_account(client, headers_a, name="Grocery Fund")
    create_account(client, headers_b, name="B Fund")

    body = client.get("/api/v1/search", params={"q": "groc"}, headers=headers_a).json()
    assert body["query"] == "groc"
    # M137 (docs/33 §Server-first computing): no token needed — the pg_trgm
    # half answers from the plaintext payee. A seeded ONE matching row; if B's
    # identical "Metro Grocery" leaked in, this would be 2.
    assert [t["normalized_payee"] for t in body["transactions"]] == ["Metro Grocery"]
    assert body["withheld"] == 0
    assert any(c["name"] == "groceries" for c in body["categories"])
    assert any(a["name"] == "Grocery Fund" and a["type"] == "chequing" for a in body["accounts"])
    # Tenant lock: B's rows never leak into A's results.
    assert all(a["name"] != "B Fund" for a in body["accounts"])

    empty = client.get("/api/v1/search", params={"q": "zzzzqqq"}, headers=headers_a)
    assert empty.status_code == 200
    assert empty.json() == {
        "query": "zzzzqqq",
        "transactions": [],
        "categories": [],
        "accounts": [],
        "securities": [],
        "tags": [],
        "withheld": 0,
    }

    missing = client.get("/api/v1/search", headers=headers_a)
    assert missing.status_code == 422
    assert missing.json()["code"] == "validation_error"


def test_search_returns_tag_group_and_is_tenant_scoped(
    client: TestClient, session: Session
) -> None:
    headers_a, _, _ = _seed_metro(client, session, "search-tag-a@example.com")
    headers_b, _, _ = _seed_metro(client, session, "search-tag-b@example.com")
    tag_a = client.post("/api/v1/tags", json={"name": "receipts"}, headers=headers_a).json()
    client.post("/api/v1/tags", json={"name": "warranty"}, headers=headers_b)

    body = client.get("/api/v1/search", params={"q": "receipt"}, headers=headers_a).json()
    assert [(t["id"], t["name"]) for t in body["tags"]] == [(tag_a["id"], "receipts")]
    assert all(t["name"] != "warranty" for t in body["tags"])


# --- direction filter on GET /transactions -------------------------------------


def test_direction_filter_paid_received_composes_with_amount_min(
    client: TestClient, session: Session
) -> None:
    headers, _, _ = _seed_metro(client, session, "dir@example.com")

    paid = client.get("/api/v1/transactions", params={"direction": "paid"}, headers=headers).json()
    assert paid["total"] == 1
    assert float(paid["items"][0]["amount_native"]) < 0

    received = client.get(
        "/api/v1/transactions", params={"direction": "received"}, headers=headers
    ).json()
    assert received["total"] == 1
    assert float(received["items"][0]["amount_native"]) > 0

    big_receipts = client.get(
        "/api/v1/transactions",
        params={"direction": "received", "amount_min": 100},
        headers=headers,
    ).json()
    assert big_receipts["total"] == 0

    both = client.get("/api/v1/transactions", headers=headers).json()
    assert both["total"] == 2


# --- fuzzy q on GET /transactions (search view) --------------------------------


def test_list_q_ranks_the_search_view_server_side(client: TestClient, session: Session) -> None:
    """M137 (docs/33 §Server-first computing): `GET /transactions?q=` answers
    the relevance question AGAIN — the guard M48-S5 added is gone.

    Threshold (0.3), `score desc, id desc`, `total` = the HIT count and the
    `{items,total,limit,offset}` envelope are the contract `searchPage()` used
    to compute in the browser over a full-ledger download.
    """
    headers, user_id = _register(client, session, "q-rank@example.com")
    account_id = create_account(client, headers)

    # Four rows, inserted in this order so ids ascend. Live-measured scores:
    #   q=payroll → "PAYROLL DIRECT DEPOSIT" 1.000, "CARD PAYMENT…" 0.375
    #   q=metrop  → both "Metro Groceries"    0.714 (a tie)
    # so the first pair makes SCORE beat ID and the second makes ID decide.
    payroll_id = make_transaction(
        session, user_id, account_id, "PAYROLL DIRECT DEPOSIT", "-900.00", day=date(2026, 5, 1)
    ).id
    groceries_a = make_transaction(
        session, user_id, account_id, "Metro Groceries", "-12.00", day=date(2026, 5, 2)
    ).id
    groceries_b = make_transaction(
        session, user_id, account_id, "Metro Groceries", "-13.00", day=date(2026, 5, 3)
    ).id
    card_id = make_transaction(
        session, user_id, account_id, "CARD PAYMENT THANKYOU", "-40.00", day=date(2026, 5, 4)
    ).id

    ranked = client.get(
        "/api/v1/transactions", params={"q": "payroll", "limit": 50}, headers=headers
    )
    assert ranked.status_code == 200, ranked.text
    body = ranked.json()
    # score desc WINS over id desc: the higher-scoring row has the LOWER id.
    assert [row["id"] for row in body["items"]] == [payroll_id, card_id]
    assert body["total"] == 2
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["withheld"] == 0

    # Equal scores fall back to `id desc` — the removed endpoint's ordering.
    tied = client.get(
        "/api/v1/transactions", params={"q": "metrop", "limit": 50}, headers=headers
    ).json()
    assert [row["id"] for row in tied["items"]] == [groceries_b, groceries_a]
    assert tied["total"] == 2

    # `total` counts HITS, not rows, so the window pages where it used to.
    window = client.get(
        "/api/v1/transactions",
        params={"q": "payroll", "limit": 1, "offset": 1},
        headers=headers,
    ).json()
    assert window["total"] == 2
    assert [row["id"] for row in window["items"]] == [card_id]

    # Below threshold is an empty page, never an unranked list.
    miss = client.get("/api/v1/transactions", params={"q": "zzzzqqq"}, headers=headers)
    assert miss.status_code == 200
    assert miss.json()["total"] == 0

    # Without `q` the ledger keeps its date sort (the two modes are distinct).
    plain = client.get("/api/v1/transactions", headers=headers).json()
    assert [row["id"] for row in plain["items"]] == [
        card_id,
        groceries_b,
        groceries_a,
        payroll_id,
    ]


def test_list_q_composes_with_structured_filters(client: TestClient, session: Session) -> None:
    """`q` ANDs with every structured filter — one scope, one predicate."""
    email = "q-dir@example.com"
    headers, _, _ = _seed_metro(client, session, email)

    both = client.get(
        "/api/v1/transactions",
        params={"q": "metro", "direction": "paid"},
        headers=headers,
    )
    assert both.status_code == 200, both.text
    assert both.json()["total"] == 1
    assert float(both.json()["items"][0]["amount_native"]) < 0

    received = client.get(
        "/api/v1/transactions",
        params={"q": "metro", "direction": "received"},
        headers=headers,
    ).json()
    assert received["total"] == 1
    assert float(received["items"][0]["amount_native"]) > 0

    # The structured filters alone keep working exactly as before.
    body = client.get("/api/v1/transactions", params={"direction": "paid"}, headers=headers).json()
    assert body["total"] == 1
    assert float(body["items"][0]["amount_native"]) < 0


def test_list_q_without_hide_encrypted_reports_every_match(
    client: TestClient, session: Session
) -> None:
    """A locked-vault client asks for `hide_encrypted`; the answer carries the
    real `withheld` count instead of a silent shrink (docs/33 A18 substrate)."""
    email = "q-hide@example.com"
    headers, account_id, _ = _seed_metro(client, session, email)
    user_id = session.exec(select(User.id).where(User.email == email)).one()
    make_transaction(
        session,
        user_id,
        account_id,
        "Metro Cipher",
        "-8.00",
        payee_cipher="ciphertext-opaque",
    )

    everything = client.get(
        "/api/v1/transactions", params={"q": "metro", "limit": 50}, headers=headers
    ).json()
    assert everything["total"] == 3
    assert everything["withheld"] == 0

    locked = client.get(
        "/api/v1/transactions",
        params={"q": "metro", "limit": 50, "hide_encrypted": "true"},
        headers=headers,
    ).json()
    assert locked["total"] == 2
    assert locked["withheld"] == 1
    assert locked["total"] + locked["withheld"] == everything["total"]
    assert all(row["payee_cipher"] is None for row in locked["items"])


# --- P1-5: every ported READ surface is indifferent to the write guard ------
#
# `users.encryption_required` is a WRITE guard: it rejects future writes that
# carry no ciphertext (docs/33). M134 P1-5 asks for the read side to be proven
# lock-invariant surface by surface — the two surfaces that already carry the
# pattern are `POST /rules/preview` and `GET /subscriptions`. Each test below
# asserts ANSWER equality (the same rows in the same order, the same counts),
# not merely a 200: it reads the answer under one value, flips the flag, reads
# again, and requires the two bodies to be byte-identical.


def _set_encryption_required(client: TestClient, headers: dict, value: bool) -> None:
    r = client.patch("/api/v1/users/me", json={"encryption_required": value}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["encryption_required"] is value


@pytest.mark.parametrize("encryption_required", [False, True])
def test_list_q_ranking_is_indifferent_to_encryption_required(
    client: TestClient, session: Session, encryption_required: bool
) -> None:
    """P1-5: `GET /transactions?q=` ranks on `normalized_payee` (mandatory
    plaintext, docs/33 §Server-readable plaintext), so the ranking — the same
    rows, the same order, the same `total` — cannot move under the guard."""
    headers, user_id = _register(client, session, "q-rank-guard@example.com")
    account_id = create_account(client, headers)
    payroll_id = make_transaction(
        session, user_id, account_id, "PAYROLL DIRECT DEPOSIT", "-900.00", day=date(2026, 5, 1)
    ).id
    groceries_a = make_transaction(
        session, user_id, account_id, "Metro Groceries", "-12.00", day=date(2026, 5, 2)
    ).id
    groceries_b = make_transaction(
        session, user_id, account_id, "Metro Groceries", "-13.00", day=date(2026, 5, 3)
    ).id
    card_id = make_transaction(
        session, user_id, account_id, "CARD PAYMENT THANKYOU", "-40.00", day=date(2026, 5, 4)
    ).id

    _set_encryption_required(client, headers, encryption_required)

    def ranked(q: str, **params: str) -> dict:
        resp = client.get(
            "/api/v1/transactions", params={"q": q, "limit": 50, **params}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    under_flag = ranked("payroll")
    _set_encryption_required(client, headers, not encryption_required)
    under_other = ranked("payroll")
    assert under_flag == under_other, "the guard must not move a single row"

    # The pinned answer is the SAME one the unparametrized test asserts.
    assert [row["id"] for row in under_flag["items"]] == [payroll_id, card_id]
    assert under_flag["total"] == 2
    assert under_flag["withheld"] == 0
    # Score beat ID above; below, an exact-score tie falls back to `id desc`.
    assert [row["id"] for row in ranked("metrop")["items"]] == [groceries_b, groceries_a]
    assert ranked("zzzzqqq")["total"] == 0


@pytest.mark.parametrize("encryption_required", [False, True])
def test_similar_route_is_indifferent_to_encryption_required(
    client: TestClient, session: Session, encryption_required: bool
) -> None:
    """P1-5: `GET /transactions/similar` ranks the same plaintext payee
    family, so its answer is identical under the write guard."""
    email = "sim-guard@example.com"
    headers, _account_id, (grocery_id, pharmacy_id) = _seed_metro(client, session, email)

    _set_encryption_required(client, headers, encryption_required)

    def similar() -> dict:
        resp = client.get("/api/v1/transactions/similar", params={"q": "metro"}, headers=headers)
        assert resp.status_code == 200, resp.text
        return resp.json()

    under_flag = similar()
    _set_encryption_required(client, headers, not encryption_required)
    under_other = similar()
    assert under_flag == under_other, "the guard must not move a single row"

    assert {row["id"] for row in under_flag["items"]} == {grocery_id, pharmacy_id}
    assert under_flag["total"] == 2
    assert under_flag["withheld"] == 0


@pytest.mark.parametrize("encryption_required", [False, True])
def test_search_groups_are_indifferent_to_encryption_required(
    client: TestClient, session: Session, encryption_required: bool
) -> None:
    """P1-5: the `GET /search` FUZZY TRANSACTION GROUP scores the same
    plaintext payees, so the group — and its `withheld` count — are identical
    under the write guard. (The blind-index half is a separate contract: it
    matches `payee_token`, which the guard does not create or remove.)"""
    headers, _, _ = _seed_metro(client, session, "search-guard@example.com")

    _set_encryption_required(client, headers, encryption_required)

    def search(q: str) -> dict:
        resp = client.get("/api/v1/search", params={"q": q}, headers=headers)
        assert resp.status_code == 200, resp.text
        return resp.json()

    under_flag = search("groc")
    _set_encryption_required(client, headers, not encryption_required)
    under_other = search("groc")
    assert under_flag == under_other, "the guard must not move a single row"

    assert [t["normalized_payee"] for t in under_flag["transactions"]] == ["Metro Grocery"]
    assert under_flag["withheld"] == 0
    # The no-match sentinel answers the same empty envelope under both values.
    empty_under_flag = search("zzzzqqq")
    _set_encryption_required(client, headers, not encryption_required)
    assert empty_under_flag == search("zzzzqqq")
    assert empty_under_flag["transactions"] == []

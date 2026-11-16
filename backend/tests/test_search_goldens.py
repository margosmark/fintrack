"""P0-1 parity goldens: the TS matchers' vectors, run against the RE-MOUNTED
server matcher (docs/33 §Server-first computing, M137).

Two families, both moved here from `frontend/tests` rather than deleted:

1. ``PG_GOLDEN`` — the 19 live-pg_trgm score vectors `searchMatch.test.ts`
   pinned, plus the two S5 pairs the old heuristic got wrong. They assert the
   DATABASE's own `greatest(similarity, word_similarity)` agrees with the
   numbers the TypeScript port was written against, at the 0.3 threshold.
2. ``fixtures/s5-vectors.json`` — the 10 live goldens captured from the
   deployed `/transactions?q=` endpoint before M48-S5 deleted it, over the
   344-row demo substrate: threshold, `score desc, id desc` ordering and the
   `{items,total,limit,offset}` envelope.

The fixtures stay on disk under `frontend/tests/` because the client engines
are still the E2EE/offline fallback (P2-5 retires them, not this card).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import String, func, literal
from sqlmodel import Session, select

from app.api._search_helpers import SCORE_THRESHOLD
from app.models.transactions import Transaction
from app.models.users import User
from tests.test_cat_common import auth, create_account

PASSWORD = "correct-horse-battery"
REPO_ROOT = Path(__file__).resolve().parents[2]
S5_VECTORS = REPO_ROOT / "frontend" / "tests" / "fixtures" / "s5-vectors.json"

# [text, query, pg greatest(similarity, word_similarity)] — captured from live
# Postgres before the server-side scan was removed (docs/33 S1), verbatim from
# `frontend/tests/searchMatch.test.ts`.
PG_GOLDEN: list[tuple[str, str, float]] = [
    ("Metro Groceries", "metro", 1),
    ("metro", "metro", 1),
    ("METRO", "metro", 1),
    ("Metro Groceries", "metrop", 0.71428573),
    ("FreshCo Oakville", "metro", 0),
    ("Metro Groceries Oakville", "metro", 1),
    ("Uber Eats", "ubereats", 0.5833333),
    ("Uber Eats", "uber", 1),
    ("Uber Eats", "zipcar", 0),
    ("Canadian Tire", "canadian", 1),
    ("Shell Hwy 401", "shell station", 0.46153846),
    ("Shell Hwy 401", "shell", 1),
    ("Tim Hortons", "tim hortons", 1),
    ("Tim Hortons", "hospital", 0.22222222),
    ("No Frills", "nofrills", 0.5833333),
    ("No Frills", "no frills", 1),
    ("Costco Wholesale", "cost", 0.8),
    ("Pay: NETFLIX.COM", "netflix", 1),
    ("Tangerine Savings", "tangerine bank", 0.6666667),
    # The two S5 pairs whose scores the whole-string-padding heuristic got
    # wrong (0.273 vs 0.75, 0.2 vs 0.375) — both sit near the threshold.
    ("GROCERY MART #12", "gro", 0.75),
    ("CARD PAYMENT THANKYOU", "payroll", 0.375),
]


def _server_score(session: Session, text: str, query: str) -> float:
    """`greatest(similarity(text, query), word_similarity(query, text))` —
    the exact expression `_payee_score` builds, over two literals."""
    txt = literal(text, String)
    qry = literal(query, String)
    stmt = select(func.greatest(func.similarity(txt, qry), func.word_similarity(qry, txt)))
    return float(session.exec(stmt).one())


@pytest.mark.parametrize(
    ("text", "query", "pg_score"),
    PG_GOLDEN,
    ids=[f"{t}~{q}" for t, q, _ in PG_GOLDEN],
)
def test_pg_trgm_score_matches_the_vectors_the_ts_port_was_written_against(
    session: Session, text: str, query: str, pg_score: float
) -> None:
    """The database's own numbers are the contract — not the port's."""
    mine = _server_score(session, text, query)
    assert mine == pytest.approx(pg_score, abs=1e-6), f"{text!r} ~ {query!r}"
    # The threshold DECISION is what every consumer acts on.
    assert (mine >= SCORE_THRESHOLD) is (pg_score >= SCORE_THRESHOLD)


def _register(client: TestClient, session: Session, email: str) -> tuple[dict, int]:
    client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    headers = auth(token)
    user_id = session.exec(select(User).where(User.email == email)).one().id
    return headers, user_id


def _load_s5() -> dict:
    return json.loads(S5_VECTORS.read_text())


def _seed_demo_substrate(session: Session, user_id: int, account_id: int) -> dict[int, int]:
    """Insert the 344-row demo substrate; return old-id → new-id.

    Ids are NOT preserved: the suite database is shared with every other test
    in the session, so claiming 1..344 would collide with the sequence. Rows go
    in ascending old-id order, so the new ids are order-isomorphic and the
    `id desc` tiebreak the goldens pin behaves identically — every expected id
    is translated through this map before comparison.
    """
    rows = sorted(_load_s5()["datasets"]["demo"]["transactions"], key=lambda r: r["id"])
    created: list[tuple[int, Transaction]] = []
    for row in rows:
        txn = Transaction(
            user_id=user_id,
            account_id=account_id,
            date=date.fromisoformat(row["date"]),
            posting_date=None,
            raw_description=row["raw_description"],
            normalized_payee=row["normalized_payee"],
            amount_native=Decimal(row["amount_native"]),
            currency=row["currency"],
            cad_amount=(Decimal(row["cad_amount"]) if row["cad_amount"] is not None else None),
            category_id=None,
            dedup_hash=hashlib.sha256(
                f"{row['id']}-{row['normalized_payee']}".encode()
            ).hexdigest(),
        )
        created.append((row["id"], txn))
        session.add(txn)
    session.flush()
    mapping = {old: int(txn.id) for old, txn in created}
    session.commit()
    assert len(mapping) == 344, len(mapping)
    return mapping


def _s5_cases() -> list:
    return _load_s5()["cases"]


@pytest.mark.parametrize("case", _s5_cases(), ids=[c["id"] for c in _s5_cases()])
def test_s5_golden_ranks_like_the_removed_endpoint(
    client: TestClient, session: Session, case: dict
) -> None:
    """The 10 live goldens of the endpoint M48-S5 deleted, answered again."""
    email = f"s5-{hashlib.sha1(case['id'].encode()).hexdigest()[:10]}@example.com"  # noqa: S324 -- email slug
    headers, user_id = _register(client, session, email)
    account_id = create_account(client, headers)
    mapping = _seed_demo_substrate(session, user_id, account_id)

    parts = urlsplit(case["request"]["path"])
    params = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    resp = client.get(f"/api/v1{parts.path}", params=params, headers=headers)
    expected = case["expected"]
    assert resp.status_code == expected["status"], resp.text

    body = resp.json()
    want = expected["body"]
    assert body["total"] == want["total"]
    assert body["limit"] == want["limit"]
    assert body["offset"] == want["offset"]
    assert [mapping[row["id"]] for row in want["items"]] == [row["id"] for row in body["items"]]
    # `withheld` is additive: a request that never asked for it gets 0.
    assert body["withheld"] == 0

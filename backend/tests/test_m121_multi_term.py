"""M121 — multi-term match rules (AND/OR) for bills, rules and the detector.

docs/25 §M121 is the spec; this module is its executable half. Four things get
pinned hard, because each is a way the feature could look right and be wrong:

* **the shared matcher** — `match_terms(terms, mode, normalize, subject,
  comparison)` must answer ANY vs ALL under every comparison mode, and must
  normalize BOTH sides with the ONE grouping normalizer
  (`subscriptions.normalize_payee_key`, the M27/M96 helper) — a near-copy of it
  is exactly the drift this milestone exists to remove;
* **the three surfaces call that one function** — a bill's rule, the
  categorization rules' payee matchers and the recurring detector's
  "already answered" test (`tests/test_m121_multi_term.py` asserts each of the
  three directly, so a surface that quietly forks the matcher fails here);
* **widening and narrowing are visible in the product** — the detail page's
  `matched_count`, the list's `payment_count`, the chart totals and the
  paginated `total` must all move TOGETHER when the term list or the mode
  changes (the M91 anti-fabrication standard);
* **a pre-M121 row survives** — a bill written before this milestone (columns
  only, no term rows) reads back as `match_logic="any"` with
  `match_terms=[old_term]`, and migration 0046's own `BACKFILL_SQL` gives it a
  position-0 row WITHOUT changing its answer.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import runpy
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.bill_reminders import BillReminder
from app.models.rules import Rule, RuleMatchType
from app.models.transactions import Transaction
from app.services.bill_match import BillMatchRule, load_terms, payee_matches, terms_of
from app.services.match_terms import MAX_TERMS, as_is, match_terms, validate_terms
from app.services.recurring import (
    LedgerRow,
    detect_recurring,
    merchant_answered,
    merchant_key,
)
from app.services.rules_engine import rule_matches
from app.services.subscriptions import normalize_payee_key

API = "/api/v1"
BILLS = f"{API}/bills"

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0046_multi_term_match.py"
)
# The migration module itself, so the SQL this file tests is byte-identical to
# the SQL `alembic upgrade` runs — not a paraphrase of it.
BACKFILL_SQL = runpy.run_path(str(_MIGRATION))["BACKFILL_SQL"]


# --------------------------------------------------------------------------- #
# helpers (the same shapes tests/test_bills_match.py uses)
# --------------------------------------------------------------------------- #


def _auth(client: TestClient) -> tuple[dict[str, str], int]:
    email = f"m121-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    user_id = client.get(f"{API}/users/me", headers=headers).json()["id"]
    return headers, user_id


def _account(session: Session, user_id: int) -> Account:
    account = Account(
        user_id=user_id,
        name=f"Chequing {user_id}",
        type=AccountType.chequing,
        currency="CAD",
        taxable=False,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _txn(
    session: Session, user_id: int, account: Account, *, payee: str, day: dt.date, amount: str
) -> None:
    session.add(
        Transaction(
            user_id=user_id,
            account_id=account.id,
            date=day,
            raw_description=payee,
            normalized_payee=payee,
            amount_native=Decimal(amount),
            currency="CAD",
            dedup_hash=f"m121-{user_id}-{payee}-{day.isoformat()}",
        )
    )
    session.commit()


def _create_bill(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str = "Rogers",
    match_terms: list[str] | None = None,
    match_term: str | None = None,
    match_logic: str = "any",
) -> dict:
    payload: dict = {
        "name": name,
        "frequency": "monthly",
        "next_due_date": "2026-11-01",
        "match_logic": match_logic,
    }
    if match_terms is not None:
        payload["match_terms"] = match_terms
    if match_term is not None:
        payload["match_term"] = match_term
    response = client.post(BILLS, headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _put_rule(
    client: TestClient,
    headers: dict[str, str],
    bill_id: int,
    *,
    match_terms: list[str],
    match_logic: str,
    match_mode: str = "contains",
) -> dict:
    response = client.put(
        f"{BILLS}/{bill_id}/rule",
        headers=headers,
        json={
            "match_mode": match_mode,
            "match_terms": match_terms,
            "match_logic": match_logic,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _detail(client: TestClient, headers: dict[str, str], bill_id: int, **query) -> dict:
    qs = "".join(f"&{k}={v}" for k, v in query.items())
    response = client.get(f"{BILLS}/{bill_id}/detail?{qs.lstrip('&')}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 1. the shared matcher — ANY/ALL x contains/starts_with/exact
# --------------------------------------------------------------------------- #


def test_any_and_all_under_every_comparison_mode() -> None:
    """The AND/OR contract, cross-checked under all three comparisons.

    The subjects are chosen so each comparison has a case it alone decides:
    `contains` is the only one that can hit a term in the MIDDLE, `starts_with`
    only a term at the FRONT, `exact` only a term that IS the whole key.
    """
    contains_subject = "ROGERS COMMUNICATIONS"
    # ANY: one term paying is enough.
    assert match_terms(("ROGERS", "RCOM"), "any", as_is, contains_subject, "contains")
    assert not match_terms(("RCOM", "FIDO"), "any", as_is, contains_subject, "contains")
    # ALL: every term must pay.
    assert match_terms(("ROGERS", "COMMUNICATIONS"), "all", as_is, contains_subject, "contains")
    assert not match_terms(("ROGERS", "RCOM"), "all", as_is, contains_subject, "contains")

    # starts_with: only a term at the front can hit.
    assert match_terms(("ROGERS", "RCOM"), "any", as_is, "ROGERS WIRELESS", "starts_with")
    assert not match_terms(("WIRELESS",), "any", as_is, "ROGERS WIRELESS", "starts_with")
    # Two terms that are BOTH prefixes (one a prefix of the other) satisfy ALL…
    assert match_terms(("ROG", "ROGERS"), "all", as_is, "ROGERS WIRELESS", "starts_with")
    # …and one that only appears later in the subject does not.
    assert not match_terms(("ROGERS", "WIRELESS"), "all", as_is, "ROGERS WIRELESS", "starts_with")

    # exact: the term must BE the key — distinct terms can never satisfy ALL.
    assert match_terms(("ROGERS", "RCOM"), "any", as_is, "ROGERS", "exact")
    assert not match_terms(("ROGERS",), "any", as_is, "ROGERS COMMUNICATIONS", "exact")
    assert not match_terms(("ROGERS", "RCOM"), "all", as_is, "ROGERS", "exact")


def test_no_terms_and_no_normalizable_term_never_match() -> None:
    """Silent wrong answers are refused by the matcher itself (docs/25 §M121 D2)."""
    # No rule → no claim, under EITHER mode (never vacuous truth).
    assert not match_terms((), "any", as_is, "ROGERS", "contains")
    assert not match_terms((), "all", as_is, "ROGERS", "contains")
    # A blank subject matches nothing.
    assert not match_terms(("ROGERS",), "any", as_is, "", "contains")
    assert not match_terms(("ROGERS",), "all", as_is, "", "contains")
    # A term that normalizes to nothing cannot identify a payee: it fails an
    # ALL rule outright and simply never contributes under ANY.
    digits_only = normalize_payee_key("123")
    assert digits_only == ""
    assert not match_terms((digits_only,), "all", as_is, "ROGERS", "contains")
    assert match_terms((digits_only, "ROGERS"), "any", as_is, "ROGERS", "contains")
    assert not match_terms((digits_only, "FIDO"), "any", as_is, "ROGERS", "contains")


def test_normalization_parity_with_the_m96_helper() -> None:
    """Both sides go through `normalize_payee_key` — the ONE grouping rule.

    The pairs below only agree AFTER that normalizer runs (case, trailing
    digits, `#` prefixes): a matcher with its own normalization would answer
    differently here, which is the drift M121 removes.
    """
    for raw_term, raw_subject in (
        ("rogers", "ROGERS"),
        ("rogers #12", "Rogers 1234"),
        ("  Superstore", "SUPERSTORE 4711"),
        ("amazon mktp", "AMAZON MKTP 123"),
    ):
        assert match_terms((raw_term,), "any", normalize_payee_key, raw_subject, "exact"), (
            raw_term,
            raw_subject,
        )
        assert match_terms((raw_term,), "any", normalize_payee_key, raw_subject, "contains")
    # And the detector's own key is the SAME function, not a look-alike:
    assert normalize_payee_key("Superstore #1234") == merchant_key("Superstore #1234")


def test_validate_terms_bounds() -> None:
    """1..10 terms, each 1..120 chars and payable — or ValueError (→ 422)."""
    assert validate_terms(["  Rogers  "]) == ["Rogers"]
    assert (
        validate_terms([f"NETFLIX{i}" for i in range(MAX_TERMS)])[-1] == f"NETFLIX{MAX_TERMS - 1}"
    )
    for bad in (
        [],
        ["   "],
        ["123"],
        ["x" * 121],
        [f"NETFLIX{i}" for i in range(MAX_TERMS + 1)],
    ):
        try:
            validate_terms(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


# --------------------------------------------------------------------------- #
# 2. the three surfaces all call the shared matcher
# --------------------------------------------------------------------------- #


def test_bills_and_the_rules_engine_agree_term_for_term() -> None:
    """The same subject, the same term, the same answer — two surfaces."""
    rule = BillMatchRule.build(
        match_mode="contains",
        match_terms=["rogers"],
        match_logic="any",
        category_id=None,
        bill_amount=None,
        amount_tolerance=None,
    )
    key = normalize_payee_key("Rogers Communications")
    assert payee_matches(key, rule)
    assert match_terms(rule.term_keys, rule.match_logic, as_is, key, rule.match_mode)

    # exact_payee and contains keep their docs/04 meanings, byte for byte.
    exact = Rule(
        user_id=1,
        match_type=RuleMatchType.exact_payee,
        pattern="Rogers",
        priority=1,
        category_id=1,
    )
    assert rule_matches(exact, "Rogers", Decimal("-1"))
    assert not rule_matches(exact, "rogers", Decimal("-1"))  # case-SENSITIVE, as pinned

    contains = Rule(
        user_id=1,
        match_type=RuleMatchType.contains,
        pattern="Rogers",
        priority=1,
        category_id=1,
    )
    assert rule_matches(contains, "ROGERS COMMUNICATIONS", Decimal("-1"))
    assert not rule_matches(contains, "FIDO", Decimal("-1"))


def test_recurring_detector_answers_through_the_shared_matcher() -> None:
    """M96's "already answered" test is the same ANY-of contract (docs/25 §M121 D3)."""
    rows = [
        LedgerRow(id=i, date=day, payee="Rogers", amount=Decimal("-50.00"))
        for i, day in enumerate(
            (dt.date(2026, 1, 5), dt.date(2026, 2, 5), dt.date(2026, 3, 5)), start=1
        )
    ]
    groups = detect_recurring(rows)
    assert len(groups) == 1
    group = groups[0]

    assert not merchant_answered(group, [])
    assert not merchant_answered(group, ["FIDO"])
    assert merchant_answered(group, ["FIDO", group.merchant_key])
    # …and the answer is exactly what the shared matcher says it is.
    assert merchant_answered(group, ["FIDO", group.merchant_key]) == match_terms(
        ("FIDO", group.merchant_key), "any", as_is, group.merchant_key, "exact"
    )


# --------------------------------------------------------------------------- #
# 3. the wire: create / replace / validate
# --------------------------------------------------------------------------- #


def test_create_with_a_term_list(client: TestClient, session: Session) -> None:
    """`match_terms` + `match_logic` round-trip; `match_term` stays terms[0]."""
    headers, _user_id = _auth(client)
    bill = _create_bill(
        client,
        headers,
        name="Rogers",
        match_terms=["ROGERS", "RCOM"],
        match_logic="any",
    )
    assert bill["match_terms"] == ["ROGERS", "RCOM"]
    assert bill["match_logic"] == "any"
    # The legacy field never lies about terms[0] (docs/25 §M121 D5).
    assert bill["match_term"] == "ROGERS"

    rows = session.execute(
        text(
            "SELECT position, term FROM bill_reminder_match_terms "
            "WHERE bill_reminder_id = :id ORDER BY position"
        ),
        {"id": bill["id"]},
    ).all()
    assert [(r[0], r[1]) for r in rows] == [(0, "ROGERS"), (1, "RCOM")]


def test_legacy_single_term_still_works_and_defaults_to_any(
    client: TestClient, session: Session
) -> None:
    """A pre-M121 body (`match_term` alone) creates a ONE-term, OR-mode rule."""
    headers, _user_id = _auth(client)
    bill = _create_bill(client, headers, name="Spotify", match_term="Spotify")
    assert bill["match_terms"] == ["Spotify"]
    assert bill["match_logic"] == "any"
    assert bill["match_term"] == "Spotify"


def test_rule_replacement_drops_terms_the_new_rule_does_not_ask_for(
    client: TestClient, session: Session
) -> None:
    """PUT is a FULL replacement — no term survives it by accident."""
    headers, _user_id = _auth(client)
    bill = _create_bill(client, headers, match_terms=["ROGERS", "RCOM", "ROGERS WIRELESS"])
    replaced = _put_rule(client, headers, bill["id"], match_terms=["ROGERS"], match_logic="all")
    assert replaced["match_terms"] == ["ROGERS"]
    assert replaced["match_logic"] == "all"
    assert replaced["match_term"] == "ROGERS"
    rows = session.execute(
        text(
            "SELECT term FROM bill_reminder_match_terms WHERE bill_reminder_id = :id "
            "ORDER BY position"
        ),
        {"id": bill["id"]},
    ).all()
    assert [r[0] for r in rows] == ["ROGERS"]


def test_term_list_validation_is_422_at_the_seam(client: TestClient) -> None:
    """Every bound in docs/25 §M121 D7 answers 422 `validation_error`."""
    headers, _user_id = _auth(client)
    base = {"name": "Netflix", "frequency": "monthly", "next_due_date": "2026-11-01"}

    def post(extra: dict) -> dict:
        response = client.post(BILLS, headers=headers, json={**base, **extra})
        return {"status": response.status_code, "body": response.json()}

    # 11 terms — over the rate limit.
    assert post({"match_terms": [f"NETFLIX{i}" for i in range(11)]})["status"] == 422
    # A blank chip.
    assert post({"match_terms": ["NETFLIX", "   "]})["status"] == 422
    # Over-length term.
    assert post({"match_terms": ["N" * 121]})["status"] == 422
    # Digits alone cannot identify a payee.
    assert post({"match_terms": ["NETFLIX", "123"]})["status"] == 422
    # An unknown mode.
    assert post({"match_terms": ["NETFLIX"], "match_logic": "maybe"})["status"] == 422
    # A client that disagrees with itself.
    disagree = post({"match_term": "NETFLIX", "match_terms": ["HULU"]})
    assert disagree["status"] == 422
    # …and one that agrees is accepted.
    agree = post({"match_term": "NETFLIX", "match_terms": ["NETFLIX", "HULU"]})
    assert agree["status"] == 201, agree["body"]
    assert agree["body"]["match_terms"] == ["NETFLIX", "HULU"]
    for extra in (
        {"match_terms": [f"NETFLIX{i}" for i in range(11)]},
        {"match_terms": ["NETFLIX", "   "]},
    ):
        response = client.post(BILLS, headers=headers, json={**base, **extra})
        assert response.json()["code"] == "validation_error"


def test_find_past_preview_runs_the_multi_term_rule(client: TestClient, session: Session) -> None:
    """The preview answers with the SAME terms it ran, and the same counts."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    for payee in ("ROGERS", "RCOM", "FIDO"):
        _txn(session, user_id, account, payee=payee, day=dt.date(2026, 6, 5), amount="-50.00")

    draft = client.post(
        f"{BILLS}/find-past",
        headers=headers,
        json={
            "match_mode": "contains",
            "match_terms": ["ROGERS", "RCOM"],
            "match_logic": "any",
            "frequency": "monthly",
        },
    )
    assert draft.status_code == 200, draft.text
    body = draft.json()
    assert body["match_terms"] == ["ROGERS", "RCOM"]
    assert body["match_logic"] == "any"
    assert body["match_term"] == "ROGERS"
    assert body["total_count"] == 2


# --------------------------------------------------------------------------- #
# 4. the product: widening, narrowing and honest counts
# --------------------------------------------------------------------------- #


def test_alias_terms_widen_under_any_and_narrow_under_all(
    client: TestClient, session: Session
) -> None:
    """The acceptance case: a Rogers-style bill with two alias terms.

    ANY widens the matched set over the single-term answer, ALL narrows it,
    and the list count, the detail count, the chart and the paginated total
    all move together — no surface may keep a stale number.
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    for index, payee in enumerate(
        ("ROGERS", "ROGERS COMMUNICATIONS", "RCOM", "ROGERS WIRELESS", "FIDO"), start=1
    ):
        _txn(
            session,
            user_id,
            account,
            payee=payee,
            day=dt.date(2026, 6, index * 3),
            amount="-50.00",
        )

    bill = _create_bill(client, headers, name="Rogers", match_terms=["ROGERS"])
    single = _detail(client, headers, bill["id"])
    # "ROGERS", "ROGERS COMMUNICATIONS", "ROGERS CA"
    assert single["matched_count"] == 3
    assert single["bill"]["payment_count"] == 3

    # ANY: the aliases widen the set (adds "RCOM").
    _put_rule(client, headers, bill["id"], match_terms=["ROGERS", "RCOM"], match_logic="any")
    widened = _detail(client, headers, bill["id"])
    assert widened["matched_count"] == 4
    assert widened["bill"]["payment_count"] == 4
    assert widened["chart"]["points"][-1]["count"] == 4
    assert Decimal(widened["chart"]["points"][-1]["total"]) == Decimal("200.00")
    assert widened["rolling"]["twelve"]["payments"] == 4
    # Pagination reports the TRUE set size, not the page size (M91).
    paged = _detail(client, headers, bill["id"], limit=2)
    assert paged["matched_count"] == 4
    assert paged["transactions"]["total"] == 4
    assert len(paged["transactions"]["items"]) == 2

    # ALL: nothing carries both aliases at once → the set collapses to zero,
    # and every number on the page says so honestly.
    _put_rule(client, headers, bill["id"], match_terms=["ROGERS", "RCOM"], match_logic="all")
    collapsed = _detail(client, headers, bill["id"])
    assert collapsed["matched_count"] == 0
    assert collapsed["bill"]["payment_count"] == 0
    assert collapsed["transactions"]["total"] == 0
    assert collapsed["rolling"]["twelve"]["payments"] == 0

    # ALL with an intersecting pair narrows to exactly the rows that carry BOTH.
    _put_rule(client, headers, bill["id"], match_terms=["ROGERS", "WIRELESS"], match_logic="all")
    narrowed = _detail(client, headers, bill["id"])
    assert narrowed["matched_count"] == 1
    assert [t["normalized_payee"] for t in narrowed["transactions"]["items"]] == ["ROGERS WIRELESS"]

    # The LIST row agrees with the detail page.
    listing = client.get(BILLS, headers=headers)
    assert listing.status_code == 200
    row = next(b for b in listing.json() if b["id"] == bill["id"])
    assert row["payment_count"] == 1
    assert row["match_terms"] == ["ROGERS", "WIRELESS"]
    assert row["match_logic"] == "all"


# --------------------------------------------------------------------------- #
# 5. backward compatibility: a pre-M121 row survives untouched
# --------------------------------------------------------------------------- #


def _insert_legacy_bill(session: Session, user_id: int, *, name: str, term: str) -> int:
    """A bill exactly as the pre-M121 schema left it.

    Columns only: NO `match_logic` in the INSERT (the migration's server
    default `'any'` fills it, which is precisely what `ADD COLUMN … DEFAULT`
    does to an existing row) and NO rows in `bill_reminder_match_terms`.
    """
    bill_id = session.execute(
        text(
            "INSERT INTO bill_reminders "
            "(user_id, name, frequency, next_due_date, remind_days_before, enabled, "
            " match_mode, match_term) "
            "VALUES (:user_id, :name, 'monthly', '2026-11-01', 3, true, 'contains', :term) "
            "RETURNING id"
        ),
        {"user_id": user_id, "name": name, "term": term},
    ).scalar_one()
    session.commit()
    return int(bill_id)


def test_pre_m121_row_reads_back_with_any_and_its_single_term(
    client: TestClient, session: Session
) -> None:
    """A rule that predates this milestone keeps its exact semantics."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    _txn(session, user_id, account, payee="Netflix", day=dt.date(2026, 6, 5), amount="-15.99")
    _txn(session, user_id, account, payee="Hulu", day=dt.date(2026, 6, 6), amount="-15.99")
    bill_id = _insert_legacy_bill(session, user_id, name="Netflix", term="Netflix")

    # No term rows at all — the fallback IS the compatibility guarantee.
    assert (
        session.execute(
            text("SELECT count(*) FROM bill_reminder_match_terms WHERE bill_reminder_id = :id"),
            {"id": bill_id},
        ).scalar_one()
        == 0
    )

    listing = client.get(BILLS, headers=headers)
    assert listing.status_code == 200, listing.text
    row = next(b for b in listing.json() if b["id"] == bill_id)
    assert row["match_logic"] == "any"
    assert row["match_terms"] == ["Netflix"]
    assert row["match_term"] == "Netflix"
    assert row["payment_count"] == 1

    detail = _detail(client, headers, bill_id)
    assert detail["matched_count"] == 1

    # The rule object a matcher sees is the same one-term, OR-mode rule.
    bill = session.get(BillReminder, bill_id)
    assert bill is not None
    assert terms_of(bill, {}) == ["Netflix"]
    rule = BillMatchRule.from_bill(bill, terms_of(bill, {}))
    assert rule.match_logic.value == "any"
    assert rule.terms == ("Netflix",)


def test_migration_backfill_gives_a_legacy_row_its_position_zero_term(
    client: TestClient, session: Session
) -> None:
    """Migration 0046's own SQL backfills WITHOUT changing the answer."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    _txn(session, user_id, account, payee="Netflix", day=dt.date(2026, 6, 5), amount="-15.99")
    bill_id = _insert_legacy_bill(session, user_id, name="Netflix", term="Netflix")

    before = _detail(client, headers, bill_id)

    session.execute(text(BACKFILL_SQL))
    session.commit()

    rows = session.execute(
        text(
            "SELECT position, term FROM bill_reminder_match_terms "
            "WHERE bill_reminder_id = :id ORDER BY position"
        ),
        {"id": bill_id},
    ).all()
    assert [(r[0], r[1]) for r in rows] == [(0, "Netflix")]

    after = _detail(client, headers, bill_id)
    assert after["matched_count"] == before["matched_count"] == 1
    assert after["bill"]["match_terms"] == ["Netflix"]
    assert after["bill"]["match_logic"] == "any"

    # Re-running it can never duplicate a term (idempotent by NOT EXISTS).
    session.execute(text(BACKFILL_SQL))
    session.commit()
    count = session.execute(
        text("SELECT count(*) FROM bill_reminder_match_terms WHERE bill_reminder_id = :id"),
        {"id": bill_id},
    ).scalar_one()
    assert count == 1


def test_load_terms_reads_every_bill_in_one_query(client: TestClient, session: Session) -> None:
    """The list endpoint's per-bill terms come from ONE batched read."""
    headers, _user_id = _auth(client)
    first = _create_bill(client, headers, name="Rogers", match_terms=["ROGERS", "RCOM"])
    second = _create_bill(client, headers, name="Netflix", match_terms=["NETFLIX"])
    loaded = load_terms(session, [first["id"], second["id"]])
    assert loaded[first["id"]] == ["ROGERS", "RCOM"]
    assert loaded[second["id"]] == ["NETFLIX"]
    # An unknown id answers nothing rather than inventing a term.
    assert load_terms(session, []) == {}


def test_deleting_a_bill_drops_its_terms(client: TestClient, session: Session) -> None:
    """The FK cascades: no orphan term row can outlive its bill."""
    headers, _user_id = _auth(client)
    bill = _create_bill(client, headers, match_terms=["ROGERS", "RCOM"])
    response = client.delete(f"{BILLS}/{bill['id']}", headers=headers)
    assert response.status_code == 204
    count = session.execute(
        text("SELECT count(*) FROM bill_reminder_match_terms WHERE bill_reminder_id = :id"),
        {"id": bill["id"]},
    ).scalar_one()
    assert count == 0

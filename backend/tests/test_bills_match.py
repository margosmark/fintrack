"""M116 — bill match rules, the derived detail view and "find past payments".

docs/25 §M116 is the spec; this module is its executable half. Three things
get pinned hard here, because each is a way the feature could look right and be
wrong:

* **the shared normalizer** — `"AMAZON MKTP 123"` vs `"Amazon"`, case and
  trailing digits, must behave EXACTLY like `subscriptions.normalize_payee_key`
  (M27/M96), not like a near-copy of it;
* **derived, not copied** — editing a rule must change which transactions the
  bill contains IMMEDIATELY, with no link table to fall out of sync (D3);
* **the arithmetic** — one rolling average and one chart total are recomputed
  by hand in the test body (the M91 anti-fabrication standard), so a refactor
  that quietly averages the wrong population fails here.

Staging and tenancy ride on the shared predicates: a pending-import row never
matches, and a foreign `bill_id` is a 404 before a single row is scanned.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from conftest import latency_budget_blocker
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transactions import Transaction
from app.services.bill_match import _rows_statement, load_rows

API = "/api/v1"
BILLS = f"{API}/bills"


def _auth(client: TestClient) -> tuple[dict[str, str], int]:
    email = f"m116-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
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
    session: Session,
    user_id: int,
    account: Account,
    *,
    payee: str,
    day: dt.date,
    amount: str,
    category_id: int | None = None,
    currency: str = "CAD",
    batch_id: int | None = None,
    dedup: str | None = None,
) -> Transaction:
    txn = Transaction(
        user_id=user_id,
        account_id=account.id,
        date=day,
        raw_description=payee,
        normalized_payee=payee,
        amount_native=Decimal(amount),
        currency=currency,
        category_id=category_id,
        import_batch_id=batch_id,
        dedup_hash=dedup or f"m116-{user_id}-{payee}-{day.isoformat()}-{amount}",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _category(client: TestClient, headers: dict[str, str], name: str) -> int:
    response = client.post(f"{API}/categories", headers=headers, json={"name": name})
    assert response.status_code in (200, 201), response.text
    return int(response.json()["id"])


def _bill(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str = "Netflix",
    amount: str | float | Decimal | None = 15.99,
    frequency: str = "monthly",
    match_term: str | None = None,
    match_mode: str = "contains",
    category_id: int | None = None,
    amount_tolerance: float | None = None,
    due: str = "2026-11-01",
) -> dict:
    payload: dict = {
        "name": name,
        "frequency": frequency,
        "next_due_date": due,
        "match_mode": match_mode,
    }
    if amount is not None:
        payload["amount"] = amount
    if match_term is not None:
        payload["match_term"] = match_term
    if category_id is not None:
        payload["category_id"] = category_id
    if amount_tolerance is not None:
        payload["amount_tolerance"] = amount_tolerance
    response = client.post(BILLS, headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# B1 — the rule
# --------------------------------------------------------------------------- #


def test_create_defaults_the_match_term_to_the_name(client: TestClient) -> None:
    """Every bill ships with a rule (docs/25 §M116 D1) — `name` is the term."""
    headers, _user_id = _auth(client)
    bill = _bill(client, headers, name="Spotify", match_term=None)
    assert bill["match_mode"] == "contains"
    assert bill["match_term"] == "Spotify"
    assert bill["amount_tolerance"] is None


def test_a_blank_match_term_falls_back_to_the_name(client: TestClient) -> None:
    """The form's term field starts empty — blank is "derive", not a 422."""
    headers, _user_id = _auth(client)
    for term in (None, ""):
        bill = _bill(client, headers, name="Spotify", match_term=term)
        assert bill["match_term"] == "Spotify"
        assert bill["match_mode"] == "contains"
    # Whitespace the user typed and left is the same statement as blank —
    # it resolves to the name too, rather than becoming a rule that matches
    # everything (a term that DOES name nothing, "123", is refused below).
    bad = client.post(
        BILLS,
        headers=headers,
        json={
            "name": "Weird",
            "frequency": "monthly",
            "next_due_date": "2026-11-01",
            "match_term": "   ",
            "match_mode": "contains",
        },
    )
    assert bad.status_code in (200, 201), bad.text
    assert bad.json()["match_term"] == "Weird"


def test_create_refuses_a_term_that_normalizes_to_nothing(client: TestClient) -> None:
    """`"123"` can identify no payee — 422, never a rule that matches everything."""
    headers, _user_id = _auth(client)
    response = client.post(
        BILLS,
        headers=headers,
        json={
            "name": "Weird",
            "frequency": "monthly",
            "next_due_date": "2026-11-01",
            "match_term": "123",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_match_modes_and_the_shared_normalizer(client: TestClient, session: Session) -> None:
    """contains / starts_with / exact over `normalize_payee_key`, not raw text.

    The payees below are chosen so a RAW comparison and a NORMALIZED one
    disagree: trailing digits and case only collapse after
    `normalize_payee_key` runs.
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    _txn(session, user_id, account, payee="AMAZON MKTP 123", day=dt.date(2026, 1, 5), amount="-100")
    _txn(session, user_id, account, payee="amazon mktP", day=dt.date(2026, 2, 5), amount="-50")
    _txn(session, user_id, account, payee="AMAZON PRIME", day=dt.date(2026, 3, 5), amount="-20")
    _txn(session, user_id, account, payee="SUPERSTORE #12", day=dt.date(2026, 4, 5), amount="-10")

    def pays(term: str, mode: str) -> list[str]:
        bill = _bill(client, headers, name="Probe", match_term=term, match_mode=mode, amount=None)
        detail = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers)
        assert detail.status_code == 200, detail.text
        items = detail.json()["transactions"]["items"]
        return sorted(item["normalized_payee"] for item in items)

    # contains: the term collapses too, so "AMAZON MKTP" also catches the
    # un-numbered twin — a RAW contains would only see "AMAZON MKTP 123".
    assert pays("AMAZON MKTP", "contains") == ["AMAZON MKTP 123", "amazon mktP"]
    # starts_with: "AMAZON" stops "SUPERSTORE" but keeps both Amazon spellings.
    assert pays("amazon", "starts_with") == ["AMAZON MKTP 123", "AMAZON PRIME", "amazon mktP"]
    # exact: the trailing digits are stripped from BOTH sides, so the numbered
    # row is exactly "AMAZON MKTP" — and "AMAZON PRIME" is not.
    assert pays("Amazon Mktp", "exact") == ["AMAZON MKTP 123", "amazon mktP"]
    assert pays("AMAZON MKTP 123", "exact") == ["AMAZON MKTP 123", "amazon mktP"]
    # exact is genuinely exact: a prefix of a real payee finds nothing.
    assert pays("AMAZON", "exact") == []


def test_one_term_covers_every_statement_spelling_of_a_payee(
    client: TestClient, session: Session
) -> None:
    """M165 — whitespace is not identity, so ONE term covers all spellings.

    The rows are Mark's own YouTube Premium subscription: the same payee as
    three statement formats differ only in WHERE the spaces sit
    (`GOOGLE *YouTubePremium`, `Google *Youtubepremium`, `Google*Youtubepremium`).
    Before M165 the space before the `*` survived normalization, so a bill
    needed a second term (or missed rows); now one term is the whole rule —
    and the recurring detector buckets by the same key, which is what collapses
    his two duplicate "Create bill" candidates into one (docs/09 §Recurring
    prompt → Already a bill).
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    spellings = ["GOOGLE *YouTubePremium", "Google *Youtubepremium", "Google*Youtubepremium"]
    for index, payee in enumerate(spellings):
        _txn(
            session,
            user_id,
            account,
            payee=payee,
            day=dt.date(2026, 1, 5) + dt.timedelta(days=index),
            amount="-25.98",
        )

    def pays(term: str) -> list[str]:
        # amount=None -> a variable bill, so only the payee clause can answer.
        bill = _bill(
            client,
            headers,
            name="YouTube Premium",
            match_term=term,
            match_mode="contains",
            amount=None,
        )
        detail = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers)
        assert detail.status_code == 200, detail.text
        return sorted(item["normalized_payee"] for item in detail.json()["transactions"]["items"])

    expected = sorted(spellings)
    assert pays("Google*Youtubepremium") == expected
    assert pays("GOOGLE *YOUTUBEPREMIUM") == expected
    # ... and a payee outside the family still finds nothing (no fuzzy creep).
    assert pays("SP REITMANS") == []


def test_category_scope_and_amount_band(client: TestClient, session: Session) -> None:
    """Category (when set) AND the amount band must BOTH hold (docs/25 D2)."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)

    category_id = _category(client, headers, "Streaming")
    other_id = _category(client, headers, "Food")

    _txn(
        session,
        user_id,
        account,
        payee="NETFLIX",
        day=dt.date(2026, 1, 5),
        amount="-15.99",
        category_id=category_id,
    )
    _txn(
        session,
        user_id,
        account,
        payee="NETFLIX",
        day=dt.date(2026, 2, 5),
        amount="-9.99",
        category_id=other_id,
    )
    _txn(
        session,
        user_id,
        account,
        payee="NETFLIX",
        day=dt.date(2026, 3, 5),
        amount="-15.99",
        category_id=category_id,
    )

    scoped = _bill(
        client,
        headers,
        name="Netflix",
        match_term="NETFLIX",
        category_id=category_id,
        amount="15.99",
    )
    detail = client.get(f"{BILLS}/{scoped['id']}/detail", headers=headers).json()
    assert detail["matched_count"] == 2
    assert detail["bill"]["payment_count"] == 2

    # Same payee, no category scope: the differently-categorised row comes back.
    open_bill = _bill(client, headers, name="Netflix2", match_term="NETFLIX", amount=None)
    detail = client.get(f"{BILLS}/{open_bill['id']}/detail", headers=headers).json()
    assert detail["matched_count"] == 3


def test_amount_tolerance_is_exact_by_default_and_symmetric(
    client: TestClient, session: Session
) -> None:
    """`tolerance = None` means exact; `±T` accepts both directions."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    for day, amount in (
        (dt.date(2026, 1, 5), "-15.99"),  # exact
        (dt.date(2026, 2, 5), "-18.50"),  # +2.51
        (dt.date(2026, 3, 5), "-13.00"),  # -2.99
        (dt.date(2026, 4, 5), "-30.00"),  # way off
    ):
        _txn(session, user_id, account, payee="HYDRO", day=day, amount=amount)

    strict = _bill(client, headers, name="Hydro", match_term="HYDRO", amount="15.99")
    assert (
        client.get(f"{BILLS}/{strict['id']}/detail", headers=headers).json()["matched_count"] == 1
    )

    band = _bill(
        client,
        headers,
        name="Hydro",
        match_term="HYDRO",
        amount="15.99",
        amount_tolerance=3.0,
    )
    detail = client.get(f"{BILLS}/{band['id']}/detail", headers=headers).json()
    assert detail["matched_count"] == 3, "tolerance must accept BOTH directions"
    amounts = sorted(
        abs(Decimal(item["amount_native"])) for item in detail["transactions"]["items"]
    )
    assert amounts == [Decimal("13.00"), Decimal("15.99"), Decimal("18.50")]


def test_a_variable_bill_ignores_the_amount_clause(client: TestClient, session: Session) -> None:
    """No `amount` = nothing to be within, so every payee hit counts."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    _txn(session, user_id, account, payee="GROCERY", day=dt.date(2026, 1, 5), amount="-41.20")
    _txn(session, user_id, account, payee="GROCERY", day=dt.date(2026, 2, 5), amount="-97.75")
    bill = _bill(client, headers, name="Groceries", match_term="GROCERY", amount=None)
    assert client.get(f"{BILLS}/{bill['id']}/detail", headers=headers).json()["matched_count"] == 2


def test_rule_validation_and_the_amount_tolerance_guard(client: TestClient) -> None:
    """`PUT /bills/{id}/rule`: a full replacement with two hard refusals."""
    headers, _user_id = _auth(client)
    bill = _bill(client, headers, name="Telus")

    ok = client.put(
        f"{BILLS}/{bill['id']}/rule",
        headers=headers,
        json={"match_mode": "starts_with", "match_term": "TELUS", "amount_tolerance": 5},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["match_mode"] == "starts_with"
    assert ok.json()["match_term"] == "TELUS"
    assert ok.json()["amount_tolerance"] == "5.00"

    blank = client.put(
        f"{BILLS}/{bill['id']}/rule",
        headers=headers,
        json={"match_mode": "contains", "match_term": "   "},
    )
    assert blank.status_code == 422 and blank.json()["code"] == "validation_error"

    # A tolerance with no amount to measure against is a misconfiguration.
    variable = _bill(client, headers, name="Variable", amount=None)
    stranded = client.put(
        f"{BILLS}/{variable['id']}/rule",
        headers=headers,
        json={"match_mode": "contains", "match_term": "VARIABLE", "amount_tolerance": 5},
    )
    assert stranded.status_code == 422
    assert stranded.json()["code"] == "validation_error"

    # ...and the bill side cannot strand an existing one either.
    banded = _bill(client, headers, name="Banded", amount=10, amount_tolerance=2)
    null_amount = client.patch(f"{BILLS}/{banded['id']}", headers=headers, json={"amount": None})
    assert null_amount.status_code == 422
    assert null_amount.json()["code"] == "validation_error"


def test_the_matched_set_is_derived_so_a_rule_edit_is_retroactive(
    client: TestClient, session: Session
) -> None:
    """D3: nothing is copied, so widening the rule widens the bill AT ONCE."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    _txn(session, user_id, account, payee="AMAZON MKTP", day=dt.date(2026, 1, 5), amount="-60")
    _txn(session, user_id, account, payee="AMAZON PRIME", day=dt.date(2026, 2, 5), amount="-10")

    bill = _bill(client, headers, name="Amazon", match_term="AMAZON MKTP", amount=None)
    before = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers).json()
    assert before["matched_count"] == 1

    client.put(
        f"{BILLS}/{bill['id']}/rule",
        headers=headers,
        json={"match_mode": "starts_with", "match_term": "AMAZON", "amount_tolerance": None},
    )
    after = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers).json()
    assert after["matched_count"] == 2, "the rule IS the linkage — no copy to refresh"

    client.put(
        f"{BILLS}/{bill['id']}/rule",
        headers=headers,
        json={"match_mode": "exact", "match_term": "AMAZON PRIME", "amount_tolerance": None},
    )
    narrowed = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers).json()
    assert narrowed["matched_count"] == 1
    assert narrowed["transactions"]["items"][0]["normalized_payee"] == "AMAZON PRIME"


# --------------------------------------------------------------------------- #
# B2 — the detail page: rolling average + period chart
# --------------------------------------------------------------------------- #


def test_rolling_average_and_chart_are_hand_recomputable(
    client: TestClient, session: Session
) -> None:
    """Six monthly payments, one obvious mean/median, all six bars present.

    Hand computation (the M91 standard):
      totals  = 10.00 20.00 30.00 40.00 50.00 60.00
      mean    = 210 / 6           = 35.00
      median  = (30.00 + 40.00)/2 = 35.00
      6-period and 12-period windows both see the SAME six bars (the chart
      only spans first→last payment), so both means are 35.00.
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    amounts = ["-10.00", "-20.00", "-30.00", "-40.00", "-50.00", "-60.00"]
    for index, amount in enumerate(amounts):
        _txn(
            session,
            user_id,
            account,
            payee="SIXBILL",
            day=dt.date(2026, 1 + index, 7),
            amount=amount,
        )
    bill = _bill(client, headers, name="Six", match_term="SIXBILL", amount=None)

    detail = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()

    assert body["matched_count"] == 6
    assert body["bill"]["payment_count"] == 6
    assert body["rolling"]["six"]["mean"] == "35.00"
    assert body["rolling"]["six"]["median"] == "35.00"
    assert body["rolling"]["twelve"]["mean"] == "35.00"
    assert body["rolling"]["twelve"]["window_periods"] == 6  # only 6 exist
    assert body["bill"]["rolling_average"] == "35.00"

    points = body["chart"]["points"]
    assert [point["period"] for point in points] == [
        "2026-01-01",
        "2026-02-01",
        "2026-03-01",
        "2026-04-01",
        "2026-05-01",
        "2026-06-01",
    ]
    assert [point["total"] for point in points] == [
        "10.00",
        "20.00",
        "30.00",
        "40.00",
        "50.00",
        "60.00",
    ]
    assert all(not point["missed"] for point in points)
    # The chart total over the whole span, recomputed by hand: 210.00.
    assert sum(Decimal(point["total"]) for point in points) == Decimal("210.00")


def test_a_skipped_period_is_a_zero_bar_with_a_missed_marker(
    client: TestClient, session: Session
) -> None:
    """Jan and Mar paid, Feb skipped → three bars, the middle one `missed`."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    for month in (1, 3):
        _txn(
            session,
            user_id,
            account,
            payee="GAPPY",
            day=dt.date(2026, month, 9),
            amount="-25.00",
        )
    bill = _bill(client, headers, name="Gappy", match_term="GAPPY", amount=None)
    body = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers).json()

    points = body["chart"]["points"]
    assert [point["period"] for point in points] == [
        "2026-01-01",
        "2026-02-01",
        "2026-03-01",
    ]
    february = points[1]
    assert february["missed"] is True
    assert february["count"] == 0
    assert february["total"] == "0.00"
    # A miss still counts against the average: 25 + 0 + 25 over 3 = 16.67.
    assert body["rolling"]["six"]["mean"] == "16.67"
    assert body["rolling"]["six"]["window_periods"] == 3


def test_quarterly_and_yearly_bills_bucket_on_their_own_cadence(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    for month in (1, 4, 7):
        _txn(
            session, user_id, account, payee="INSURANCE", day=dt.date(2026, month, 3), amount="-300"
        )
    for year in (2025, 2026):
        _txn(session, user_id, account, payee="DOMAIN", day=dt.date(year, 6, 3), amount="-20")

    quarterly = _bill(
        client,
        headers,
        name="Insurance",
        match_term="INSURANCE",
        frequency="quarterly",
        amount=None,
    )
    body = client.get(f"{BILLS}/{quarterly['id']}/detail", headers=headers).json()
    assert [point["period"] for point in body["chart"]["points"]] == [
        "2026-01-01",
        "2026-04-01",
        "2026-07-01",
    ]
    assert body["chart"]["points"][0]["label"] == "Q1 2026"

    yearly = _bill(
        client,
        headers,
        name="Domain",
        match_term="DOMAIN",
        frequency="yearly",
        amount=None,
    )
    body = client.get(f"{BILLS}/{yearly['id']}/detail", headers=headers).json()
    assert [point["period"] for point in body["chart"]["points"]] == [
        "2025-01-01",
        "2026-01-01",
    ]
    assert body["chart"]["points"][1]["label"] == "2026"


def test_detail_paginates_the_matched_rows_and_keeps_the_true_total(
    client: TestClient, session: Session
) -> None:
    """docs/08 page shape: `{items, total, limit, offset}` over the match."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    for day in range(1, 21):
        _txn(
            session,
            user_id,
            account,
            payee="MANY",
            day=dt.date(2026, 1, day),
            amount=f"-{day}.00",
        )
    bill = _bill(client, headers, name="Many", match_term="MANY", amount=None)

    page = client.get(
        f"{BILLS}/{bill['id']}/detail", headers=headers, params={"limit": 5, "offset": 0}
    ).json()
    assert page["transactions"]["limit"] == 5
    assert page["transactions"]["offset"] == 0
    assert page["transactions"]["total"] == 20
    assert len(page["transactions"]["items"]) == 5
    # Newest first, the same order the ledger list uses.
    assert page["transactions"]["items"][0]["date"] == "2026-01-20"
    assert page["matched_count"] == 20

    page2 = client.get(
        f"{BILLS}/{bill['id']}/detail", headers=headers, params={"limit": 5, "offset": 5}
    ).json()
    assert [item["date"] for item in page2["transactions"]["items"]] == [
        "2026-01-15",
        "2026-01-14",
        "2026-01-13",
        "2026-01-12",
        "2026-01-11",
    ]


def test_detail_from_to_window_bounds_the_whole_response(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    for month in (1, 2, 3):
        _txn(session, user_id, account, payee="WINDOW", day=dt.date(2026, month, 4), amount="-9.00")
    bill = _bill(client, headers, name="Window", match_term="WINDOW", amount=None)

    body = client.get(
        f"{BILLS}/{bill['id']}/detail",
        headers=headers,
        params={"from": "2026-02-01", "to": "2026-03-31"},
    ).json()
    assert body["matched_count"] == 2
    assert body["transactions"]["total"] == 2
    assert [point["period"] for point in body["chart"]["points"]] == [
        "2026-02-01",
        "2026-03-01",
    ]
    # The LIST number stays lifetime — narrowing the picture doesn't rewrite it.
    assert body["bill"]["payment_count"] == 3


# --------------------------------------------------------------------------- #
# B1 — "Find past payments"
# --------------------------------------------------------------------------- #


def test_find_past_groups_by_period_and_writes_nothing(
    client: TestClient, session: Session
) -> None:
    """A read-only preview: grouped per period, and NO linkage row appears."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    for index, month in enumerate((1, 2, 2)):
        _txn(
            session,
            user_id,
            account,
            payee="DUAL",
            day=dt.date(2026, month, 5 + index),
            amount="-11.00",
            dedup=f"dual-{index}",
        )
    bill = _bill(client, headers, name="Dual", match_term="DUAL", amount=None)
    rows_before = len(load_rows(session, user_id))

    response = client.post(f"{BILLS}/{bill['id']}/find-past", headers=headers, json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["match_term"] == "DUAL"
    assert body["total_count"] == 3
    assert [group["period"] for group in body["groups"]] == ["2026-02-01", "2026-01-01"]
    assert body["groups"][0]["count"] == 2
    assert body["groups"][0]["total"] == "22.00"
    assert body["groups"][1]["total"] == "11.00"
    assert all(group["items"][0]["account_name"] for group in body["groups"])
    assert body["truncated"] is False

    # Nothing was persisted: the matched set stays derived (D3).
    assert len(load_rows(session, user_id)) == rows_before
    assert client.get(f"{BILLS}/{bill['id']}/detail", headers=headers).json()["matched_count"] == 3


def test_find_past_runs_an_unsaved_rule_for_the_create_form(
    client: TestClient, session: Session
) -> None:
    """`POST /bills/find-past` previews before any bill row exists (B4)."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    _txn(session, user_id, account, payee="NEWBILL", day=dt.date(2026, 5, 2), amount="-9.99")
    _txn(session, user_id, account, payee="NEWBILLER", day=dt.date(2026, 5, 3), amount="-1.00")

    body = client.post(
        f"{BILLS}/find-past",
        headers=headers,
        json={
            "match_term": "NEWBILL",
            "match_mode": "exact",
            "amount": 9.99,
            "frequency": "monthly",
        },
    )
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["total_count"] == 1
    assert payload["groups"][0]["items"][0]["normalized_payee"] == "NEWBILL"

    # A pending (uncommitted) import never appears — staging is the §2 rule.
    batch = ImportBatch(
        user_id=user_id,
        account_id=account.id,
        institution="ofx",
        file_name="staged.ofx",
        status=ImportBatchStatus.pending_review,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    _txn(
        session,
        user_id,
        account,
        payee="NEWBILL",
        day=dt.date(2026, 6, 2),
        amount="-9.99",
        batch_id=batch.id,
    )
    staged = client.post(
        f"{BILLS}/find-past",
        headers=headers,
        json={"match_term": "NEWBILL", "match_mode": "exact"},
    ).json()
    assert staged["total_count"] == 1, "a pending import must not count as a payment"


# --------------------------------------------------------------------------- #
# B4 + tenancy
# --------------------------------------------------------------------------- #


def test_the_list_carries_payment_count_and_rolling_average(
    client: TestClient, session: Session
) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    for month in (5, 6):
        _txn(
            session, user_id, account, payee="LISTED", day=dt.date(2026, month, 4), amount="-30.00"
        )
    _txn(session, user_id, account, payee="UNRELATED", day=dt.date(2026, 5, 4), amount="-7.00")

    found = _bill(client, headers, name="Listed", match_term="LISTED", amount=None)
    empty = _bill(client, headers, name="Nothing yet", match_term="NOTHERE", amount=None)

    rows = {row["id"]: row for row in client.get(BILLS, headers=headers).json()}
    assert rows[found["id"]]["payment_count"] == 2
    assert rows[found["id"]]["rolling_average"] == "30.00"
    assert rows[empty["id"]]["payment_count"] == 0
    assert rows[empty["id"]]["rolling_average"] is None, "0 payments yet, honestly null"


def test_a_foreign_bill_is_404_and_another_tenant_sees_nothing(
    client: TestClient, session: Session
) -> None:
    headers_a, user_a = _auth(client)
    headers_b, _user_b = _auth(client)
    account = _account(session, user_a)
    _txn(session, user_a, account, payee="SECRET", day=dt.date(2026, 1, 5), amount="-5.00")
    bill = _bill(client, headers_a, name="Secret", match_term="SECRET", amount=None)

    assert client.get(f"{BILLS}/{bill['id']}/detail", headers=headers_b).status_code == 404
    assert (
        client.post(f"{BILLS}/{bill['id']}/find-past", headers=headers_b, json={}).status_code
        == 404
    )
    assert (
        client.put(
            f"{BILLS}/{bill['id']}/rule",
            headers=headers_b,
            json={"match_mode": "contains", "match_term": "X"},
        ).status_code
        == 404
    )

    assert client.get(BILLS, headers=headers_b).json() == []
    # The other tenant's OWN scan finds none of user A's rows.
    assert (
        client.post(f"{BILLS}/find-past", headers=headers_b, json={"match_term": "SECRET"}).json()[
            "total_count"
        ]
        == 0
    )


def test_an_awaiting_import_row_never_counts_as_a_payment(
    client: TestClient, session: Session
) -> None:
    """Staging: only `status='committed'` rows are in the ledger (§2)."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    batch = ImportBatch(
        user_id=user_id,
        account_id=account.id,
        institution="ofx",
        file_name="pending.ofx",
        status=ImportBatchStatus.pending_review,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    _txn(
        session,
        user_id,
        account,
        payee="PENDING",
        day=dt.date(2026, 1, 5),
        amount="-42.00",
        batch_id=batch.id,
    )
    bill = _bill(client, headers, name="Pending", match_term="PENDING", amount=None)
    body = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers).json()
    assert body["matched_count"] == 0


def test_split_parents_are_the_statement_lines(client: TestClient, session: Session) -> None:
    """M107 IDENTITY: a split parent counts once, its children never do."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    parent = _txn(
        session, user_id, account, payee="SPLITPAY", day=dt.date(2026, 1, 5), amount="-100.00"
    )
    parent.is_split = True
    parent.split_checksum = Decimal("-100.00")
    session.add(parent)
    child = _txn(
        session,
        user_id,
        account,
        payee="SPLITPAY part",
        day=dt.date(2026, 1, 5),
        amount="-60.00",
        dedup="m116-child-1",
    )
    child.parent_split_id = parent.id
    session.add(child)
    session.commit()

    bill = _bill(client, headers, name="Split", match_term="SPLITPAY", amount=None)
    body = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers).json()
    assert body["matched_count"] == 1, "the parent is the statement line; parts are not bills"
    assert body["transactions"]["items"][0]["id"] == parent.id


# --------------------------------------------------------------------------- #
# Performance: the scan is bounded by the tenant, and 10k rows fit the budget
# --------------------------------------------------------------------------- #


def _explain(session: Session, sql: str) -> list[str]:
    return [str(line) for line in session.execute(text(f"EXPLAIN {sql}")).scalars().all()]


def _loader_sql(user_id: int) -> str:
    statement = _rows_statement(user_id).order_by(None)
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled)


def test_detail_first_paint_is_under_the_1_5s_budget_at_10k_rows(
    client: TestClient, session: Session, tmp_path
) -> None:
    """docs/25 §M116 Wire: `< 1.5s` at 10k committed transactions.

    Two claims get measured rather than asserted in prose:

    * the SCAN is tenant-bounded — its plan carries the `user_id = ` predicate,
      so the cost tracks one person's ledger, not the table;
    * the PAYEE predicate is deliberately NOT in the plan (the shared Python
      normalizer is the single source of truth, `bill_match` module docstring),
      and the plan an `ILIKE` would have used is recorded alongside it so
      docs/02 can state which index serves which consumer honestly.

    The evidence file lands next to the test run so the docs/25 claim can be
    re-checked instead of believed.
    """
    headers, user_id = _auth(client)
    account = _account(session, user_id)

    total = 10_000
    session.execute(
        text(
            """
            INSERT INTO transactions
              (user_id, account_id, date, raw_description, normalized_payee,
               amount_native, currency, dedup_hash, created_at, updated_at)
            SELECT :uid, :aid, date '2024-01-01' + (i % 730),
                   payee, payee, -((i % 200) + 0.99), 'CAD', 'm116-perf-' || i,
                   now(), now()
            FROM generate_series(1, :total) AS i,
                 LATERAL (SELECT (ARRAY['AMAZON MKTP','SOBOY','NETFLIX.COM',
                                        'PAYROLL','TTC FARE'])[1 + (i % 5)] AS payee) p
            """
        ),
        {"uid": user_id, "aid": account.id, "total": total},
    )
    session.commit()

    loader = _loader_sql(user_id)
    plan = _explain(session, loader)
    # The tripwire: whatever access path the planner picks, the tenant
    # predicate is IN the plan. Drop `user_id` from the statement and this
    # line disappears — the scan would silently become a table-wide one.
    assert any("user_id" in line for line in plan), plan

    # Evidence for the index question docs/02 answers: what a payee ILIKE
    # WOULD have used (the pg_trgm GIN index on normalized_payee, migration
    # 0004) next to the plan we actually run.
    ilike_plan = _explain(
        session,
        "SELECT id FROM transactions "  # noqa: S608 -- int user_id from this test's fixture
        f"WHERE user_id = {user_id} AND normalized_payee ILIKE '%AMAZON MKTP%'",
    )

    bill = _bill(client, headers, name="Amazon", match_term="AMAZON MKTP", amount=None)
    started = time.perf_counter()
    response = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers)
    elapsed = time.perf_counter() - started
    assert response.status_code == 200, response.text
    assert response.json()["matched_count"] == 2000

    list_started = time.perf_counter()
    assert client.get(BILLS, headers=headers).status_code == 200
    list_elapsed = time.perf_counter() - list_started

    (tmp_path / "m116_perf_evidence.json").write_text(
        json.dumps(
            {
                "rows": total,
                "matched": 2000,
                "detail_first_paint_s": round(elapsed, 4),
                "list_s": round(list_elapsed, 4),
                "budget_s": 1.5,
                "loader_plan": plan,
                "payee_ilike_plan_for_reference": ilike_plan,
                "payee_predicate_in_sql": False,
                "reason": (
                    "normalize_payee_key is a Python rule; Postgres case folding "
                    "is a different function, so no ILIKE/upper() predicate over "
                    "the raw column is a proven superset of it."
                ),
            },
            indent=2,
        )
    )

    # The two WALL-CLOCK claims last, and only when the box can answer them:
    # everything above (plan shape, tenant predicate, matched count, the
    # evidence file itself) is enforced on every run, loaded or not.
    # `latency_budget_blocker` lives in tests/conftest.py so every wall-clock
    # pin in the suite answers the same question the same way (docs/34).
    blocker = latency_budget_blocker()
    if blocker is not None:
        pytest.skip(
            "1.5s first-paint budget not measurable on this box: "
            f"{blocker}; measured {elapsed:.3f}s detail / {list_elapsed:.3f}s"
            f" list at {total} rows (evidence written regardless)"
        )
    assert elapsed < 1.5, f"detail first paint took {elapsed:.3f}s at {total} rows"
    assert list_elapsed < 1.5, f"the list took {list_elapsed:.3f}s at {total} rows"

"""M122 — bill-rule UX: term suggestions, dry-run stats, rolling line, spend stats.

docs/25 §M122 is the spec; this module is its executable half. Four things get
pinned hard, because each is a way the feature could look right and be wrong:

* **suggestions are the caller's OWN ledger** — grouped by the SAME key the
  matcher compares (`normalize_payee_key`), ranked frequency-then-recency,
  excluding exactly the payees the bill's CURRENT rule already catches (a
  suggestion that changes nothing is noise), and never reaching across
  tenants or outside the database (docs/11);
* **the dry-run summary counts the WHOLE matched set** — the previewed
  `items` truncate, so `stats` must be computed off `matched` itself (M91
  anti-fabrication);
* **the chart line and the card's stat are ONE number** — the last point's
  `rolling_six` equals `rolling.six.mean` by construction, and each point's
  value is recomputed by hand here;
* **all-time / YTD are recomputed from the rule each read** — missed months
  never enter the total, YTD is the CALENDAR year (Jan 1 -> server today, not
  the bill's anniversary), and a windowed `from`/`to` read never narrows them.
"""

from __future__ import annotations

import ast
import datetime as dt
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.transactions import Transaction
from app.services.txn_filters import today_et

API = "/api/v1"
BILLS = f"{API}/bills"


def _auth(client: TestClient) -> tuple[dict[str, str], int]:
    email = f"m122-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    user_id = client.get(f"{API}/users/me", headers=headers).json()["id"]
    return headers, user_id


def _account(session: Session, user_id: int, name: str = "Chequing") -> Account:
    account = Account(
        user_id=user_id,
        name=f"{name} {user_id}",
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
    dedup: str | None = None,
) -> Transaction:
    txn = Transaction(
        user_id=user_id,
        account_id=account.id,
        date=day,
        raw_description=payee,
        normalized_payee=payee,
        amount_native=Decimal(amount),
        currency="CAD",
        dedup_hash=dedup or f"m122-{user_id}-{payee}-{day.isoformat()}-{amount}",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _bill(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str = "Netflix",
    amount: str | float | None = None,
    frequency: str = "monthly",
    match_terms: list[str] | None = None,
    match_mode: str = "contains",
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
    if match_terms is not None:
        payload["match_terms"] = match_terms
    response = client.post(BILLS, headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# T1 — GET /bills/suggest-terms
# --------------------------------------------------------------------------- #


def test_suggest_terms_ranks_by_frequency_then_recency(client, session) -> None:
    """Frequency first; recency breaks ties; the label is the NEWEST spelling."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    year = today_et().year - 1
    # Same payee key ("ROGERS") under two spellings — ONE group, labelled with
    # the newest row's real string out of the ledger.
    _txn(session, user_id, account, payee="Rogers 1234", day=dt.date(year, 1, 5), amount="-50.00")
    _txn(session, user_id, account, payee="rogers #77", day=dt.date(year, 3, 5), amount="-55.00")
    _txn(session, user_id, account, payee="NETFLIX", day=dt.date(year, 2, 10), amount="-15.99")
    _txn(session, user_id, account, payee="NETFLIX", day=dt.date(year, 3, 10), amount="-15.99")
    _txn(session, user_id, account, payee="COFFEE SHOP", day=dt.date(year, 3, 20), amount="-4.50")

    body = client.get(f"{BILLS}/suggest-terms", headers=headers)
    assert body.status_code == 200, body.text
    suggestions = body.json()
    # NETFLIX (2, newest 03-10) beats rogers (2, newest 03-05) on recency,
    # COFFEE SHOP (1) comes last.
    assert [item["normalized_payee"] for item in suggestions] == [
        "NETFLIX",
        "rogers #77",
        "COFFEE SHOP",
    ]
    assert [item["count"] for item in suggestions] == [2, 2, 1]
    rogers = suggestions[1]
    assert rogers["total"] == "105.00"
    assert rogers["last_date"] == f"{year}-03-05"


def test_suggest_terms_filters_by_the_draft(client, session) -> None:
    """`q` narrows through the SAME normalizer the matcher uses."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    year = today_et().year - 1
    _txn(session, user_id, account, payee="rogers #77", day=dt.date(year, 1, 5), amount="-50.00")
    _txn(session, user_id, account, payee="NETFLIX", day=dt.date(year, 2, 5), amount="-15.99")

    for query, expected in [
        ("rog", ["rogers #77"]),  # case-insensitive through the normalizer
        ("ROG", ["rogers #77"]),
        ("netf", ["NETFLIX"]),
        ("xyz", []),
        # A draft that can never name a payee answers [] — the same answer
        # its committed chip would get (docs/25 §M121 D7).
        ("123", []),
    ]:
        body = client.get(f"{BILLS}/suggest-terms", headers=headers, params={"q": query})
        assert body.status_code == 200, body.text
        assert [item["normalized_payee"] for item in body.json()] == expected, query


def test_suggest_terms_follows_the_editors_match_mode(client, session) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    day = dt.date(today_et().year - 1, 3, 1)
    _txn(session, user_id, account, payee="MAINNET CAFE", day=day, amount="-5.00")

    contains = client.get(
        f"{BILLS}/suggest-terms", headers=headers, params={"q": "net", "match_mode": "contains"}
    ).json()
    starts = client.get(
        f"{BILLS}/suggest-terms",
        headers=headers,
        params={"q": "net", "match_mode": "starts_with"},
    ).json()
    assert [item["normalized_payee"] for item in contains] == ["MAINNET CAFE"]
    assert starts == []
    unknown = client.get(f"{BILLS}/suggest-terms", headers=headers, params={"match_mode": "fuzzy"})
    assert unknown.status_code == 422


def test_suggest_terms_excludes_the_bills_current_matched_set(client, session) -> None:
    """A payee the rule ALREADY catches is noise — excluded, server-side."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    day = dt.date(today_et().year - 1, 3, 1)
    _txn(session, user_id, account, payee="NETFLIX", day=day, amount="-15.99")
    _txn(
        session,
        user_id,
        account,
        payee="NETFLIX",
        day=dt.date(today_et().year - 1, 4, 1),
        amount="-15.99",
    )
    _txn(session, user_id, account, payee="rogers #77", day=day, amount="-50.00")
    bill = _bill(client, headers, name="Netflix", match_terms=["NETFLIX"])

    everything = client.get(f"{BILLS}/suggest-terms", headers=headers).json()
    assert [item["normalized_payee"] for item in everything] == ["NETFLIX", "rogers #77"]

    scoped = client.get(
        f"{BILLS}/suggest-terms", headers=headers, params={"bill_id": bill["id"]}
    ).json()
    assert [item["normalized_payee"] for item in scoped] == ["rogers #77"]


def test_suggest_terms_is_tenants_own_ledger_only(client, session) -> None:
    headers_a, _user_a = _auth(client)
    _headers_b, user_b = _auth(client)
    account_b = _account(session, user_b, name="B's account")
    _txn(
        session,
        user_b,
        account_b,
        payee="SECRET PAYEE",
        day=dt.date(today_et().year - 1, 1, 1),
        amount="-9.99",
    )

    body = client.get(f"{BILLS}/suggest-terms", headers=headers_a)
    assert body.status_code == 200
    assert body.json() == []


def test_suggest_terms_foreign_bill_id_is_404(client, session) -> None:
    headers_a, _user_a = _auth(client)
    headers_b, _user_b = _auth(client)
    bill_b = _bill(client, headers_b, name="Netflix", match_terms=["NETFLIX"])

    body = client.get(f"{BILLS}/suggest-terms", headers=headers_a, params={"bill_id": bill_b["id"]})
    assert body.status_code == 404
    assert body.json()["code"] == "not_found"


def test_suggest_terms_limit_and_bounds(client, session) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    base = dt.date(today_et().year - 1, 1, 1)
    # Distinct keys (trailing digits normalize away — "PAYEE 0".."PAYEE 4"
    # would be ONE group), recency-ordered so the ranking is deterministic.
    for index, payee in enumerate(["ZEBRA COFFEE", "YAK SHOP", "XRAY CLINIC", "WINE BAR"]):
        _txn(
            session,
            user_id,
            account,
            payee=payee,
            day=base + dt.timedelta(days=index),
            amount="-1.00",
        )
    _txn(
        session,
        user_id,
        account,
        payee="VOLVO SERVICE",
        day=base + dt.timedelta(days=9),
        amount="-1.00",
    )

    top = client.get(f"{BILLS}/suggest-terms", headers=headers, params={"limit": 1}).json()
    assert len(top) == 1
    assert top[0]["normalized_payee"] == "VOLVO SERVICE"  # newest of the five 1-count groups

    assert (
        client.get(f"{BILLS}/suggest-terms", headers=headers, params={"limit": 0}).status_code
        == 422
    )
    assert (
        client.get(f"{BILLS}/suggest-terms", headers=headers, params={"limit": 26}).status_code
        == 422
    )
    assert (
        client.get(f"{BILLS}/suggest-terms", headers=headers, params={"q": "x" * 121}).status_code
        == 422
    )


def test_suggest_terms_works_under_privacy_mode_and_never_leaves_the_db(client, session) -> None:
    """Privacy (docs/11): the suggestions are a DB query — no egress to seal."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    _txn(
        session,
        user_id,
        account,
        payee="NETFLIX",
        day=dt.date(today_et().year - 1, 1, 1),
        amount="-15.99",
    )
    patched = client.patch(f"{API}/users/me", json={"privacy_mode": True}, headers=headers)
    assert patched.status_code == 200, patched.text

    body = client.get(f"{BILLS}/suggest-terms", headers=headers)
    assert body.status_code == 200, body.text
    assert [item["normalized_payee"] for item in body.json()] == ["NETFLIX"]

    # The modules behind the endpoint import NO network client — the same
    # docs/11 pin `bill_match`'s own module docstring makes.
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[1]
    banned = {
        "httpx",
        "requests",
        "yfinance",
        "socket",
        "urllib",
        "app.llm",
        "app.services.market_data",
        "app.services.fx",
    }
    for module in ("app/api/bill_reminders.py", "app/services/bill_match.py"):
        tree = ast.parse((backend / module).read_text())
        seen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                seen.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen.add(node.module)
        assert not (seen & banned), f"{module} imports a network client: {seen & banned}"


# --------------------------------------------------------------------------- #
# T2 — the dry-run's stats block (both find-past routes)
# --------------------------------------------------------------------------- #


def test_find_past_stats_are_hand_recomputed(client, session) -> None:
    """first/last date, the 12-period sum and per-account counts over the
    WHOLE matched set — with a miss and a non-matching row in the fixture."""
    headers, user_id = _auth(client)
    chequing = _account(session, user_id, name="Chequing")
    visa = _account(session, user_id, name="Visa")
    year = today_et().year - 1
    _txn(session, user_id, chequing, payee="NETFLIX", day=dt.date(year, 11, 20), amount="-15.99")
    _txn(session, user_id, visa, payee="NETFLIX", day=dt.date(year, 12, 20), amount="-15.99")
    _txn(session, user_id, chequing, payee="NETFLIX", day=dt.date(year + 1, 1, 20), amount="-15.99")
    # February is a MISS — it must count as $0.00 nowhere here because the sum
    # is over the payments; the bucket simply is not in the groups.
    _txn(session, user_id, chequing, payee="NETFLIX", day=dt.date(year + 1, 3, 20), amount="-15.99")
    # A coffee next to the last payment: matched by NEITHER the rule nor the
    # stats.
    _txn(session, user_id, chequing, payee="COFFEE", day=dt.date(year + 1, 3, 21), amount="-5.00")
    bill = _bill(client, headers, name="Netflix", match_terms=["NETFLIX"])

    body = client.post(f"{BILLS}/{bill['id']}/find-past", headers=headers, json={})
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["total_count"] == 4
    stats = payload["stats"]
    assert stats["first_date"] == f"{year}-11-20"
    assert stats["last_date"] == f"{year + 1}-03-20"
    # 4 paid buckets inside the last 12 periods: 4 x 15.99 = 63.96 (the
    # missed February contributes $0.00).
    assert stats["last_12_total"] == "63.96"
    assert stats["account_counts"] == [
        {"account_id": chequing.id, "account_name": chequing.name, "count": 3},
        {"account_id": visa.id, "account_name": visa.name, "count": 1},
    ]


def test_find_past_draft_preview_carries_the_same_stats(client, session) -> None:
    """The CREATE form's unsaved rule (`POST /bills/find-past`) answers the
    same summary — that is what makes a pre-save Test honest."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    day = dt.date(today_et().year - 1, 5, 1)
    _txn(session, user_id, account, payee="ROGERS", day=day, amount="-80.00")
    _txn(
        session,
        user_id,
        account,
        payee="ROGERS",
        day=dt.date(today_et().year - 1, 6, 1),
        amount="-80.00",
    )

    body = client.post(
        f"{BILLS}/find-past",
        headers=headers,
        json={"match_mode": "contains", "match_terms": ["ROGERS"], "frequency": "monthly"},
    )
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["total_count"] == 2
    assert payload["stats"]["first_date"] == f"{today_et().year - 1}-05-01"
    assert payload["stats"]["last_12_total"] == "160.00"
    assert payload["stats"]["account_counts"][0]["count"] == 2


def test_find_past_stats_empty_rule(client, session) -> None:
    headers, _user_id = _auth(client)
    bill = _bill(client, headers, name="Netflix", match_terms=["NETFLIX"])
    body = client.post(f"{BILLS}/{bill['id']}/find-past", headers=headers, json={})
    assert body.status_code == 200, body.text
    assert body.json()["stats"] == {
        "first_date": None,
        "last_date": None,
        "last_12_total": None,
        "account_counts": [],
    }


# --------------------------------------------------------------------------- #
# T3 — the rolling-average line on the chart
# --------------------------------------------------------------------------- #


def test_chart_points_carry_the_rolling_line(client, session) -> None:
    """Each point's `rolling_six` recomputed by hand; the LAST point IS the
    card's `rolling.six.mean` (one number, two places)."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    year = today_et().year - 1
    _txn(session, user_id, account, payee="GYM", day=dt.date(year, 10, 5), amount="-10.00")
    # November is a MISS: the line must dip through it at $0.00.
    _txn(session, user_id, account, payee="GYM", day=dt.date(year, 12, 5), amount="-20.00")
    _txn(session, user_id, account, payee="GYM", day=dt.date(year + 1, 1, 5), amount="-30.00")
    bill = _bill(client, headers, name="Gym", match_terms=["GYM"])

    body = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers)
    assert body.status_code == 200, body.text
    payload = body.json()
    points = payload["chart"]["points"]
    assert [point["total"] for point in points] == ["10.00", "0.00", "20.00", "30.00"]
    # Hand-recomputed trailing means: 10 / (10+0)/2 / (10+0+20)/3 / (10+0+20+30)/4
    assert [point["rolling_six"] for point in points] == [
        "10.00",
        "5.00",
        "10.00",
        "15.00",
    ]
    assert points[-1]["rolling_six"] == payload["rolling"]["six"]["mean"] == "15.00"
    # A missed point still carries its line value (it is $0.00, not absent).
    assert points[1]["missed"] is True
    assert points[1]["rolling_six"] == "5.00"


# --------------------------------------------------------------------------- #
# T4 — all-time + YTD spend stats
# --------------------------------------------------------------------------- #


def test_detail_stats_all_time_and_ytd_with_missed_years(client, session) -> None:
    """M91 standard: every figure hand-recomputed; missed periods contribute
    nothing, YTD is the calendar year, a future row never inflates it, and a
    windowed read never narrows the lifetime stats."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    today = today_et()
    if today.month == 1 and today.day == 1:
        pytest.skip(
            "today IS Jan 1: the Jan-1 row and the today row would be the same "
            "day (same dedup_hash), so the two-row YTD this pins cannot exist"
        )
    _txn(
        session,
        user_id,
        account,
        payee="NETFLIX",
        day=dt.date(today.year - 2, 12, 15),
        amount="-15.99",
    )
    # (today.year - 1 has a single payment — the "years with payments" divisor
    # counts it once, and the empty months around it are simply not spend.)
    _txn(
        session,
        user_id,
        account,
        payee="NETFLIX",
        day=dt.date(today.year - 1, 6, 15),
        amount="-19.99",
    )
    _txn(session, user_id, account, payee="NETFLIX", day=dt.date(today.year, 1, 1), amount="-15.99")
    _txn(session, user_id, account, payee="NETFLIX", day=today, amount="-15.99")
    # Dated NEXT year: real spend (it is in the ledger), but never YTD.
    _txn(
        session,
        user_id,
        account,
        payee="NETFLIX",
        day=dt.date(today.year + 1, 6, 1),
        amount="-99.99",
    )
    bill = _bill(client, headers, name="Netflix", match_terms=["NETFLIX"])

    body = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers)
    assert body.status_code == 200, body.text
    stats = body.json()["stats"]
    # total 15.99 + 19.99 + 15.99 + 15.99 + 99.99 = 167.95 over 5 payments;
    # average 167.95 / 5 = 33.59; four DISTINCT years with payments, so
    # yearly_average = 167.95 / 4 = 41.99 (quantized to cents).
    assert stats["all_time"] == {
        "total": "167.95",
        "payments": 5,
        "average": "33.59",
        "years": 4,
        "yearly_average": "41.99",
    }
    # YTD = Jan 1 -> today: two rows (Jan 1 + today), never the future one.
    assert stats["ytd"] == {"year": today.year, "total": "31.98", "payments": 2}

    # The `bill` inside the detail carries the same lifetime YTD figure.
    assert body.json()["bill"]["ytd_total"] == "31.98"

    # A NARROW window shrinks matched_count and the chart, never the stats.
    # June of last year: exactly one seeded row there on ANY calendar day —
    # a Jan-1..Jan-31 window would hold BOTH Jan-1 rows when today is Jan 1.
    windowed = client.get(
        f"{BILLS}/{bill['id']}/detail",
        headers=headers,
        params={"from": f"{today.year - 1}-06-01", "to": f"{today.year - 1}-06-30"},
    ).json()
    assert windowed["matched_count"] == 1
    assert windowed["stats"] == stats


def test_list_rows_carry_ytd_total(client, session) -> None:
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    today = today_et()
    _txn(
        session,
        user_id,
        account,
        payee="NETFLIX",
        day=dt.date(today.year - 1, 12, 15),
        amount="-15.99",
    )
    _txn(session, user_id, account, payee="NETFLIX", day=today, amount="-15.99")
    _txn(session, user_id, account, payee="AMZN", day=today, amount="-42.00")
    _bill(client, headers, name="Netflix", match_terms=["NETFLIX"])
    _bill(client, headers, name="Amazon", match_terms=["AMZN"])
    _bill(client, headers, name="Spotify", match_terms=["SPOTIFY"])

    body = client.get(BILLS, headers=headers)
    assert body.status_code == 200, body.text
    ytd = {row["name"]: row["ytd_total"] for row in body.json()}
    assert ytd == {"Netflix": "15.99", "Amazon": "42.00", "Spotify": "0.00"}


def test_stats_react_to_the_rule_being_changed(client, session) -> None:
    """Derived, not copied (M116 D3, still true for the M122 numbers): editing
    the rule reshapes all-time/YTD on the next read, with nothing stored."""
    headers, user_id = _auth(client)
    account = _account(session, user_id)
    today = today_et()
    _txn(session, user_id, account, payee="ROGERS", day=today, amount="-80.00")
    # Jan 1 of THIS calendar year: always in-YTD, whatever day the suite runs.
    _txn(session, user_id, account, payee="ROGERS", day=dt.date(today.year, 1, 1), amount="-79.50")
    bill = _bill(client, headers, name="Rogers", match_terms=["ROGERS"])

    before = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers).json()
    assert before["stats"]["ytd"]["payments"] == 2

    # ANY ["ROGERS"] -> ALL ["ROGERS","COMMUNICATIONS"]: nothing carries both
    # words, so the matched set empties and every stat follows.
    narrowed = client.put(
        f"{BILLS}/{bill['id']}/rule",
        headers=headers,
        json={
            "match_mode": "contains",
            "match_terms": ["ROGERS", "COMMUNICATIONS"],
            "match_logic": "all",
        },
    )
    assert narrowed.status_code == 200, narrowed.text
    after = client.get(f"{BILLS}/{bill['id']}/detail", headers=headers).json()
    assert after["stats"]["all_time"]["payments"] == 0
    assert after["stats"]["all_time"]["average"] is None
    assert after["stats"]["ytd"] == {"year": today.year, "total": "0.00", "payments": 0}

"""M96 B2 — passive recurring-transactions detection (docs/09 §Recurring prompt).

Three layers, in the order they are trusted:

1. the PURE heuristic over an in-memory fixture (mixed merchants, amount
   drift, cadence breaks) — this is where the false-positive/negative
   behaviour is pinned;
2. the LOCAL-ONLY claim behind the prompt card's copy — the detector imports
   no network client and runs without a socket;
3. the routes: candidates → accept (tag columns) → suppress (one row,
   idempotent), with tenancy, staging and validation on each.

Run: cd backend && uv run pytest tests/test_recurring.py -q
"""

import socket
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transactions import RecurringCadence, Transaction
from app.services.recurring import (
    LedgerRow,
    detect_recurring,
    make_group_key,
    merchant_key,
    monthly_total,
    next_expected_date,
)
from app.services.txn_filters import today_et
from tests.test_deep_common import auth, create_account, register, seed_transaction


def _today() -> date:
    """The clock, read at CALL time — never captured at import (t_435c2d14)."""
    return today_et()


_BASE = _today() - timedelta(days=60)


def _rows(specs: list[tuple[str, str, date]]) -> list[LedgerRow]:
    return [
        LedgerRow(id=index, date=day, payee=payee, amount=Decimal(amount))
        for index, (payee, amount, day) in enumerate(specs, start=1)
    ]


def _series(
    payee: str,
    amount: str,
    days: list[date],
) -> list[LedgerRow]:
    return _rows([(payee, amount, day) for day in days])


def _monthly_dates(count: int, anchor: date) -> list[date]:
    """`count` same-day dates ending at `anchor`, 30 days apart."""
    return [anchor - timedelta(days=30 * offset) for offset in range(count - 1, -1, -1)]


# --- 1. the heuristic ---------------------------------------------------------


def test_monthly_same_day_series_is_one_candidate_group() -> None:
    groups = detect_recurring(_series("Netflix", "-15.99", _monthly_dates(4, _today())))
    assert len(groups) == 1
    group = groups[0]
    assert group.cadence is RecurringCadence.monthly
    assert group.occurrence_count == 4
    assert group.monthly_total == Decimal("-15.99")
    assert group.last_date == _today()
    assert group.transaction_ids == (1, 2, 3, 4)


def test_three_occurrences_is_the_floor() -> None:
    assert detect_recurring(_series("Spotify", "-5.99", _monthly_dates(2, _today()))) == []
    assert len(detect_recurring(_series("Spotify", "-5.99", _monthly_dates(3, _today())))) == 1


def test_branch_numbers_and_case_collapse_to_one_merchant() -> None:
    """ "SUPERSTORE 1234" ≈ "Superstore #1234" — the import-dedup normalization."""
    days = _monthly_dates(4, _today())
    mixed = [
        LedgerRow(id=1, date=days[0], payee="SUPERSTORE 1234", amount=Decimal("-82.10")),
        LedgerRow(id=2, date=days[1], payee="Superstore #1234", amount=Decimal("-79.40")),
        LedgerRow(id=3, date=days[2], payee="superstore", amount=Decimal("-84.00")),
    ]
    groups = detect_recurring(mixed)
    assert len(groups) == 1
    assert groups[0].occurrence_count == 3
    assert groups[0].merchant_key == "SUPERSTORE"


def test_amount_band_keeps_a_drifted_series_together_and_splits_a_new_one() -> None:
    days = _monthly_dates(4, _today())
    # ±10% drift: same band as the first amount (100 → 110 is 10%).
    drifted = _rows(
        [
            ("Hydro", "-100.00", days[0]),
            ("Hydro", "-110.00", days[1]),
            ("Hydro", "-105.00", days[2]),
        ]
    )
    assert len(detect_recurring(drifted)) == 1

    # 40% apart: a second cluster forms, and neither half reaches 3 rows.
    split = _rows(
        [
            ("Hydro", "-100.00", days[0]),
            ("Hydro", "-140.00", days[1]),
            ("Hydro", "-101.00", days[2]),
        ]
    )
    assert detect_recurring(split) == []

    # Opposite signs are never the same commitment (a refund is not a repeat).
    signed = _rows(
        [
            ("Refund", "-100.00", days[0]),
            ("Refund", "100.00", days[1]),
            ("Refund", "-100.00", days[2]),
        ]
    )
    assert detect_recurring(signed) == []


def test_a_broken_cadence_is_not_recurring() -> None:
    dates = [_today() - timedelta(days=120), _today() - timedelta(days=90), _today()]
    assert detect_recurring(_series("Gym", "-40.00", dates)) == []


def test_fixed_cadences_fit_their_own_gap() -> None:
    weekly = _series("Coffee", "-4.25", [_today() - timedelta(days=7 * i) for i in (3, 2, 1, 0)])
    assert [g.cadence for g in detect_recurring(weekly)] == [RecurringCadence.weekly]

    biweekly = _series("Cut", "-30.00", [_today() - timedelta(days=14 * i) for i in (3, 2, 1, 0)])
    assert [g.cadence for g in detect_recurring(biweekly)] == [RecurringCadence.biweekly]

    # A 28-day cycle walks backwards through the month, so it must NOT be read
    # as monthly — the day-of-month spread is what separates the two.
    four_weekly = _series(
        "Storage", "-50.00", [_today() - timedelta(days=28 * i) for i in (3, 2, 1, 0)]
    )
    assert [g.cadence for g in detect_recurring(four_weekly)] == [RecurringCadence.four_weekly]


def test_merchants_do_not_merge_and_groups_sort_by_commitment() -> None:
    days = _monthly_dates(3, _today())
    groups = detect_recurring(
        _rows(
            [
                ("Netflix", "-15.99", days[0]),
                ("Netflix", "-15.99", days[1]),
                ("Netflix", "-15.99", days[2]),
                ("Rent", "-2000.00", days[0]),
                ("Rent", "-2000.00", days[1]),
                ("Rent", "-2000.00", days[2]),
            ]
        )
    )
    assert [g.payee for g in groups] == ["Rent", "Netflix"]


def test_group_key_is_amount_free_so_drift_cannot_re_prompt() -> None:
    """The suppression key must survive an amount drift inside the band."""
    assert make_group_key("Superstore #1234", RecurringCadence.monthly) == (
        make_group_key("SUPERSTORE 1234", RecurringCadence.monthly)
    )
    assert make_group_key("Netflix", RecurringCadence.monthly) != make_group_key(
        "Netflix", RecurringCadence.weekly
    )
    assert len(make_group_key("x" * 300, RecurringCadence.monthly)) <= 120


def test_monthly_total_normalises_by_cadence() -> None:
    # 7.99/week x 30.4375/7 = 34.7422… → -34.74
    assert monthly_total([Decimal("-7.99")], RecurringCadence.weekly) == Decimal("-34.74")
    # 100.00/fortnight x 30.4375/14 = 217.4107… → -217.41
    assert monthly_total([Decimal("-100.00")], RecurringCadence.biweekly) == Decimal("-217.41")
    assert monthly_total([Decimal("-15.99")], RecurringCadence.monthly) == Decimal("-15.99")
    assert monthly_total([], RecurringCadence.monthly) == Decimal("0.00")


def test_next_expected_monthly_clamps_to_a_short_month() -> None:
    assert next_expected_date(date(2026, 1, 31), RecurringCadence.monthly) == date(2026, 2, 28)
    assert next_expected_date(date(2026, 1, 31), RecurringCadence.weekly) == date(2026, 2, 7)
    assert next_expected_date(date(2026, 12, 15), RecurringCadence.monthly) == date(2027, 1, 15)


# --- 2. local-only -------------------------------------------------------------


def test_detection_module_makes_no_network_calls() -> None:
    """The half of docs/09's copy that runs on the server.

    "This detection runs entirely in your browser / on your server; nothing
    leaves." The server half is an import fact: no HTTP client, no LLM, no fx
    or market-data seam (the only two modules AGENTS §2 allows to reach out).
    """
    source = Path(
        Path(__file__).resolve().parents[1] / "app" / "services" / "recurring.py"
    ).read_text()
    import_lines = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    banned = (
        "httpx",
        "requests",
        "urllib",
        "socket",
        "aiohttp",
        "openai",
        "app.llm",
        "app.services.fx",
        "app.services.market_data",
    )
    for token in banned:
        assert not any(token in line for line in import_lines), token


def test_detection_computes_with_the_socket_layer_switched_off(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("detection touched the network")

    monkeypatch.setattr(socket, "socket", boom)
    groups = detect_recurring(_series("Netflix", "-15.99", _monthly_dates(4, _today())))
    assert len(groups) == 1


# --- 3. the routes --------------------------------------------------------------


def _setup(client: TestClient) -> tuple[dict, int, int]:
    """(headers, user_id, account_id) for a fresh throwaway tenant."""
    body = register(client)
    headers = auth(body["access_token"])
    return headers, body["user"]["id"], create_account(client, headers)


def _seed_group(
    session: Session,
    user_id: int,
    account_id: int,
    payee: str,
    amount: str,
    count: int = 3,
) -> list[int]:
    return [
        seed_transaction(
            session,
            user_id,
            account_id,
            payee=payee,
            amount=amount,
            day=day,
        ).id
        for day in _monthly_dates(count, _today())
    ]


def test_candidates_accept_and_suppress_round_trip(client: TestClient, session: Session) -> None:
    headers, user_id, account_id = _setup(client)
    ids = _seed_group(session, user_id, account_id, "Netflix", "-15.99")

    # --- candidates
    page = client.get("/api/v1/transactions/recurring/candidates", headers=headers)
    assert page.status_code == 200
    body = page.json()
    assert len(body["groups"]) == 1
    group = body["groups"][0]
    assert group["cadence"] == "monthly"
    assert group["transaction_ids"] == ids
    assert group["monthly_total"] == "-15.99"
    assert group["occurrence_count"] == 3
    assert group["group_key"].startswith("NETFLIX")
    assert body["combined_monthly_total"] == "-15.99"
    assert body["suppressed_count"] == 0

    # --- accept: the three tag columns land
    accept = client.post(
        "/api/v1/transactions/recurring/accept",
        headers=headers,
        json={
            "group_key": group["group_key"],
            "transaction_ids": ids,
            "cadence": "monthly",
        },
    )
    assert accept.status_code == 200
    assert accept.json()["updated"] == 3
    tagged = session.exec(select(Transaction).where(Transaction.id.in_(ids))).all()
    assert [row.is_recurring for row in tagged] == [True, True, True]
    assert {row.recurring_cadence for row in tagged} == {RecurringCadence.monthly}
    assert {row.recurring_next_date for row in tagged} == {
        next_expected_date(_today(), RecurringCadence.monthly)
    }

    # The badge columns ride on every list row (docs/08 TransactionPublic).
    listed = client.get("/api/v1/transactions", headers=headers).json()["items"][0]
    assert listed["is_recurring"] is True
    assert listed["recurring_cadence"] == "monthly"
    assert listed["recurring_next_date"] is not None

    # Already tagged → the same merchant is never proposed again.
    again = client.get("/api/v1/transactions/recurring/candidates", headers=headers).json()
    assert again["groups"] == []
    assert again["suppressed_count"] == 0


def test_decline_suppresses_the_group_forever_and_is_idempotent(
    client: TestClient, session: Session
) -> None:
    headers, user_id, account_id = _setup(client)
    _seed_group(session, user_id, account_id, "Spotify", "-5.99")

    first = client.get("/api/v1/transactions/recurring/candidates", headers=headers).json()
    assert len(first["groups"]) == 1
    key = first["groups"][0]["group_key"]

    decline = client.post(
        "/api/v1/transactions/recurring/suppress", headers=headers, json={"group_key": key}
    )
    assert decline.status_code == 204

    second = client.get("/api/v1/transactions/recurring/candidates", headers=headers).json()
    assert second["groups"] == []
    assert second["suppressed_count"] == 1

    # Repeat decline: same answer, no duplicate row (UNIQUE user_id+group_key).
    assert (
        client.post(
            "/api/v1/transactions/recurring/suppress", headers=headers, json={"group_key": key}
        ).status_code
        == 204
    )
    from app.models.recurring_suppressions import RecurringSuppression

    rows = session.exec(
        select(RecurringSuppression).where(RecurringSuppression.user_id == user_id)
    ).all()
    assert len(rows) == 1
    assert rows[0].group_key == key


def test_suppression_is_per_user(client: TestClient, session: Session) -> None:
    owner_headers, owner_id, owner_account = _setup(client)
    _seed_group(session, owner_id, owner_account, "Spotify", "-5.99")
    key = client.get("/api/v1/transactions/recurring/candidates", headers=owner_headers).json()[
        "groups"
    ][0]["group_key"]

    other_headers, other_id, other_account = _setup(client)
    _seed_group(session, other_id, other_account, "Spotify", "-5.99")

    # Both tenants see their OWN group: the key is per-user state, not a
    # global blacklist — two tenants can hold the same "SPOTIFY|monthly".
    other_view = client.get(
        "/api/v1/transactions/recurring/candidates", headers=other_headers
    ).json()
    assert len(other_view["groups"]) == 1
    assert other_view["suppressed_count"] == 0

    assert (
        client.post(
            "/api/v1/transactions/recurring/suppress",
            headers=other_headers,
            json={"group_key": key},
        ).status_code
        == 204
    )
    # The decliner stops seeing it…
    assert (
        client.get("/api/v1/transactions/recurring/candidates", headers=other_headers).json()[
            "groups"
        ]
        == []
    )
    # …and the owner, who never answered, is untouched by someone else's key.
    owner_view = client.get(
        "/api/v1/transactions/recurring/candidates", headers=owner_headers
    ).json()
    assert len(owner_view["groups"]) == 1
    assert owner_view["suppressed_count"] == 0


def test_accept_requires_owned_live_rows(client: TestClient, session: Session) -> None:
    owner_headers, owner_id, owner_account = _setup(client)
    owner_ids = _seed_group(session, owner_id, owner_account, "Netflix", "-15.99")

    other_headers, _, _ = _setup(client)

    # A foreign id is 404, and NOTHING is written for the ids that were valid.
    foreign = client.post(
        "/api/v1/transactions/recurring/accept",
        headers=other_headers,
        json={"group_key": "X|monthly", "transaction_ids": owner_ids, "cadence": "monthly"},
    )
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "not_found"
    first = session.exec(select(Transaction).where(Transaction.id.in_(owner_ids))).first()
    assert first is not None and first.is_recurring is False

    # A pending-review batch row is batch-managed: 409, not tagged.
    batch = ImportBatch(
        user_id=owner_id,
        account_id=owner_account,
        institution="",
        file_name="pending.qfx",
        status=ImportBatchStatus.pending_review,
    )
    session.add(batch)
    session.flush()
    session.add(
        Transaction(
            user_id=owner_id,
            account_id=owner_account,
            date=_today(),
            raw_description="Pending",
            normalized_payee="Pending",
            amount_native=Decimal("-3.00"),
            currency="CAD",
            import_batch_id=batch.id,
            dedup_hash="pending-recurring-hash",
        )
    )
    session.commit()
    pending = session.exec(select(Transaction).where(Transaction.import_batch_id == batch.id)).one()
    blocked = client.post(
        "/api/v1/transactions/recurring/accept",
        headers=owner_headers,
        json={
            "group_key": "P|monthly",
            "transaction_ids": [pending.id],
            "cadence": "monthly",
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "transaction_in_pending_batch"


def test_candidates_never_propose_pending_batch_rows(client: TestClient, session: Session) -> None:
    headers, user_id, account_id = _setup(client)
    batch = ImportBatch(
        user_id=user_id,
        account_id=account_id,
        institution="",
        file_name="pending.qfx",
        status=ImportBatchStatus.pending_review,
    )
    session.add(batch)
    session.flush()
    for offset in range(3):
        session.add(
            Transaction(
                user_id=user_id,
                account_id=account_id,
                date=_today() - timedelta(days=30 * offset),
                raw_description="Staged",
                normalized_payee="Staged",
                amount_native=Decimal("-9.99"),
                currency="CAD",
                import_batch_id=batch.id,
                dedup_hash=f"staged-recurring-{offset}",
            )
        )
    session.commit()
    body = client.get("/api/v1/transactions/recurring/candidates", headers=headers).json()
    assert body["groups"] == []


def test_accept_validates_its_payload(client: TestClient, session: Session) -> None:
    headers, user_id, account_id = _setup(client)
    ids = _seed_group(session, user_id, account_id, "Netflix", "-15.99")

    empty = client.post(
        "/api/v1/transactions/recurring/accept",
        headers=headers,
        json={"group_key": "N|monthly", "transaction_ids": [], "cadence": "monthly"},
    )
    assert empty.status_code == 422
    assert empty.json()["code"] == "validation_error"

    bad_cadence = client.post(
        "/api/v1/transactions/recurring/accept",
        headers=headers,
        json={"group_key": "N|monthly", "transaction_ids": ids, "cadence": "yearly"},
    )
    assert bad_cadence.status_code == 422
    assert bad_cadence.json()["code"] == "validation_error"

    extra_field = client.post(
        "/api/v1/transactions/recurring/suppress",
        headers=headers,
        json={"group_key": "N|monthly", "surprise": True},
    )
    assert extra_field.status_code == 422
    assert extra_field.json()["code"] == "validation_error"


def test_recurring_routes_require_a_session(client: TestClient) -> None:
    assert client.get("/api/v1/transactions/recurring/candidates").status_code == 401
    assert (
        client.post(
            "/api/v1/transactions/recurring/accept",
            json={"group_key": "a|monthly", "transaction_ids": [1], "cadence": "monthly"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/transactions/recurring/suppress", json={"group_key": "a|monthly"}
        ).status_code
        == 401
    )


# --- M165 — one payee, one key, one candidate --------------------------------
#
# "Never offer to create for something already a bill" starts with knowing
# what "the same payee" MEANS: Mark's ledger carries ONE YouTube Premium
# subscription under three statement spellings, and the detector used to split
# them into two candidate groups (both with a Create-bill button) because the
# space before the `*` survived normalization. The bill matcher, the detector
# and the suppression keys all read `normalize_payee_key`, so the fix is in
# that one function — never in a second, friendlier matcher.

#: The three spellings his own rows carry (2025-09-30 → 2026-09-28, all
#: -25.98): a space before the `*`, a space after `Google`, and no space.
YOUTUBE_SPELLINGS = (
    "GOOGLE *YouTubePremium",
    "Google *Youtubepremium",
    "Google*Youtubepremium",
)


def test_statement_spacing_variants_are_one_merchant_key() -> None:
    from app.services.subscriptions import normalize_payee_key

    assert {merchant_key(payee) for payee in YOUTUBE_SPELLINGS} == {"GOOGLE*YOUTUBEPREMIUM"}
    # ... and therefore ONE group key, which is the identity both suppression
    # sources and the Create-bill coverage check compare against.
    assert (
        len({make_group_key(payee, RecurringCadence.monthly) for payee in YOUTUBE_SPELLINGS}) == 1
    )

    # Whitespace goes BEFORE the trailing `#?digits` strip, so branch numbers
    # still collapse however the statement spaced them.
    assert merchant_key("GROCERY MART #12") == merchant_key("grocerymart 12") == "GROCERYMART"
    # Digits alone can never name a payee — the term-validation 422's input.
    assert normalize_payee_key("123") == "" and normalize_payee_key("#7") == ""
    # Whitespace is not identity; punctuation is still punctuation: no fuzzy
    # match crept in behind the spacing rule.
    assert merchant_key("A *B") != merchant_key("AB")
    # Idempotent — the property that lets a stored key be RE-READ through today's
    # normalizer without a data migration.
    for spelling in YOUTUBE_SPELLINGS:
        key = merchant_key(spelling)
        assert merchant_key(key) == key


def test_statement_spellings_are_one_candidate_group() -> None:
    days = _monthly_dates(6, _today())
    rows = [
        LedgerRow(
            id=index + 1,
            date=day,
            payee=YOUTUBE_SPELLINGS[index % len(YOUTUBE_SPELLINGS)],
            amount=Decimal("-25.98"),
        )
        for index, day in enumerate(days)
    ]
    groups = detect_recurring(rows)
    assert len(groups) == 1
    group = groups[0]
    assert group.merchant_key == "GOOGLE*YOUTUBEPREMIUM"
    assert group.group_key == "GOOGLE*YOUTUBEPREMIUM|monthly"
    assert group.occurrence_count == 6
    assert group.cadence is RecurringCadence.monthly
    assert group.payee == YOUTUBE_SPELLINGS[0]  # the label is the oldest row's spelling


def test_rekey_suppression_is_pure_and_idempotent() -> None:
    from app.services.recurring import rekey_suppression

    # An OLD-form stored key maps onto the key today's detector builds ...
    assert rekey_suppression("GOOGLE *YOUTUBEPREMIUM|monthly") == "GOOGLE*YOUTUBEPREMIUM|monthly"
    assert rekey_suppression("SP REITMANS|subscriptions") == "SPREITMANS|subscriptions"
    # ... a current-form key comes back untouched, which is why no row has to
    # be rewritten (and why UNIQUE(user_id, group_key) can never be tripped).
    for stored in (
        "GOOGLE *YOUTUBEPREMIUM|monthly",
        "GOOGLE*YOUTUBEPREMIUM|monthly",
        "SP REITMANS|subscriptions",
        "SPREITMANS|subscriptions",
    ):
        once = rekey_suppression(stored)
        assert rekey_suppression(once) == once
    # A key with no merchant/suffix split is copied verbatim, never guessed at.
    assert rekey_suppression("noseparator") == "noseparator"


def test_a_prompt_suppression_written_before_the_rekey_still_answers(
    client: TestClient, session: Session
) -> None:
    """A pre-M165 "no" must stay a "no" after the key format moves (docs/09)."""
    from app.models.recurring_suppressions import RecurringSuppression

    headers, user_id, account_id = _setup(client)
    _seed_group(session, user_id, account_id, "Google *Youtubepremium", "-25.98")
    session.add(RecurringSuppression(user_id=user_id, group_key="GOOGLE *YOUTUBEPREMIUM|monthly"))
    session.commit()

    body = client.get("/api/v1/transactions/recurring/candidates", headers=headers).json()
    assert body["groups"] == []
    assert body["suppressed_count"] == 1


def test_a_dashboard_suppression_written_before_the_rekey_still_answers(
    client: TestClient, session: Session
) -> None:
    from app.models.recurring_suppressions import RecurringSuppression

    headers, user_id, account_id = _setup(client)
    _seed_group(session, user_id, account_id, "SP REITMANS", "-331.83")
    session.add(RecurringSuppression(user_id=user_id, group_key="SP REITMANS|subscriptions"))
    session.commit()

    dashboard = client.get(
        "/api/v1/transactions/recurring/candidates?source=subscriptions", headers=headers
    ).json()
    assert dashboard["groups"] == []
    assert dashboard["suppressed_count"] == 1

    # The M96 prompt's own answer is untouched by that dashboard "no" — the
    # source boundary the M138 tests pin still holds across the rekey.
    prompt = client.get("/api/v1/transactions/recurring/candidates", headers=headers).json()
    assert len(prompt["groups"]) == 1
    assert prompt["groups"][0]["group_key"] == "SPREITMANS|monthly"

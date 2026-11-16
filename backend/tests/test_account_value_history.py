"""M118 — per-account value history (docs/08 §Account value history).

Every expected figure below is arithmetic done by hand from the seed, never
captured from a run:

    cash      = starting_balance + Σ committed transactions + Σ docs/05 trade
                cash effects, converted at the bucket end's cached rate
    positions = Σ signed quantity x newest price ≤ bucket end x listing rate
    value     = cash + positions

Seed (all on one TFSA, account row born 2024-12-01):

    2025-01-05  txn    +1,000.00  committed, category "Deposit", payee Wealthsimple
    2025-01-10  trade  +16,500.00 contribution (cash row)
    2025-01-15  trade  buy 1,000 @ 10.00            → cash -10,000, +1,000 shares
    2025-01-20  txn      -300.00  committed
    2025-01-25  txn   +99,999.00  PENDING batch     → invisible (docs/03)
    2025-02-20  trade    -200.00 contribution (a negative contribution is money
                                     leaving — real account 731 has one)
    prices: 2025-01-31 = 10.00 (exact for the Jan bucket),
            2025-03-31 = 30.00 (exact for the Mar bucket)
            → the FEB bucket falls back 28 days → `estimated: true`
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from conftest import latency_budget_blocker
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select
from test_tax_acb import _user, add_trade, make_account, make_security

from app.core.security import create_access_token
from app.models.accounts import Account, AccountType
from app.models.categories import Category
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import TradeAction as TA
from app.models.transactions import Transaction
from app.models.users import User

D = Decimal

RANGE = {"from": "2025-01-01", "to": "2025-03-31", "granularity": "month"}

# Hand-computed (see the module docstring):
#   Jan  cash 1,000 + 16,500 - 10,000 - 300 = 7,200  positions 1,000x10 = 10,000
#   Feb  cash 7,200 - 200                   = 7,000  positions 1,000x10 = 10,000
#   Mar  cash 7,000                          = 7,000  positions 1,000x30 = 30,000
EXPECTED_BUCKETS = [
    ("2025-01-31", D("7200.00"), D("10000.00"), D("17200.00"), False),
    ("2025-02-28", D("7000.00"), D("10000.00"), D("17000.00"), True),
    ("2025-03-31", D("7000.00"), D("30000.00"), D("37000.00"), False),
]


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _batch(
    session: Session, user: User, account: Account, status: ImportBatchStatus
) -> ImportBatch:
    batch = ImportBatch(
        user_id=user.id,
        account_id=account.id,
        institution="T",
        file_name="f.csv",
        status=status,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def _txn(
    session: Session,
    user: User,
    account: Account,
    batch: ImportBatch,
    day: str,
    amount: str,
    *,
    payee: str = "p",
    category_id: int | None = None,
) -> None:
    session.add(
        Transaction(
            user_id=user.id,
            account_id=account.id,
            date=date.fromisoformat(day),
            raw_description="r",
            normalized_payee=payee,
            amount_native=D(amount),
            currency=account.currency,
            cad_amount=None,
            import_batch_id=batch.id,
            category_id=category_id,
            dedup_hash=f"{account.id}-{day}-{amount}-{payee}",
        )
    )
    session.commit()


def _seed(session: Session) -> tuple[User, Account]:
    user = _user(session)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    # The endpoint clamps `from` to the account's own birth — nothing can
    # predate the account row (FK) — so pin it before the data.
    account.created_at = datetime(2024, 12, 1, tzinfo=UTC)
    session.add(account)
    session.commit()
    session.refresh(account)

    category = Category(user_id=user.id, name="Deposit")
    session.add(category)
    session.commit()
    session.refresh(category)

    committed = _batch(session, user, account, ImportBatchStatus.committed)
    pending = _batch(session, user, account, ImportBatchStatus.pending_review)

    _txn(
        session,
        user,
        account,
        committed,
        "2025-01-05",
        "1000.00",
        payee="Wealthsimple",
        category_id=category.id,
    )
    _txn(session, user, account, committed, "2025-01-20", "-300.00")
    _txn(session, user, account, pending, "2025-01-25", "99999.00")

    abc = make_security(session, user, "ABC", "CAD")
    add_trade(session, user, account, abc, TA.contribution, "2025-01-10", "0", "16500.00")
    add_trade(session, user, account, abc, TA.buy, "2025-01-15", "1000", "10.00")
    add_trade(session, user, account, abc, TA.contribution, "2025-02-20", "0", "-200.00")

    staged = add_trade(session, user, account, abc, TA.contribution, "2025-01-26", "0", "77777.00")
    staged.import_batch_id = pending.id
    session.add(staged)

    for day, price, source in (
        ("2025-01-31", "10.00", PriceSource.yfinance),
        ("2025-03-31", "30.00", PriceSource.manual),
    ):
        session.add(
            Price(
                user_id=user.id,
                security_id=abc.id,
                date=date.fromisoformat(day),
                price=D(price),
                source=source,
            )
        )
    session.commit()
    return user, account


def _get(client: TestClient, user: User, account: Account, **params: object):
    return client.get(
        f"/api/v1/accounts/{account.id}/value-history",
        params={**RANGE, **params},
        headers=_headers(user),
    )


def _rows(body: dict) -> list[tuple]:
    return [
        (
            bucket["bucket_end"],
            D(str(bucket["cash_cad"])),
            D(str(bucket["positions_cad"])),
            D(str(bucket["value_cad"])),
            bucket["estimated"],
        )
        for bucket in body["buckets"]
    ]


def test_series_hand_recomputed_at_three_probes(client: TestClient, session: Session) -> None:
    user, account = _seed(session)
    resp = _get(client, user, account)
    assert resp.status_code == 200
    body = resp.json()
    assert (body["from"], body["to"]) == ("2025-01-01", "2025-03-31")
    assert (body["granularity"], body["requested_granularity"]) == ("month", "month")
    assert body["currency"] == "CAD"
    assert body["native_currency"] == "CAD"
    assert _rows(body) == EXPECTED_BUCKETS


def test_staged_rows_never_move_the_series(client: TestClient, session: Session) -> None:
    """docs/03: the +99,999 transaction and the +77,777 trade both sit in a
    `pending_review` batch — neither cash nor a marker may see them."""
    user, account = _seed(session)
    body = _get(client, user, account).json()
    assert all(D(str(b["cash_cad"])) < D("50000") for b in body["buckets"])
    assert not any("77777" in str(e["amount_cad"]) for e in body["events"])


def test_events_respect_the_default_threshold(client: TestClient, session: Session) -> None:
    """$1,000 deposit + $20,000 contribution clear the default floor; the
    $300 and $200 outflows do not (window delta 19,800 → 10% = 1,980 > 1,000,
    so the relative leg does not lower the floor either)."""
    user, account = _seed(session)
    events = _get(client, user, account).json()["events"]
    assert [
        (e["date"], e["kind"], D(str(e["amount_cad"])), e["direction"], e["source"]) for e in events
    ] == [
        ("2025-01-05", "deposit", D("1000.00"), 1, "transaction"),
        ("2025-01-10", "contribution", D("16500.00"), 1, "trade"),
    ]
    deposit = events[0]
    assert deposit["payee"] == "Wealthsimple"
    assert deposit["category_name"] == "Deposit"
    assert deposit["transaction_id"] is not None and deposit["trade_id"] is None
    assert deposit["bucket_end"] == "2025-01-31"


def test_min_event_value_zero_returns_every_flow(client: TestClient, session: Session) -> None:
    user, account = _seed(session)
    events = _get(client, user, account, min_event_value=0).json()["events"]
    assert [(e["date"], e["kind"], D(str(e["amount_cad"]))) for e in events] == [
        ("2025-01-05", "deposit", D("1000.00")),
        ("2025-01-10", "contribution", D("16500.00")),
        ("2025-01-20", "withdrawal", D("-300.00")),
        ("2025-02-20", "withdrawal", D("-200.00")),
    ]
    # The negative contribution is money leaving — direction says so, and the
    # raw action is still on the wire for the tooltip's evidence line.
    negative = events[3]
    assert negative["direction"] == -1
    assert negative["action"] == "contribution"
    assert negative["trade_id"] is not None


def test_missing_price_gaps_the_line_instead_of_faking_a_zero(
    client: TestClient, session: Session
) -> None:
    """A holding with NO price row at all makes the total UNKNOWN: cash is
    still reported, `value_cad` is null and `valued` is false so the chart
    draws a visible break (docs/08 §Honesty)."""
    user = _user(session)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    account.created_at = datetime(2024, 12, 1, tzinfo=UTC)
    session.add(account)
    session.commit()
    session.refresh(account)
    batch = _batch(session, user, account, ImportBatchStatus.committed)
    _txn(session, user, account, batch, "2025-01-05", "2000.00")
    unpriced = make_security(session, user, "NOPRICE", "CAD")
    add_trade(session, user, account, unpriced, TA.buy, "2025-01-15", "10", "100.00")

    body = _get(client, user, account).json()
    assert body["buckets"], "the series still renders — it is the VALUE that is unknown"
    for bucket in body["buckets"]:
        assert bucket["valued"] is False
        assert bucket["value_cad"] is None
        assert bucket["positions_cad"] is None
        assert D(str(bucket["cash_cad"])) == D("1000.00")  # 2,000 - 1,000


def test_unresolved_fx_gaps_the_whole_bucket(client: TestClient, session: Session) -> None:
    """A USD account with no cached rate has no honest CAD figure at all —
    cash included — so every bucket is a gap, never a native number in a CAD
    chart."""
    user = _user(session)
    account = Account(user_id=user.id, name="USD Savings", type=AccountType.savings, currency="USD")
    account.created_at = datetime(2024, 12, 1, tzinfo=UTC)
    session.add(account)
    session.commit()
    session.refresh(account)
    batch = _batch(session, user, account, ImportBatchStatus.committed)
    _txn(session, user, account, batch, "2025-01-05", "500.00")

    body = _get(client, user, account).json()
    assert body["buckets"]
    for bucket in body["buckets"]:
        assert bucket["valued"] is False
        assert bucket["cash_cad"] is None
        assert bucket["value_cad"] is None


def test_brand_new_account_reports_no_data_for_the_empty_state(
    client: TestClient, session: Session
) -> None:
    """docs/08 §Empty state: nothing recorded yet → `has_data: false`, so the
    chart renders the three-part empty contract instead of a flat $0 line."""
    user = _user(session)
    account = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    account.created_at = datetime(2024, 12, 1, tzinfo=UTC)
    session.add(account)
    session.commit()
    session.refresh(account)

    body = _get(client, user, account).json()
    assert body["has_data"] is False
    assert body["buckets"], "the series still arrives — only the rendering is gated"
    assert all(D(str(b["value_cad"])) == D("0.00") for b in body["buckets"])

    account.starting_balance = D("250.00")
    session.add(account)
    session.commit()
    body = _get(client, user, account).json()
    assert body["has_data"] is True
    assert D(str(body["buckets"][-1]["value_cad"])) == D("250.00")


def test_imported_history_that_predates_created_at_is_kept(
    client: TestClient, session: Session
) -> None:
    """Real account 731 (anonymized): `created_at` = 2026-10-08 (the day the row
    was written) with trades back to 2025-11-13 — an import of years of history.
    Flooring the window at `created_at` ALONE would draw one point and delete
    the very timeline the chart exists for, so the floor is the earliest of
    the account row and its first committed row."""
    user, account = _seed(session)
    account.created_at = datetime(2026, 10, 8, tzinfo=UTC)
    session.add(account)
    session.commit()

    body = _get(client, user, account).json()
    assert body["from"] == "2025-01-05"  # first committed row, not created_at
    assert len(body["buckets"]) == 3  # the whole seeded range still renders
    assert _rows(body) == EXPECTED_BUCKETS
    assert [e["date"] for e in body["events"]] == ["2025-01-05", "2025-01-10"]


def test_granularity_coarsens_to_the_point_roofline(client: TestClient, session: Session) -> None:
    """day over ~7 years = ~730 points > 400 → weeks, with both granularities
    echoed (docs/08 §Point cap)."""
    user, account = _seed(session)
    body = _get(
        client, user, account, **{"from": "2020-01-01", "to": "2026-12-31", "granularity": "day"}
    ).json()
    assert body["requested_granularity"] == "day"
    assert body["granularity"] == "week"
    assert len(body["buckets"]) <= 400
    # `from` is clamped to the account's own birth — no pre-creation buckets.
    assert body["from"] == "2024-12-01"


def test_ancient_window_coarsens_to_year(client: TestClient, session: Session) -> None:
    user, account = _seed(session)
    account.created_at = datetime(1990, 1, 1, tzinfo=UTC)
    session.add(account)
    session.commit()
    body = _get(
        client, user, account, **{"from": "1990-01-01", "to": "2026-12-31", "granularity": "day"}
    ).json()
    assert (body["granularity"], body["requested_granularity"]) == ("year", "day")
    assert len(body["buckets"]) <= 400


def test_range_validation_and_cache_header(client: TestClient, session: Session) -> None:
    user, account = _seed(session)
    bad = _get(client, user, account, **{"from": "2025-06-01", "to": "2025-01-01"})
    assert bad.status_code == 422
    assert bad.json()["code"] == "invalid_date_range"

    ok = _get(client, user, account)
    assert ok.headers["cache-control"] == "private, max-age=60"


def test_tenancy_404_for_another_users_account(client: TestClient, session: Session) -> None:
    _owner, account = _seed(session)
    # A second tenant needs its own email — `_user` derives it from the session.
    stranger = User(email=f"stranger-{id(session)}@example.com", hashed_password="x")
    session.add(stranger)
    session.commit()
    session.refresh(stranger)
    resp = client.get(
        f"/api/v1/accounts/{account.id}/value-history", params=RANGE, headers=_headers(stranger)
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_perf_366_days_by_six_securities_under_1_5s(client: TestClient, session: Session) -> None:
    """The house first-paint cap (docs/08 §Performance): a full year of daily
    buckets across six holdings must come back inside 1.5s.

    The shape is docs/34's "what is NOT enforced": the shape claims — 366
    buckets, day granularity, 200 — are enforced on EVERY run, the measured
    milliseconds are printed, and the stopwatch assertion goes LAST, behind
    `latency_budget_blocker`. This pin is DB-bound (366 buckets over the shared
    Postgres), and AGENTS §5 lets two full suites share these 4 cores, so an
    unguarded clock here measures the box, not the query (t_4980b7ed).
    """
    user = _user(session)
    account = make_account(
        session, user, "Brokerage", AccountType.brokerage_non_registered, taxable=True
    )
    account.created_at = datetime(2023, 12, 1, tzinfo=UTC)
    session.add(account)
    session.commit()
    session.refresh(account)

    holdings = [make_security(session, user, f"P{index}", "CAD") for index in range(6)]
    for security in holdings:
        add_trade(session, user, account, security, TA.buy, "2024-01-02", "100", "10.00")

    prices: list[Price] = []
    day = date(2024, 1, 1)
    while day <= date(2024, 12, 31):  # 366 days — 2024 is a leap year
        for index, security in enumerate(holdings):
            prices.append(
                Price(
                    user_id=user.id,
                    security_id=security.id,
                    date=day,
                    price=D("10.00") + index,
                    source=PriceSource.yfinance,
                )
            )
        day = date.fromordinal(day.toordinal() + 1)
    session.add_all(prices)
    session.commit()
    held = session.exec(select(Security).where(Security.user_id == user.id)).all()
    assert len(held) == 6

    started = time.perf_counter()
    resp = client.get(
        f"/api/v1/accounts/{account.id}/value-history",
        params={"from": "2024-01-01", "to": "2024-12-31", "granularity": "day"},
        headers=_headers(user),
    )
    elapsed = time.perf_counter() - started
    assert resp.status_code == 200
    body = resp.json()
    assert body["granularity"] == "day"
    assert len(body["buckets"]) == 366
    print(f"\nPERF value-history={elapsed * 1000:.1f}ms buckets={len(body['buckets'])}")

    # --- the acceptance number, asked last ----------------------------------
    # Everything above (366 buckets, day granularity, the 200) is enforced on
    # every run, loaded or not; the clock is the one claim this box cannot
    # always answer (docs/34 "what is NOT enforced").
    blocker = latency_budget_blocker()
    if blocker is not None:
        pytest.skip(
            "1.5s value-history budget not measurable on this box: "
            f"{blocker}; measured {elapsed:.3f}s for 366 daily buckets at"
            f" {len(held)} securities (shape assertions above enforced"
            " regardless)"
        )
    assert elapsed < 1.5, f"value history took {elapsed:.3f}s (budget 1.5s)"

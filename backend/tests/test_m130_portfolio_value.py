"""M130 — the total-portfolio view (docs/08 §Portfolio value history,
§Portfolio summary; docs/09 §Portfolio header).

Every expected figure is hand-computed from the seed, never captured:

    holdings  = Σ rows of compute_positions  (the SAME call the table renders)
    cash      = starting_balance + Σ committed transactions + Σ docs/05 trade
                cash effects, at today's cached rate — the M118 chain's leg
    headline  = holdings + cash                       (server-added)
    day Δ     = Σ per-row cents(qty x (price - prior close) x rate)

Fixtures
--------
`_seed_history` — two investment accounts + one CHEQUING account carrying
999,999.00 of committed transactions (which must never reach the portfolio),
a window of three month buckets, and two contributions that are the markers:

    TFSA  AAA  contribution +5,000  buy 100 @ 10   prices 10 → 12 → 15
    RRSP  BBB  contribution +4,000  buy 200 @ 20   prices 20 → 21 → 22

    Jan  TFSA 4,000 cash + 1,000 positions = 5,000   RRSP 0 + 4,000 = 4,000
    Feb  TFSA 4,000 + 1,200 = 5,200                  RRSP 0 + 4,200 = 4,200
    Mar  TFSA 4,000 + 1,500 = 5,500                  RRSP 0 + 4,400 = 4,400
    port 9,000 → 9,400 → 9,900        stats +900.00 (+10.00%)

`_seed_day_change` — today-relative so the positions seam can be pinned:
prices at `today-4` and `today` with NOTHING between them, which is exactly
the Boxing-Day shape (the prior market day printed no close, so the baseline
is the last one printed — Friday's) — and the label carries that real date:

    AAA 100 sh  90 → 100    BBB 200 sh  18 → 20
    today 100x100 + 200x20 = 14,000.00
    prior 100x90  + 200x18 = 12,600.00
    day Δ = 1,400.00  (+11.11% of 12,600.00)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select
from test_tax_acb import _user, add_trade, make_account, make_security

from app.core.security import create_access_token
from app.models.accounts import Account, AccountType
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.prices import Price, PriceSource
from app.models.securities import Security
from app.models.trades import TradeAction as TA
from app.models.transactions import Transaction
from app.models.users import User
from app.services import account_value

D = Decimal

HISTORY_RANGE = {"from": "2025-01-01", "to": "2025-03-31", "granularity": "month"}

# Hand-computed (see the module docstring): cash 4,000.00 in every bucket.
EXPECTED_PORTFOLIO = [
    ("2025-01-31", D("4000.00"), D("5000.00"), D("9000.00")),
    ("2025-02-28", D("4000.00"), D("5400.00"), D("9400.00")),
    ("2025-03-31", D("4000.00"), D("5900.00"), D("9900.00")),
]


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _price(session: Session, user: User, security: Security, day: date, value: str) -> None:
    session.add(
        Price(
            user_id=user.id,
            security_id=security.id,
            date=day,
            price=D(value),
            source=PriceSource.manual,
        )
    )
    session.commit()


def _first_security(session: Session, user: User) -> Security:
    return session.exec(select(Security).where(Security.user_id == user.id)).first()


def _seed_history(session: Session) -> tuple[User, Account, Account, Account]:
    """Two investment accounts (the sum) + one chequing account (the control)."""
    user = _user(session)
    tfsa = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    rrsp = make_account(session, user, "RRSP", AccountType.rrsp, taxable=False)
    chequing = make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    for account in (tfsa, rrsp, chequing):
        account.created_at = datetime(2024, 12, 1, tzinfo=UTC)
    session.add_all([tfsa, rrsp, chequing])
    session.commit()

    # The control: committed cash in a NON-investment account. If the scope
    # ever widens, every portfolio figure below moves by this much.
    session.add(
        Transaction(
            user_id=user.id,
            account_id=chequing.id,
            date=date(2025, 1, 2),
            raw_description="r",
            normalized_payee="Payroll",
            amount_native=D("999999.00"),
            currency="CAD",
            cad_amount=D("999999.00"),
            dedup_hash="m130-chequing-payroll",
        )
    )
    session.commit()

    aaa = make_security(session, user, "AAA", "CAD")
    bbb = make_security(session, user, "BBB", "CAD")
    add_trade(session, user, tfsa, aaa, TA.contribution, "2025-01-05", "0", "5000.00")
    add_trade(session, user, tfsa, aaa, TA.buy, "2025-01-15", "100", "10.00")
    add_trade(session, user, rrsp, bbb, TA.contribution, "2025-01-05", "0", "4000.00")
    add_trade(session, user, rrsp, bbb, TA.buy, "2025-01-15", "200", "20.00")

    for day, aaa_price, bbb_price in (
        ("2025-01-31", "10", "20"),
        ("2025-02-28", "12", "21"),
        ("2025-03-31", "15", "22"),
    ):
        _price(session, user, aaa, date.fromisoformat(day), aaa_price)
        _price(session, user, bbb, date.fromisoformat(day), bbb_price)
    return user, tfsa, rrsp, chequing


def _seed_day_change(session: Session) -> tuple[User, Account, Account]:
    """Today-relative: two securities, a 4-day gap = the carry-forward shape."""
    user = _user(session)
    tfsa = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    rrsp = make_account(session, user, "RRSP", AccountType.rrsp, taxable=False)
    today = datetime.now(UTC).date()

    aaa = make_security(session, user, "AAA", "CAD")
    bbb = make_security(session, user, "BBB", "CAD")
    # Contribution == the buy in BOTH accounts, so each ends at exactly 0.00
    # cash and `portfolio_value_cad` must equal `total_value_cad` to the cent.
    add_trade(session, user, tfsa, aaa, TA.contribution, "2025-06-02", "0", "10000.00")
    add_trade(session, user, tfsa, aaa, TA.buy, "2025-06-02", "100", "100.00")
    add_trade(session, user, rrsp, bbb, TA.contribution, "2025-06-02", "0", "4000.00")
    add_trade(session, user, rrsp, bbb, TA.buy, "2025-06-02", "200", "20.00")

    prior = today - timedelta(days=4)  # no close printed between here and today
    _price(session, user, aaa, prior, "90")
    _price(session, user, aaa, today, "100")
    _price(session, user, bbb, prior, "18")
    _price(session, user, bbb, today, "20")
    return user, tfsa, rrsp


def _portfolio(client: TestClient, user: User, **params: object):
    return client.get(
        "/api/v1/portfolio/value-history",
        params={**HISTORY_RANGE, **params},
        headers=_headers(user),
    )


def _positions(client: TestClient, user: User):
    return client.get("/api/v1/portfolio/positions", headers=_headers(user))


def _rows(body: dict) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    return [
        (
            bucket["bucket_end"],
            D(str(bucket["cash_cad"])),
            D(str(bucket["positions_cad"])),
            D(str(bucket["value_cad"])),
        )
        for bucket in body["buckets"]
    ]


# --- P1 wire: the header's numbers, single-sourced ---------------------------


def test_positions_sigma_is_the_headers_holdings_and_headline_is_sigma_plus_cash(
    client: TestClient, session: Session
) -> None:
    """The anti-fabrication identity (docs/08 §Portfolio summary): the header's
    holdings figure IS the Σ of the rows the table rendered, and the headline
    is that Σ plus the server's cash — no second sum, no client arithmetic."""
    user, _tfsa, _rrsp = _seed_day_change(session)
    resp = _positions(client, user)
    assert resp.status_code == 200
    body = resp.json()

    sigma = sum(D(str(row["market_value_cad"])) for row in body["items"])
    assert sigma == D("14000.00")
    assert D(str(body["total_value_cad"])) == sigma
    # Contribution == buy in both accounts, so cash is exactly 0.00 and the
    # headline collapses onto the Σ — the acceptance's "Σ == header value".
    assert D(str(body["cash_cad"])) == D("0.00")
    assert D(str(body["portfolio_value_cad"])) == sigma
    assert len(body["items"]) == 2
    assert {row["symbol"] for row in body["items"]} == {"AAA", "BBB"}


def test_cash_line_is_the_m118_chain_and_moves_the_headline(
    client: TestClient, session: Session
) -> None:
    """An un-invested contribution is CASH, and only cash: it enters the
    headline and never the holdings Σ (docs/09 §Portfolio header)."""
    user, tfsa, _rrsp = _seed_day_change(session)
    aaa = _first_security(session, user)
    add_trade(session, user, tfsa, aaa, TA.contribution, "2025-06-03", "0", "500.00")

    body = _positions(client, user).json()
    assert D(str(body["cash_cad"])) == D("500.00")
    assert D(str(body["total_value_cad"])) == D("14000.00")
    assert D(str(body["portfolio_value_cad"])) == D("14500.00")
    assert D(str(body["portfolio_value_cad"])) == D(str(body["total_value_cad"])) + D(
        str(body["cash_cad"])
    )


def test_day_change_is_prior_close_sum_vs_today_sum_and_is_labeled(
    client: TestClient, session: Session
) -> None:
    """Hand-recompute (docs/09 §Day change): 14,000 - 12,600 = 1,400 on the Σ
    of per-row `day_change_cad`, and the baseline DATE is the carried-forward
    prior market day — not "yesterday"."""
    user, _tfsa, _rrsp = _seed_day_change(session)
    body = _positions(client, user).json()
    prior = datetime.now(UTC).date() - timedelta(days=4)

    today_sum = D(str(body["total_value_cad"]))
    assert today_sum == D("14000.00")

    per_row = [D(str(row["day_change_cad"])) for row in body["items"]]
    assert sorted(per_row) == [D("400.00"), D("1000.00")]
    assert sum(per_row) == D("1400.00")

    prior_sum = sum(
        (D(str(row["quantity"])) * D(str(row["previous_close_native"]))).quantize(D("0.01"))
        for row in body["items"]
    )
    assert prior_sum == D("12600.00")
    assert today_sum - prior_sum == D(str(body["total_day_change_cad"]))

    # The percentage is against YESTERDAY's level, not today's.
    assert body["total_day_change_pct"] == 11.11
    # The label names the real market day the baseline came from.
    assert body["day_change_as_of"] == prior.isoformat()
    assert body["day_change_mixed_baseline"] is False
    assert {row["previous_close_date"] for row in body["items"]} == {prior.isoformat()}


def test_day_change_flag_reports_a_mixed_baseline(client: TestClient, session: Session) -> None:
    """A security that last closed earlier than the rest is not silently folded
    into one market day: the Σ still adds every row (it must match the table),
    the LATEST baseline is named, and the flag says the dates disagree."""
    user, _tfsa, _rrsp = _seed_day_change(session)
    aaa = _first_security(session, user)
    today = datetime.now(UTC).date()
    older = today - timedelta(days=6)  # inside the ≤7d freshness window
    _price(session, user, aaa, older, "80")
    # Drop AAA's `today` row so its newest close is `today-4` while BBB's is
    # `today` — two different prior-market days inside ONE reported Δ.
    for row in session.exec(
        select(Price).where(Price.user_id == user.id, Price.security_id == aaa.id)
    ).all():
        if row.date == today:
            session.delete(row)
    session.commit()

    body = _positions(client, user).json()
    assert body["day_change_mixed_baseline"] is True
    assert body["day_change_as_of"] == (today - timedelta(days=4)).isoformat()
    assert {row["previous_close_date"] for row in body["items"]} == {
        (today - timedelta(days=6)).isoformat(),
        (today - timedelta(days=4)).isoformat(),
    }
    # AAA 100x(90-80)=1,000 and BBB 200x(20-18)=400 — both counted.
    assert D(str(body["total_day_change_cad"])) == sum(
        D(str(row["day_change_cad"])) for row in body["items"]
    )


# --- P1 chart: the aggregated series ----------------------------------------


def test_portfolio_series_is_the_sum_of_the_per_account_series(
    client: TestClient, session: Session
) -> None:
    """M118's walk, summed (docs/08 §Portfolio value history): every bucket of
    the portfolio equals the two account buckets added, and the chequing
    account's 999,999.00 never enters."""
    user, tfsa, rrsp, _chequing = _seed_history(session)
    body = _portfolio(client, user).json()

    assert _rows(body) == EXPECTED_PORTFOLIO

    per_account: list[dict] = []
    for account in (tfsa, rrsp):
        resp = client.get(
            f"/api/v1/accounts/{account.id}/value-history",
            params=HISTORY_RANGE,
            headers=_headers(user),
        )
        assert resp.status_code == 200
        per_account.append(resp.json())

    for bucket in body["buckets"]:
        end = bucket["bucket_end"]
        cash = D("0.00")
        positions = D("0.00")
        for other in per_account:
            match = next((b for b in other["buckets"] if b["bucket_end"] == end), None)
            if match is None:  # the account had not started yet → contributes 0
                continue
            cash += D(str(match["cash_cad"]))
            positions += D(str(match["positions_cad"]))
        assert D(str(bucket["cash_cad"])) == cash, end
        assert D(str(bucket["positions_cad"])) == positions, end

    assert body["account_ids"] == sorted([tfsa.id, rrsp.id])
    assert body["currency"] == "CAD"
    # The portfolio has no single native currency, so the chart's "values in
    # <cur>" badge must stay off rather than claim one account's currency.
    assert body["native_currency"] == body["currency"]


def test_range_stats_are_the_charts_first_and_last_point(
    client: TestClient, session: Session
) -> None:
    """The header's "over <range>" figure is `stats`, computed server-side from
    the SAME buckets the chart draws — the client never subtracts (docs/09)."""
    user, _tfsa, _rrsp, _chequing = _seed_history(session)
    body = _portfolio(client, user).json()
    stats = body["stats"]

    valued = [b for b in body["buckets"] if b["valued"] and b["value_cad"] is not None]
    first, last = valued[0], valued[-1]
    assert stats["from_bucket"] == first["bucket_end"]
    assert stats["to_bucket"] == last["bucket_end"]
    assert D(str(stats["from_value_cad"])) == D(str(first["value_cad"])) == D("9000.00")
    assert D(str(stats["to_value_cad"])) == D(str(last["value_cad"])) == D("9900.00")
    assert D(str(stats["change_cad"])) == D(str(last["value_cad"])) - D(str(first["value_cad"]))
    assert D(str(stats["change_cad"])) == D("900.00")
    assert stats["change_pct"] == 10.0


def test_markers_carry_their_account_and_the_chequing_row_is_absent(
    client: TestClient, session: Session
) -> None:
    """One marker per event id, each naming the account it came from (docs/08
    §Event markers); the control account's transaction is out of scope."""
    user, tfsa, rrsp, _chequing = _seed_history(session)
    events = _portfolio(client, user).json()["events"]

    assert len(events) == 2, [event["id"] for event in events]
    assert len({event["id"] for event in events}) == 2
    assert {tuple(event["account_ids"]) for event in events} == {(tfsa.id,), (rrsp.id,)}
    assert {event["account_names"][0] for event in events} == {"TFSA", "RRSP"}
    assert {D(str(event["amount_cad"])) for event in events} == {D("5000.00"), D("4000.00")}
    # No Payroll: the chequing account is not in the portfolio's scope.
    assert all(event["payee"] != "Payroll" for event in events)


def test_marker_dedup_one_event_spanning_two_accounts_renders_one_marker() -> None:
    """The dedup contract itself (docs/08 §Event markers): an id surfaced by two
    account series is ONE marker carrying BOTH accounts — the guarantee behind
    "one marker per event even if multiple accounts involved", and the reason
    the tooltip can name more than one account."""
    first = Account(id=1, user_id=1, name="TFSA", type=AccountType.tfsa, currency="CAD")
    second = Account(id=2, user_id=1, name="RRSP", type=AccountType.rrsp, currency="CAD")
    shared = {
        "id": "transaction:7",
        "source": "transaction",
        "trade_id": None,
        "transaction_id": 7,
        "date": date(2025, 1, 5),
        "bucket_end": date(2025, 1, 31),
        "kind": "deposit",
        "direction": 1,
        "amount_cad": D("1000.00"),
        "amount_native": D("1000.00"),
        "currency": "CAD",
        "payee": "Move",
        "category_name": None,
        "symbol": None,
        "security_id": None,
        "quantity": None,
        "action": None,
    }
    merged = account_value._merge_events(
        [first, second], [{"events": [dict(shared)]}, {"events": [dict(shared)]}]
    )
    assert len(merged) == 1
    assert merged[0]["id"] == "transaction:7"
    assert merged[0]["account_ids"] == [1, 2]
    assert merged[0]["account_names"] == ["TFSA", "RRSP"]


# --- scope, granularity, contract -------------------------------------------


def test_one_granularity_across_accounts_with_different_floors(
    client: TestClient, session: Session
) -> None:
    """Two accounts whose windows differ by six years resolve day differently
    (one coarsens, one does not); the SUMMED series must still have exactly one
    bucket size or the chart's x-axis is a lie."""
    user = _user(session)
    old = make_account(session, user, "Old", AccountType.tfsa, taxable=False)
    new = make_account(session, user, "New", AccountType.rrsp, taxable=False)
    old.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    new.created_at = datetime(2026, 1, 15, tzinfo=UTC)
    session.add_all([old, new])
    session.commit()

    body = _portfolio(
        client, user, **{"from": "2020-01-01", "to": "2026-12-31", "granularity": "day"}
    ).json()
    assert body["requested_granularity"] == "day"
    assert body["granularity"] == "week"
    ends = [bucket["bucket_end"] for bucket in body["buckets"]]
    assert ends == sorted(ends)
    assert len(ends) <= 400
    # The new account's series starts at its own floor, not at 2020 — the
    # buckets before it exist (the old account alone), not as a fabricated 0.
    assert ends[0] < "2026-01-15"
    assert any(end >= "2026-01-15" for end in ends)


def test_empty_state_when_the_user_has_no_investment_account(
    client: TestClient, session: Session
) -> None:
    """No investment account → the documented empty contract, never a flat $0
    line (docs/09 §Empty states)."""
    user = _user(session)
    make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    body = _portfolio(client, user).json()
    assert body["has_data"] is False
    assert body["account_ids"] == []
    assert body["buckets"] == []
    assert body["events"] == []
    assert body["stats"] == {
        "from_bucket": None,
        "to_bucket": None,
        "from_value_cad": None,
        "to_value_cad": None,
        "change_cad": None,
        "change_pct": None,
    }


def test_range_validation_and_cache_header(client: TestClient, session: Session) -> None:
    user, _tfsa, _rrsp, _chequing = _seed_history(session)
    bad = _portfolio(client, user, **{"from": "2025-06-01", "to": "2025-01-01"})
    assert bad.status_code == 422
    assert bad.json()["code"] == "invalid_date_range"

    ok = _portfolio(client, user)
    assert ok.status_code == 200
    assert ok.headers["cache-control"] == "private, max-age=60"


def test_staged_rows_never_move_the_series(client: TestClient, session: Session) -> None:
    """docs/03 staging model, at portfolio scope: a pending batch moves nothing."""
    user, tfsa, _rrsp, _chequing = _seed_history(session)
    aaa = _first_security(session, user)
    batch = ImportBatch(
        user_id=user.id,
        account_id=tfsa.id,
        institution="T",
        file_name="f.csv",
        status=ImportBatchStatus.pending_review,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    staged = add_trade(session, user, tfsa, aaa, TA.contribution, "2025-03-01", "0", "77777.00")
    staged.import_batch_id = batch.id
    session.add(staged)
    session.commit()

    body = _portfolio(client, user).json()
    assert _rows(body) == EXPECTED_PORTFOLIO


def test_f_m130_1_positions_header_and_history_last_bucket_drift_is_pennies_bounded(
    client: TestClient, session: Session
) -> None:
    """F-M130-1: the header (`/positions`) and the "over <range>" line
    (`/portfolio/value-history` stats) are two DELIBERATELY separate valuation
    passes — per-security cents then summed vs per-account cents then summed —
    so fractional-cent prices can put them a penny apart. `_cents` is
    banker's-rounded (ROUND_HALF_EVEN), so each row rounds UP here while the
    account-level raw sum rounds DOWN. Hand-computed:

        AAA 1 sh @ 10.006 → row cents 10.01     | raw 10.006
        BBB 1 sh @ 10.006 → row cents 10.01     | raw 10.006
        positions total = 20.02 (Σ per-row)     | walk = _cents(20.012) = 20.01

    Both legs are otherwise identical (same cash helper, same committed-only
    predicates), so the drift is EXACTLY the rounding-order penny and is
    bounded by 0.005 per position row — never dollars. Aligning the two
    orders is not an option: docs/08 pins "no second valuation pass anywhere"
    and docs/09 pins the range line to the chart's own first/last bucket, so
    the fix for the visible mismatch is documentation, not shared arithmetic.
    """
    user = _user(session)
    today = datetime.now(UTC).date()

    def _iso(day: date) -> str:
        return day.isoformat()

    tfsa = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    # Today-relative (same seam as _seed_day_change): prices dated TODAY stay
    # inside ensure_recent_price's freshness window, so the positions path
    # reads the seeded rows instead of fetching a live yfinance quote.
    tfsa.created_at = datetime.now(UTC) - timedelta(days=30)
    session.add(tfsa)
    session.commit()
    aaa = make_security(session, user, "AAA", "CAD")
    bbb = make_security(session, user, "BBB", "CAD")
    # Funded exactly: cash leg lands on 0.00 in BOTH passes.
    add_trade(
        session, user, tfsa, aaa, TA.contribution, _iso(today - timedelta(days=5)), "0", "20.00"
    )
    add_trade(session, user, tfsa, aaa, TA.buy, _iso(today - timedelta(days=4)), "1", "10.00")
    add_trade(session, user, tfsa, bbb, TA.buy, _iso(today - timedelta(days=4)), "1", "10.00")
    _price(session, user, aaa, today, "10.006")
    _price(session, user, bbb, today, "10.006")

    positions = _positions(client, user).json()
    history = client.get(
        f"/api/v1/portfolio/value-history?from={_iso(today)}&to={_iso(today)}&granularity=day",
        headers=_headers(user),
    ).json()

    header = D(str(positions["portfolio_value_cad"]))
    last_bucket = D(str(history["stats"]["to_value_cad"]))
    assert header == D("20.02")  # Σ per-security row cents
    assert last_bucket == D("20.01")  # per-account raw sum, one rounding
    assert header - last_bucket == D("0.01")  # the rounding-order penny

    # The contract the docs now state: |drift| ≤ 0.005 per position row
    # (the cash leg is shared, so it contributes nothing).
    rows = len(positions["items"])
    assert abs(header - last_bucket) <= D("0.005") * rows
    # And the cash leg is provably identical in both passes here.
    assert D(str(positions["cash_cad"])) == D("0.00")
    assert D(str(history["buckets"][-1]["cash_cad"])) == D("0.00")


def test_empty_positions_report_zero_holdings_and_a_withheld_day_change(
    client: TestClient, session: Session
) -> None:
    """No holdings → zero holdings (a true statement about an empty set) but a
    WITHHELD day change, never a zero the UI could print as a real move."""
    user = _user(session)
    make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    body = _positions(client, user).json()
    assert body["items"] == []
    assert D(str(body["total_value_cad"])) == D("0.00")
    assert body["total_day_change_cad"] is None
    assert body["total_day_change_pct"] is None
    assert body["day_change_as_of"] is None
    assert body["day_change_mixed_baseline"] is False
    # Cash of an untouched account is exactly zero, so the headline is 0.00
    # and the cash line (non-zero only) stays hidden.
    assert D(str(body["cash_cad"])) == D("0.00")
    assert D(str(body["portfolio_value_cad"])) == D("0.00")

"""Spending velocity: structural and behavioral tests."""

import hashlib
from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.fx_rates import FxRate, FxSource
from app.models.transactions import Transaction
from app.services.insights import compute_insights
from app.services.txn_filters import today_et
from tests.test_deep_common import auth, create_account, get_category, register, seed_transaction


def _today() -> date:
    """The clock, read at CALL time — never captured at import (t_435c2d14)."""
    return today_et()


def _as_currency(session: Session, account_id: int, currency: str) -> None:
    """Flip an account's currency in-session (see test_networth_snapshot).

    Creating a foreign account via the API triggers a real BoC fetch that
    writes the SHARED fx_rates cache, which would out-rank a seeded rate.
    """
    from app.models.accounts import Account

    account = session.get(Account, account_id)
    account.currency = currency
    session.add(account)
    session.commit()


def _prev_month(day: int, anchor: date | None = None) -> date:
    """A date `day`-ish in the month before the one containing `anchor`."""
    first = (anchor or _today()).replace(day=1)
    last = first - timedelta(days=1)
    return last.replace(day=min(day, 28))


def test_insights_zero_for_new_user(client: TestClient) -> None:
    """New user with no transactions gets zero spending."""
    body = register(client)
    headers = auth(body["access_token"])
    r = client.get("/api/v1/reports/insights", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert float(body["current_month_spending"]) == 0
    assert float(body["previous_month_spending"]) == 0
    assert body["month_over_month_pct"] == 0.0
    assert body["category_deltas"] == []


def test_insights_response_shape(client: TestClient) -> None:
    """Response has all required fields."""
    body = register(client)
    headers = auth(body["access_token"])
    r = client.get("/api/v1/reports/insights", headers=headers)
    body = r.json()
    for key in [
        "current_month_spending",
        "previous_month_spending",
        "month_over_month_pct",
        "daily_burn_rate",
        "projected_month_end",
        "days_elapsed",
        "days_in_month",
        "category_deltas",
    ]:
        assert key in body, f"Missing key: {key}"
    assert isinstance(body["category_deltas"], list)
    assert isinstance(body["days_elapsed"], int)
    assert isinstance(body["days_in_month"], int)
    assert 1 <= body["days_elapsed"] <= 31
    assert 28 <= body["days_in_month"] <= 31


def test_insights_with_previous_month_data(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """Insights show spending when seeded transactions exist."""
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    acct = create_account(client, headers)

    # Seed a transaction in the current month (frozen — the current-month
    # window is empty on the 1st, see the `frozen_today` fixture).
    seed_transaction(
        session, user_id, acct, "Coffee Shop", amount="-45.00", day=frozen_today.replace(day=1)
    )

    r = client.get("/api/v1/reports/insights", headers=headers)
    assert r.status_code == 200
    body = r.json()
    # At least some spending should be detected
    assert float(body["current_month_spending"]) > 0
    assert float(body["daily_burn_rate"]) > 0
    assert body["projected_month_end"] is not None


def test_insights_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/reports/insights")
    assert r.status_code == 401


# ------------------------------------------- signed CAD spending regressions


def _tx(
    session: Session,
    user_id: int,
    account_id: int,
    payee: str,
    amount: str,
    day: date,
    category_id: int | None = None,
    currency: str = "CAD",
    cad_amount: str | None = None,
) -> None:
    session.add(
        Transaction(
            user_id=user_id,
            account_id=account_id,
            date=day,
            posting_date=day,
            raw_description=payee,
            normalized_payee=payee,
            amount_native=Decimal(amount),
            currency=currency,
            cad_amount=Decimal(cad_amount) if cad_amount is not None else None,
            dedup_hash=hashlib.sha256(f"{payee}-{day}-{amount}-{account_id}".encode()).hexdigest(),
            category_id=category_id,
        )
    )
    session.commit()


def _register_with_account(
    client: TestClient,
) -> tuple[dict, int, int]:
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    acct = create_account(client, headers)
    return headers, user_id, acct


def test_insights_refund_reduces_spending_and_never_goes_negative(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """A refund offsets spending; net spend floors at zero.

    Regression: insights summed `abs(amount_native)`, so a +50.00 return
    counted as 50.00 of SPENDING on top of the -100.00 purchase.
    """
    _, user_id, acct = _register_with_account(client)
    cat_id = get_category(session, user_id, "groceries")

    _tx(
        session,
        user_id,
        acct,
        "Shop",
        "-100.00",
        frozen_today.replace(day=2),
        cat_id,
        cad_amount="-100.00",
    )
    _tx(
        session,
        user_id,
        acct,
        "Refund",
        "40.00",
        frozen_today.replace(day=3),
        cat_id,
        cad_amount="40.00",
    )

    body = compute_insights(session, user_id)
    assert body["current_month_spending"] == Decimal("60.00")


def test_insights_income_rows_are_not_spending(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """Payroll/interest booked into a category never inflates the burn rate."""
    _, user_id, acct = _register_with_account(client)
    cat_id = get_category(session, user_id, "salary")

    _tx(
        session,
        user_id,
        acct,
        "PAYROLL",
        "5000.00",
        frozen_today.replace(day=1),
        cat_id,
        cad_amount="5000.00",
    )
    _tx(
        session,
        user_id,
        acct,
        "Interest",
        "12.00",
        frozen_today.replace(day=2),
        cat_id,
        cad_amount="12.00",
    )

    body = compute_insights(session, user_id)
    assert body["current_month_spending"] == Decimal("0.00")
    assert body["daily_burn_rate"] == Decimal("0.00")
    assert body["projected_month_end"] == Decimal("0.00")


def test_insights_category_delta_refund_is_signed(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """Per-category deltas are signed too — the refund shows as a decrease."""
    _, user_id, acct = _register_with_account(client)
    cat_id = get_category(session, user_id, "dining")
    prev_month_day = _prev_month(2, frozen_today)

    _tx(
        session,
        user_id,
        acct,
        "Old Dinner",
        "-200.00",
        prev_month_day,
        cat_id,
        cad_amount="-200.00",
    )
    _tx(
        session,
        user_id,
        acct,
        "New Dinner",
        "-50.00",
        frozen_today.replace(day=2),
        cat_id,
        cad_amount="-50.00",
    )
    _tx(
        session,
        user_id,
        acct,
        "Dinner Refund",
        "20.00",
        frozen_today.replace(day=3),
        cat_id,
        cad_amount="20.00",
    )

    body = compute_insights(session, user_id)
    delta = next(d for d in body["category_deltas"] if d["category_id"] == cat_id)
    assert delta["current"] == Decimal("30.00")  # 50 - 20, not 70
    assert delta["previous"] == Decimal("200.00")
    assert delta["change_pct"] == -85.0


def test_category_deltas_reconcile_with_the_headline_total(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """Per-category spend must sum to the headline — no premature clamp.

    Regression: `_category_sums` clamped each category at zero BEFORE the
    un-mirrored (fallback) rows were accumulated, so a refund booked to one
    category was discarded by that clamp. Two categories could then sum to MORE
    than the reported total — a breakdown that contradicts the number above it.

    The cross-category case is what the existing tests miss: they always put a
    refund in the SAME category as the purchase, which a per-category clamp
    happens to handle.
    """
    _, user_id, acct = _register_with_account(client)
    shopping = get_category(session, user_id, "groceries")
    dining = get_category(session, user_id, "dining")

    # Dining: a purchase and a LARGER refund, both CAD-mirrored → nets to a credit.
    _tx(
        session,
        user_id,
        acct,
        "Dinner",
        "-50.00",
        frozen_today.replace(day=4),
        dining,
        cad_amount="-50.00",
    )
    _tx(
        session,
        user_id,
        acct,
        "Dinner Refund",
        "70.00",
        frozen_today.replace(day=5),
        dining,
        cad_amount="70.00",
    )
    # Groceries: a purchase with NO cad mirror → takes the fallback path.
    _tx(session, user_id, acct, "Market", "-40.00", frozen_today.replace(day=6), shopping)

    body = compute_insights(session, user_id)

    deltas = {d["category_id"]: Decimal(str(d["current"])) for d in body["category_deltas"]}
    # The breakdown must add up to the headline. A category that nets to a
    # credit reports NEGATIVE (that is a credit, not spending) — the headline is
    # `max(Σ, 0)`, so summing the parts gives the total or the floor.
    assert sum(deltas.values()) == max(body["current_month_spending"], Decimal("0")), body
    # Dining nets to a CREDIT (-50 spend + 70 refund), so it reports negative
    # spend. That is the point: the two parts (40 + -20) reconcile to the 20.00
    # headline, which is what the old per-category clamp destroyed.
    assert deltas.get(dining, Decimal("0")) == Decimal("-20.00")
    assert deltas.get(shopping, Decimal("0")) == Decimal("40.00")


def test_insights_spending_uses_cad_mirror_not_native(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """USD spend converts to CAD before the burn rate is computed.

    Regression: the aggregate used `amount_native`, so a -100.00 USD charge at
    1.35 counted as 100.00 CAD.
    """
    _, user_id, acct = _register_with_account(client)

    _as_currency(session, acct, "USD")
    _tx(
        session,
        user_id,
        acct,
        "US STORE",
        "-100.00",
        frozen_today.replace(day=2),
        None,
        currency="USD",
        cad_amount="-135.00",
    )

    body = compute_insights(session, user_id)
    assert body["current_month_spending"] == Decimal("135.00")


def test_insights_multi_currency_month_sums_in_cad(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """CAD + USD spend aggregates in CAD, never by adding native amounts."""
    headers, user_id, cad_acct = _register_with_account(client)
    usd_acct = create_account(client, headers, "USD Card")
    _as_currency(session, usd_acct, "USD")

    _tx(
        session,
        user_id,
        cad_acct,
        "Metro",
        "-40.00",
        frozen_today.replace(day=2),
        None,
        cad_amount="-40.00",
    )
    _tx(
        session,
        user_id,
        usd_acct,
        "US MARKET",
        "-20.00",
        frozen_today.replace(day=3),
        None,
        currency="USD",
        cad_amount="-27.00",
    )

    body = compute_insights(session, user_id)
    assert body["current_month_spending"] == Decimal("67.00")


def test_insights_falls_back_to_newest_cached_rate_when_mirror_missing(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """A NULL CAD mirror converts at the newest cached rate on/before the date."""
    _, user_id, acct = _register_with_account(client)
    day = frozen_today.replace(day=5)

    session.add(
        FxRate(
            base="USD",
            quote="CAD",
            rate=Decimal("1.20"),
            date=day,
            source=FxSource.manual,
        )
    )
    session.commit()

    _as_currency(session, acct, "USD")
    _tx(session, user_id, acct, "US STORE", "-100.00", day, None, currency="USD", cad_amount=None)

    body = compute_insights(session, user_id)
    assert body["current_month_spending"] == Decimal("120.00")


def test_insights_unresolved_fx_row_is_excluded(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """A foreign row with no resolvable rate is EXCLUDED, not counted at face value."""
    _, user_id, acct = _register_with_account(client)

    _tx(
        session,
        user_id,
        acct,
        "Known",
        "-50.00",
        frozen_today.replace(day=2),
        None,
        cad_amount="-50.00",
    )
    _as_currency(session, acct, "USD")
    _tx(
        session,
        user_id,
        acct,
        "Unresolved",
        "-999.00",
        frozen_today.replace(day=3),
        None,
        currency="USD",
        cad_amount=None,
    )

    body = compute_insights(session, user_id)
    assert body["current_month_spending"] == Decimal("50.00")


# ------------------------------------------- M138 D-1: the $0 headline (M133 P1)


def test_insights_reports_real_spending_when_income_exceeds_spending(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """M138 (D-1 — M133's 2nd P1): income > spending must never print $0.

    Regression: `db_exec_sum` netted EVERY row in the window — payroll
    included — and floored at zero, so a month that spent $183.47 rendered
    `THIS MONTH $0 / 0% / $0/day / projected $0` on the dashboard while the
    same response's `category_deltas` and the same page's budget banner both
    carried the real number.
    """
    _, user_id, acct = _register_with_account(client)
    groceries = get_category(session, user_id, "groceries")
    salary = get_category(session, user_id, "salary")

    _tx(
        session,
        user_id,
        acct,
        "PAYROLL",
        "5000.00",
        frozen_today.replace(day=1),
        salary,
        cad_amount="5000.00",
    )
    # An UNCATEGORIZED inflow too — the shape a real statement import lands in.
    _tx(
        session,
        user_id,
        acct,
        "INTEREST",
        "4.00",
        frozen_today.replace(day=2),
        None,
        cad_amount="4.00",
    )
    _tx(
        session,
        user_id,
        acct,
        "Groceries",
        "-183.47",
        frozen_today.replace(day=3),
        groceries,
        cad_amount="-183.47",
    )

    body = compute_insights(session, user_id)
    assert body["current_month_spending"] == Decimal("183.47")
    assert body["daily_burn_rate"] > 0
    assert body["projected_month_end"] > 0

    deltas = {d["category_id"]: Decimal(str(d["current"])) for d in body["category_deltas"]}
    # Income is not a "biggest change" in spending — it never enters the list.
    assert salary not in deltas
    assert deltas[groceries] == Decimal("183.47")
    # ...and the parts still reconcile with the headline (docs/12).
    assert sum(deltas.values()) == body["current_month_spending"]


def test_insights_income_only_month_still_reports_zero(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """A month with ONLY inflows spends $0 — "$0" means nothing went out."""
    _, user_id, acct = _register_with_account(client)
    salary = get_category(session, user_id, "salary")
    _tx(
        session,
        user_id,
        acct,
        "PAYROLL",
        "5000.00",
        frozen_today.replace(day=1),
        salary,
        cad_amount="5000.00",
    )

    body = compute_insights(session, user_id)
    assert body["current_month_spending"] == Decimal("0.00")
    assert body["category_deltas"] == []

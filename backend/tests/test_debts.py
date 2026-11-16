"""Debt tracking and payoff projections."""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.test_deep_common import auth, create_account, register


def _setup(client: TestClient, session: Session) -> tuple[dict, int]:
    body = register(client)
    headers = auth(body["access_token"])
    acct = create_account(client, headers, "Mortgage")
    return headers, acct


def test_create_and_list_debts(client: TestClient, session: Session) -> None:
    headers, acct = _setup(client, session)
    r = client.post(
        "/api/v1/debts",
        headers=headers,
        json={
            "name": "Mortgage",
            "account_id": acct,
            "debt_type": "mortgage",
            "interest_rate": 4.5,
            "minimum_payment": 2000,
        },
    )
    assert r.status_code == 201
    assert r.json()["name"] == "Mortgage"

    r = client.get("/api/v1/debts", headers=headers)
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_debt_projection(client: TestClient, session: Session) -> None:
    headers, acct = _setup(client, session)
    client.post(
        "/api/v1/debts",
        headers=headers,
        json={
            "name": "Loan",
            "account_id": acct,
            "debt_type": "loan",
            "interest_rate": 5.0,
            "minimum_payment": 500,
        },
    )
    r = client.get("/api/v1/debts/projection", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "total_debt" in body
    assert "total_interest" in body
    assert len(body["debts"]) == 1


def test_debt_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/debts")
    assert r.status_code == 401


# ---------------------------------------- payoff-projection regressions


def test_zero_rate_non_divisible_balance_pays_off_with_ceiling(
    client: TestClient, session: Session
) -> None:
    """0% + balance not divisible by payment → months = ceil, total_paid = the
    final (smaller) payment, NOT `payment x months`.

    Regression: the old zero-rate branch reported total_paid as
    payment x months (e.g. 13 x $1,000 = $13,000 for a $12,345.67 balance),
    overstating cash out by the overpayment; it also mismatched the
    interest-bearing branch, which reports balance + interest.
    """
    from app.api.debts import _payoff_projection

    months, interest, paid = _payoff_projection(
        Decimal("12345.67"), Decimal("0"), Decimal("1000.00")
    ).values()
    assert months == 13
    assert interest == "0"
    # 12 full payments of 1000 + a final 345.67
    assert paid == "12345.67"


def test_zero_rate_exact_divisor_reports_exact_totals() -> None:
    """0% and an exactly divisible balance: months exact, no phantom overpayment."""
    from app.api.debts import _payoff_projection

    proj = _payoff_projection(Decimal("1000.00"), Decimal("0"), Decimal("250.00"))
    assert proj["months"] == 4
    assert proj["total_interest"] == "0"
    assert proj["total_paid"] == "1000.00"


def test_zero_rate_single_payment_clears_exact_balance() -> None:
    from app.api.debts import _payoff_projection

    proj = _payoff_projection(Decimal("100.00"), Decimal("0"), Decimal("100.00"))
    assert proj["months"] == 1
    assert proj["total_paid"] == "100.00"


def test_biweekly_frequency_uses_26_periods_per_year(client: TestClient, session: Session) -> None:
    """`payment_frequency=biweekly` pays 26x/yr, not 12x/yr.

    Regression: every debt was amortized as monthly regardless of frequency, so
    a biweekly plan projected double the time and roughly double the interest.
    """
    headers, acct = _setup(client, session)
    client.post(
        "/api/v1/debts",
        headers=headers,
        json={
            "name": "Biweekly Loan",
            "account_id": acct,
            "debt_type": "loan",
            "original_amount": 1000,
            "interest_rate": 5.0,
            "minimum_payment": 300,
            "payment_frequency": "biweekly",
        },
    )
    body = client.get("/api/v1/debts/projection", headers=headers).json()
    row = body["debts"][0]
    # 26 payments/yr → 4 payments clears $1,000 @5% (≈2 months), not 4 months.
    assert row["months"] == 2
    assert float(row["total_interest"]) == 4.25


def test_monthly_frequency_unchanged_by_biweekly_fix(client: TestClient, session: Session) -> None:
    """The monthly path keeps its documented 12-period amortization."""
    headers, acct = _setup(client, session)
    client.post(
        "/api/v1/debts",
        headers=headers,
        json={
            "name": "Monthly Loan",
            "account_id": acct,
            "debt_type": "loan",
            "original_amount": 1000,
            "interest_rate": 5.0,
            "minimum_payment": 300,
            "payment_frequency": "monthly",
        },
    )
    row = client.get("/api/v1/debts/projection", headers=headers).json()["debts"][0]
    assert row["months"] == 4
    assert float(row["total_interest"]) == 9.25


def test_biweekly_projection_reports_infinite_when_payment_below_interest() -> None:
    """A biweekly payment that never covers the interest still returns -1.

    At 24% the biweekly period rate is 0.923%; the first interest charge on
    $1,000 is $9.23, so a $5 payment never retires any principal.
    """
    from app.api.debts import _payoff_projection

    proj = _payoff_projection(
        Decimal("1000.00"), Decimal("24"), Decimal("5.00"), periods_per_year=26
    )
    assert proj["months"] == -1
    assert proj["total_interest"] == "infinite"
    assert proj["total_paid"] == "infinite"


def test_monthly_projection_reports_infinite_when_payment_below_interest() -> None:
    """The pre-existing monthly infinite case is unchanged."""
    from app.api.debts import _payoff_projection

    proj = _payoff_projection(Decimal("1000.00"), Decimal("24"), Decimal("5.00"))
    assert proj["months"] == -1
    assert proj["total_interest"] == "infinite"


def test_projection_reports_payment_frequency_used(client: TestClient, session: Session) -> None:
    """The projection echoes the frequency it amortized with."""
    headers, acct = _setup(client, session)
    client.post(
        "/api/v1/debts",
        headers=headers,
        json={
            "name": "Biweekly Loan",
            "account_id": acct,
            "debt_type": "loan",
            "original_amount": 1000,
            "interest_rate": 5.0,
            "minimum_payment": 300,
            "payment_frequency": "biweekly",
        },
    )
    row = client.get("/api/v1/debts/projection", headers=headers).json()["debts"][0]
    assert row["payment_frequency"] == "biweekly"
    assert row["periods_per_year"] == 26


def test_long_biweekly_mortgage_does_not_report_infinite() -> None:
    """A 30-year biweekly plan amortizes instead of hitting the loop guard.

    Regression: the loop guard was a flat 600 periods, which is only ~23 years
    of biweekly payments — a 30-year biweekly mortgage (780 payments) was
    reported as `months=-1, total_interest="infinite"`.
    """
    from app.api.debts import _payoff_projection

    # $500,000 @ 5% amortized over 30 years with a biweekly payment.
    proj = _payoff_projection(Decimal("500000.00"), Decimal("5"), Decimal("1238.22"), 26)
    assert proj["months"] == 361  # ~30 years, not "never"
    assert proj["total_interest"] != "infinite"
    assert float(proj["total_paid"]) > 500000.00


def test_underpaying_loan_beyond_horizon_reports_never_amortizes() -> None:
    """A payment too small to clear the balance inside the horizon says so."""
    from app.api.debts import _payoff_projection

    # 0% balance needing 700 monthly payments (> the 50-year horizon).
    proj = _payoff_projection(Decimal("700.00"), Decimal("0"), Decimal("1.00"))
    assert proj["months"] == -1
    assert proj["total_interest"] == "infinite"


# --------------------------------------------------- negative-input rejections


def test_debt_create_rejects_negative_minimum_payment(client: TestClient, session: Session) -> None:
    """A negative payment is rejected 422, not silently stored.

    Regression: `minimum_payment` had no lower bound, so -500 was accepted and
    the projection returned a nonsense $0 month / $0 interest.
    """
    headers, acct = _setup(client, session)
    r = client.post(
        "/api/v1/debts",
        headers=headers,
        json={
            "name": "Bad Debt",
            "account_id": acct,
            "debt_type": "loan",
            "interest_rate": 5.0,
            "minimum_payment": -500,
        },
    )
    assert r.status_code == 422


def test_debt_create_rejects_negative_original_amount(client: TestClient, session: Session) -> None:
    headers, acct = _setup(client, session)
    r = client.post(
        "/api/v1/debts",
        headers=headers,
        json={
            "name": "Bad Debt",
            "account_id": acct,
            "debt_type": "loan",
            "original_amount": -500,
        },
    )
    assert r.status_code == 422


def test_debt_update_rejects_negative_minimum_payment(client: TestClient, session: Session) -> None:
    """PATCH mirrors the create bounds (docs/29 F7)."""
    headers, acct = _setup(client, session)
    debt_id = client.post(
        "/api/v1/debts",
        headers=headers,
        json={
            "name": "Loan",
            "account_id": acct,
            "debt_type": "loan",
            "original_amount": 1000,
            "interest_rate": 5.0,
            "minimum_payment": 300,
        },
    ).json()["id"]

    r = client.patch(f"/api/v1/debts/{debt_id}", headers=headers, json={"minimum_payment": -1})
    assert r.status_code == 422

    r = client.patch(f"/api/v1/debts/{debt_id}", headers=headers, json={"original_amount": -1})
    assert r.status_code == 422

    r = client.patch(
        f"/api/v1/debts/{debt_id}", headers=headers, json={"payment_frequency": "weekly"}
    )
    assert r.status_code == 422


def test_debt_create_rejects_unknown_payment_frequency(
    client: TestClient, session: Session
) -> None:
    """Only the two documented frequencies are accepted (docs/29 schema)."""
    headers, acct = _setup(client, session)
    r = client.post(
        "/api/v1/debts",
        headers=headers,
        json={
            "name": "Weekly Debt",
            "account_id": acct,
            "debt_type": "loan",
            "payment_frequency": "weekly",
        },
    )
    assert r.status_code == 422

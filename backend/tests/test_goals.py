"""Goals: CRUD + progress computation with account linking."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.test_deep_common import auth, create_account, register, seed_transaction


def test_goal_progress_from_override(client: TestClient) -> None:
    """Progress computed from current_amount_override."""
    body = register(client)
    headers = auth(body["access_token"])

    r = client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "name": "Emergency Fund",
            "target_amount": 10000,
            "current_amount_override": 5000,
        },
    )
    assert r.status_code == 201

    r = client.get("/api/v1/goals", headers=headers)
    goal = r.json()[0]
    assert float(goal["current_amount"]) == 5000
    assert goal["progress_pct"] == 50.0
    assert goal["status"] == "on_track"


def test_goal_completed_at_100_pct(client: TestClient) -> None:
    """Goal marked completed when progress >= 100%."""
    body = register(client)
    headers = auth(body["access_token"])

    client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "name": "Full Goal",
            "target_amount": 5000,
            "current_amount_override": 5000,
        },
    )

    r = client.get("/api/v1/goals", headers=headers)
    assert r.json()[0]["status"] == "completed"
    assert r.json()[0]["progress_pct"] == 100.0


def test_goal_monthly_needed_computation(client: TestClient) -> None:
    """Monthly needed computed from remaining amount and months to target."""
    body = register(client)
    headers = auth(body["access_token"])

    # Target in 10 months, need 10000, have 0 -> 1000/month. The date is
    # ANCHORED to the clock (the 1st of the month 10 months out): a hard-coded
    # literal drifts toward the present as the calendar rolls, so months_left
    # fell 10 -> 8 on 2026-11-01 and 10000/8 = 1250 blew the band below.
    today = date.today()
    months = today.year * 12 + today.month + 10
    client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "name": "Car Fund",
            "target_amount": 10000,
            "target_date": date(months // 12, months % 12 + 1, 1).isoformat(),
            "current_amount_override": 0,
        },
    )

    r = client.get("/api/v1/goals", headers=headers)
    goal = r.json()[0]
    assert goal["monthly_needed"] is not None
    monthly = float(goal["monthly_needed"])
    assert 800 <= monthly <= 1200  # rough check: ~1000/month


def test_goal_update_and_delete(client: TestClient) -> None:
    """Update fields and delete goal."""
    body = register(client)
    headers = auth(body["access_token"])

    r = client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "name": "Vacation",
            "target_amount": 3000,
        },
    )
    gid = r.json()["id"]

    r = client.patch(
        f"/api/v1/goals/{gid}", headers=headers, json={"name": "Europe Trip", "target_amount": 5000}
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Europe Trip"
    assert float(r.json()["target_amount"]) == 5000

    r = client.delete(f"/api/v1/goals/{gid}", headers=headers)
    assert r.status_code == 204

    r = client.get("/api/v1/goals", headers=headers)
    assert len(r.json()) == 0


def test_goal_foreign_404(client: TestClient) -> None:
    body = register(client)
    headers = auth(body["access_token"])
    r = client.patch("/api/v1/goals/99999", headers=headers, json={"name": "X"})
    assert r.status_code == 404


# ------------------------------------------------- starting-balance regression


def test_goal_progress_includes_account_starting_balance(
    client: TestClient, session: Session
) -> None:
    """An account-linked goal counts `starting_balance` as saved.

    Regression: progress summed transactions only, so an account opened with
    a $5,000 starting balance and no activity read as $0 saved.
    """
    from app.models.accounts import Account

    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    acct = create_account(client, headers, "Emergency Fund")

    account = session.get(Account, acct)
    account.starting_balance = Decimal("5000.00")
    session.add(account)
    session.commit()

    seed_transaction(session, user_id, acct, "Top-up", "1000.00", day=date(2026, 9, 1))

    r = client.post(
        "/api/v1/goals",
        headers=headers,
        json={"name": "Emergency Fund", "target_amount": 10000, "account_id": acct},
    )
    assert r.status_code == 201

    goal = client.get("/api/v1/goals", headers=headers).json()[0]
    assert float(goal["current_amount"]) == 6000.00  # 5000 starting + 1000 deposited
    assert goal["progress_pct"] == 60.0


def test_goal_progress_zero_when_account_has_no_activity_or_starting_balance(
    client: TestClient, session: Session
) -> None:
    """A freshly created, empty account still reads 0 — not a phantom balance."""
    body = register(client)
    headers = auth(body["access_token"])
    acct = create_account(client, headers, "Fresh")

    client.post(
        "/api/v1/goals",
        headers=headers,
        json={"name": "Vacation", "target_amount": 3000, "account_id": acct},
    )

    goal = client.get("/api/v1/goals", headers=headers).json()[0]
    assert float(goal["current_amount"]) == 0.00
    assert goal["progress_pct"] == 0.0

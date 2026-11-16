"""Bill reminders CRUD and mark-paid."""

from fastapi.testclient import TestClient


def _auth(client: TestClient) -> dict:
    from datetime import UTC, datetime

    email = f"bill-{datetime.now(UTC).timestamp()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    r = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_create_and_list_bills(client: TestClient) -> None:
    headers = _auth(client)
    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={
            "name": "Rent",
            "amount": 1500,
            "frequency": "monthly",
            "next_due_date": "2026-10-01",
        },
    )
    assert r.status_code == 201
    assert r.json()["name"] == "Rent"

    r = client.get("/api/v1/bills", headers=headers)
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["name"] == "Rent"


def test_mark_paid_advances_date(client: TestClient) -> None:
    headers = _auth(client)
    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={
            "name": "Internet",
            "amount": 80,
            "frequency": "monthly",
            "next_due_date": "2026-09-15",
        },
    )
    bill_id = r.json()["id"]

    r = client.post(f"/api/v1/bills/{bill_id}/mark-paid", headers=headers)
    assert r.status_code == 200
    assert r.json()["next_due_date"] == "2026-10-15"


def test_bill_delete(client: TestClient) -> None:
    headers = _auth(client)
    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={
            "name": "Test",
            "frequency": "monthly",
            "next_due_date": "2026-10-01",
        },
    )
    r = client.delete(f"/api/v1/bills/{r.json()['id']}", headers=headers)
    assert r.status_code == 204


def test_bills_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/bills")
    assert r.status_code == 401

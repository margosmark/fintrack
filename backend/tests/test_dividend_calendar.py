"""Dividend calendar endpoint."""

from fastapi.testclient import TestClient


def _auth(client: TestClient) -> dict[str, str]:
    from datetime import UTC, datetime

    email = f"divcal-{datetime.now(UTC).timestamp()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    r = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_dividend_calendar_empty_for_new_user(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get("/api/v1/reports/dividend-calendar", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["upcoming"] == []
    assert body["summary"] == {
        "this_month": None,
        "this_quarter": None,
        "this_year": None,
        "by_currency": [],
        "unresolved_counts": {"this_month": 0, "this_quarter": 0, "this_year": 0},
    }


def test_dividend_calendar_response_shape(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get("/api/v1/reports/dividend-calendar", headers=headers)
    body = r.json()
    assert "upcoming" in body
    assert "summary" in body
    assert isinstance(body["upcoming"], list)
    assert isinstance(body["summary"], dict)
    for key in ["this_month", "this_quarter", "this_year"]:
        assert key in body["summary"]


def test_dividend_calendar_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/reports/dividend-calendar")
    assert r.status_code == 401

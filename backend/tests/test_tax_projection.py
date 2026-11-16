"""Tax projection endpoint."""

from fastapi.testclient import TestClient


def _auth(client: TestClient) -> dict:
    from datetime import UTC, datetime

    email = f"taxproj-{datetime.now(UTC).timestamp()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    r = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_projection_zero_income(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get("/api/v1/taxes/projection?year=2026", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert float(body["total_tax"]) == 0
    assert body["province"] == "ON"


def test_projection_with_income(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get("/api/v1/taxes/projection?year=2026", headers=headers)
    assert r.status_code == 200
    assert float(r.json()["taxable_income"]) == 0


def test_projection_response_shape(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get("/api/v1/taxes/projection?year=2026", headers=headers)
    body = r.json()
    for key in [
        "taxable_income",
        "federal_tax",
        "provincial_tax",
        "total_tax",
        "average_rate",
        "province",
    ]:
        assert key in body


def test_projection_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/taxes/projection?year=2026")
    assert r.status_code == 401


# ----------------------------------------- year-aware bracket regressions


def test_2026_projection_uses_2026_federal_brackets(client: TestClient) -> None:
    """A $100,000 ON income is taxed with the 2026 CRA tables, not 2024's.

    Regression: the service hardcoded the 2024 tables while every caller asked
    for `year=2026`, so 2026 users paid 2024 tax.
    """
    headers = _auth(client)
    r = client.patch(
        "/api/v1/taxes/projection",
        headers=headers,
        params={"year": 2026, "province": "ON", "estimated_income": "100000"},
    )
    assert r.status_code == 200
    body = r.json()
    # 2026 CRA: fed 14% to 58,523 then 20.5% to 117,045; ON 5.05% to 53,891
    # then 9.15% to 107,785.
    assert float(body["federal_tax"]) == 16696.00
    assert float(body["provincial_tax"]) == 6940.47
    assert float(body["total_tax"]) == 23636.47


def test_2025_projection_uses_2025_brackets_and_differs_from_2026(
    client: TestClient,
) -> None:
    """2025 and 2026 are computed from their own tables, not one shared list."""
    headers = _auth(client)
    r2025 = client.patch(
        "/api/v1/taxes/projection",
        headers=headers,
        params={"year": 2025, "province": "ON", "estimated_income": "120000"},
    )
    assert r2025.status_code == 200
    b2025 = r2025.json()
    # 2025 CRA: fed 14.5% to 57,375 then 20.5%; ON 5.05% to 52,886 then 9.15%.
    assert float(b2025["federal_tax"]) == 21446.25
    assert float(b2025["provincial_tax"]) == 9097.60
    assert float(b2025["total_tax"]) == 30543.85

    r2026 = client.patch(
        "/api/v1/taxes/projection",
        headers=headers,
        params={"year": 2026, "province": "ON", "estimated_income": "120000"},
    )
    b2026 = r2026.json()
    assert float(b2026["total_tax"]) == 29974.52
    assert float(b2026["total_tax"]) != float(b2025["total_tax"])


def test_projection_reports_the_bracket_year_it_used(client: TestClient) -> None:
    """The response states which tax year's tables produced the numbers.

    Without this a stale table is indistinguishable from a correct one.
    """
    headers = _auth(client)
    body = client.get("/api/v1/taxes/projection?year=2025", headers=headers).json()
    assert body["bracket_year"] == 2025
    body = client.get("/api/v1/taxes/projection?year=2026", headers=headers).json()
    assert body["bracket_year"] == 2026


def test_unknown_year_falls_back_explicitly_not_silently(
    client: TestClient,
) -> None:
    """A year with no table is flagged, not silently priced with another year's."""
    headers = _auth(client)
    body = client.get("/api/v1/taxes/projection?year=2031", headers=headers).json()
    assert body["bracket_year"] is None
    assert body["brackets_fallback"] is True


def test_bc_and_ab_use_their_own_2026_tables(client: TestClient) -> None:
    """BC/AB brackets are year-aware too (BC 5.6% first band in 2026)."""
    headers = _auth(client)
    r = client.patch(
        "/api/v1/taxes/projection",
        headers=headers,
        params={"year": 2026, "province": "BC", "estimated_income": "100000"},
    )
    body = r.json()
    # 2026 BC: 5.6% on the first $50,363 then 7.7% up to $100,728.
    assert float(body["provincial_tax"]) == 6642.38
    assert body["province"] == "BC"


def test_projection_falls_back_to_ontario_for_unknown_province(client: TestClient) -> None:
    """A well-formed but unknown code keeps the documented Ontario fallback."""
    headers = _auth(client)
    r = client.patch(
        "/api/v1/taxes/projection",
        headers=headers,
        params={"year": 2026, "province": "ZZ", "estimated_income": "100000"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["province"] == "ZZ"
    assert float(body["provincial_tax"]) == 6940.47  # Ontario tables


def test_first_bracket_rate_is_year_specific(client: TestClient) -> None:
    """The 2026 federal first band is 14% (it was 15% in 2024, 14.5% in 2025)."""
    headers = _auth(client)
    r = client.patch(
        "/api/v1/taxes/projection",
        headers=headers,
        params={"year": 2026, "province": "ON", "estimated_income": "50000"},
    )
    body = r.json()
    assert float(body["federal_tax"]) == 7000.00  # 50,000 x 14%
    assert float(body["provincial_tax"]) == 2525.00  # 50,000 x 5.05%

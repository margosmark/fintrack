"""Tax-loss harvesting alerts: loss detection with seeded positions."""

from fastapi.testclient import TestClient

from tests.test_deep_common import auth, register


def test_tax_alerts_empty_for_no_positions(client: TestClient) -> None:
    body = register(client)
    headers = auth(body["access_token"])
    r = client.get("/api/v1/taxes/alerts", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["alerts"] == []
    assert body["summary"]["count"] == 0


def test_tax_alerts_response_shape(client: TestClient) -> None:
    body = register(client)
    headers = auth(body["access_token"])
    r = client.get("/api/v1/taxes/alerts", headers=headers)
    body = r.json()
    assert "alerts" in body
    assert "summary" in body
    assert "count" in body["summary"]
    assert "total_unrealized_loss_cad" in body["summary"]
    assert "total_estimated_tax_benefit_cad" in body["summary"]


def test_tax_alerts_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/taxes/alerts")
    assert r.status_code == 401

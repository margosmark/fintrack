"""Tests for the health check endpoint."""

from unittest.mock import MagicMock, patch

from sqlalchemy.exc import OperationalError


def test_health_returns_200_when_db_healthy(client):
    """Health endpoint returns 200 with healthy status when DB is reachable."""
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "timestamp" in data
    assert "checks" in data
    assert "database" in data["checks"]
    assert data["checks"]["database"]["status"] == "healthy"


def test_health_includes_latency_measurement(client):
    """Health endpoint includes latency_ms field in database check."""
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    data = response.json()
    db_check = data["checks"]["database"]
    assert "latency_ms" in db_check
    assert isinstance(db_check["latency_ms"], int)
    assert db_check["latency_ms"] >= 0


def test_health_returns_503_when_db_unreachable(client):
    """Health endpoint returns 503 with unhealthy status when DB fails."""
    # Mock the DB session to raise an error on execute
    with patch("app.api.health.Session") as mock_session_class:
        mock_session = MagicMock()
        mock_session.execute.side_effect = OperationalError(
            "statement", {}, Exception("connection refused")
        )
        mock_session_class.return_value = mock_session

        # Override the dependency to use our mock
        from app.api.deps import get_db

        def override_get_db():
            yield mock_session

        from app.main import app

        app.dependency_overrides[get_db] = override_get_db

        try:
            response = client.get("/api/v1/health")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "unhealthy"
            assert "timestamp" in data
            assert "checks" in data
            assert "database" in data["checks"]
            assert data["checks"]["database"]["status"] == "unhealthy"
            assert "error" in data["checks"]["database"]
        finally:
            app.dependency_overrides.clear()

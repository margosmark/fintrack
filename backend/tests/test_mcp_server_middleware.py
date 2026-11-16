"""MCP auth middleware tests: L20 default-deny route allowlist.

No real network: the backend token-validation call is replaced with a fake
httpx.AsyncClient.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_server.auth import MCP_ENDPOINT_PATH, McpAuthMiddleware


async def _mcp_endpoint(request) -> JSONResponse:
    return JSONResponse({"ok": 1, "path": request.url.path})


async def _decoy_endpoint(request) -> JSONResponse:
    return JSONResponse({"decoy": True})


@pytest.fixture(name="client")
def client_fixture(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    base = Starlette(
        routes=[
            Route(MCP_ENDPOINT_PATH, _mcp_endpoint, methods=["GET", "POST", "DELETE"]),
            # A route the app "gained" — must not be reachable (L20).
            Route("/decoy", _decoy_endpoint),
        ]
    )
    app = McpAuthMiddleware(base, backend_url="http://backend.invalid")
    return TestClient(app)


def test_unknown_path_is_404_not_served(client: TestClient) -> None:
    resp = client.get("/decoy")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not found", "code": "not_found"}


def test_root_and_traversal_style_paths_are_404(client: TestClient) -> None:
    for path in ("/", "/mcp/extra", "/mcp/../../etc", "/docs", "/health"):
        resp = client.get(path)
        assert resp.status_code == 404, path
        assert resp.json()["code"] == "not_found"


def test_mcp_path_without_bearer_is_401_not_404(client: TestClient) -> None:
    """The MCP endpoint itself is served (auth challenge, not a 404)."""
    resp = client.get("/mcp")
    assert resp.status_code == 401
    assert resp.json() == {"error": "Missing bearer token"}


def test_mcp_path_with_trailing_slash_still_served(client: TestClient) -> None:
    resp = client.get("/mcp/")
    assert resp.status_code == 401


def test_valid_bearer_passes_through_and_context_resets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_server.auth import current_user_id

    posted: dict[str, str] = {}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, url, **kwargs):
            posted["url"] = url
            posted["authorization"] = kwargs["headers"]["Authorization"]
            return httpx.Response(200, json={"user_id": 7})

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    resp = client.post(
        "/mcp",
        json={"method": "tools/list"},
        headers={"Authorization": "Bearer mcp-opaque-token"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": 1, "path": "/mcp"}
    assert posted["authorization"] == "Bearer mcp-opaque-token"
    assert posted["url"].endswith("/api/v1/auth/validate-mcp-token")
    # dispatch() resets the context var in its finally block.
    assert current_user_id.get(None) is None


def test_invalid_bearer_is_401_from_validator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, url, **kwargs):
            return httpx.Response(401, json={})

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    resp = client.get("/mcp", headers={"Authorization": "Bearer nope"})

    assert resp.status_code == 401
    assert resp.json() == {"error": "Invalid MCP token"}


def test_backend_down_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, url, **kwargs):
            raise httpx.ConnectError("backend down")

    monkeypatch.setattr(httpx, "AsyncClient", _BoomAsyncClient)

    resp = client.get("/mcp", headers={"Authorization": "Bearer x"})

    assert resp.status_code == 503
    assert resp.json() == {"error": "Auth service unavailable"}


def test_real_streamable_http_app_default_denies() -> None:
    """Against the actual create_auth_app(): only /mcp is served.

    Verifies the path claim from the SDK side too — the wrapped app must not
    expose anything else on this listener (audit L20).
    """
    from mcp_server.server import create_auth_app

    real = TestClient(create_auth_app())
    for path in ("/", "/decoy", "/sse", "/messages/", "/docs", "/health"):
        resp = real.get(path)
        assert resp.status_code == 404, path
        assert resp.json()["code"] == "not_found", path
    # The MCP endpoint itself is served — it challenges for the bearer.
    resp = real.get("/mcp")
    assert resp.status_code == 401
    assert resp.json() == {"error": "Missing bearer token"}

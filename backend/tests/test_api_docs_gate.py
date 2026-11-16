"""M87 (docs/42): the API map — `/docs`, `/redoc`, `/openapi.json` — is gated.

All three used to be served open by FastAPI's defaults: a complete, annotated
map of every endpoint of an internet-facing app. The gate is a FastAPI
dependency rather than an nginx rule so it holds at the origin as well as
behind the edge, and it accepts the two credentials a developer actually
holds — a session access token and a live MCP token. `API_DOCS_PUBLIC=true`
is the local-dev escape hatch (it fails closed when unset).

Run: cd backend && uv run pytest tests/test_api_docs_gate.py -q
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.models.mcp_tokens import McpToken, hash_mcp_token

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
TOKENS = "/api/v1/auth/mcp-tokens"
DOC_PATHS = ("/docs", "/redoc", "/openapi.json")

EMAIL = "docs-map@example.com"
PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _closed_by_default() -> Iterator[None]:
    """Pin the gate closed regardless of whatever a local `.env` says."""
    settings = get_settings()
    original = settings.api_docs_public
    settings.api_docs_public = False
    try:
        yield
    finally:
        settings.api_docs_public = original


def _headers(client: TestClient) -> dict:
    client.post(REGISTER, json={"email": EMAIL, "password": PASSWORD})
    login = client.post(LOGIN, json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_anonymous_api_map_is_refused_with_the_error_envelope(
    client: TestClient,
) -> None:
    for path in DOC_PATHS:
        response = client.get(path)
        assert response.status_code == 401, path
        assert set(response.json()) == {"detail", "code"}
        assert response.json()["code"] == "docs_auth_required"


def test_garbage_bearer_token_is_401_not_500(client: TestClient) -> None:
    """A malformed credential must fall through to the same refusal — the
    guard decodes tokens on a request path, so a decode failure has to be a
    401, never a leaked exception."""
    for path in DOC_PATHS:
        response = client.get(path, headers={"Authorization": "Bearer not.a.jwt"})
        assert response.status_code == 401, path
        assert response.json()["code"] == "docs_auth_required"


def test_session_access_token_reads_the_map(client: TestClient) -> None:
    headers = _headers(client)

    schema = client.get("/openapi.json", headers=headers)
    assert schema.status_code == 200
    assert schema.json()["openapi"].startswith("3.")
    assert "/api/v1/auth/register" in schema.json()["paths"]

    for path in ("/docs", "/redoc"):
        page = client.get(path, headers=headers)
        assert page.status_code == 200, path
        assert "text/html" in page.headers["content-type"]


def test_live_mcp_token_reads_the_map_but_an_expired_one_does_not(
    client: TestClient, session
) -> None:
    headers = _headers(client)
    created = client.post(TOKENS, json={"name": "api-map"}, headers=headers)
    assert created.status_code == 201, created.text
    mcp_headers = {"Authorization": f"Bearer {created.json()['token']}"}

    assert client.get("/openapi.json", headers=mcp_headers).status_code == 200

    # Expire it in place: the same string is now refused, exactly like
    # POST /auth/validate-mcp-token refuses it (docs/40).
    row = session.get(McpToken, int(created.json()["id"]))
    assert row is not None
    assert row.token_hash == hash_mcp_token(created.json()["token"])
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    session.add(row)
    session.commit()

    refused = client.get("/openapi.json", headers=mcp_headers)
    assert refused.status_code == 401
    assert refused.json()["code"] == "docs_auth_required"


def test_api_docs_public_opens_the_map_for_local_dev(client: TestClient) -> None:
    settings = get_settings()
    settings.api_docs_public = True
    try:
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
    finally:
        settings.api_docs_public = False

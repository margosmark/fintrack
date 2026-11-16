"""Tests for Ollama base-URL normalization and POST /users/me/llm-test."""

import httpx
from fastapi.testclient import TestClient

from app.llm.ollama import OllamaProvider

REGISTER = "/api/v1/auth/register"


def _auth(client: TestClient) -> dict:
    client.post(REGISTER, json={"email": "llmtest@example.com", "password": "pw-12345"})
    r = client.post(
        "/api/v1/auth/login", json={"email": "llmtest@example.com", "password": "pw-12345"}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_ollama_provider_normalizes_v1_suffix() -> None:
    # Reported failure: http://192.168.1.6:11434/chat/completions -> 404 because
    # Ollama's OpenAI-compatible path is /v1/chat/completions.
    assert (
        OllamaProvider(base_url="http://192.168.1.6:11434", model="m").base_url
        == "http://192.168.1.6:11434/v1"
    )
    assert (
        OllamaProvider(base_url="http://192.168.1.6:11434/", model="m").base_url
        == "http://192.168.1.6:11434/v1"
    )
    assert (
        OllamaProvider(base_url="http://192.168.1.6:11434/v1", model="m").base_url
        == "http://192.168.1.6:11434/v1"
    )
    assert (
        OllamaProvider(base_url="http://192.168.1.6:11434/chat/completions", model="m").base_url
        == "http://192.168.1.6:11434/v1"
    )
    # Empty falls back to the default, which already carries /v1.
    assert OllamaProvider(base_url="", model="m").base_url == "http://localhost:11434/v1"


class _OkProvider:
    def chat(self, messages: list[dict], json_schema: dict) -> dict:
        return {"ok": True}


class _HttpErrorProvider:
    def chat(self, messages: list[dict], json_schema: dict) -> dict:
        raise httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("POST", "http://x/chat/completions"),
            response=httpx.Response(
                404, request=httpx.Request("POST", "http://x/chat/completions")
            ),
        )


def test_llm_test_endpoint_success(client: TestClient, monkeypatch) -> None:
    import app.api.users as users_mod

    monkeypatch.setattr(users_mod, "make_provider", lambda *a, **k: _OkProvider())
    r = client.post("/api/v1/users/me/llm-test", json={}, headers=_auth(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "ollama"


def test_llm_test_endpoint_invalid_config(client: TestClient, monkeypatch) -> None:
    import app.api.users as users_mod

    monkeypatch.setattr(users_mod, "make_provider", lambda *a, **k: None)
    r = client.post("/api/v1/users/me/llm-test", json={}, headers=_auth(client))
    assert r.status_code == 400
    assert r.json()["code"] == "llm_test_invalid_config"


def test_llm_test_endpoint_http_error(client: TestClient, monkeypatch) -> None:
    import app.api.users as users_mod

    monkeypatch.setattr(users_mod, "make_provider", lambda *a, **k: _HttpErrorProvider())
    r = client.post("/api/v1/users/me/llm-test", json={}, headers=_auth(client))
    assert r.status_code == 502
    assert r.json()["code"] == "llm_upstream_error"

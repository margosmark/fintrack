"""Core ratelimit: config + client-key derivation (security review #1/#3)."""

import asyncio
import json


class _FakeRequest:
    def __init__(self, xff=None, host=None):
        self.headers = type("H", (), {"get": lambda self, _k: xff})()
        self.client = type("C", (), {"host": host})() if host else None


def test_limiter_config():
    from app.core.ratelimit import _client_key, limiter

    assert isinstance(limiter.enabled, bool)
    assert limiter._key_func is _client_key


def test_client_key_derivation():
    from app.core.ratelimit import _client_key

    # Rightmost X-Forwarded-For entry wins (relies on the trusted nginx strip).
    assert _client_key(_FakeRequest(xff="1.2.3.4, 5.6.7.8", host="9.9.9.9")) == "5.6.7.8"
    assert _client_key(_FakeRequest(xff="1.2.3.4", host="9.9.9.9")) == "1.2.3.4"
    assert _client_key(_FakeRequest(host="9.9.9.9")) == "9.9.9.9"
    # No client info at all -> "?" placeholder.
    assert _client_key(_FakeRequest()) == "?"


def test_rate_limit_exceeded_handler_envelope():
    from app.core.ratelimit import rate_limit_exceeded_handler

    resp = asyncio.run(rate_limit_exceeded_handler(_FakeRequest(), object()))
    assert resp.status_code == 429
    assert json.loads(resp.body) == {"detail": "Too many attempts", "code": "rate_limited"}

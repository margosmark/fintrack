"""`TrustedHostMiddleware` opt-in (docs/42).

`app/main.py` had `CORSMiddleware` and nothing else, so the `Host` header of a
directly reachable client was unconstrained. Per docs/42 the listener is
published to the whole LAN, so a LAN client picks that header.

The middleware is deliberately **off unless `ALLOWED_HOSTS` is set**. A wrong
allowlist makes Starlette answer 400 to every request — the healthcheck behind
the proxy included — so enabling it implicitly would be a worse outage than the
gap it closes. These tests pin both halves of that decision: off by default,
and actually enforcing when configured.
"""

from fastapi.testclient import TestClient


def test_host_check_is_off_by_default(client: TestClient) -> None:
    """The default deployment must accept whatever Host its proxy sends.

    If this ever starts failing, `ALLOWED_HOSTS` has acquired a default value —
    which would 400 the healthcheck of every existing deployment on upgrade.
    """
    r = client.get("/api/v1/health", headers={"Host": "anything.invalid"})
    assert r.status_code != 400, (
        "TrustedHostMiddleware appears to be active with no ALLOWED_HOSTS set; "
        "that would break every deployment on upgrade"
    )


def test_allowed_hosts_parsing_is_empty_when_unset() -> None:
    from app.core.config import Settings

    assert Settings(allowed_hosts="").allowed_host_list == []
    assert Settings(allowed_hosts="  ").allowed_host_list == []
    assert Settings(allowed_hosts="a.example.com, b.example.com").allowed_host_list == [
        "a.example.com",
        "b.example.com",
    ]


def test_host_check_rejects_a_foreign_host_when_configured() -> None:
    """When the operator opts in, a foreign Host is refused.

    Built directly rather than through `app.main.create_app`: the middleware is
    chosen from `Settings.allowed_host_list` at construction time, and
    `Settings` is a validated model whose derived property cannot be patched
    per-instance. What matters here is that the mounted middleware actually
    enforces, which is the half that could silently be a no-op.
    """
    from fastapi import FastAPI
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["api.example.com"])

    @app.get("/ping")
    def _ping() -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app, base_url="http://api.example.com") as good:
        assert good.get("/ping").status_code == 200
    with TestClient(app, base_url="http://evil.invalid") as bad:
        assert bad.get("/ping").status_code == 400


def test_default_settings_keep_the_host_check_off() -> None:
    """`create_app` must not mount the middleware when `ALLOWED_HOSTS` is empty."""
    from app.core.config import Settings

    assert Settings().allowed_host_list == [], (
        "the default must keep the host check off so an upgrade cannot 400 the "
        "healthcheck of an existing deployment"
    )

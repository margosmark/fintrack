from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).parents[1] / "ledger_perf.py"
spec = importlib.util.spec_from_file_location("ledger_perf", SOURCE)
assert spec and spec.loader
ledger_perf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledger_perf)


def test_target_urls_reject_all_secret_bearing_components() -> None:
    clean = ledger_perf.parse_targets("local=http://127.0.0.1:4173/app/")
    assert clean == [("local", "http://127.0.0.1:4173/app")]

    with pytest.raises(ValueError):
        ledger_perf.parse_targets("remote=https://example.test/app")
    approved = {ledger_perf.validate_approved_origin("https://example.test")}
    assert ledger_perf.parse_targets("remote=https://example.test/app", approved) == [
        ("remote", "https://example.test/app")
    ]
    with pytest.raises(ValueError):
        ledger_perf.validate_approved_origin("https://example.test/app")

    for value, secret in (
        ("local=https://user:password@example.test", "password"),
        ("local=https://example.test/?access_token=secret", "secret"),
        ("local=https://example.test/#refresh_token=secret", "secret"),
    ):
        with pytest.raises(ValueError) as exc:
            ledger_perf.parse_targets(value)
        assert secret not in str(exc.value)


def test_main_refuses_missing_explicit_read_only_auth_before_browser_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started = False

    def fail_if_started(*_args: object, **_kwargs: object) -> None:
        nonlocal started
        started = True
        raise AssertionError("browser must not start without explicit auth")

    monkeypatch.setattr(ledger_perf, "Probe", fail_if_started)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ledger_perf.py",
            "cold",
            "--targets",
            "local=http://127.0.0.1:4173",
            "--out",
            str(tmp_path / "report.json"),
        ],
    )

    assert ledger_perf.main() == 2
    assert not started
    assert "POST" not in capsys.readouterr().err


def test_main_rejects_unapproved_remote_before_browser_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started = False

    def fail_if_started(*_args: object, **_kwargs: object) -> None:
        nonlocal started
        started = True
        raise AssertionError("browser must not start for an unapproved remote")

    monkeypatch.setattr(ledger_perf, "Probe", fail_if_started)
    monkeypatch.setenv("READ_ONLY_TOKEN", "not-used")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ledger_perf.py",
            "cold",
            "--targets",
            "remote=https://evil.example",
            "--out",
            str(tmp_path / "report.json"),
            "--auth-token-env",
            "READ_ONLY_TOKEN",
        ],
    )

    assert ledger_perf.main() == 2
    assert not started
    assert "evil.example" not in capsys.readouterr().err


def test_main_rejects_secret_url_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "super-secret-token"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ledger_perf.py",
            "cold",
            "--targets",
            f"remote=https://example.test/?token={secret}",
            "--out",
            str(tmp_path / "report.json"),
            "--auth-token-env",
            "READ_ONLY_TOKEN",
        ],
    )

    assert ledger_perf.main() == 2
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_wait_ready_reports_wrong_path() -> None:
    class FakeCDP:
        def eval_js(self, *_args: object, **_kwargs: object) -> str:
            return '{"p":"/dashboard","h":"Dashboard","pend":0,"ap":0,"now":1,"h1At":1}'

    probe = object.__new__(ledger_perf.Probe)
    probe.cdp = FakeCDP()
    latency, meta = probe.wait_ready("Transactions", "/transactions", timeout=0.2)

    assert latency is None
    assert meta == {
        "fail": "route_mismatch",
        "expected_path": "/transactions",
        "path": "/dashboard",
    }


def test_browser_ownership_requires_the_run_marker_and_profile_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".ledger-perf-owner").write_text("owner", encoding="utf-8")
    proc = SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(
        ledger_perf,
        "_cmdline",
        lambda _pid: f"chromium --user-data-dir={profile.resolve()} --remote-debugging-port=0",
    )

    assert ledger_perf.verify_browser_ownership(proc, str(profile))
    (profile / ".ledger-perf-owner").unlink()
    assert not ledger_perf.verify_browser_ownership(proc, str(profile))


def test_close_does_not_delete_a_profile_with_a_different_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = tmp_path / "foreign"
    profile.mkdir()
    (profile / ".ledger-perf-owner").write_text("foreign-owner", encoding="utf-8")
    proc = SimpleNamespace(pid=456, poll=lambda: None, terminate=lambda: None, kill=lambda: None)
    probe = object.__new__(ledger_perf.Probe)
    probe.profile = str(profile)
    probe._owner = "this-run"
    probe.proc = proc
    monkeypatch.setattr(ledger_perf, "verify_browser_ownership", lambda *_args: False)

    probe.close()

    assert profile.exists()
    assert (profile / ".ledger-perf-owner").read_text(encoding="utf-8") == "foreign-owner"


def test_probe_source_has_no_demo_login_or_shared_debugger_endpoint() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert "/auth/demo" not in source
    assert "Explore the demo" not in source
    assert "remote-debugging-port=9555" not in source
    assert "ledger-perf-probe-prof" not in source
    assert "trustedOrigin" in source
    assert "SAFE_METHODS" in source
    assert "redirect: 'error'" in source


def test_preprovisioned_auth_intercepts_refresh_without_posting() -> None:
    posts: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path == "/api/v1/users/me":
                body = json.dumps({"id": 7, "email": "readonly@example.test"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            posts.append(self.path)
            self.send_response(500)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    probe = ledger_perf.Probe("preprovisioned-read-only-test-token")
    try:
        probe.set_target(f"http://127.0.0.1:{server.server_port}")
        probe.goto(f"http://127.0.0.1:{server.server_port}/")
        result = probe.cdp.eval_js(
            "fetch('/api/v1/auth/refresh', {method: 'POST'}).then((r) => r.json())",
            await_promise=True,
        )
        unsafe = probe.cdp.eval_js(
            "fetch('/api/v1/transactions', {method: 'POST'}).catch((e) => e.name)",
            await_promise=True,
        )
    finally:
        probe.close()
        server.shutdown()
        thread.join(timeout=5)

    assert result["access_token"] == "preprovisioned-read-only-test-token"
    assert unsafe == "TypeError"
    assert posts == []


def test_bearer_is_not_forwarded_to_an_unapproved_origin() -> None:
    foreign_auth: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            foreign_auth.append(self.headers.get("Authorization"))
            body = b"ok"
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    trusted = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    foreign = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    trusted_thread = threading.Thread(target=trusted.serve_forever, daemon=True)
    foreign_thread = threading.Thread(target=foreign.serve_forever, daemon=True)
    trusted_thread.start()
    foreign_thread.start()
    probe = ledger_perf.Probe("origin-bound-token")
    try:
        probe.set_target(f"http://127.0.0.1:{trusted.server_port}")
        probe.goto(f"http://127.0.0.1:{trusted.server_port}/")
        foreign_auth.clear()
        result = probe.cdp.eval_js(
            f"fetch({json.dumps(f'http://127.0.0.1:{foreign.server_port}/api/v1/read')}, "
            "{headers: {Authorization: 'Bearer origin-bound-token'}}).then((r) => r.status)",
            await_promise=True,
        )
    finally:
        probe.close()
        trusted.shutdown()
        foreign.shutdown()
        trusted_thread.join(timeout=5)
        foreign_thread.join(timeout=5)

    assert result == 200
    assert foreign_auth
    assert all(value is None for value in foreign_auth)

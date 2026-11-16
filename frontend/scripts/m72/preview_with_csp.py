#!/usr/bin/env python3
"""Serve `dist/` the way the nginx image serves it — so the M72 audit's
console-error and touch-target claims can be measured BEFORE a deploy.

Two things make a plain `vite preview` insufficient:

  * CSP. `vite preview` sends no `Content-Security-Policy`, so it cannot show
    the QA F2 defect (or prove its fix): the two inline scripts in index.html
    are only blocked — or only allowed — by the nginx header. This server reads
    the header straight out of the Dockerfile (single source of truth, so it
    can never drift from what actually ships) and sends it on every response,
    along with the SPA fallback nginx does (`try_files $uri $uri/
    /index.html`).
  * API. The audit logs in as the demo user and walks six pages, so `/api` has
    to reach a real backend; it is proxied to a running stack (the LAN tunnel).

Run:
  python3 scripts/m72/preview_with_csp.py --dist dist --api http://127.0.0.1:18080 --port 4173
  python3 scripts/m72/responsive_audit.py --base http://127.0.0.1:4173 --out audit.json
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[2]  # …/frontend


def csp_from_dockerfile() -> str:
    """Every `add_header Content-Security-Policy "…"` must agree; return it."""
    text = (ROOT / "Dockerfile").read_text()
    values = set(re.findall(r'add_header Content-Security-Policy \\"([^"]+)\\"', text))
    if len(values) != 1:
        raise SystemExit(
            f"Dockerfile has {len(values)} distinct CSP values (expected 1): {values}"
        )
    return values.pop()


class Handler(BaseHTTPRequestHandler):
    dist: pathlib.Path
    api_base: str
    csp: str

    def log_message(self, format: str, *args) -> None:  # quiet
        pass

    # -- helpers ----------------------------------------------------------
    def _headers(self, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Security-Policy", self.csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)

    def _write(self, payload: bytes) -> None:
        # HEAD must not carry a body (curl -I / probe scripts use it).
        if self.command != "HEAD":
            self.wfile.write(payload)

    # -- proxy ------------------------------------------------------------
    def _proxy(self) -> None:
        started = time.monotonic()
        try:
            self._proxy_inner()
        finally:
            duration = time.monotonic() - started
            if duration > 2.0 or getattr(self, "_status", 200) >= 400:
                print(
                    f"PROXY {self.command} {self.path} -> "
                    f"{getattr(self, '_status', '?')} {duration:.2f}s",
                    file=sys.stderr,
                    flush=True,
                )

    def _proxy_inner(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        url = self.api_base + self.path
        req = urllib.request.Request(
            url, data=body, method=self.command
        )
        for name in ("Content-Type", "Accept", "Authorization", "Cookie"):
            if self.headers.get(name):
                req.add_header(name, self.headers[name])
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
                self._status = resp.status
                self.send_response(resp.status)
                for name in ("Content-Type", "Set-Cookie", "Cache-Control"):
                    for value in resp.headers.get_all(name) or []:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self._write(payload)
        except urllib.error.HTTPError as err:  # pass the API's own error body
            payload = err.read()
            self._status = err.code
            self.send_response(err.code)
            for name in ("Content-Type", "Set-Cookie"):
                for value in err.headers.get_all(name) or []:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write(payload)
        except Exception as err:  # backend down — say so instead of hanging
            payload = json.dumps({"detail": f"preview proxy: {err}"}).encode()
            self._status = 502
            self.send_response(502)
            self._headers("application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write(payload)

    # -- static + SPA fallback --------------------------------------------
    def _serve_static(self) -> None:
        rel = self.path.lstrip("/").split("?", 1)[0]
        candidate = (self.dist / rel).resolve()
        if not str(candidate).startswith(str(self.dist.resolve())) or not candidate.is_file():
            candidate = self.dist / "index.html"  # nginx `try_files … /index.html`
        ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if candidate.suffix == ".js":
            ctype = "text/javascript"
        payload = candidate.read_bytes()
        extra = (
            {"Cache-Control": "no-cache, must-revalidate"}
            if candidate.name == "index.html"
            else {"Cache-Control": "public, max-age=31536000, immutable"}
        )
        self.send_response(200)
        self._headers(ctype, extra)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write(payload)

    def _dispatch(self) -> None:
        try:
            if self.path.startswith("/api"):
                self._proxy()
            else:
                self._serve_static()
        except BrokenPipeError:
            pass

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=str(ROOT / "dist"))
    ap.add_argument("--api", default="http://127.0.0.1:18080")
    ap.add_argument("--port", type=int, default=4173)
    args = ap.parse_args()

    dist = pathlib.Path(args.dist).resolve()
    if not (dist / "index.html").is_file():
        raise SystemExit(f"{dist}/index.html not found — run `npm run build` first")

    Handler.dist = dist
    Handler.api_base = args.api.rstrip("/")
    Handler.csp = csp_from_dockerfile()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serving {dist} with the Dockerfile CSP on http://127.0.0.1:{args.port}")
    print(f"csp: {Handler.csp}")
    print(f"api: {Handler.api_base}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

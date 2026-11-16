#!/usr/bin/env python3
"""Local static server for the built frontend + /api reverse proxy to prod.

Lets a CDP probe exercise a locally-built bundle (with the real backend data)
without needing docker or a local Postgres. Read-only use: every /api request
is forwarded verbatim to https://fintrac.princessandmark.com.

Run: python3 scripts/local/serve_proxy.py --dist dist --port 4173
"""
from __future__ import annotations

import argparse
import http.client
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = "fintrac.princessandmark.com"
UPSTREAM_PORT = 443

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    dist = "dist"
    quiet = True

    # ---- helpers -----------------------------------------------------
    def log_message(self, format, *args):  # noqa: A002
        if not self.quiet:
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _send(self, code, headers, body: bytes):
        self.send_response(code)
        for k, v in headers:
            if k.lower() in HOP_BY_HOP:
                continue
            if k.lower() == "content-length":
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _file(self, rel_path: str) -> bool:
        root = os.path.abspath(self.dist)
        full = os.path.abspath(os.path.join(root, rel_path.lstrip("/")))
        if not full.startswith(root + os.sep) and full != root:
            return False
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            return False
        with open(full, "rb") as fh:
            body = fh.read()
        ctype = "application/octet-stream"
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript",
            ".mjs": "text/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".webmanifest": "application/manifest+json",
            ".woff2": "font/woff2",
            ".txt": "text/plain; charset=utf-8",
        }.get(ext, ctype)
        self._send(200, [("Content-Type", ctype), ("Cache-Control", "no-store")], body)
        return True

    # ---- verbs -------------------------------------------------------
    def _handle(self, body: bytes | None = None):
        path = self.path
        if path.startswith("/api"):
            self._proxy(path, body)
            return
        rel = path.split("?", 1)[0]
        if self._file(rel):
            return
        # SPA fallback (never for asset-looking paths)
        if "." not in os.path.basename(rel):
            if self._file("/index.html"):
                return
        self._send(404, [("Content-Type", "text/plain")], b"not found")

    def _proxy(self, path: str, body: bytes | None):
        conn = http.client.HTTPSConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=60)
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != "host"
        }
        headers["Host"] = UPSTREAM_HOST
        headers["Accept-Encoding"] = "identity"
        if body:
            headers["Content-Length"] = str(len(body))
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            out = [
                (k, v)
                for k, v in resp.getheaders()
                if k.lower() not in HOP_BY_HOP and k.lower() != "content-length"
            ]
            self._send(resp.status, out, raw)
        except Exception as exc:  # pragma: no cover - diagnostics
            self._send(502, [("Content-Type", "text/plain")], f"proxy error: {exc}".encode())
        finally:
            conn.close()

    def do_GET(self):  # noqa: N802
        self._handle()

    def do_HEAD(self):  # noqa: N802
        self._handle()

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self._handle(self.rfile.read(n) if n else b"")

    def do_PUT(self):  # noqa: N802
        self.do_POST()

    def do_PATCH(self):  # noqa: N802
        self.do_POST()

    def do_DELETE(self):  # noqa: N802
        self.do_POST()

    def do_OPTIONS(self):  # noqa: N802
        self._handle()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default="dist")
    ap.add_argument("--port", type=int, default=4173)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    Handler.dist = args.dist
    Handler.quiet = not args.verbose
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serving {args.dist} on http://127.0.0.1:{args.port} (api -> {UPSTREAM_HOST})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

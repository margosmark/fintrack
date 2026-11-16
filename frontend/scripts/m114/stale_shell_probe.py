#!/usr/bin/env python3
"""M114 deploy-sim: a stale shell's lazy ⌘K chunk 404s after a deploy.

Two production builds are served by ONE local origin that can be re-pointed
mid-session (that IS a deploy), with `/api` proxied to a live backend so the
probe can log in as the demo user and reach the palette at all:

    /tmp/m114/A   build of origin/main   (pre-fix: cache-first, no 404 healing)
    /tmp/m114/B   build of the fix       (BUILD_ID cache + 404 self-heal)

Scenarios (each `--runs N`, fresh browser profile per run):

  before            shell A + worker A,  deploy B, open the palette
                    -> the import 404s and the page dies on the error UI
  after             shell A + worker A,  deploy B, then the worker updates to
                    B's sw.js (what every client gets on its next navigation
                    once this ships), open the palette
                    -> 404 -> marker module -> ONE guarded reload into shell B
  after-new-shell   shell B + worker B,  deploy A, open the palette
                    -> same heal on a shell whose OWN JS listens for the
                       worker's RELOAD_MESSAGE (the other half of the pair)

Run:  python3 stale_shell_probe.py --dist-a /tmp/m114/A --dist-b /tmp/m114/B
Writes m114-stale-shell.json plus one screenshot per scenario.
"""
from __future__ import annotations

import argparse
import http.client
import importlib
import json
import mimetypes
import os
import pathlib
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[2]  # .../frontend
A11Y = ROOT / "scripts" / "a11y"

STATE: dict = {"root": None, "api": None, "swaps": []}

ENTRY_CACHE = "public, max-age=31536000, immutable"
SHELL_CACHE = "no-cache, must-revalidate"

PALETTE_OK = "!!document.querySelector('[data-testid=\"command-palette\"]')"
TRIGGER_READY = "!!document.querySelector('[data-testid=\"command-palette-trigger\"]')"
OPEN_PALETTE = (
    "(() => { const b = document.querySelector("
    "'[data-testid=\"command-palette-trigger\"]'); if (!b) return 'no trigger';"
    " b.click(); return 'clicked'; })()"
)
ERROR_UI = (
    "(() => { const t = document.body.innerText || ''; "
    "return /Unexpected Application Error|error loading dynamically imported module|"
    "Failed to fetch dynamically imported module/.test(t); })()"
)
ENTRY_SRC = (
    "(document.querySelector('script[type=\"module\"]') || {}).src || ''"
)
RELOAD_GUARD = "sessionStorage.getItem('fintrack-stale-reload-at')"
SW_KEYS = "navigator.serviceWorker ? caches.keys() : []"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "m114-probe"

    def log_message(self, format, *args):  # noqa: A002 - keep the run quiet
        pass

    # -- helpers ---------------------------------------------------------
    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, data: bytes, ctype: str, cache: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _proxy(self, method: str) -> None:
        body = self._body() if method not in ("GET", "HEAD") else None
        host, port = STATE["api"]
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower()
            not in (
                "host",
                "connection",
                "content-length",
                "accept-encoding",
                "keep-alive",
                "transfer-encoding",
                "upgrade",
                "proxy-connection",
            )
        }
        conn = http.client.HTTPConnection(host, port, timeout=180)
        try:
            conn.request(method, self.path, body=body, headers=headers)
            response = conn.getresponse()
            ctype = response.getheader("Content-Type", "application/octet-stream")
            streaming = ctype.startswith("text/event-stream") or not response.getheader(
                "Content-Length"
            )
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in ("connection", "transfer-encoding", "content-length"):
                    continue
                self.send_header(key, value)
            if streaming:
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    line = response.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
                self.close_connection = True
            else:
                data = response.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
        finally:
            conn.close()

    def _serve_static(self) -> None:
        path = urlsplit(self.path).path
        root = pathlib.Path(STATE["root"])
        rel = path.lstrip("/") or "index.html"
        target = root / rel
        if target.is_file() and target.resolve().is_relative_to(root.resolve()):
            data = target.read_bytes()
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            cache = None
            if target.suffix == ".html":
                cache = SHELL_CACHE
            elif rel.startswith("assets/"):
                cache = ENTRY_CACHE
            self._send(200, data, ctype, cache)
            return
        if rel.startswith("assets/") or "." in pathlib.Path(rel).name:
            # nginx: an unknown /assets/* (or any unknown file) is a real 404,
            # never the SPA fallback.
            self._send(404, b"<html><head><title>404 Not Found</title></head>"
                            b"<body><center><h1>404 Not Found</h1></center></body></html>",
                       "text/html")
            return
        self._send(200, (root / "index.html").read_bytes(), "text/html", SHELL_CACHE)

    def handle_any(self, method: str) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            self._proxy(method)
            return
        self._serve_static()

    def do_GET(self):
        self.handle_any("GET")

    def do_HEAD(self):
        self.handle_any("HEAD")

    def do_POST(self):
        self.handle_any("POST")

    def do_PATCH(self):
        self.handle_any("PATCH")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_DELETE(self):
        self.handle_any("DELETE")


def start_server(dist_a: str, dist_b: str, api: str, port: int) -> tuple[ThreadingHTTPServer, str]:
    STATE["root"] = dist_a
    host, _, api_port = api.partition(":")
    STATE["api"] = (host, int(api_port or 80))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def login(tab, base: str) -> None:
    """The demo account is the evaluation front door (docs/09).

    `/auth/demo` is rate-limited per IP (docs/09 smoke notes: ~1 minute), and
    this probe signs in once per run — so a throttled attempt waits out the
    window and retries instead of reporting a false failure.
    """
    last_body = ""
    for attempt in range(3):
        box = None
        for _ in range(60):
            box = tab.evaluate(
                """(() => {
                  const b = [...document.querySelectorAll('button')]
                    .find((x) => x.innerText.includes('Explore the demo'));
                  if (!b) return null;
                  const r = b.getBoundingClientRect();
                  return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
                })()"""
            )
            if box:
                break
            time.sleep(0.25)
        if not box:
            raise RuntimeError("demo button never appeared")
        tab.click_at(box["x"], box["y"])
        try:
            tab.wait_for("location.pathname === '/dashboard'", timeout=40)
            return
        except TimeoutError:
            try:
                last_body = str(tab.evaluate("document.body.innerText"))[:600]
            except Exception:
                pass
            if "Too many attempts" in last_body and attempt < 2:
                time.sleep(65)
                continue
            raise RuntimeError(
                f"demo login never reached /dashboard; body={last_body!r} "
                f"console={tab.console_errors()[:6]}"
            )
    raise RuntimeError(f"demo login never reached /dashboard; body={last_body!r}")


def palette_chunk(dist: pathlib.Path) -> str:
    """The palette chunk this build ships — the one the OTHER build lacks."""
    for chunk in sorted((dist / "assets").glob("CommandPalette-*.js")):
        return "/assets/" + chunk.name
    return ""


def evaluate_quiet(tab, expr: str):
    try:
        return tab.evaluate(expr, await_promise=True)
    except Exception:
        return None


RUN_COUNTER = 0


def run_one(kind: str, args, dist_a: str, dist_b: str, base: str, index: int) -> dict:
    global RUN_COUNTER
    RUN_COUNTER += 1
    # a11y_driver reads BOTH of these at import time, and a profile that
    # survives a run carries its service-worker registration — run 2 would then
    # drive run 1's browser state (measured: a fresh shell reporting the
    # PREVIOUS build's cache name). So: new profile, new port, and reload the
    # module so its constants are re-read before Chrome is launched.
    profile = tempfile.mkdtemp(prefix=f"m114-{kind}-{index}-")
    os.environ["A11Y_PROFILE_DIR"] = profile
    os.environ["A11Y_CDP_PORT"] = str(args.cdp_port + RUN_COUNTER)
    import a11y_driver  # noqa: PLC0415

    a11y_driver = importlib.reload(a11y_driver)
    Browser = a11y_driver.Browser

    shell = dist_a if kind in ("before", "after") else dist_b
    # The origin starts on the build the browser is about to load, and the
    # "deploy" is re-pointing it at the OTHER build.
    deploy_to = dist_b if shell == dist_a else dist_a

    STATE["root"] = shell
    record: dict = {"scenario": kind, "run": index, "shell": pathlib.Path(shell).name,
                    "deploy_to": pathlib.Path(deploy_to).name}

    browser = Browser(headless_old=True, width=1440, height=900)
    try:
        tab = browser.new_tab(f"{base}/login")
        tab.enable_runtime()
        tab.navigate(f"{base}/login")
        tab.wait_for("document.readyState === 'complete'")
        login(tab, base)
        tab.wait_for("!!navigator.serviceWorker && !!navigator.serviceWorker.controller",
                     timeout=30)
        record["caches_after_load"] = evaluate_quiet(tab, SW_KEYS)
        record["entry_after_load"] = evaluate_quiet(tab, ENTRY_SRC)

        # -- deploy: the origin now serves the other build -----------------
        STATE["root"] = deploy_to
        record["deployed_at"] = time.time()

        if kind == "after":
            # What a client does on its next navigation once the fix ships:
            # re-check /sw.js, install the NEW worker, activate + claim.
            # `caches.keys()` gains the new name at INSTALL; the page is only
            # handed over at ACTIVATE (skipWaiting + clients.claim), and the
            # palette must be opened against the NEW worker or the probe is
            # measuring the old one. controllerchange is the exact boundary.
            evaluate_quiet(
                tab,
                "(() => { window.__m114cc = 0; navigator.serviceWorker"
                ".addEventListener('controllerchange', () => { window.__m114cc++; });"
                " return true; })()",
            )
            evaluate_quiet(
                tab,
                "navigator.serviceWorker.getRegistration().then(r => r && r.update())",
            )
            wanted = new_cache_name(pathlib.Path(deploy_to))
            # Tab.wait_for cannot await a promise, so poll the cache list the
            # way the page would: install -> activate (skipWaiting) -> claim.
            # One retry: a precache fetch that gets a connection reset fails
            # the install (the browser retries on the NEXT navigation, not
            # this one), and the probe would otherwise report a page-level
            # failure for what is a transient server hiccup.
            keys = None
            claimed = None
            for attempt in range(2):
                update_deadline = time.time() + 30
                while time.time() < update_deadline:
                    keys = evaluate_quiet(tab, SW_KEYS)
                    claimed = evaluate_quiet(tab, "window.__m114cc || 0")
                    if keys == [wanted] and claimed:
                        break
                    time.sleep(0.3)
                if keys == [wanted] and claimed:
                    break
                evaluate_quiet(
                    tab,
                    "navigator.serviceWorker.getRegistration().then(r => r && r.update())",
                )
            else:
                raise RuntimeError(
                    f"new worker never took control; caches={keys} controllerchange={claimed}"
                )
            record["caches_after_update"] = keys
            record["controllerchange"] = claimed
            record["worker_after_update"] = wanted

        # -- open the palette (the lazy chunk is only fetched NOW) ----------
        record["trigger"] = evaluate_quiet(tab, OPEN_PALETTE)
        opened: dict = {"palette": False, "error": False, "reloaded_entry": None}
        initial_entry = record["entry_after_load"]
        deadline = time.time() + 15
        while time.time() < deadline:
            if evaluate_quiet(tab, PALETTE_OK):
                opened["palette"] = True
                break
            entry = evaluate_quiet(tab, ENTRY_SRC)
            if entry and entry != initial_entry:
                opened["reloaded_entry"] = entry
                break
            if evaluate_quiet(tab, ERROR_UI):
                opened["error"] = True
                if kind == "before":
                    break  # nothing can heal a worker that has no heal
                # otherwise keep polling: the marker's reload is on its way
            time.sleep(0.25)

        if not (opened["palette"] or opened["error"] or opened["reloaded_entry"]):
            # The reload may have landed between polls — take stock once more,
            # but only an entry that CHANGED counts as a heal.
            final_entry = evaluate_quiet(tab, ENTRY_SRC)
            if final_entry and final_entry != record["entry_after_load"]:
                opened["reloaded_entry"] = final_entry

        if opened["reloaded_entry"]:
            # The heal replaced the document with the CURRENT shell; the
            # palette has to open on it, with no error boundary in between.
            opened["palette_after_reload"] = False
            ready = time.time() + 30
            while time.time() < ready:
                if evaluate_quiet(tab, TRIGGER_READY):
                    break
                time.sleep(0.3)
            evaluate_quiet(tab, OPEN_PALETTE)
            palette_deadline = time.time() + 20
            while time.time() < palette_deadline:
                if evaluate_quiet(tab, PALETTE_OK):
                    opened["palette_after_reload"] = True
                    break
                time.sleep(0.3)
            opened["error_after_reload"] = bool(evaluate_quiet(tab, ERROR_UI))
            if not opened["palette_after_reload"]:
                opened["post_reload_body"] = str(
                    evaluate_quiet(tab, "document.body.innerText")
                )[:600]

        if opened["error"] and not opened["reloaded_entry"]:
            # Why did the import fail? Ask the worker for the SAME URL it just
            # refused: 200 + marker = the heal is live and this document is the
            # legacy shell that cannot listen; 404 = the worker did not heal.
            chunk = palette_chunk(pathlib.Path(shell))
            opened["worker_probe"] = evaluate_quiet(
                tab,
                "fetch(" + json.dumps(chunk) + ").then(async r => ({"
                " status: r.status, type: r.headers.get('content-type'),"
                " marker: (await r.text()).indexOf("
                "'M114 stale-shell self-heal') === 0 })"
                ".catch(e => ({ error: String(e) })))",
            )
        record.update(opened)
        record["guard_value"] = evaluate_quiet(tab, RELOAD_GUARD)
        record["caches_final"] = evaluate_quiet(tab, SW_KEYS)
        record["entry_final"] = evaluate_quiet(tab, ENTRY_SRC)
        record["pathname"] = evaluate_quiet(tab, "location.pathname")
        record["console_errors"] = [
            line for line in tab.console_errors() if "favicon" not in line
        ][:8]
        # One screenshot per scenario keeps the evidence (and the repo) small;
        # the JSON carries the numbers for every run.
        record["screenshot"] = (
            str(tab.screenshot(str(A11Y / f"m114-{kind}-{index}.png")))
            if index == 0
            else None
        )
        return record
    finally:
        browser.close()


def new_cache_name(dist: pathlib.Path) -> str:
    """The CACHE_NAME the build in `dist` stamps into its worker."""
    text = (dist / "sw.js").read_text()
    marker = 'const BUILD_ID = "'
    if marker in text:
        build_id = text.split(marker, 1)[1].split('"', 1)[0]
        return f"fintrack-shell-v1-{build_id}"
    return "fintrack-shell-v1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-a", required=True, help="pre-fix build (origin/main)")
    parser.add_argument("--dist-b", required=True, help="fixed build")
    parser.add_argument("--api", default="127.0.0.1:18080")
    parser.add_argument("--port", type=int, default=4188)
    parser.add_argument("--cdp-port", type=int, default=9611)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scenarios", default="before,after,after-new-shell")
    parser.add_argument("--out", default=str(A11Y / "m114-stale-shell.json"))
    args = parser.parse_args()

    dist_a = str(pathlib.Path(args.dist_a).resolve())
    dist_b = str(pathlib.Path(args.dist_b).resolve())
    for dist in (dist_a, dist_b):
        if not pathlib.Path(dist, "index.html").exists():
            raise SystemExit(f"{dist} has no index.html")

    sys.path.insert(0, str(A11Y))
    server, base = start_server(dist_a, dist_b, args.api, args.port)

    records = []
    for scenario in args.scenarios.split(","):
        for index in range(args.runs):
            print(f"[{scenario} #{index + 1}/{args.runs}] …", flush=True)
            try:
                records.append(run_one(scenario, args, dist_a, dist_b, base, index))
            except Exception as exc:  # keep going: a failed run is evidence too
                records.append({"scenario": scenario, "run": index, "failed": repr(exc)})
            print(json.dumps(records[-1], indent=2)[:1400], flush=True)
            # /auth/demo is per-IP rate-limited: spacing the runs keeps the
            # probe out of the throttle window (login() still retries).
            time.sleep(6)

    server.shutdown()

    summary = {}
    for scenario in args.scenarios.split(","):
        rows = [r for r in records if r.get("scenario") == scenario and "failed" not in r]
        summary[scenario] = {
            "runs": len(rows),
            "palette_opened": sum(1 for r in rows if r.get("palette")),
            "palette_after_reload": sum(
                1 for r in rows if r.get("palette_after_reload")
            ),
            "error_ui": sum(1 for r in rows if r.get("error")),
            "healed_by_reload": sum(1 for r in rows if r.get("reloaded_entry")),
            "failed": len([r for r in records
                           if r.get("scenario") == scenario and "failed" in r]),
        }
    payload = {"summary": summary, "records": records,
               "dist_a": dist_a, "dist_b": dist_b, "base": base}
    pathlib.Path(args.out).write_text(json.dumps(payload, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

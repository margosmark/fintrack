#!/usr/bin/env python3
"""Ledger startup + 390px render-budget probe.

This is a report-only harness. Thresholds are documented in thresholds.json as
proposals; this script deliberately does not enforce them in CI or cron.

Three phases, each its own invocation (fresh Chrome + fresh profile per run):

  cold    Cold-route timings: SW unregistered, HTTP cache disabled, full
          document navigation per route. Per run: TTFB, DCL, load, FCP, LCP,
          first-page-<h1> time, API settle time, transferred bytes, overflow.
          --viewport 1440x900 (desktop) or 390x844 (mobile).

  budget  390px interaction/render budget against a WARM app (cache + service
          worker on, i.e. real post-load behaviour): scripted scroll sweeps,
          bottom-tab switches, More panel open/close, Add editor open/close.
          Per workload: frame deltas, long tasks, CLS delta, input-event
          durations, click-to-content latency.

  login   Establish a pre-provisioned read-only session and report the shell
          (diagnostic; no credentials are created or refreshed).

Targets are passed as label=url pairs, e.g.
  --targets main=http://127.0.0.1:4173,pre=http://127.0.0.1:4174,prod=https://fintrac.princessandmark.com

Design notes:
  * own Chrome on an ephemeral loopback debugging port + an exclusive profile
    under scratch — never the shared browsers (9333/9222/9444 are other cards').
  * instrumentation runs via Page.addScriptToEvaluateOnNewDocument BEFORE any
    app script: fetch/XHR pending counters, first-<h1> timestamp, longtask /
    event-timing / layout-shift observers, rAF frame ticker.
  * `Page.bringToFront` first — a hidden tab services rAF at ~0.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit, urlunsplit

import websocket

SCRATCH = os.environ.get("TMPDIR", "/tmp")
ROUTES = ["dashboard", "transactions", "imports", "investments", "reports"]
routes = ROUTES
H1_LABEL = {r: r.capitalize() for r in ROUTES}  # page h1 text per route

# ---------------------------------------------------------------- instrumentation
INSTRUMENT = r"""
(() => {
  if (window.__p) return;
  window.__p = { h1At: null, pending: 0, apiPending: 0, seq: 0, inflight: {} };
  const of = window.fetch;
  window.fetch = function (...a) {
    const isApi = typeof a[0] === "string" && a[0].includes("/api/");
    const url = typeof a[0] === "string" ? a[0] : (a[0] && a[0].url) || "?";
    const key = (window.__p.seq++) + "|" + url.slice(0, 120);
    window.__p.inflight[key] = 1;
    window.__p.pending++; if (isApi) window.__p.apiPending++;
    return of.apply(this, a).finally(() => {
      delete window.__p.inflight[key];
      window.__p.pending--; if (isApi) window.__p.apiPending--;
    });
  };
  const xo = XMLHttpRequest.prototype.open, xs = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u, ...r) {
    this.__api = typeof u === "string" && u.includes("/api/");
    this.__key = (window.__p.seq++) + "|xhr:" + String(u || "?").slice(0, 110);
    this.__counted = false;
    return xo.call(this, m, u, ...r);
  };
  XMLHttpRequest.prototype.send = function (...a) {
    if (!this.__counted) {
      this.__counted = true;
      window.__p.inflight[this.__key] = 1;
      window.__p.pending++; if (this.__api) window.__p.apiPending++;
      this.addEventListener("loadend", () => {
        if (this.__counted) {
          this.__counted = false;
          delete window.__p.inflight[this.__key];
          window.__p.pending--; if (this.__api) window.__p.apiPending--;
        }
      });
    }
    return xs.apply(this, a);
  };
  window.__mb = { long: [], frames: [], events: [], cls: 0,
    reset() { this.long = []; this.frames = []; this.events = []; this.cls = 0; } };
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) window.__mb.long.push({ d: e.duration, t: e.startTime });
    }).observe({ type: "longtask", buffered: true });
  } catch (e) {}
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) window.__mb.events.push({ n: e.name, d: e.duration, t: e.startTime });
    }).observe({ type: "event", buffered: true, durationThreshold: 16 });
  } catch (e) {}
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) if (!e.hadRecentInput) window.__mb.cls += e.value;
    }).observe({ type: "layout-shift", buffered: true });
  } catch (e) {}
  let last = null;
  const tick = (ts) => { if (last != null) window.__mb.frames.push(ts - last); last = ts; requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  const check = () => {
    if (window.__p.h1At == null && document.querySelector("h1")) window.__p.h1At = performance.now();
  };
  const mo = new MutationObserver(check);
  const start = () => { mo.observe(document.documentElement, { childList: true, subtree: true }); check(); };
  if (document.documentElement) start(); else document.addEventListener("DOMContentLoaded", start);
})();
"""

SNAPSHOT = r"""
(async () => {
  const nav = performance.getEntriesByType("navigation")[0] || {};
  const paints = performance.getEntriesByType("paint");
  const fcpE = paints.find((p) => p.name === "first-contentful-paint");
  const lcp = await new Promise((res) => {
    let v = null;
    try {
      const po = new PerformanceObserver((l) => { for (const e of l.getEntries()) v = e.startTime; });
      po.observe({ type: "largest-contentful-paint", buffered: true });
      setTimeout(() => { try { po.disconnect(); } catch (e) {} res(v); }, 150);
    } catch (e) { res(null); }
  });
  const rs = performance.getEntriesByType("resource");
  const api = rs.filter((r) => r.name.includes("/api/"));
  const js = rs.filter((r) => r.initiatorType === "script" || r.name.endsWith(".js"));
  const css = rs.filter((r) => r.name.endsWith(".css"));
  const ends = rs.map((r) => r.responseEnd);
  const bytes = (arr) => arr.reduce((s, r) => s + (r.transferSize || r.encodedBodySize || 0), 0);
  const top = rs.filter((r) => r.name.endsWith(".js") || r.name.endsWith(".css"))
    .map((r) => ({ n: r.name.split("/").pop(), b: r.transferSize || r.encodedBodySize || 0 }))
    .sort((a, b) => b.b - a.b).slice(0, 8);
  return {
    path: location.pathname,
    h1At: window.__p ? window.__p.h1At : null,
    pending: window.__p ? window.__p.pending : -1,
    apiPending: window.__p ? window.__p.apiPending : -1,
    ttfb: nav.responseStart || null,
    dcl: nav.domContentLoadedEventEnd || null,
    load: nav.loadEventEnd || null,
    fcp: fcpE ? fcpE.startTime : null,
    lcp: lcp,
    lastResp: ends.length ? Math.max.apply(null, ends) : (nav.responseEnd || null),
    apiCount: api.length,
    apiLast: api.length ? Math.max.apply(null, api.map((r) => r.responseEnd)) : null,
    apiDur: api.length ? Math.max.apply(null, api.map((r) => r.duration)) : null,
    apiBytes: bytes(api),
    jsBytes: bytes(js), jsCount: js.length,
    cssBytes: bytes(css),
    totalTransfer: bytes(rs),
    reqCount: rs.length,
    cls: window.__mb ? window.__mb.cls : null,
    overflow: document.documentElement.scrollWidth - window.innerWidth,
    sw: document.documentElement.scrollWidth, iw: window.innerWidth,
  };
})()
"""

RESET_MB = "window.__mb && window.__mb.reset(); true"
MARK = "window.__mark = performance.now(); window.__mb && window.__mb.reset(); true"

SKIP_TOUR_JS = r"""
(() => {
  const tour = document.querySelector('[data-testid="tour"]');
  if (!tour) return "no-tour";
  const skip = [...tour.querySelectorAll("button")].find((b) => /skip/i.test(b.textContent || ""));
  if (skip) { skip.click(); return "skipped"; }
  return "tour-no-skip";
})()
"""

SCROLL_SWEEP = r"""
(async () => {
  window.__mb.reset();
  const t0 = performance.now();
  const max = () => Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
  const dur = 1000;
  await new Promise((res) => {
    let phase = 0, pt = performance.now();
    const step = (ts) => {
      const p = Math.min(1, (ts - pt) / dur);
      if (phase === 0) {
        window.scrollTo(0, max() * p);
        if (p >= 1) { phase = 1; pt = ts; }
      } else {
        window.scrollTo(0, max() * (1 - p));
        if (p >= 1) { res(); return; }
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
  const f = window.__mb.frames.slice();
  const s = [...f].sort((a, b) => a - b);
  const q = (q) => (s.length ? s[Math.min(s.length - 1, Math.floor(q * s.length))] : 0);
  return {
    elapsed_ms: +(performance.now() - t0).toFixed(1),
    frames: f.length,
    fps: +(f.length / ((performance.now() - t0) / 1000)).toFixed(1),
    p50: +q(0.5).toFixed(1), p95: +q(0.95).toFixed(1), max: +(s[s.length - 1] || 0).toFixed(1),
    over_33: f.filter((x) => x > 33).length,
    over_50: f.filter((x) => x > 50).length,
    long: window.__mb.long.map((x) => ({ d: +x.d.toFixed(1), t: +x.t.toFixed(1) })),
    events: window.__mb.events.map((x) => ({ n: x.name, d: +x.d.toFixed(1) })),
    cls: +window.__mb.cls.toFixed(4),
  };
})()
"""

MB_SUMMARY = r"""
(() => {
  const m = window.__mb; if (!m) return null;
  return {
    long: m.long.map((x) => ({ d: +x.d.toFixed(1), t: +x.t.toFixed(1) })),
    events: m.events.map((x) => ({ n: x.name, d: +x.d.toFixed(1) })),
    cls: +m.cls.toFixed(4),
    frames: m.frames.length,
  };
})()
"""


# ---------------------------------------------------------------- chrome / cdp
def http_json(url: str):
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode() or "null")


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _origin(parsed) -> str:
    """Return the browser-style origin for a parsed URL."""
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port = parsed.port
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    return f"{scheme}://{hostname}{f':{port}' if port and not default_port else ''}"


def validate_target_url(value: str) -> str:
    """Return a safe target URL, refusing every URL-bearing secret channel.

    The report contains target URLs, so redaction after parsing is too easy to
    get wrong.  Targets are deliberately origin/path-only: credentials,
    query strings, and fragments are rejected before they can reach stdout or
    the JSON report.
    """
    try:
        parsed = urlsplit(value)
        has_userinfo = bool(parsed.username or parsed.password)
        hostname = parsed.hostname
    except ValueError:
        has_userinfo = True
        hostname = None
        parsed = None
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or not hostname
        or has_userinfo
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "target must be an http(s) URL without userinfo, query, or fragment"
        )
    try:
        path = parsed.path.rstrip("/")
        _origin(parsed)
    except ValueError:
        raise ValueError(
            "target must be an http(s) URL without userinfo, query, or fragment"
        ) from None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def validate_approved_origin(value: str) -> str:
    """Validate one exact origin supplied by the operator's allowlist."""
    validated = validate_target_url(value)
    parsed = urlsplit(validated)
    if parsed.path or parsed.scheme != "https":
        raise ValueError("approved origins must be exact https origins")
    return _origin(parsed)


def target_origin(value: str) -> str:
    parsed = urlsplit(validate_target_url(value))
    return _origin(parsed)


def _origin_is_allowed(value: str, approved_origins: set[str]) -> bool:
    parsed = urlsplit(value)
    return (parsed.hostname or "").lower() in LOOPBACK_HOSTS or _origin(
        parsed
    ) in approved_origins


def parse_targets(
    s: str, approved_origins: set[str] | None = None
) -> list[tuple[str, str]]:
    approved = approved_origins or set()
    out = []
    labels = set()
    for part in s.split(","):
        label, sep, value = part.partition("=")
        label = label.strip()
        if not sep or not label or label in labels:
            raise ValueError("targets must contain unique label=url pairs")
        labels.add(label)
        target = validate_target_url(value.strip())
        if not _origin_is_allowed(target, approved):
            raise ValueError("target origin is not loopback or explicitly approved")
        out.append((label, target))
    if not out:
        raise ValueError("at least one target is required")
    return out


def route_path(base: str, route: str) -> str:
    prefix = urlsplit(base).path.rstrip("/")
    return f"{prefix}/{route.lstrip('/')}" or "/"


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def verify_browser_ownership(proc: subprocess.Popen, profile: str) -> bool:
    """Prove the CDP process is this run's browser before attaching."""
    if proc.poll() is not None:
        return False
    marker = os.path.join(profile, ".ledger-perf-owner")
    try:
        with open(marker, encoding="utf-8") as fh:
            owner = fh.read().strip()
    except OSError:
        return False
    return bool(owner) and f"--user-data-dir={os.path.realpath(profile)}" in _cmdline(proc.pid)


def _auth_script(token: str, trusted_origin: str) -> str:
    """Inject a pre-provisioned bearer token without persisting or printing it."""
    encoded = json.dumps(token)
    encoded_origin = json.dumps(trusted_origin)
    return r"""
(() => {
  const token = __LEDGER_TOKEN__;
  const trustedOrigin = __LEDGER_ORIGIN__;
  const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
  const parsed = (url) => { try { return new URL(url, location.href); } catch (_) { return null; } };
  const isTrusted = (url) => { const u = parsed(url); return u !== null && u.origin === trustedOrigin; };
  const isApi = (url) => { const u = parsed(url); return u !== null && u.origin === trustedOrigin && u.pathname.includes('/api/'); };
  const methodOf = (input, init) => String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
  const withoutAuthorization = (headers) => {
    const clean = new Headers(headers || undefined);
    clean.delete('Authorization');
    return clean;
  };
  const fetch0 = window.fetch;
  window.fetch = function(input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const requestMethod = methodOf(input, init);
    const u = parsed(url);
    const trusted = isTrusted(url);
    if (isApi(url) && u.pathname.endsWith('/auth/refresh') && requestMethod === 'POST') {
      // AuthContext probes a refresh cookie on every fresh document.  Do not
      // send that mutating request: prove the supplied bearer can read /users/me
      // and synthesize the same response shape.
      return fetch0.call(this, '/api/v1/users/me', {
        method: 'GET', redirect: 'error',
        headers: { Authorization: 'Bearer ' + token },
      }).then(async (response) => {
        if (!response.ok) return new Response('', { status: 401 });
        const user = await response.json();
        return new Response(JSON.stringify({ access_token: token, user }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });
    }
    if (!SAFE_METHODS.has(requestMethod)) {
      return Promise.reject(new TypeError('ledger performance probe blocks unsafe HTTP methods'));
    }
    if (!trusted) {
      // Never forward an application bearer to an unapproved origin, including
      // an origin reached by an API-looking URL or a redirect target.
      return fetch0.call(this, input, { ...(init || {}), headers: withoutAuthorization(init && init.headers || (input && input.headers)), redirect: 'error' });
    }
    if (!isApi(url)) return fetch0.call(this, input, init);
    const headers = new Headers(init && init.headers || (input && input.headers) || undefined);
    if (!headers.has('Authorization')) headers.set('Authorization', 'Bearer ' + token);
    return fetch0.call(this, input, { ...(init || {}), headers, redirect: 'error' });
  };
  const open0 = XMLHttpRequest.prototype.open;
  const setHeader0 = XMLHttpRequest.prototype.setRequestHeader;
  const send0 = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this.__ledgerPerfApi = isApi(url);
    this.__ledgerPerfTrusted = isTrusted(url);
    this.__ledgerPerfMethod = String(method || 'GET').toUpperCase();
    this.setRequestHeader = function(name, value) {
      if (!this.__ledgerPerfTrusted && String(name).toLowerCase() === 'authorization') return;
      return setHeader0.call(this, name, value);
    };
    return open0.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function(...args) {
    if (!SAFE_METHODS.has(this.__ledgerPerfMethod || 'GET')) {
      this.abort();
      return;
    }
    if (this.__ledgerPerfApi) this.setRequestHeader('Authorization', 'Bearer ' + token);
    return send0.apply(this, args);
  };
})();
""".replace("__LEDGER_TOKEN__", encoded).replace("__LEDGER_ORIGIN__", encoded_origin)


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, max_size=256 * 1024 * 1024, timeout=120)
        self._id = 0

    def cmd(self, method: str, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval_js(self, expr: str, await_promise: bool = False, timeout: float = 30.0):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({
            "id": mid, "method": "Runtime.evaluate",
            "params": {"expression": expr, "awaitPromise": await_promise,
                       "returnByValue": True, "timeout": timeout * 1000},
        }))
        end = time.time() + timeout + 5
        while time.time() < end:
            self.ws.settimeout(max(0.1, end - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"eval: {msg['error']}")
                res = msg.get("result", {})
                if "exceptionDetails" in res:
                    det = res["exceptionDetails"]
                    raise RuntimeError(f"eval-exception: {det.get('exception', {}).get('description', det)}")
                return res.get("result", {}).get("value")
        raise TimeoutError("Runtime.evaluate timed out")


class Probe:
    def __init__(self, auth_token: str, approved_origins: set[str] | None = None):
        if not auth_token:
            raise ValueError("a pre-provisioned read-only auth token is required")
        self.auth_token = auth_token
        self.approved_origins = approved_origins or set()
        self.auth_origin = None
        self.auth_script_id = None
        self.profile = tempfile.mkdtemp(prefix="ledger-perf-", dir=SCRATCH)
        self._owner = secrets.token_hex(16)
        with open(os.path.join(self.profile, ".ledger-perf-owner"), "w", encoding="utf-8") as fh:
            fh.write(self._owner)
        os.chmod(self.profile, 0o700)
        cmd = [
            "chromium",
            "--remote-debugging-port=0",
            f"--user-data-dir={os.path.realpath(self.profile)}",
            "--no-sandbox", "--disable-dev-shm-usage",
            "--remote-allow-origins=*", "--no-first-run",
            "--no-default-browser-check", "--hide-scrollbars",
            "--headless=new", "about:blank",
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws_url = None
        for _ in range(60):
            time.sleep(0.5)
            try:
                with open(os.path.join(self.profile, "DevToolsActivePort"), encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
                self.port = int(lines[0])
                if not verify_browser_ownership(self.proc, self.profile):
                    raise RuntimeError("refusing to attach to an unowned browser")
                version = http_json(f"http://127.0.0.1:{self.port}/json/version")
                if not str(version.get("webSocketDebuggerUrl", "")).startswith(
                    f"ws://127.0.0.1:{self.port}/"
                ):
                    raise RuntimeError("browser debugging endpoint is not loopback-owned")
                pages = http_json(f"http://127.0.0.1:{self.port}/json")
                pages = [p for p in pages if p.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                continue
        if not ws_url:
            self.close()
            raise RuntimeError("chrome did not come up")
        self.cdp = CDP(ws_url)
        self.cdp.cmd("Page.enable")
        self.cdp.cmd("Runtime.enable")
        self.cdp.cmd("Network.enable")
        self.cdp.cmd("Log.enable")
        self.cdp.cmd("Page.bringToFront")
        self.cdp.cmd("Page.addScriptToEvaluateOnNewDocument", source=INSTRUMENT)

    def set_target(self, base: str) -> None:
        """Install auth for exactly one approved origin before navigation."""
        validated = validate_target_url(base)
        origin = target_origin(validated)
        if not _origin_is_allowed(validated, self.approved_origins):
            raise ValueError("target origin is not loopback or explicitly approved")
        if origin == self.auth_origin:
            return
        if self.auth_script_id is not None:
            self.cdp.cmd(
                "Page.removeScriptToEvaluateOnNewDocument",
                identifier=self.auth_script_id,
            )
        result = self.cdp.cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            source=_auth_script(self.auth_token, origin),
        )
        self.auth_script_id = result.get("identifier")
        self.auth_origin = origin

    def close(self):
        if not getattr(self, "profile", None):
            return
        try:
            if verify_browser_ownership(self.proc, self.profile):
                self.proc.terminate()
                self.proc.wait(timeout=10)
        except Exception:
            try:
                if verify_browser_ownership(self.proc, self.profile):
                    self.proc.kill()
            except Exception:
                pass
        marker = os.path.join(self.profile, ".ledger-perf-owner")
        try:
            with open(marker, encoding="utf-8") as fh:
                owned = fh.read().strip() == self._owner
        except OSError:
            owned = False
        if owned:
            shutil.rmtree(self.profile, ignore_errors=True)
        self.profile = ""

    # ------------------------------------------------------------ plumbing
    def set_viewport(self, vp: str, mobile: bool):
        w, h = (int(x) for x in vp.split("x"))
        dpr = 3 if mobile else 1
        self.cdp.cmd("Emulation.setDeviceMetricsOverride", width=w, height=h,
                     deviceScaleFactor=dpr, mobile=mobile)

    def eval_ok(self, expr):
        try:
            return self.cdp.eval_js(expr, await_promise="Promise" in expr or "async" in expr)
        except Exception:
            return None

    def goto(self, url: str, timeout: float = 30.0):
        try:
            res = self.cdp.cmd("Page.navigate", url=url)
            if res.get("errorText"):
                raise RuntimeError(f"navigate: {res['errorText']}")
        except RuntimeError as exc:
            if "Execution context" in str(exc) or "context destroyed" in str(exc):
                pass  # navigation already in flight from a previous nav
            else:
                raise
        end = time.time() + timeout
        while time.time() < end:
            try:
                state = self.cdp.eval_js("document.readyState", timeout=5)
                if state == "complete":
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def wait_ready(self, label: str, want_path: str, timeout: float = 25.0, quiet_ms: int = 300):
        """Wait until the page's own <h1> equals `label` and no request is in
        flight on two samples >= quiet_ms apart. Returns (latency_from_nav, meta)."""
        start = time.time()
        first_ok = None
        last_path = None
        trace = []
        debug = bool(os.environ.get("PROBE_DEBUG"))
        while time.time() - start < timeout:
            try:
                st = self.cdp.eval_js(
                    "(()=>{const h=document.querySelector('h1');"
                    "return JSON.stringify({p:location.pathname,h:h?(h.textContent||'').trim():null,"
                    "pend:window.__p?window.__p.pending:-1,ap:window.__p?window.__p.apiPending:-1,"
                    "now:performance.now(),"
                    "h1At:window.__p?window.__p.h1At:null});})()",
                    timeout=5)
                st = json.loads(st)
            except Exception:
                time.sleep(0.05)
                continue
            last_path = st["p"]
            if debug:
                trace.append({"t": round(st["now"], 0), "h": st["h"], "pend": st["pend"],
                              "ap": st["ap"]})
            if st["p"] in {"/login", "/register"}:
                return None, {"fail": "auth_redirect", "path": st["p"]}
            if st["p"] != want_path:
                return None, {"fail": "route_mismatch", "expected_path": want_path, "path": st["p"]}
            ok = st["h"] == label and st["pend"] == 0 and st["ap"] == 0
            if ok:
                now = time.time()
                if first_ok is None:
                    first_ok = now
                    first_page = st["now"]
                elif (now - first_ok) * 1000 >= quiet_ms:
                    return (now - start) * 1000, {
                        "h1At": st["h1At"], "path": st["p"],
                        "firstPage": round(first_page, 1), "stablePage": round(st["now"], 1),
                        **({"trace": trace} if debug else {}),
                    }
            else:
                first_ok = None
            time.sleep(0.05)
        return None, {"fail": "timeout", "path": last_path}

    def snapshot(self):
        return self.cdp.eval_js(SNAPSHOT, await_promise=True, timeout=30)

    # ------------------------------------------------------------ phases
    def cold_target(self, label: str, base: str, viewport: str, mobile: bool, runs: int):
        self.set_viewport(viewport, mobile)
        print(f"[cold/{label}] establish pre-provisioned session {base}", flush=True)
        if not self.prepare_session(base):
            return {"error": "pre_provisioned_auth_failed", "base": base}
        # true-cold: no SW, no HTTP cache
        self.eval_ok(
            "(async()=>{try{const r=await navigator.serviceWorker.getRegistrations();"
            "for(const x of r) await x.unregister();}catch(e){}"
            "try{const k=await caches.keys();for(const x of k) await caches.delete(x);}catch(e){}"
            "return 'ok';})()")
        self.cdp.cmd("Network.setCacheDisabled", cacheDisabled=True)
        time.sleep(0.5)
        out = {"label": label, "base": base, "viewport": viewport, "runs": []}
        for route in routes:
            for i in range(runs):
                url = f"{base}/{route}"
                rec = {"route": route, "run": i + 1, "url": url}
                t_nav = time.time()
                self.goto(url, timeout=30)
                rec["nav_wall_ms"] = round((time.time() - t_nav) * 1000, 1)
                lat, meta = self.wait_ready(H1_LABEL[route], route_path(base, route))
                if lat is None and meta.get("fail") == "auth_redirect":
                    print(f"  [reauth] session dropped on /{route}, re-establishing", flush=True)
                    if self.prepare_session(base):
                        self.goto(url, timeout=30)
                        lat, retry_meta = self.wait_ready(H1_LABEL[route], route_path(base, route))
                        retry_meta["retried"] = True
                        meta = retry_meta
                    else:
                        meta = {"fail": "relogin_failed", "retried": True}
                rec.update(meta)
                rec["ready_ms"] = round(lat, 1) if lat is not None else None
                time.sleep(0.4)  # let trailing requests settle for the snapshot
                try:
                    rec["snap"] = self.snapshot()
                except Exception as exc:
                    rec["snap_error"] = str(exc)
                out["runs"].append(rec)
                print(f"  {route} run{i+1}: ready={rec.get('ready_ms')} "
                      f"fail={meta.get('fail','-')} h1At={meta.get('h1At')}", flush=True)
        self.cdp.cmd("Network.setCacheDisabled", cacheDisabled=False)
        return out

    def prepare_session(self, base: str, timeout: float = 40.0) -> bool:
        self.set_target(base)
        self.goto(base, timeout=30)
        end = time.time() + timeout
        while time.time() < end:
            try:
                path = self.cdp.eval_js("location.pathname", timeout=5)
                if path and path not in ("/login", "/register", "/", ""):
                    time.sleep(0.3)
                    self.eval_ok(SKIP_TOUR_JS)
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def warm(self, base: str, route: str, label: str):
        """Warm navigation (cache + SW on) to a route; returns ready latency."""
        self.goto(f"{base}/{route}", timeout=30)
        lat, meta = self.wait_ready(label, route_path(base, route))
        return lat, meta

    def warm_retry(self, base: str, route: str, label: str, tries: int = 3):
        """warm(), re-establishing the demo session if it drops mid-phase."""
        lat, meta = self.warm(base, route, label)
        for _ in range(tries):
            if lat is not None or meta.get("fail") != "auth_redirect":
                break
            print(f"  [reauth] session dropped on /{route}, re-establishing", flush=True)
            if not self.prepare_session(base):
                meta = {"fail": "relogin_failed"}
                break
            lat, meta = self.warm(base, route, label)
        return lat, meta

    def click(self, selector_js: str):
        """selector_js evaluates to an element or null; dispatch a real click at
        its centre. Returns the client rect used or None."""
        rect = self.eval_ok(
            f"(()=>{{const el={selector_js}; if(!el) return null;"
            "const r=el.getBoundingClientRect();"
            "return {x:r.x+r.width/2, y:r.y+r.height/2, w:r.width, h:r.height};})()")
        if not rect:
            return None
        x, y = rect["x"], rect["y"]
        for t in ("mousePressed", "mouseReleased"):
            self.cdp.cmd("Input.dispatchMouseEvent", type=t, x=x, y=y,
                         button="left", clickCount=1,
                         buttons=1 if t == "mousePressed" else 0)
        return rect

    def wait_cond(self, js_expr: str, timeout: float = 8.0, quiet_ms: int = 0):
        """Poll js_expr (returns truthy value). Returns elapsed ms since mark,
        reading window.__mark for a page-clock origin, or wall ms fallback."""
        start = time.time()
        first_ok = None
        while time.time() - start < timeout:
            try:
                v = self.cdp.eval_js(
                    f"(()=>{{const r=({js_expr}); return JSON.stringify({{v:!!r, now:performance.now(), mark:window.__mark||0}});}})()",
                    timeout=5)
                v = json.loads(v)
            except Exception:
                time.sleep(0.03)
                continue
            if v["v"]:
                now = time.time()
                if first_ok is None:
                    first_ok = now
                elif (now - first_ok) * 1000 >= quiet_ms:
                    return round(v["now"] - v["mark"], 1) if v["mark"] else round((now - start) * 1000, 1)
            else:
                first_ok = None
            time.sleep(0.03)
        return None


def frame_stats(frames: list) -> dict:
    s = sorted(frames)
    if not s:
        return {}
    q = lambda p: s[min(len(s) - 1, int(p * len(s)))]  # noqa: E731
    return {"count": len(s), "p50": round(q(0.5), 1), "p95": round(q(0.95), 1),
            "max": round(s[-1], 1), "over_33": sum(1 for x in s if x > 33),
            "over_50": sum(1 for x in s if x > 50)}


def long_stats(longs: list) -> dict:
    if not longs:
        return {"count": 0, "total_ms": 0, "max_ms": 0, "over_50": 0}
    ds = [x["d"] for x in longs]
    return {"count": len(ds), "total_ms": round(sum(ds), 1), "max_ms": round(max(ds), 1),
            "over_50": sum(1 for d in ds if d > 50)}


# ---------------------------------------------------------------- budget phase
def run_budget(probe: Probe, label: str, base: str, runs: int, full: bool):
    """full=True -> whole suite (tabs/More/Add); full=False -> scroll sweeps only."""
    probe.set_viewport("390x844", mobile=True)
    print(f"[budget/{label}] establish pre-provisioned session {base}", flush=True)
    if not probe.prepare_session(base):
        return {"error": "pre_provisioned_auth_failed", "base": base}
    probe.cdp.cmd("Network.setCacheDisabled", cacheDisabled=False)  # warm world
    out = {"label": label, "base": base, "scroll": [], "interactions": [], "overflow": []}

    for route in ["dashboard", "transactions", "reports"]:
        for i in range(runs):
            lat, meta = probe.warm_retry(base, route, H1_LABEL[route])
            if lat is None:
                out["scroll"].append({"route": route, "run": i + 1, "fail": meta})
                continue
            ov = probe.eval_ok(
                "JSON.stringify({sw:document.documentElement.scrollWidth,iw:window.innerWidth})")
            out["overflow"].append({"route": route, **json.loads(ov or "{}")})
            sweep = probe.eval_ok(SCROLL_SWEEP)
            if isinstance(sweep, str):
                sweep = json.loads(sweep)
            rec = {"route": route, "run": i + 1, "sweep": sweep}
            out["scroll"].append(rec)
            print(f"  scroll {route} r{i+1}: p95={sweep and sweep.get('p95')} "
                  f"long={sweep and long_stats(sweep.get('long', []))}", flush=True)

    if not full:
        return out

    # ---- bottom tab switches (SPA, warm)
    tab = lambda p: ('[...document.querySelectorAll(\'[data-testid="mobile-tab-bar"] a\')]'
                     '.find(a=>a.getAttribute("href")===' + json.dumps(p) + ')')
    tabs = [("Transactions", tab("/transactions")),
            ("Investments", tab("/investments")),
            ("Dashboard", tab("/dashboard"))]
    lat, meta = probe.warm_retry(base, "dashboard", "Dashboard")
    for i in range(runs):
        for label_h1, sel in tabs:
            probe.eval_ok(MARK)
            rect = probe.click(sel)
            ms = probe.wait_cond(
                f"document.querySelector('h1') && document.querySelector('h1').textContent.trim()==={json.dumps(label_h1)}",
                timeout=10, quiet_ms=250)
            time.sleep(0.3)
            mb = probe.eval_ok(MB_SUMMARY)
            if isinstance(mb, str):
                mb = json.loads(mb)
            out["interactions"].append({
                "kind": "tab_switch", "to": label_h1, "run": i + 1,
                "click_ms": ms, "hit": bool(rect), **long_stats(mb.get("long", [])),
                "events_p95": max([e["d"] for e in mb.get("events", [])], default=None),
            })
            print(f"  tab->{label_h1} r{i+1}: {ms}ms", flush=True)

    # ---- More panel
    for i in range(max(1, runs)):
        probe.eval_ok(MARK)
        rect = probe.click('document.querySelector(\'[data-testid="tab-more"]\')')
        ms = probe.wait_cond(
            "(()=>{const el=document.querySelector('[data-testid=\"app-nav\"]');"
            "return el && el.getBoundingClientRect().x >= -1;})()", timeout=6, quiet_ms=150)
        time.sleep(0.2)
        mb = probe.eval_ok(MB_SUMMARY)
        if isinstance(mb, str):
            mb = json.loads(mb)
        open_rec = {"kind": "more_open", "run": i + 1, "click_ms": ms, "hit": bool(rect),
                    **long_stats(mb.get("long", []))}
        probe.eval_ok(MARK)
        probe.cdp.cmd("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)
        probe.cdp.cmd("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)
        ms2 = probe.wait_cond(
            "(()=>{const el=document.querySelector('[data-testid=\"app-nav\"]');"
            "return !el || el.getBoundingClientRect().x <= -240;})()", timeout=6, quiet_ms=150)
        open_rec["close_ms"] = ms2
        out["interactions"].append(open_rec)
        print(f"  more open/close r{i+1}: {ms}/{ms2}ms", flush=True)

    # ---- Add editor modal
    lat, meta = probe.warm_retry(base, "dashboard", "Dashboard")
    for i in range(max(1, runs)):
        probe.eval_ok(MARK)
        rect = probe.click('document.querySelector(\'[data-testid="tab-add"]\')')
        ms = probe.wait_cond("!!document.querySelector('[role=\"dialog\"]')", timeout=8, quiet_ms=250)
        time.sleep(0.3)
        mb = probe.eval_ok(MB_SUMMARY)
        if isinstance(mb, str):
            mb = json.loads(mb)
        rec = {"kind": "add_open", "run": i + 1, "click_ms": ms, "hit": bool(rect),
               **long_stats(mb.get("long", [])),
               "events_p95": max([e["d"] for e in mb.get("events", [])], default=None)}
        probe.eval_ok(MARK)
        probe.cdp.cmd("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)
        probe.cdp.cmd("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)
        rec["close_ms"] = probe.wait_cond("!document.querySelector('[role=\"dialog\"]')", timeout=6, quiet_ms=150)
        out["interactions"].append(rec)
        print(f"  add open/close r{i+1}: {ms}/{rec['close_ms']}ms", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["cold", "budget", "login"])
    ap.add_argument("--targets", required=True, help="label=url,label=url")
    ap.add_argument("--viewport", default="1440x900")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--scroll-only", action="store_true", help="budget: sweeps only")
    ap.add_argument("--routes", default=",".join(ROUTES))
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        help="exact remote https origin allowed for this run (repeatable)",
    )
    ap.add_argument(
        "--auth-token-env",
        help="name of an environment variable containing a pre-provisioned read-only bearer token",
    )
    args = ap.parse_args()

    try:
        approved_origins = {validate_approved_origin(value) for value in args.allow_origin}
        targets = parse_targets(args.targets, approved_origins)
    except ValueError:
        print(
            "refusing target: use loopback or an exact approved https origin; "
            "targets cannot contain userinfo, query, or fragment",
            file=sys.stderr,
        )
        return 2
    if not args.auth_token_env:
        print("refusing protected target: pass --auth-token-env with pre-provisioned read-only auth", file=sys.stderr)
        return 2
    auth_token = os.environ.get(args.auth_token_env)
    if not auth_token:
        print("refusing protected target: the requested auth-token environment variable is empty", file=sys.stderr)
        return 2
    results = {"phase": args.phase, "viewport": args.viewport if args.phase == "cold" else "390x844",
               "runs": args.runs, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "auth": "pre_provisioned_read_only", "targets": {}}
    probe = Probe(auth_token, approved_origins)
    try:
        for label, base in targets:
            if args.phase == "cold":
                routes[:] = args.routes.split(",")
                res = probe.cold_target(label, base, args.viewport,
                                        args.viewport.startswith("390"), args.runs)
            elif args.phase == "budget":
                res = run_budget(probe, label, base, args.runs, full=not args.scroll_only)
            else:
                probe.set_viewport(args.viewport, False)
                res = {"session_ready": probe.prepare_session(base)}
            results["targets"][label] = res
            with open(args.out, "w") as f:
                json.dump(results, f, indent=1)
            print(f"== {label} done -> {args.out}", flush=True)
    finally:
        probe.close()
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

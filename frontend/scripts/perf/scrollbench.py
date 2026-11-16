#!/usr/bin/env python3
"""M101 scroll-perf A/B bench (fin_tracker).

Measures scripted-scroll frame health on a Pixel-ish viewport (412x915,
DPR 2.6) against a locally-built bundle served by scripts/local/serve_proxy.py
with /api proxied to prod (demo tenant).

Two browsers, one identical injected workload:

* chromium — driven over CDP (device metrics + CPU throttle emulation),
  optional trace capture for repaint attribution.
* firefox   — driven over WebDriver (system Firefox ESR), profile pref
  `layout.css.devPixelsPerPx` stands in for the device pixel ratio.

The workload itself is JS-driven and byte-identical in both browsers so the
A/B deltas are comparable: a there-and-back sweep of the document at a fixed
target speed, recording every rAF frame delta plus longtask entries.

Variants are pure CSS overrides injected after load (unlayered rules beat the
`@layer components` rules in index.css), so one build can be A/B'd without
rebuilding. The winning variant is then re-implemented in source.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request

import websocket

WS = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URL = "http://127.0.0.1:4173"
SCRATCH = os.environ.get("TMPDIR", "/tmp")

# ---------------------------------------------------------------- variants
# Each variant is a list of CSS rules appended in an unlayered <style> tag.
VARIANTS: dict[str, list[str]] = {
    "base": [],
    # (a) .glass / .glass-strong backdrop-filter -> opaque token background.
    "a_glass": [
        ".glass{backdrop-filter:none!important;-webkit-backdrop-filter:none!important;"
        "background:rgb(var(--color-surface-raised))!important}",
        ".glass-strong{backdrop-filter:none!important;-webkit-backdrop-filter:none!important;"
        "background:rgb(var(--color-surface-raised))!important}",
    ],
    # (a-split) isolate WHICH half of .glass costs: the blur or the alpha.
    "a_blur_only": [
        ".glass{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}",
        ".glass-strong{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}",
    ],
    "a_bg_only": [
        ".glass{background:rgb(var(--color-surface-raised))!important}",
        ".glass-strong{background:rgb(var(--color-surface-raised))!important}",
    ],
    # (m) the shipping candidate: NO backdrop-filter at mobile widths,
    # alpha untouched (measured free), header forced opaque for hierarchy.
    "m_media": [
        "@media (max-width: 767.98px){"
        "*{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}}"
        "@media (max-width: 767.98px){"
        "header.fixed.inset-x-0.top-0{background:rgb(var(--color-surface-raised))!important}}"
    ],
    # (m2) same, but alpha left alone entirely (isolate the header's role).
    "m_media_keepalpha": [
        "@media (max-width: 767.98px){"
        "*{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}}"
    ],
    # (b) AppShell fixed mobile header + off-canvas drawer: blur -> solid.
    "b_chrome": [
        "header.fixed.inset-x-0.top-0.backdrop-blur-xl{backdrop-filter:none!important;"
        "-webkit-backdrop-filter:none!important;background:rgb(var(--color-surface-raised))!important}",
        "#app-nav.backdrop-blur-xl{backdrop-filter:none!important;"
        "-webkit-backdrop-filter:none!important;background:rgb(var(--color-surface-raised))!important}",
    ],
    # (b-lite) mobile header only (the always-visible fixed surface).
    "b_header": [
        "header.fixed.inset-x-0.top-0.backdrop-blur-xl{backdrop-filter:none!important;"
        "-webkit-backdrop-filter:none!important;background:rgb(var(--color-surface-raised))!important}",
    ],
    # (c) translucent surfaces -> opaque (alpha was only there for the blur).
    "c_alpha": [
        "#app-nav.bg-surface-raised\\/70{background:rgb(var(--color-surface-raised))!important}",
        "header.fixed.bg-surface-raised\\/80{background:rgb(var(--color-surface-raised))!important}",
    ],
    # every remaining backdrop-filter in the tree (screening upper bound).
    "z_all": [
        "*{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}",
    ],
}

# ---------------------------------------------------------------- workload
# Identical in both browsers. Sweeps top->bottom->top at SPEED px/s and
# records every rAF delta; `longtask` entries are collected when the engine
# supports them (Chromium does, Firefox does not).
WORKLOAD_JS = r"""
(() => {
  const SPEED = 1400;           // px per second
  const MAX_MS = 45000;         // hard stop so a runaway can't hang the run
  const frames = [];
  const longTasks = [];
  let obs = null;
  try {
    obs = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) longTasks.push(e.duration);
    });
    obs.observe({ entryTypes: ["longtask"] });
  } catch (_) { /* unsupported */ }

  const maxY = () =>
    Math.max(0, document.documentElement.scrollHeight - window.innerHeight);

  const sweep = () => new Promise((resolve) => {
    let y = window.scrollY;
    let dir = y >= maxY() ? -1 : 1;
    let prev = performance.now();
    const t0 = prev;
    const step = (now) => {
      const dt = now - prev;
      prev = now;
      frames.push(dt);
      y += dir * SPEED * (dt / 1000);
      if (y >= maxY()) { y = maxY(); dir = -1; }
      if (y <= 0 && dir === -1) {
        window.scrollTo(0, 0);
        resolve({ reason: "swept", elapsed: performance.now() - t0 });
        return;
      }
      window.scrollTo(0, y);
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    setTimeout(() => {
      window.scrollTo(0, 0);
      resolve({ reason: "timeout", elapsed: performance.now() - t0 });
    }, MAX_MS);
  });

  const pct = (arr, p) => {
    if (!arr.length) return 0;
    const s = [...arr].sort((a, b) => a - b);
    return s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))];
  };

  return sweep().then((res) => {
    if (obs) obs.disconnect();
    const sorted = [...frames].sort((a, b) => a - b);
    const out = {
      reason: res.reason,
      elapsed_ms: +res.elapsed.toFixed(1),
      frames: frames.length,
      long_frames_50: frames.filter((f) => f > 50).length,
      long_frames_33: frames.filter((f) => f > 33).length,
      long_frames_100: frames.filter((f) => f > 100).length,
      frame_p50: +pct(sorted, 50).toFixed(1),
      frame_p95: +pct(sorted, 95).toFixed(1),
      frame_p99: +pct(sorted, 99).toFixed(1),
      frame_max: +Math.max(0, ...frames).toFixed(1),
      longtask_count: longTasks.length,
      longtask_total_ms: +longTasks.reduce((a, b) => a + b, 0).toFixed(1),
      scroll_height: document.documentElement.scrollHeight,
      viewport_h: window.innerHeight,
      dpr: window.devicePixelRatio,
      variant_style: !!document.getElementById("__m101"),
    };
    window.__m101_result = out;
    return out;
  });
})()
"""

INJECT_STYLE_JS = """
(css) => {
  const prev = document.getElementById("__m101");
  if (prev) prev.remove();
  const el = document.createElement("style");
  el.id = "__m101";
  el.textContent = css;
  document.head.appendChild(el);
  return true;
}
"""

# Verifies a variant actually changed what it claims to change.
EFFECT_JS = r"""
(() => {
  const cs = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const s = getComputedStyle(el);
    return { backdrop: s.backdropFilter || s.webkitBackdropFilter || "none",
             bg: s.backgroundColor };
  };
  const blurred = [...document.querySelectorAll("*")].filter((el) => {
    const b = getComputedStyle(el).backdropFilter;
    return b && b !== "none";
  });
  const glass = document.querySelector(".glass");
  return {
    header: cs("header.fixed"),
    nav: cs("#app-nav"),
    glass: glass
      ? { backdrop: getComputedStyle(glass).backdropFilter,
          bg: getComputedStyle(glass).backgroundColor }
      : null,
    blurred_elements: blurred.length,
    blurred_area: blurred.reduce((a, el) => {
      const r = el.getBoundingClientRect();
      const vh = window.innerHeight;
      const vis = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
      return a + Math.max(0, Math.min(r.width, window.innerWidth)) * vis;
    }, 0) | 0,
    style_present: !!document.getElementById("__m101"),
  };
})()
"""

LOGIN_JS = r"""
(() => {
  const btn = [...document.querySelectorAll("button")].find((b) =>
    /explore the demo/i.test(b.textContent || ""));
  if (btn) { btn.click(); return "clicked-demo"; }
  return "no-demo-button";
})()
"""


# ---------------------------------------------------------------- helpers
def http_json(url: str, method: str = "GET", payload: bytes | None = None):
    req = urllib.request.Request(url, method=method, data=payload)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode() or "null")


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
            # events are ignored here (trace events are collected separately)

    def drain(self, timeout: float = 0.0):
        """Read and drop pending events for `timeout` seconds."""
        end = time.time() + timeout
        while time.time() < end:
            self.ws.settimeout(max(0.05, end - time.time()))
            try:
                self.ws.recv()
            except Exception:
                return


class TraceCollector:
    """Collects Tracing.* events from a CDP connection while a run executes."""

    def __init__(self, cdp: CDP):
        self.cdp = cdp
        self.events: list[dict] = []
        self._collecting = False

    def start(self, categories: str):
        self.events = []
        self._collecting = True
        self.cdp.cmd(
            "Tracing.start",
            categories=categories,
            options="record-as-much-as-possible",
            transferMode="ReportEvents",
        )

    def pump(self, seconds: float):
        end = time.time() + seconds
        ws = self.cdp.ws
        while time.time() < end and self._collecting:
            ws.settimeout(max(0.05, end - time.time()))
            try:
                raw = ws.recv()
            except Exception:
                continue
            msg = json.loads(raw)
            if msg.get("method") == "Tracing.dataCollected":
                self.events.extend(msg.get("params", {}).get("value", []))

    def stop(self) -> list[dict]:
        self.cdp.cmd("Tracing.end")
        end = time.time() + 10
        ws = self.cdp.ws
        while time.time() < end:
            ws.settimeout(max(0.2, end - time.time()))
            try:
                raw = ws.recv()
            except Exception:
                break
            msg = json.loads(raw)
            if msg.get("method") == "Tracing.dataCollected":
                self.events.extend(msg.get("params", {}).get("value", []))
            elif msg.get("method") == "Tracing.tracingComplete":
                self._collecting = False
                break
        return self.events


def frame_stats(events: list[dict]) -> dict:
    """Compositor-side frame accounting.

    Main-thread rAF cadence can stay at 60 Hz while the compositor/raster
    thread falls behind (that is exactly what an Android device does), so the
    trace's own draw events are the honest dropped-frame signal.
    """
    names = (
        "Display::DrawAndSwap",
        "DirectRenderer::DrawFrame",
        "DrawFrame",
        "ProxyMain::BeginMainFrame",
    )
    ts_by: dict[str, list[float]] = {}
    for ev in events:
        n = ev.get("name")
        if n in names and ev.get("ts") is not None:
            ts_by.setdefault(n, []).append(ev["ts"] + ev.get("dur", 0))
    out: dict[str, dict] = {}
    for n, ts in ts_by.items():
        ts.sort()
        gaps = [(b - a) / 1000.0 for a, b in zip(ts, ts[1:], strict=False)]
        span = (ts[-1] - ts[0]) / 1000.0 if len(ts) > 1 else 0.0
        out[n] = {
            "count": len(ts),
            "span_ms": round(span, 1),
            "gap_p95": round(sorted(gaps)[int(0.95 * len(gaps))] if gaps else 0, 1),
            "gaps_over_33": sum(1 for g in gaps if g > 33),
            "gaps_over_50": sum(1 for g in gaps if g > 50),
            "max_gap": round(max(gaps), 1) if gaps else 0,
        }
    return out


def summarise_trace(events: list[dict]) -> dict:
    """Aggregate raster/paint/style/layout work from a trace."""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for ev in events:
        name = ev.get("name")
        if not name:
            continue
        dur = ev.get("dur")
        if dur is None:
            continue
        ms = dur / 1000.0
        totals[name] = totals.get(name, 0.0) + ms
        counts[name] = counts.get(name, 0) + 1
    keep = [
        "RasterTask",
        "Paint",
        "PrePaint",
        "Layout",
        "UpdateLayoutTree",
        "Style",
        "CompositeLayers",
        "DrawFrame",
        "Layerize",
        "ActivateLayerTree",
        "Commit",
        "Rasterize",
    ]
    out = {k: round(totals[k], 1) for k in keep if k in totals}
    out_counts = {k: counts[k] for k in keep if k in totals}
    top = sorted(
        ((n, round(v, 1)) for n, v in totals.items() if v >= 1.0),
        key=lambda kv: -kv[1],
    )[:15]
    return {
        "totals_ms": out,
        "counts": out_counts,
        "top_by_ms": top,
        "frames": frame_stats(events),
    }


def paint_attribution(events: list[dict]) -> list[tuple[str, int, float]]:
    """Which elements actually painted, from devtools.timeline Paint events."""
    nodes: dict[int, str] = {}
    agg: dict[int, float] = {}
    cnt: dict[int, int] = {}
    for ev in events:
        if ev.get("name") != "Paint":
            continue
        data = ev.get("args", {}).get("data", {})
        node_id = data.get("nodeId") or data.get("backendNodeId")
        node_name = data.get("nodeName") or "?"
        if node_id:
            nodes.setdefault(node_id, node_name)
            agg[node_id] = agg.get(node_id, 0.0) + ev.get("dur", 0) / 1000.0
            cnt[node_id] = cnt.get(node_id, 0) + 1
    rows = []
    for nid, ms in sorted(agg.items(), key=lambda kv: -kv[1])[:20]:
        rows.append((nodes.get(nid, "?"), cnt[nid], round(ms, 1)))
    return rows


# ---------------------------------------------------------------- chromium
class ChromiumBench:
    def __init__(
        self,
        base_url: str,
        throttle: float,
        headless: bool = True,
        vw: int = 412,
        vh: int = 915,
        dpr: float = 2.6,
    ):
        self.base_url = base_url
        self.throttle = throttle
        self.vw, self.vh, self.dpr = vw, vh, dpr
        self.port = 9444
        self.prof = os.path.join(SCRATCH, "m101-chrome-prof")
        shutil.rmtree(self.prof, ignore_errors=True)
        cmd = [
            "chromium",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.prof}",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
        ]
        if headless:
            cmd.append("--headless=new")
        cmd.append("about:blank")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.ws_url = self._wait_target()
        self.cdp = CDP(self.ws_url)
        self.cdp.cmd("Page.enable")
        self.cdp.cmd("Runtime.enable")
        self.cdp.cmd("Log.enable")
        self.cdp.cmd(
            "Emulation.setDeviceMetricsOverride",
            width=self.vw,
            height=self.vh,
            deviceScaleFactor=self.dpr,
            mobile=self.vw < 768,
        )
        if throttle and throttle != 1:
            self.cdp.cmd("Emulation.setCPUThrottlingRate", rate=throttle)

    def _wait_target(self) -> str:
        for _ in range(100):
            try:
                targets = http_json(f"http://127.0.0.1:{self.port}/json/list")
                for t in targets:
                    if t.get("type") == "page":
                        return t["webSocketDebuggerUrl"]
            except Exception:
                pass
            time.sleep(0.2)
        raise RuntimeError("chromium did not expose a page target")

    def goto(self, path: str):
        url = self.base_url + path
        self.cdp.cmd("Page.navigate", url=url)
        for _ in range(200):
            http_json(f"http://127.0.0.1:{self.port}/json/list")
            # cheap liveness: wait for the document to exist
            try:
                res = self.cdp.cmd(
                    "Runtime.evaluate",
                    expression="document.readyState",
                    returnByValue=True,
                )
                if res.get("result", {}).get("value") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.1)
        time.sleep(1.2)

    def evaluate(self, expression: str, await_promise: bool = False):
        res = self.cdp.cmd(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=await_promise,
        )
        if "exceptionDetails" in res:
            raise RuntimeError(json.dumps(res["exceptionDetails"])[:800])
        return res["result"].get("value")

    def apply_variant(self, name: str):
        css = "\n".join(VARIANTS[name])
        if not css:
            self.evaluate(
                "(() => { const p = document.getElementById('__m101');"
                " if (p) p.remove(); return true; })()"
            )
            return
        self.evaluate(f"({INJECT_STYLE_JS})({json.dumps(css)})")

    def wait_settled(self, selector: str | None = None):
        deadline = time.time() + 20
        while time.time() < deadline:
            ready = self.evaluate("document.readyState === 'complete'")
            if ready:
                break
            time.sleep(0.2)
        time.sleep(1.5)

    def run_workload(self, trace: bool = True):
        collector = None
        if trace:
            self.cdp.cmd("LayerTree.enable")
            collector = TraceCollector(self.cdp)
            collector.start(
                "devtools.timeline,disabled-by-default-devtools.timeline,"
                "blink.user_timing,raster,cc,viz,skia,gpu"
            )
        result = self.evaluate(WORKLOAD_JS, await_promise=True)
        events = collector.stop() if collector else []
        layers = self.layer_report() if trace else []
        return result, events, layers

    def layer_report(self, top: int = 25) -> list[list]:
        """Which composited layers actually painted, after the sweep."""
        try:
            res = self.cdp.cmd("LayerTree.getLayers")
        except Exception as exc:  # pragma: no cover - diagnostics
            return [[f"layer-query-failed: {exc}", 0, 0, 0]]
        rows = []
        for layer in res.get("layers", []):
            if not layer.get("drawsContent"):
                continue
            bid = layer.get("backendNodeId")
            name = "?"
            if bid:
                try:
                    obj = self.cdp.cmd("DOM.resolveNode", backendNodeId=bid)
                    desc = self.cdp.cmd(
                        "Runtime.callFunctionOn",
                        objectId=obj["object"]["objectId"],
                        functionDeclaration=(
                            "function(){const e=this;"
                            " return (e.tagName||'?')"
                            "+(e.id?'#'+e.id:'')"
                            "+(typeof e.className==='string'&&e.className?"
                            "'.'+e.className.trim().split(/\\s+/).slice(0,3).join('.'):'');}"
                        ),
                        returnByValue=True,
                    )
                    name = desc["result"]["value"]
                except Exception:
                    name = f"node-{bid}"
            rows.append(
                [
                    layer.get("paintCount", 0),
                    round(layer.get("width", 0)),
                    round(layer.get("height", 0)),
                    name,
                ]
            )
        rows.sort(reverse=True)
        return rows[:top]

    def close(self):
        try:
            self.cdp.cmd("Browser.close")
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


# ---------------------------------------------------------------- firefox
def firefox_bench(base_url: str, prof_pref_dpr: float, vw: int = 500, vh: int = 915):
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service

    if not os.environ.get("DISPLAY"):
        raise RuntimeError("firefox bench needs DISPLAY (start Xvfb :99)")
    opts = Options()
    opts.set_preference("layout.css.devPixelsPerPx", str(prof_pref_dpr))
    opts.set_preference("dom.w3c_touch_events.enabled", 1)
    opts.set_preference("media.autoplay.default", 5)
    opts.set_preference("toolkit.cosmeticAnimations.enabled", False)
    opts.set_preference("svg.disabled", False)
    driver = webdriver.Firefox(options=opts, service=Service(log_output=os.devnull))
    # Firefox's toolkit clamps a top-level window below 500 CSS px, so a
    # mobile viewport bottoms out at 500 (still <768: same mobile layout).
    driver.set_window_size(max(500, vw), vh)
    return driver


def ff_evaluate(driver, expression: str, await_promise: bool = False):
    if await_promise:
        return driver.execute_async_script(
            """
            const expr = arguments[0];
            const done = arguments[arguments.length - 1];
            Promise.resolve(eval(expr)).then(
              (v) => done({ok: true, value: v}),
              (e) => done({ok: false, error: String(e)}),
            );
            """,
            expression,
        )
    return driver.execute_script("return eval(arguments[0]);", expression)


# ---------------------------------------------------------------- reporting
def aggregate(runs: list[dict]) -> dict:
    keys = [
        "elapsed_ms",
        "frames",
        "long_frames_50",
        "long_frames_33",
        "long_frames_100",
        "frame_p50",
        "frame_p95",
        "frame_p99",
        "frame_max",
        "longtask_count",
        "longtask_total_ms",
    ]
    out = {}
    for k in keys:
        vals = [r[k] for r in runs if k in r and r[k] is not None]
        if not vals:
            continue
        out[k] = {
            "median": round(statistics.median(vals), 1),
            "min": round(min(vals), 1),
            "max": round(max(vals), 1),
            "runs": vals,
        }
    out["scroll_height"] = runs[-1].get("scroll_height")
    out["dpr"] = runs[-1].get("dpr")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", choices=["chromium", "firefox"], default="chromium")
    ap.add_argument("--variant", default="base")
    ap.add_argument("--variants", default=None, help="comma list; measured in ONE browser session")
    ap.add_argument("--page", default="/transactions")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--throttle", type=float, default=6.0)
    ap.add_argument("--dpr", type=float, default=2.6)
    ap.add_argument("--vw", type=int, default=412)
    ap.add_argument("--vh", type=int, default=915)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.variant not in VARIANTS:
        print(f"unknown variant {args.variant}; have {sorted(VARIANTS)}", file=sys.stderr)
        return 2

    variants = [v.strip() for v in args.variants.split(",")] if args.variants else [args.variant]
    for v in variants:
        if v not in VARIANTS:
            print(f"unknown variant {v}; have {sorted(VARIANTS)}", file=sys.stderr)
            return 2
    results: list[dict] = []
    per_variant: dict[str, list[dict]] = {}
    trace_summary = None
    paints = None
    layer_report = None
    trace_by_variant: dict[str, dict] = {}

    if args.browser == "chromium":
        bench = ChromiumBench(args.url, args.throttle, vw=args.vw, vh=args.vh, dpr=args.dpr)
        try:
            bench.goto("/login")
            login = bench.evaluate(LOGIN_JS)
            time.sleep(2.5)
            print(f"login: {login}", file=sys.stderr)
            for variant in variants:
                for i in range(args.runs):
                    bench.goto(args.page)
                    bench.wait_settled()
                    bench.apply_variant(variant)
                    time.sleep(0.6)
                    res, events, layers = bench.run_workload(trace=True)
                    res["effect"] = bench.evaluate(EFFECT_JS)
                    if variant != args.variant:
                        per_variant.setdefault(variant, []).append(res)
                    else:
                        results.append(res)
                    print(
                        f"[{variant}] run {i + 1}: {res['long_frames_50']} long frames "
                        f"(p95 {res['frame_p95']}ms, max {res['frame_max']}ms, "
                        f"{res['frames']} frames / {res['elapsed_ms']}ms, "
                        f"reason={res['reason']})",
                        file=sys.stderr,
                    )
                    if events:
                        trace_summary = summarise_trace(events)
                        trace_by_variant[variant] = trace_summary
                        paints = paint_attribution(events)
                        layer_report = layers
        finally:
            bench.close()
    else:
        driver = firefox_bench(args.url, args.dpr, vw=args.vw, vh=args.vh)
        try:
            driver.get(args.url + "/login")
            time.sleep(2)
            clicked = ff_evaluate(driver, LOGIN_JS)
            print(f"login: {clicked}", file=sys.stderr)
            time.sleep(3)
            for variant in variants:
                for i in range(args.runs):
                    driver.get(args.url + args.page)
                    time.sleep(3)
                    css = "\n".join(VARIANTS[variant])
                    ff_evaluate(driver, "(" + INJECT_STYLE_JS + ")(" + json.dumps(css) + ")")
                    res = ff_evaluate(driver, WORKLOAD_JS, await_promise=True)
                    eff = ff_evaluate(driver, EFFECT_JS)
                    if isinstance(res, dict) and "ok" in res:
                        if not res["ok"]:
                            raise RuntimeError(res.get("error"))
                        res = res["value"]
                    if isinstance(res, dict):
                        res["effect"] = eff
                    if variant != args.variant:
                        per_variant.setdefault(variant, []).append(res)
                    else:
                        results.append(res)
                    effv = res.get("effect") or {}
                    print(
                        f"[{variant}] run {i + 1}: {res['long_frames_50']} long frames "
                        f"(p95 {res['frame_p95']}ms, max {res['frame_max']}ms, "
                        f"{res['frames']} frames / {res['elapsed_ms']}ms) "
                        f"[blurred={effv.get('blurred_elements')} "
                        f"visArea={effv.get('blurred_area')} "
                        f"header={((effv.get('header') or {}).get('backdrop') or '')[:18]} "
                        f"glass={((effv.get('glass') or {}).get('backdrop') or '')[:18]}]",
                        file=sys.stderr,
                    )
        finally:
            driver.quit()

    report = {
        "browser": args.browser,
        "variant": args.variant,
        "variants": {k: aggregate(v) for k, v in per_variant.items()},
        "page": args.page,
        "runs": args.runs,
        "throttle": args.throttle if args.browser == "chromium" else None,
        "dpr": args.dpr,
        "metrics": aggregate(results),
        "raw": results,
    }
    if trace_summary:
        report["trace"] = trace_summary
        report["traces"] = trace_by_variant
        report["paint_attribution"] = paints
        report["layers"] = layer_report
    text = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

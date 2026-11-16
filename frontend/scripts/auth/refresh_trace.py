"""Attribute every `POST /auth/refresh` the app makes: when, from which call site, and the result.

Installs an in-page `fetch` wrapper (via `Page.addScriptToEvaluateOnNewDocument`,
so it is in place before any document script runs) which records, per
`/api` request: wall clock, `performance.now()`, `location.pathname`, the JS
call stack of the caller, and the response status. It also records console
errors — `usePriceStream` logs "SSE connection error" immediately before its
`restoreSession()` call — so the trace answers *which caller(s) fire*, which
is what the rotation race in docs/09 was about (three callers, one dedupe).

Entries live in `sessionStorage`, because `window.__trace` does not survive a
navigation and the whole point is to follow callers across page loads.

    python3 refresh_trace.py [--block-sse] [base_url]

`--block-sse` blocks the price-stream URL so `usePriceStream`'s `onerror`
takes the reconnect path instead of quietly staying connected.

Trace JSON is written next to this script as `refresh_trace.json`.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "a11y")
)
from a11y_driver import Browser  # noqa: E402

BASE = "http://localhost:4173"
BLOCK_SSE = "--block-sse" in sys.argv
if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
    BASE = sys.argv[1]
PATHS = ["/dashboard", "/transactions", "/investments", "/taxes", "/budgets", "/bills"]

WRAPPER = r"""
(() => {
  // sessionStorage survives navigations inside the tab, so the trace spans the
  // whole run (window state does not).
  const read = () => {
    try { return JSON.parse(sessionStorage.getItem("__trace") || "[]"); }
    catch { return []; }
  };
  const log = read();
  sessionStorage.setItem("__trace", JSON.stringify(log));
  const mark = (entry) => {
    log.push(entry);
    try { sessionStorage.setItem("__trace", JSON.stringify(log)); } catch {}
    return entry;
  };
  const origFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const input = args[0];
    const url = typeof input === "string" ? input : input.url;
    if (!/\/api\//.test(url)) return origFetch(...args);
    const isRefresh = /\/auth\/refresh/.test(url);
    const e = mark({
      kind: isRefresh ? "refresh" : "api",
      url: url.replace(/^.*\/api\/v1/, ""),
      t: performance.now(),
      w: Date.now(),
      path: location.pathname,
      stack: (new Error().stack || "").split("\n").slice(1, 8),
    });
    try {
      const r = await origFetch(...args);
      e.status = r.status;
      e.t2 = performance.now();
      return r;
    } catch (err) {
      e.error = String(err);
      e.t2 = performance.now();
      throw err;
    }
  };
  const origError = console.error.bind(console);
  console.error = (...a) => {
    mark({ kind: "console", t: performance.now(), w: Date.now(), path: location.pathname,
           msg: a.map((x) => (x && x.message) || String(x)).join(" ").slice(0, 200) });
    origError(...a);
  };
})();
"""


def main() -> None:
    b = Browser()
    tab = b.new_tab("about:blank")
    try:
        tab.enable_runtime()
        tab.send("Network.enable")
        tab.send("Page.enable")
        tab.send("Page.addScriptToEvaluateOnNewDocument", {"source": WRAPPER})
        if BLOCK_SSE:
            # Force the second caller: a stream that cannot connect makes
            # usePriceStream's onerror call restoreSession() directly.
            tab.send("Network.setBlockedURLs", {"urls": ["*/securities/stream/prices*"]})

        tab.navigate(BASE + "/login", timeout=45)
        tab.evaluate("sessionStorage.removeItem('__trace')")
        tab.wait_for(
            "Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Explore the demo')) !== undefined",
            timeout=25,
        )
        tab.evaluate(
            "Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Explore the demo'))?.click()"
        )
        tab.wait_for("location.pathname === '/dashboard'", timeout=25)
        tab.sleep(1.0)

        for path in PATHS:
            tab.navigate(BASE + path, timeout=45)
            tab.sleep(3.0)
            if tab.evaluate("location.pathname") != path:
                print("SESSION LOST ->", tab.evaluate("location.pathname"))
                break

        trace = tab.evaluate("JSON.parse(sessionStorage.getItem('__trace') || '[]')") or []
        print(f"\n{len(trace)} entries")
        base_w = trace[0]["w"] if trace else 0
        prev = None
        for e in trace:
            delta = 0.0 if prev is None else e["w"] - prev
            prev = e["w"]
            if e["kind"] == "refresh":
                print(f"  +{(e['w'] - base_w) / 1000:8.3f}s (Δ{delta / 1000:+7.3f}s) REFRESH {e['path']:<14} "
                      f"status={e.get('status')} rt={e.get('t2', 0) - e['t']:.0f}ms")
                for fr in e["stack"][:6]:
                    print(f"        {fr.strip()}")
            elif e["kind"] == "console":
                print(f"  +{(e['w'] - base_w) / 1000:8.3f}s CONSOLE {e['path']:<14} {e['msg']}")
            elif e.get("status", 0) >= 400:
                print(f"  +{(e['w'] - base_w) / 1000:8.3f}s API {e['status']} {e['url']} {e['path']}")

        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refresh_trace.json")
        with open(out, "w") as fh:
            json.dump({"base": BASE, "trace": trace}, fh, indent=1)
        print("wrote", out)
    finally:
        b.close()


if __name__ == "__main__":
    main()

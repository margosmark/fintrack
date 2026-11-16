"""Stress reproducer for the demo-session rotation race (docs/09).

Drives N browser tabs that share ONE cookie jar — the browser a real user (or
a harness) has the demo account signed into — through page loads in lockstep
for `--duration` seconds, with a burst in which the SSE price stream is blocked
so `usePriceStream`'s `onerror` calls `restoreSession()` directly (docs/29 F5b).
Both of the app's refresh callers therefore fire, from both tabs, against one
rotating token.

Every `POST /auth/refresh` is recorded in-page (wall clock + status), so the
verdict says not just whether the session died but how many refreshes were in
flight at once when it did:

    refresh_total    refresh POSTs observed across all tabs
    refresh_401      responses with 401 (reuse cascade / cleared cookie)  [must be 0]
    refresh_429      responses with 429 (the endpoint's 30/min limit — a
                     budget artifact, reported so it is never read as the race)
    max_in_flight    most refreshes in flight at one instant in one jar
                     (>1 = two callers racing on the same cookie)
    overlapping      the racing pairs, with their overlap window
    login_landings   rounds that ended on /login (session loss)           [must be 0]

Exit code 0 only when refresh_401 == 0 and login_landings == 0.

Pre-fix this fails within ~7 s (5 x 401, six overlapping tab pairs, both tabs
stranded on /login) — see `session_stress_pre_fix.json`. Post-fix it must stay
at zero: one in-flight refresh per jar, whatever the tabs are doing.

Usage:
    python3 session_stress.py [--base URL] [--duration 60] [--tabs 2]
                              [--round-every 6] [--sse-burst SEC] [--report PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "a11y"))
from a11y_driver import Browser  # noqa: E402

PAGES = ["/dashboard", "/transactions", "/investments", "/taxes", "/budgets", "/bills"]

# Per-page fetch trace. Two entries per request (start + finish) so nothing is
# lost to a navigation in between: sessionStorage is re-serialised on every
# push, and a page that dies mid-request still writes its start entry.
WRAPPER = r"""
(() => {
  const read = () => {
    try { return JSON.parse(sessionStorage.getItem("__refreshStress") || "[]"); }
    catch { return []; }
  };
  const log = read();
  const push = (e) => {
    log.push(e);
    try { sessionStorage.setItem("__refreshStress", JSON.stringify(log)); } catch {}
    return e;
  };
  const now = () => performance.timeOrigin + performance.now();
  const origFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const input = args[0];
    const url = typeof input === "string" ? input : input.url;
    if (!/\/auth\/refresh/.test(url)) return origFetch(...args);
    const id = Math.random().toString(36).slice(2);
    push({ k: "req", id, w: now(), path: location.pathname });
    try {
      const r = await origFetch(...args);
      push({ k: "res", id, w: now(), status: r.status });
      return r;
    } catch (err) {
      push({ k: "err", id, w: now(), msg: String(err).slice(0, 120) });
      throw err;
    }
  };
})();
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="http://localhost:4173")
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--tabs", type=int, default=2)
    p.add_argument("--round-every", type=float, default=6.0)
    p.add_argument(
        "--sse-burst",
        type=float,
        default=None,
        help="seconds into the run to block the SSE price stream for 5 s "
        "(default: one burst at half duration)",
    )
    p.add_argument("--report", default=None, help="write the raw trace JSON here")
    return p.parse_args()


def wait_ready(tab, url: str, timeout: float = 45.0) -> None:
    """Non-blocking navigation (Page.navigate) so several tabs load at once."""
    tab.send("Page.navigate", {"url": url})
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if tab.evaluate("document.readyState") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.05)
    raise TimeoutError(f"{url} never reached readyState=complete")


def trace_of(tab) -> list[dict]:
    raw = tab.evaluate("JSON.parse(sessionStorage.getItem('__refreshStress') || '[]')")
    return raw or []


def report(traces: dict[str, list[dict]], landings: list[dict], elapsed: float) -> dict:
    statuses: dict[str, int] = {}
    total = 0
    spans: list[tuple[float, float, str]] = []  # (started_at, finished_at, tab)

    for label, entries in traces.items():
        started: dict[str, float] = {}
        for e in entries:
            if e["k"] == "req":
                started[e["id"]] = e["w"]
                total += 1
            elif e["k"] == "res":
                statuses[str(e["status"])] = statuses.get(str(e["status"]), 0) + 1
                if e["id"] in started:
                    spans.append((started[e["id"]], e["w"], label))
            elif e["k"] == "err":
                statuses["network-error"] = statuses.get("network-error", 0) + 1

    # Max refreshes in flight at one instant. Every tab is counted together:
    # they share one cookie jar, so one jar is one rotation chain.
    max_in_flight = 0
    live = 0
    for _, delta in sorted(
        [(w, 1) for w, _, _ in spans] + [(w, -1) for _, w, _ in spans],
        key=lambda point: (point[0], point[1]),
    ):
        live += delta
        max_in_flight = max(max_in_flight, live)

    # Overlapping intervals = two requests carrying the same cookie at once.
    overlapping = []
    ordered = sorted(spans)
    for i, (a_start, a_end, a_tab) in enumerate(ordered):
        for b_start, b_end, b_tab in ordered[i + 1 :]:
            if b_start >= a_end:
                break
            overlapping.append(
                {
                    "tabs": [a_tab, b_tab],
                    "overlap_ms": round(min(a_end, b_end) - b_start),
                    "started_ms_apart": round(b_start - a_start),
                }
            )

    return {
        "elapsed_s": round(elapsed, 1),
        "refresh_total": total,
        "statuses": statuses,
        "refresh_401": statuses.get("401", 0),
        "refresh_429": statuses.get("429", 0),
        "max_in_flight": max_in_flight,
        "overlapping": overlapping[:10],
        "login_landings": landings,
    }


def main() -> int:
    args = parse_args()
    burst_at = args.sse_burst if args.sse_burst is not None else args.duration / 2
    browser = Browser()
    tabs = []
    try:
        for i in range(args.tabs):
            tab = browser.new_tab("about:blank")
            tab.enable_runtime()
            tab.send("Network.enable")
            tab.send("Page.enable")
            tab.send("Page.addScriptToEvaluateOnNewDocument", {"source": WRAPPER})
            tabs.append(tab)

        # One sign-in for the jar: tab 0 goes through the demo button, the
        # others inherit the cookie the moment they load the origin.
        wait_ready(tabs[0], f"{args.base}/login")
        tabs[0].wait_for(
            "Array.from(document.querySelectorAll('button'))"
            ".find(b => b.textContent.includes('Explore the demo')) !== undefined",
            timeout=25,
        )
        tabs[0].evaluate(
            "Array.from(document.querySelectorAll('button'))"
            ".find(b => b.textContent.includes('Explore the demo'))?.click()"
        )
        tabs[0].wait_for("location.pathname === '/dashboard'", timeout=25)
        # Drop whatever the jar's previous session did on /login: the run
        # measures the signed-in session only.
        tabs[0].evaluate("sessionStorage.removeItem('__refreshStress')")
        print("signed in; sharing the cookie jar across", len(tabs), "tabs", flush=True)

        for tab in tabs[1:]:
            wait_ready(tab, f"{args.base}/dashboard")

        started = time.time()
        deadline = started + args.duration
        landings: list[dict] = []
        seen_landing: dict[int, str] = {}
        round_no = 0
        burst_end: float | None = None
        while time.time() < deadline:
            round_no += 1
            paths = []
            for i, tab in enumerate(tabs):
                path = PAGES[(round_no - 1 + i) % len(PAGES)]
                paths.append(path)
                tab.send("Page.navigate", {"url": f"{args.base}{path}"})

            if burst_end is None and (time.time() - started) >= burst_at:
                # SSE churn: a stream that cannot connect makes usePriceStream
                # call restoreSession() directly while the page is live.
                for tab in tabs:
                    tab.send(
                        "Network.setBlockedURLs",
                        {"urls": ["*/securities/stream/prices*"]},
                    )
                burst_end = time.time() + 5
            elif burst_end is not None and time.time() >= burst_end:
                for tab in tabs:
                    tab.send("Network.setBlockedURLs", {"urls": []})
                burst_end = None

            settle_until = started + round_no * args.round_every
            while time.time() < settle_until and time.time() < deadline:
                for i, (tab, path) in enumerate(zip(tabs, paths)):
                    try:
                        landed = tab.evaluate("location.pathname")
                    except Exception:
                        continue
                    if landed == "/login" and seen_landing.get(i) != path:
                        seen_landing[i] = path
                        landings.append(
                            {
                                "t": round(time.time() - started, 1),
                                "tab": i,
                                "wanted": path,
                                "got": landed,
                            }
                        )
                time.sleep(0.25)

        # Unblock, then let any in-flight refresh land before reading traces.
        for tab in tabs:
            try:
                tab.send("Network.setBlockedURLs", {"urls": []})
            except Exception:
                pass
        time.sleep(2.0)

        traces = {f"tab{i}": trace_of(tab) for i, tab in enumerate(tabs)}
        summary = report(traces, landings, time.time() - started)

        out = args.report or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "session_stress.json"
        )
        with open(out, "w") as fh:
            json.dump({"base": args.base, "summary": summary, "traces": traces}, fh, indent=1)

        print(json.dumps(summary, indent=1))
        print("wrote", out)
        ok = summary["refresh_401"] == 0 and not summary["login_landings"]
        print("VERDICT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        browser.close()


if __name__ == "__main__":
    sys.exit(main())

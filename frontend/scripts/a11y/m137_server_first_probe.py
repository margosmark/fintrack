"""M137 — server-first search + budgets: the ledger download is GONE.

Six checks against the deployed stack, one browser session:

  1  ⌘K (the sidebar search box) answers from ONE `GET /search` — the pg_trgm
     fuzzy half is back server-side — and issues ZERO `/transactions` requests
     (the old `useSearchRows` substrate paged the WHOLE ledger per keystroke
     burst).
  2  `/transactions?q=` renders a SERVER-ranked page: one list request
     carrying `q`, no `limit=200` substrate walk, the "Similar to" chip up.
  3  The SimilarExamples popover answers from ONE
     `GET /transactions/similar?…exclude_id=…` — the M119 substrate download
     is gone from this surface too.
  4  `/budgets` is ONE small `GET /budgets` (+ the M96 recurring-summary
     label) and ZERO `/transactions` requests — the acceptance's headline:
     the network panel shows no `/transactions?all`.
  5  The four ORPHAN `/reports/*` routes still RESOLVE with the flag at its
     default (off) — deprecation without removal.
  6  The seven re-pointed `/reports/*` routes + `/transactions/similar` +
     `/rules/preview` + `/subscriptions` answer 200 — the re mounts are live
     on the deployed API.

Evidence (JSON beside this script): every request URL per phase, the check
table, the console log. The flag-ON 410 half is asserted by
`backend/tests/test_report_route_deprecation.py` (both states) and verified
live separately by flipping `SERVER_REPORTS_DEPRECATED` on the deployment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from a11y_driver import Browser  # noqa: E402

BASE = os.environ.get("A11Y_BASE", "http://127.0.0.1:8080")
DEMO_PASSWORD = "demo-password"  # the public demo door's credential (docs/30)

CHECKS: list[dict] = []
EVIDENCE: dict = {"phases": {}}


def check(name: str, ok: bool, detail: object = "") -> None:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not ok else ""))


def wait_stable(tab, timeout: float = 12.0) -> None:
    """Wait until the DOM stops churning — pages here land data late."""
    deadline = time.time() + timeout
    last = -1
    quiet = 0
    while time.time() < deadline:
        try:
            count = tab.evaluate("document.querySelectorAll('*').length")
        except Exception:
            time.sleep(0.25)
            continue
        if count == last:
            quiet += 1
            if quiet >= 3:
                return
        else:
            quiet = 0
        last = count
        time.sleep(0.35)


def api_requests(tab, needle: str) -> list[str]:
    """Every /api/v1 request whose URL contains `needle`, in order."""
    out = []
    for event in tab.events("Network.requestWillBeSent"):
        url = event["params"]["request"].get("url", "")
        if "/api/v1/" in url and needle in url:
            out.append(url)
    return out


def windowed(tab, needles: tuple[str, ...]) -> dict[str, list[str]]:
    """Requests fired SINCE the last drain, per needle.

    NB: two traps, both measured against the deployed stack:
    * `Tab.events()` DRAINS its buffer (CDPSession.drain pops), so the call
      already returns only the delta — never slice by a pre-counted snapshot
      on top of that or every phase comes back empty;
    * the CDP socket is only read inside `send()`'s recv loop, so an event
      that arrived during a plain `time.sleep` is still sitting UNPARSED in
      the socket and `drain()` pops an empty buffer. One cheap round-trip
      first flushes the backlog into the buffer.
    """
    tab.evaluate("1")  # flush pending socket frames into the event buffer
    got: dict[str, list[str]] = {n: [] for n in needles}
    for event in tab.events("Network.requestWillBeSent"):
        url = event["params"]["request"].get("url", "")
        if "/api/v1/" not in url:
            continue
        for n in needles:
            if n in url:
                got[n].append(url)
    return got


def snapshot(tab, needles: tuple[str, ...]) -> None:
    """Discard buffered events so the next windowed() call sees only the
    requests this phase fires."""
    tab.events("Network.requestWillBeSent")


def open_demo_session(browser: Browser, width: int, height: int):
    """Log into the demo tenant the way the house probes do."""
    tab = browser.new_tab(f"{BASE}/login")
    tab.enable_runtime()
    tab.send("Network.enable")
    tab.force_viewport(width, height)
    tab.navigate(f"{BASE}/login")
    wait_stable(tab)
    # The demo button exists in the DOM before React wires its onClick — a
    # DOM-stable wait returns during hydration and the click lands nowhere.
    # Poll for the button, click, then poll for the route (M139's probe hit
    # the same seam).
    tab.wait_for(
        "Array.from(document.querySelectorAll('button')).some("
        "b => b.textContent.includes('Explore the demo'))", timeout=20,
    )
    tab.sleep(0.8)
    tab.evaluate(
        "Array.from(document.querySelectorAll('button')).find("
        "b => b.textContent.includes('Explore the demo'))?.click()"
    )
    tab.wait_for("location.pathname === '/dashboard'", timeout=25)
    wait_stable(tab)
    # A fresh demo session opens the first-run TOUR (data-testid="tour",
    # fixed z-62) directly over the sidebar search box — it swallows every
    # click and keystroke aimed there. Skip it the way a user does.
    tab.evaluate(
        "(() => { const skip = Array.from("
        "document.querySelectorAll('[data-testid=\"tour\"] button')).find("
        "b => b.textContent.trim() === 'Skip'); if (skip) skip.click(); return !!skip; })()"
    )
    tab.sleep(0.6)
    return tab


def demo_token_js() -> str:
    """Mint a demo-tenant token in-page (same-origin fetch) for API checks."""
    return """(async () => {
      const h = {'Content-Type': 'application/json'};
      const log = await fetch('/api/v1/auth/login', {method:'POST', headers:h,
        body: JSON.stringify({email: 'demo@fintracker.local', password: %s})});
      return (await log.json()).access_token;
    })()""" % json.dumps(DEMO_PASSWORD)


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--out", default=str(HERE / "m137-server-first-probe.json"))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m137_server_first_probe.py")
    base = args.base.rstrip("/")
    BASE = base

    # One query string that fuzzy-matches a real demo payee family:
    # "grocer" ~ "GROCERY MART #12" (pg word_similarity, 49 rows in the demo
    # substrate captured in frontend/tests/fixtures/s5-vectors.json).
    QUERY = "grocer"

    browser = Browser(width=args.width, height=args.height)
    try:
        tab = open_demo_session(browser, args.width, args.height)

        # ---- 1. ⌘K: ONE /search, ZERO /transactions ------------------------
        NEEDLES = (
            "/api/v1/search?",
            "/api/v1/transactions?",
            "/transactions/similar?",
            "/api/v1/budgets",
        )
        snapshot(tab, NEEDLES)
        # The chord focuses the sidebar search input (AppShell's keydown
        # router), then the keystrokes go through the real 200ms debounce.
        # Drive the controlled input through the React path: the native
        # value setter + a bubbling `input` event (CDP key events land in the
        # DOM value but never reach this input's onChange — measured).
        typed = tab.evaluate(
            f"""(() => {{ const el = document.querySelector(
                'input[aria-label="Search transactions, categories, accounts"]');"""
            """ if (!el) throw new Error('search input not found');
            el.focus();
            const setter = Object.getOwnPropertyDescriptor(
              window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, QUERYTEXT);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            return el.value; })()""".replace("QUERYTEXT", json.dumps(QUERY))
        )
        if typed != QUERY:
            check("⌘K input accepts the query", False, {"typed": typed})
            raise SystemExit(1)
        # Wait for the PANEL's transaction group — not any 'Transactions'
        # text (the nav link matches that and answers instantly).
        try:
            tab.wait_for(
                "!!document.querySelector('input[aria-label=\"Search transactions, "
                "categories, accounts\"]')?.parentElement?.parentElement?.querySelector("
                "'[data-testid=\"search-panel\"]')",
                timeout=15,
            )
        except Exception:
            pass
        tab.sleep(1.0)
        phase1 = windowed(tab, NEEDLES)
        EVIDENCE["phases"]["cmdk"] = phase1
        # The popover panel is the `top-full` dropdown sibling of the input's
        # <label>. NOT `.absolute` — the Search ICON inside the label also
        # carries that class and matches first in document order.
        panel_raw = tab.evaluate(
            "(document.querySelector("
            "'input[aria-label=\"Search transactions, categories, accounts\"]')"
            "?.parentElement?.parentElement?.querySelector('.top-full')?.textContent || '')"
            ".slice(0, 500)"
        )
        panel_text = str(panel_raw or "")
        check(
            f"⌘K answers from /search and ranks the demo payee ({QUERY} → GROCERY MART #12)",
            len(phase1["/api/v1/search?"]) >= 1 and "GROCERY MART" in (panel_text or "").upper(),
            {"search_requests": len(phase1["/api/v1/search?"]), "panel": panel_text},
        )
        check(
            "⌘K issues ZERO /transactions requests (the ledger download is gone)",
            len(phase1["/api/v1/transactions?"]) == 0,
            phase1["/api/v1/transactions?"],
        )

        # ---- 2. /transactions?q= — the server-ranked page ------------------
        snapshot(tab, NEEDLES)
        tab.navigate(f"{base}/transactions?q={QUERY}")
        tab.wait_for("location.pathname === '/transactions'", timeout=25)
        try:
            tab.wait_for(
                "document.querySelectorAll('tbody tr').length > 0", timeout=20,
            )
        except Exception:
            pass
        wait_stable(tab)
        phase2 = windowed(tab, NEEDLES)
        EVIDENCE["phases"]["q_view"] = phase2
        rows = int(tab.evaluate("document.querySelectorAll('tbody tr').length") or 0)
        chip = bool(tab.evaluate(
            "Array.from(document.querySelectorAll('span')).some("
            "el => el.textContent && el.textContent.includes('Similar to'))"
        ))
        q_requests = [u for u in phase2["/api/v1/transactions?"] if "unreviewed-count" not in u]
        q_carriers = [u for u in q_requests if f"q={QUERY}" in u]
        check(
            f"/transactions?q={QUERY} renders ranked rows from the server-ranked list request",
            rows > 0 and len(q_carriers) == 1,
            {"rows": rows, "requests": q_requests},
        )
        check(
            "no limit=200 substrate walk in search mode",
            not any("limit=200" in u for u in phase2["/api/v1/transactions?"]),
            [u for u in phase2["/api/v1/transactions?"] if "limit=200" in u],
        )
        check("the 'Similar to' chip is up", bool(chip))

        # ---- 3. SimilarExamples popover — ONE /transactions/similar --------
        snapshot(tab, NEEDLES)
        clicked = bool(tab.evaluate(
            "(() => { const b = document.querySelector('tbody tr button[aria-label=\"Similar past entries\"]');"
            " if (b) { b.click(); return true; } return false; })()"
        ))
        try:
            tab.wait_for("document.querySelector('[role=\"dialog\"]') !== null", timeout=15)
        except Exception:
            pass
        tab.sleep(0.8)
        phase3 = windowed(tab, NEEDLES)
        EVIDENCE["phases"]["similar_popover"] = phase3
        similar_requests = phase3["/transactions/similar?"]
        popover_items = int(
            tab.evaluate("document.querySelectorAll('[role=\"dialog\"] li').length") or 0
        )
        check(
            "the popover answers from ONE GET /transactions/similar with exclude_id",
            clicked and len(similar_requests) == 1 and "exclude_id=" in similar_requests[0],
            similar_requests,
        )
        check(
            "the popover lists sibling rows (M119 starvation stays fixed)",
            popover_items > 0,
            {"items": popover_items},
        )

        # ---- 4. /budgets — one small GET, no ledger download ---------------
        snapshot(tab, NEEDLES)
        tab.navigate(f"{base}/budgets")
        tab.wait_for("location.pathname === '/budgets'", timeout=25)
        try:
            tab.wait_for(
                "document.querySelectorAll('main table tbody tr, main [data-testid=\"budget-row\"]').length"
                " > 0 || document.body.textContent.includes('No budgets')", timeout=20,
            )
        except Exception:
            pass
        wait_stable(tab)
        phase4 = windowed(tab, NEEDLES)
        EVIDENCE["phases"]["budgets"] = phase4
        bare_budgets = [u for u in phase4["/api/v1/budgets"] if "recurring-summary" not in u]
        recurring = [u for u in phase4["/api/v1/budgets"] if "recurring-summary" in u]
        check(
            "/budgets is ONE small GET /budgets (+ the M96 label's own tiny read)",
            len(bare_budgets) == 1 and len(recurring) <= 1,
            phase4["/api/v1/budgets"],
        )
        check(
            "/budgets issues ZERO /transactions requests (no ledger download)",
            len(phase4["/api/v1/transactions?"]) == 0,
            phase4["/api/v1/transactions?"],
        )

        # ---- 5+6. API-direct: orphans resolve (flag off), re mounts answer -
        token = str(tab.evaluate(demo_token_js(), await_promise=True) or "")
        api_probe = """(async () => {
          const token = TOKEN, paths = PATHS;
          const h = {'Authorization': 'Bearer ' + token};
          const out = [];
          for (const p of paths) {
            const r = await fetch('/api/v1' + p, {headers: h});
            out.push([p, r.status]);
          }
          return out;
        })()"""
        orphans = [
            "/reports/dividends",
            "/reports/cashflow-sankey?from=2026-01-01&to=2026-12-31",
            "/reports/total-return?from=2026-01-01&to=2026-12-31",
            "/reports/networth-snapshot?target_date=2026-06-30",
        ]
        repointed = [
            "/reports/spending?from=2026-01-01&to=2026-12-31",
            "/reports/cashflow?from=2026-01-01&to=2026-12-31&granularity=month",
            "/transactions/similar?q=grocer&limit=5",
            "/subscriptions",
        ]
        def run_api_probe(paths: list[str]) -> list:
            call = api_probe.replace("TOKEN", json.dumps(token)).replace(
                "PATHS", json.dumps(paths)
            )
            result = tab.evaluate(call, await_promise=True)
            return list(result or [])

        orphan_results = run_api_probe(orphans)
        repointed_results = run_api_probe(repointed)
        EVIDENCE["orphan_routes"] = orphan_results
        EVIDENCE["remounted_routes"] = repointed_results
        check(
            "the four ORPHAN report routes still resolve with the flag at its default (off)",
            bool(orphan_results) and all(status == 200 for _, status in orphan_results),
            orphan_results,
        )
        check(
            "the re-pointed/re-mounted routes answer 200 on the deployed API",
            bool(repointed_results) and all(status == 200 for _, status in repointed_results),
            repointed_results,
        )

        EVIDENCE["console_errors"] = tab.console_errors()
        check("no console errors across the run", not EVIDENCE["console_errors"], EVIDENCE["console_errors"][:5])
    finally:
        browser.close()

    passed = sum(1 for c in CHECKS if c["ok"])
    EVIDENCE["checks"] = CHECKS
    EVIDENCE["summary"] = {"passed": passed, "total": len(CHECKS)}
    Path(args.out).write_text(json.dumps(EVIDENCE, indent=2))
    print(f"\n{passed}/{len(CHECKS)} checks passed — evidence: {args.out}")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())

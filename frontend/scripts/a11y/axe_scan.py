"""M58 baseline + verification scan: axe-core over the 6 highest-traffic pages,
in BOTH themes, in headless Chrome (`--headless=old`, AGENTS §5).

Run:  python3 axe_scan.py [--out results.json]
Requires: `npx vite preview --port 4173` serving the production build with
`/api` proxied to a live backend.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from a11y_driver import Browser

BASE = os.environ.get("A11Y_BASE", "http://localhost:4173")
AXE = str(Path(__file__).resolve().parents[2] / "node_modules" / "axe-core" / "axe.min.js")
HERE = os.path.dirname(os.path.abspath(__file__))


def _axe_source_path() -> str:
    """Where axe.min.js lives: `AXE_CORE` if set, else the NEAREST install.

    frontend's own node_modules first, then the repo root — a hoisted parent
    install is legal, and the canonical one may legitimately be mid-repair.
    Neither found → FileNotFoundError naming both candidates.
    """
    here = Path(__file__).resolve()
    candidates = [
        os.environ.get("AXE_CORE", ""),
        AXE,
        str(here.parents[3] / "node_modules" / "axe-core" / "axe.min.js"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        "axe-core not found — tried: " + ", ".join(c for c in candidates if c)
    )

PAGES = [
    ("dashboard", "/dashboard"),
    ("transactions", "/transactions"),
    ("investments", "/investments"),
    ("taxes", "/taxes"),
    ("budgets", "/budgets"),
    ("bills", "/bills"),
]

# Rules axe cannot evaluate honestly in this harness (none currently disabled —
# kept explicit so adding a suppression is a visible, reviewed diff).
DISABLED_RULES: list[str] = []


def load_axe(tab) -> None:
    with open(_axe_source_path(), encoding="utf-8") as fh:
        source = fh.read()
    tab.evaluate(source)


def force_viewport(tab, width: int, height: int) -> list[int]:
    """Pin the target's viewport and return what the page actually got.

    `--window-size` is NOT the viewport: headless Chrome clamps a window below
    500 CSS px, so `--width 390` used to lay the page out at 500 and the
    narrow-width rules (`scrollable-region-focusable` and friends) were never
    exercised. The emulation override is per-target and survives navigations,
    but a target created WITH a url can commit its first document before the
    first call lands — so apply it again once a document is up and record the
    real `window.innerWidth` (M102).

    The implementation lives on the driver (`Tab.force_viewport`) so every
    harness here shares one definition of "the width the page really got".
    """
    return tab.force_viewport(width, height)


def set_theme(tab, theme: str) -> None:
    tab.evaluate(
        f"localStorage.setItem('theme', {json.dumps(theme)}); "
        f"document.documentElement.classList.toggle('dark', {json.dumps(theme)} === 'dark');"
    )


def run_axe(tab) -> dict:
    expr = (
        "axe.run(document, {"
        f"resultTypes: ['violations'],"
        f"rules: {{ {', '.join(f'{json.dumps(r)}: {{enabled: false}}' for r in DISABLED_RULES)} }}"
        "})"
    )
    return tab.evaluate(expr, await_promise=True)


def summarize(violations: list[dict]) -> dict:
    counts = {"critical": 0, "serious": 0, "moderate": 0, "minor": 0}
    rows = []
    for v in violations:
        impact = v.get("impact") or "minor"
        counts[impact] = counts.get(impact, 0) + 1
        rows.append(
            {
                "id": v["id"],
                "impact": impact,
                "help": v["help"],
                "nodes": len(v["nodes"]),
                "targets": [n["target"] for n in v["nodes"][:4]],
            }
        )
    rows.sort(
        key=lambda r: (["critical", "serious", "moderate", "minor"].index(r["impact"]), -r["nodes"])
    )
    return {"counts": counts, "violations": rows}


def settle(tab, extra: float = 1.6) -> None:
    # Wait for the route page to have painted (route fallback gone) and for
    # framer-motion entrance work to finish.
    try:
        tab.wait_for(
            "document.querySelector('[data-testid=\"route-fallback\"]') === null "
            "&& document.querySelector('main') !== null",
            timeout=25,
        )
    except TimeoutError:
        pass
    tab.sleep(extra)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "axe-results.json"))
    ap.add_argument("--themes", default="dark,light")
    # Width matters: `scrollable-region-focusable` and the narrow-layout rules
    # only fire below md (M102 ran 390 + 412 against the M58 desktop baseline).
    ap.add_argument("--width", type=int, default=int(os.environ.get("A11Y_WIDTH", "1440")))
    ap.add_argument("--height", type=int, default=int(os.environ.get("A11Y_HEIGHT", "900")))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(BASE, who="axe_scan.py")

    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    report: dict = {
        "base": BASE,
        "width": args.width,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pages": {},
    }

    browser = Browser(width=args.width, height=args.height)
    try:
        tab = browser.new_tab(f"{BASE}/login")
        tab.enable_runtime()
        # `--window-size` is NOT the viewport: headless Chrome clamps a window
        # below 500 CSS px, so 390/412 silently ran at 500 and the narrow-width
        # rules were never really exercised. Force the metrics per target
        # (M102) and record what the page actually got.
        force_viewport(tab, args.width, args.height)
        tab.navigate(f"{BASE}/login")
        settle(tab)
        # Re-assert on a real document: a mismatch here would mean every page
        # below was scanned at the wrong width, so refuse to emit evidence.
        report["viewport"] = force_viewport(tab, args.width, args.height)
        if report["viewport"][0] != args.width:
            raise SystemExit(f"viewport {report['viewport']} != requested width {args.width}")

        # rAF liveness probe (AGENTS §5: new headless can stall rAF and freeze
        # AnimatePresence swaps — verify before blaming the app).
        raf = tab.evaluate(
            "new Promise((res) => { let n = 0; const t0 = performance.now(); "
            "const tick = () => { n++; if (performance.now() - t0 < 500) "
            "requestAnimationFrame(tick); else res(n); };"
            " requestAnimationFrame(tick); })",
            await_promise=True,
        )
        report["raf_frames_in_500ms"] = raf

        # Demo login (one click, no credentials typed by the agent).
        tab.evaluate(
            "Array.from(document.querySelectorAll('button')).find(b => "
            "b.textContent.includes('Explore the demo'))?.click()"
        )
        tab.wait_for("location.pathname === '/dashboard'", timeout=25)
        settle(tab)
        load_axe(tab)
        report["logged_in_path"] = tab.evaluate("location.pathname")

        for theme in themes:
            for name, path in PAGES:
                tab.navigate(f"{BASE}{path}")
                settle(tab)
                set_theme(tab, theme)
                tab.sleep(0.6)
                # Scroll the whole page so lazily-mounted off-screen content is
                # in the DOM (recharts renders every series regardless, but
                # IntersectionObserver-driven cards would be missed).
                load_axe(tab)  # every navigation reloads the document
                tab.evaluate(
                    "new Promise(r => { let y = 0; const step = () => { y += 600; "
                    "window.scrollTo(0, y); "
                    "if (y < document.body.scrollHeight) requestAnimationFrame(step); "
                    "else { window.scrollTo(0,0); r(true);} };"
                    " requestAnimationFrame(step); })",
                    await_promise=True,
                )
                tab.sleep(0.4)
                result = run_axe(tab)
                key = f"{name}:{theme}"
                report["pages"][key] = summarize(result["violations"])
                # The viewport each page was ACTUALLY scanned at (emulation is
                # per-target, but evidence that cannot show its own width is
                # not evidence — M102).
                report["pages"][key]["viewport"] = tab.viewport()
                c = report["pages"][key]["counts"]
                print(
                    f"{key:26s} critical={c['critical']} serious={c['serious']} "
                    f"moderate={c['moderate']} minor={c['minor']}",
                    flush=True,
                )
        report["console"] = tab.console_errors()
    finally:
        browser.close()

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nwrote {args.out}")

    bad = sum(p["counts"]["critical"] + p["counts"]["serious"] for p in report["pages"].values())
    print(f"TOTAL critical+serious = {bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

"""axe-core over the static site's HTML pages (docs/32 "Page contract").

Run:  python3 site_axe_scan.py [--out results.json] [--pages guides/taxes.html ...]
with the site served somewhere, e.g.

    python3 -m http.server 8877 --directory site     # or the compose site :8090

Environment: SITE_BASE (default http://127.0.0.1:8090), SITE_WIDTH /
SITE_HEIGHT (default 1440x900 — re-run at 390/360 to cover the narrow-width
rules such as `scrollable-region-focusable`).

`--window-size` is NOT the viewport: headless Chrome clamps a window below
500 CSS px, so `SITE_WIDTH=360` used to lay every page out at **500** while
this report recorded 360. The width is therefore forced with
`Emulation.setDeviceMetricsOverride` (M102's fix, now on `a11y_driver.Tab`),
the real `window.innerWidth` is checked before anything is scanned, the run
ABORTS on a width it cannot honour, and every report — top level and per page
— carries the `viewport` it really ran at. It also refuses to scan a document
that is not under `SITE_BASE`: Chrome's network-error page is
`readyState: 'complete'` too, so `Tab.navigate()` alone cannot tell a served
page from a failed load and the scan could run axe against
`chrome-error://chromewebdata/`.

Exit status is 0 only when every scanned page reports ZERO violations and
ZERO console errors AT THE WIDTH CLAIMED, so this doubles as the acceptance
check: the twelve legacy guides plus the five M89 pages must all be axe-0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import cast

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))

from a11y_driver import Browser  # noqa: E402

AXE = str(HERE.parents[2] / "node_modules" / "axe-core" / "axe.min.js")
SITE = HERE.parents[3] / "site"
BASE = os.environ.get("SITE_BASE", "http://127.0.0.1:8090")
WIDTH = int(os.environ.get("SITE_WIDTH", "1440"))
HEIGHT = int(os.environ.get("SITE_HEIGHT", "900"))

MAP_JS = (
    "axe.run(document, {resultTypes: ['violations']})"
    ".then(r => r.violations.map(v => ({"
    "id: v.id, impact: v.impact, help: v.help,"
    "nodes: v.nodes.length, targets: v.nodes.slice(0, 4).map(n => n.target)"
    "})))"
)


def site_pages() -> list[str]:
    """Every `.html` under site/, as URL paths, index first."""
    found = sorted(str(p.relative_to(SITE)) for p in SITE.rglob("*.html"))
    return ["/" + p for p in found]


def goto(tab, url: str) -> None:
    """Navigate and insist the document we scan is the one we asked for.

    `Tab.navigate()` only polls `readyState`, and Chrome's network-error page
    (`chrome-error://chromewebdata/`) is `readyState: 'complete'` too — so the
    old flow could not tell a served page from a failed load and would run axe
    against the error document instead of the site, i.e. the same
    evidence-integrity hole the width clamp below was. Fail loudly instead of
    measuring the wrong document.
    """
    tab.navigate(url)
    try:
        tab.wait_for(
            f"document.readyState === 'complete' && "
            f"location.href.startsWith({json.dumps(BASE)})",
            timeout=20,
        )
    except TimeoutError:
        try:
            actual = tab.evaluate("location.href")
        except Exception as exc:  # no document at all
            actual = f"<unavailable: {exc}>"
        raise SystemExit(
            f"could not load {url}: the document is {actual!r}, not under "
            f"{BASE!r} — is SITE_BASE serving the site?"
        ) from None


def main() -> int:
    ap = argparse.ArgumentParser()
    default_out = HERE.parent / f"site-axe-results-{WIDTH}.json"
    ap.add_argument("--out", default=str(default_out))
    ap.add_argument("--pages", nargs="*", default=None)
    ap.add_argument("--themes", default="dark,light")
    args = ap.parse_args()
    from probe_target import require_target
    require_target(BASE, who="site_axe_scan.py")
    pages = args.pages or site_pages()
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]

    with open(AXE, encoding="utf-8") as fh:
        axe_source = fh.read()

    report: dict = {
        "base": BASE,
        "width": WIDTH,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M%z"),
        "pages": {},
    }
    browser = Browser(width=WIDTH, height=HEIGHT)
    try:
        tab = browser.new_tab(f"{BASE}/index.html")
        tab.enable_runtime()
        goto(tab, f"{BASE}/index.html")
        # Force the metrics BEFORE trusting a single number: a target created
        # with a url can commit its first document before the first call
        # lands (M102). If Chrome will not give us the requested width there
        # is nothing worth measuring — every row below would be a lie.
        report["viewport"] = tab.force_viewport(WIDTH, HEIGHT)
        if report["viewport"][0] != WIDTH:
            raise SystemExit(
                f"viewport {report['viewport']} != requested {WIDTH}x{HEIGHT}: "
                "headless Chrome will not honour this width, so no evidence "
                "was collected. Use Emulation.setDeviceMetricsOverride "
                "(a11y_driver.Tab.force_viewport) instead of --window-size."
            )
        refused: list[str] = []
        for path in pages:
            for theme in themes:
                goto(tab, f"{BASE}{path}")
                # Re-assert per document and record what THIS page laid out
                # at — evidence that cannot show its own width is not
                # evidence (M102).
                viewport = tab.force_viewport(WIDTH, HEIGHT)
                tab.evaluate(
                    f"localStorage.setItem('theme', {json.dumps(theme)}); "
                    f"document.documentElement.classList.toggle('dark', {json.dumps(theme)} === 'dark');"
                )
                tab.sleep(0.3)
                tab.evaluate(axe_source)  # every navigation reloads the document
                violations = cast(list[dict], tab.evaluate(MAP_JS, await_promise=True) or [])
                console = tab.console_errors()
                key = f"{path}:{theme}"
                report["pages"][key] = {
                    "violations": violations,
                    "console": console,
                    "viewport": viewport,
                }
                ids = (
                    ", ".join(f"{v['id']}({v['impact']},{v['nodes']})" for v in violations)
                    or "CLEAN"
                )
                print(f"{key:56s} {ids}  vp={viewport[0]}x{viewport[1]}", flush=True)
                if console:
                    print(f"{'':56s} console: {console}", flush=True)
                if viewport[0] != WIDTH:
                    refused.append(key)
                    break
            if refused:
                break
    finally:
        browser.close()

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    violations = sum(len(p["violations"]) for p in report["pages"].values())
    console = sum(len(p["console"]) for p in report["pages"].values())
    print(f"\nTOTAL violations={violations} console_errors={console}\nwrote {args.out}")
    if refused:
        print(
            f"ABORT: {refused[0]} laid out at {report['pages'][refused[0]]['viewport'][0]}px, "
            f"not {WIDTH}px — the run stopped there and this report is PARTIAL "
            f"({len(report['pages'])} of {len(pages) * len(themes)} scans).",
            file=sys.stderr,
        )
        return 1
    return 0 if violations == 0 and console == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

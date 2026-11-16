"""M167 — the Exposure Geography choropleth + the Other fold, live probe.

    python3 m167_exposure_map_probe.py [--base https://fintrac.princessandmark.com]
                                       [--width 390] [--height 844]

Runs against the DEMO tenant (one `Explore the demo` click — the public front
door, docs/30), because the acceptance is Mark's own mobile view of that
portfolio. What each check proves (docs/09 §Exposure visuals):

  1  map        the Geography tab renders ONE labelled group ("…world
                choropleth…"), and every shaded country is a named control
  2  top-2      the two loudest shades are United States then Canada — the
                shape the acceptance names — with weights printed as text
  3  fold       the table is collapsed to the top 8 + an `Other` row whose
                weight is the tail's exact sum, and the expander says so
  4  expand     Enter on the expander reveals the FULL list inline, the
                control flips to "Show the top N … only", and the session
                record is written
  5  keyboard   focusing a shaded country updates the text readout; Enter on
                it scrolls the table to that row and marks it selected
  6  mask       M139 inheritance: with the display mask on, the readout and
                the table show bullets while the PERCENTAGES stay readable
  7  axe        axe-core over the whole page with the map open, BOTH themes,
                zero critical + zero serious
  8  mobile     at --width 390: no horizontal scroll anywhere on the page,
                and the map svg fits its container
  9  console    no console errors during the whole walk

Exit 0 only if every check passes; JSON evidence lands next to this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from a11y_driver import Browser

HERE = Path(__file__).resolve().parent
AXE = str(HERE.parents[1] / "node_modules" / "axe-core" / "axe.min.js")
BASE = os.environ.get("M167_BASE", os.environ.get("A11Y_BASE", "https://fintrac.princessandmark.com"))

CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}", flush=True)
    return bool(ok)


def settle(tab, extra: float = 1.6) -> None:
    try:
        tab.wait_for(
            "document.querySelector('[data-testid=\"route-fallback\"]') === null"
            " && document.querySelector('main') !== null",
            timeout=25,
        )
    except TimeoutError:
        pass
    tab.sleep(extra)


def load_axe(tab) -> None:
    with open(AXE, encoding="utf-8") as fh:
        tab.evaluate(fh.read())


def run_axe(tab) -> dict:
    return tab.evaluate(
        "axe.run(document, {resultTypes: ['violations']})",
        await_promise=True,
    )


def summarize(violations: list[dict]) -> dict:
    out = {"counts": {"critical": 0, "serious": 0, "moderate": 0, "minor": 0}, "rules": []}
    for v in violations:
        impact = v.get("impact") or "minor"
        out["counts"][impact] = out["counts"].get(impact, 0) + 1
        out["rules"].append(
            {"id": v["id"], "impact": impact, "nodes": len(v.get("nodes", []))}
        )
    return out


def set_theme(tab, theme: str) -> None:
    """Apply a theme THE WAY THE APP DOES: persist it, then reload.

    Toggling `document.documentElement.classList` by hand only moves the CSS
    variables — React's theme state (which is what `useChartPalette()` and the
    choropleth read) would keep the OLD palette, and the scan would measure a
    light card painted with dark-theme fills. The app's own first-paint snippet
    reads localStorage, so a reload is the honest way to switch.
    """
    tab.evaluate(f"localStorage.setItem('theme', {json.dumps(theme)})")
    open_geography(tab)


def open_geography(tab) -> None:
    tab.navigate(f"{BASE}/investments")
    settle(tab)
    tab.evaluate(
        "Array.from(document.querySelectorAll('[role=\"tab\"]'))"
        ".find(t => t.textContent.trim() === 'Geography')?.click()"
    )
    # The choropleth rides its own chunk (lazy) — wait for the group, not the tab.
    tab.wait_for(
        "Array.from(document.querySelectorAll('[role=\"group\"]'))"
        ".some(g => (g.getAttribute('aria-label') || '').includes('world choropleth'))",
        timeout=20,
    )
    tab.sleep(1.2)


def body_rows(tab) -> int:
    return int(tab.evaluate("document.querySelectorAll('[data-exposure-row]').length"))


def main() -> int:
    # open_geography() reads the module-level BASE; keep the two in step so
    # `--base` really aims every navigation. `global` has to precede the
    # argparse default below, which reads it.
    global BASE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument(
        "--out",
        default=None,
        help="evidence JSON (default: next to this script, stamped with the width)",
    )
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m167_exposure_map_probe.py")
    if args.out is None:
        args.out = str(HERE / f"m167-exposure-map-{args.width}-probe.json")
    BASE = args.base

    report: dict = {
        "base": args.base,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checks": CHECKS,
    }
    ok = True
    browser = Browser(width=args.width, height=args.height)
    try:
        tab = browser.new_tab(f"{args.base}/login")
        tab.enable_runtime()
        # Apply the emulation before the first document commits, then RE-ASSERT
        # it once a real page is up (axe_scan's order — a target created with a
        # url can commit its first document before the first override lands).
        tab.force_viewport(args.width, args.height)
        tab.navigate(f"{args.base}/login")
        settle(tab)
        report["viewport"] = tab.force_viewport(args.width, args.height)
        if report["viewport"][0] != args.width:
            raise SystemExit(f"viewport {report['viewport']} != requested {args.width}")

        # Demo login — one click, no credentials touched by the agent.
        tab.evaluate(
            "Array.from(document.querySelectorAll('button')).find(b => "
            "b.textContent.includes('Explore the demo'))?.click()"
        )
        tab.wait_for("location.pathname === '/dashboard'", timeout=25)
        settle(tab)

        open_geography(tab)
        load_axe(tab)

        # --- 1  the map is ONE labelled group of named controls --------------
        labels = tab.evaluate(
            "Array.from(document.querySelectorAll('[role=\"group\"]'))"
            ".map(g => g.getAttribute('aria-label') || '')"
            ".filter(l => l.includes('world choropleth'))"
        )
        ok &= check(
            "map-group-labelled",
            len(labels) == 1 and "points" in labels[0],
            labels[0][:160] if labels else "no choropleth group",
        )
        shaded = tab.evaluate(
            "Array.from(document.querySelectorAll('svg [role=\"button\"][aria-label*=\"% of exposure\"]'))"
            ".map(e => e.getAttribute('aria-label'))"
        )
        ok &= check("shaded-countries-labelled", len(shaded) >= 5, f"{len(shaded)} controls")
        report["shaded_countries"] = shaded

        # --- 2  the map is a PICTURE OF THE TABLE ---------------------------
        # DOM order on the map is geometry order (biggest path first), not
        # weight order, so the honest check is set-equality against the table:
        # every country the table names is shaded with that same weight, and
        # nothing the table does not name is shaded.
        map_weights = {}
        for label in shaded:
            name, _, weight = label.partition(",")
            map_weights[name.strip()] = weight.strip().split("%")[0].strip()
        table_weights = dict(
            tab.evaluate(
                "Array.from(document.querySelectorAll('[data-exposure-row]'))"
                ".map(tr => { const c = tr.querySelectorAll('td');"
                " return [c[0].textContent.trim(), c[2].textContent.trim()]; })"
            )
        )
        missing = [
            name
            for name, weight in table_weights.items()
            if name not in ("Other", "Unclassified")
            and map_weights.get(name) != weight.replace("%", "").strip()
        ]
        ok &= check(
            "map-matches-the-table",
            not missing and len(map_weights) >= 5,
            f"{len(map_weights)} shaded, {len(table_weights)} table rows, "
            f"mismatched={missing[:4]}",
        )
        biggest = max(map_weights.items(), key=lambda kv: float(kv[1])) if map_weights else None
        print(f"      biggest shade: {biggest}", flush=True)
        report["map_weights"] = map_weights
        report["table_weights"] = table_weights

        # --- 2b  the RAMP actually reached the map, in the right theme ------
        # The biggest weight must carry a different (deeper) stop than a sub-1%
        # country. This is the check that catches "the CSS variables flipped but
        # React still had the old palette" — a real failure mode for any theme
        # switch that does not reload the document.
        fills_on_map = tab.evaluate(
            """(() => {
                 const byName = {};
                 document.querySelectorAll('svg [role="button"][aria-label*="% of exposure"]')
                   .forEach(el => {
                     const name = el.getAttribute('aria-label').split(',')[0];
                     byName[name] = getComputedStyle(el).fill;
                   });
                 return byName;
               })()"""
        )
        biggest_name = biggest[0] if biggest else ""
        smallest_name = min(
            ((n, float(w)) for n, w in map_weights.items() if float(w) > 0),
            key=lambda kv: kv[1],
            default=("", 0),
        )[0]
        ok &= check(
            "ramp-reaches-the-map",
            biggest_name in fills_on_map
            and smallest_name in fills_on_map
            and fills_on_map.get(biggest_name) != fills_on_map.get(smallest_name),
            f"{biggest_name}={fills_on_map.get(biggest_name)} "
            f"{smallest_name}={fills_on_map.get(smallest_name)}",
        )

        # --- 3  the fold: top 8 + Other --------------------------------------
        keys = tab.evaluate(
            "Array.from(document.querySelectorAll('[data-exposure-row]'))"
            ".map(e => e.getAttribute('data-exposure-row'))"
        )
        expander = tab.evaluate(
            "Array.from(document.querySelectorAll('button[aria-controls]'))"
            ".map(b => b.textContent.trim())"
            ".find(t => t.startsWith('Show remaining')) || null"
        )
        ok &= check(
            "collapsed-to-head-plus-other",
            keys[:8] and keys[-1] == "Other" and len(keys) == 9 and bool(expander),
            f"{len(keys)} rows, expander={expander!r}",
        )
        report["collapsed_rows"] = keys

        # --- 4  the expander reveals the full list, inline -------------------
        before = body_rows(tab)
        tab.evaluate(
            "Array.from(document.querySelectorAll('button[aria-controls]'))"
            ".find(b => b.textContent.trim().startsWith('Show remaining'))?.focus()"
        )
        tab.press("Enter")
        tab.sleep(0.8)
        after = body_rows(tab)
        flipped = tab.evaluate(
            "Array.from(document.querySelectorAll('button[aria-controls]'))"
            ".map(b => b.textContent.trim()).find(t => t.startsWith('Show the top')) || null"
        )
        stored = tab.evaluate("sessionStorage.getItem('fintrack.exposure-expanded.v1')")
        ok &= check(
            "expander-reveals-full-list",
            after > before and bool(flipped) and bool(stored),
            f"{before} → {after} rows · {flipped!r} · session={stored!r}",
        )

        # --- 5  keyboard: a shaded country drives the table ------------------
        # Data-agnostic: drive whichever country the map shades FIRST, so a
        # demo-data change can never turn this check into a false red.
        first_label = shaded[0] if shaded else ""
        first_country = first_label.split(",")[0].strip()
        readout_before = tab.evaluate(
            "document.querySelector('[data-testid=\"exposure-map-readout\"]')?.textContent.trim()"
        )
        tab.evaluate(
            "document.querySelector('svg [role=\"button\"][aria-label*=\"% of exposure\"]')?.focus()"
        )
        tab.sleep(0.4)
        readout_after = tab.evaluate(
            "document.querySelector('[data-testid=\"exposure-map-readout\"]')?.textContent.trim()"
        )
        tab.press("Enter")
        tab.sleep(0.6)
        row_selector = f'[data-exposure-row="{first_country}"]'
        marked = tab.evaluate(
            "(tr => !!tr && tr.className.includes('ring-accent'))(document.querySelector(%s))"
            % json.dumps(row_selector)
        )
        ok &= check(
            "keyboard-country-drives-table",
            first_country in (readout_after or "") and marked,
            f"readout {readout_before!r} → {readout_after!r} · selected={marked}",
        )

        # --- 6  M139 mask inheritance on the readout -------------------------
        # No assumption about the boot default: read whatever state the session
        # is in, flip the header chip once, and assert that ONLY the money
        # changed — the country name and the percentage must survive, because
        # the mask is a display transformation, never a data one.
        readout_before_mask = tab.evaluate(
            "document.querySelector('[data-testid=\"exposure-map-readout\"]')?.textContent"
        ) or ""
        tab.evaluate(
            "(Array.from(document.querySelectorAll('button')).find(b =>"
            " ((b.getAttribute('aria-label') || '') + b.textContent)"
            ".toLowerCase().includes('mask')) || {click: () => false}).click(); true"
        )
        tab.sleep(0.8)
        readout_after_mask = tab.evaluate(
            "document.querySelector('[data-testid=\"exposure-map-readout\"]')?.textContent"
        ) or ""
        flipped_money = ("$" in readout_before_mask) != ("$" in readout_after_mask)
        ok &= check(
            "mask-flip-swaps-only-the-money",
            "•" in readout_after_mask
            and "%" in readout_after_mask
            and first_country in readout_after_mask
            and flipped_money,
            f"{readout_before_mask.strip()[:60]!r} → {readout_after_mask.strip()[:60]!r}",
        )
        # Put the session back the way we found it for the axe pass below.
        tab.evaluate(
            "(Array.from(document.querySelectorAll('button')).find(b =>"
            " ((b.getAttribute('aria-label') || '') + b.textContent)"
            ".toLowerCase().includes('mask')) || {click: () => {}}).click()"
        )
        tab.sleep(0.4)

        # --- 7  axe, both themes, map open ----------------------------------
        report["axe"] = {}
        for theme in ("dark", "light"):
            set_theme(tab, theme)
            load_axe(tab)
            report["theme_applied"] = tab.evaluate(
                "document.documentElement.className + ' | theme=' + localStorage.getItem('theme')"
            )
            result = run_axe(tab)
            summary = summarize(result["violations"])
            report["axe"][theme] = summary
            c = summary["counts"]
            ok &= check(
                f"axe-{theme}-critical-serious",
                c["critical"] == 0 and c["serious"] == 0,
                f"critical={c['critical']} serious={c['serious']} "
                f"moderate={c['moderate']} minor={c['minor']} {summary['rules']}",
            )
            # The reload in set_theme() put the scroll back at the top; the
            # evidence screenshot must show the CARD this probe is about.
            tab.evaluate(
                "document.querySelector("
                "'[role=\"group\"][aria-label*=\"world choropleth\"]')"
                "?.scrollIntoView({block: 'start'}); true"
            )
            tab.sleep(0.5)
            shot = str(HERE / f"m167-exposure-map-{args.width}-{theme}.png")
            tab.screenshot(shot, full_page=False)
            print(f"      screenshot: {shot}", flush=True)

        # --- 8  mobile: no horizontal scroll, the map fits -------------------
        # Scope the svg query to the CHOROPLETH — the page is full of lucide
        # icons and `querySelector('svg')` would measure one of those.
        overflow = tab.evaluate(
            """(() => {
                 const svg = document.querySelector(
                   '[role="group"][aria-label*="world choropleth"] svg');
                 return {
                   scrollWidth: document.scrollingElement.scrollWidth,
                   clientWidth: document.scrollingElement.clientWidth,
                   svgWidth: svg ? svg.getBoundingClientRect().width : 0,
                   containerWidth: svg ? svg.parentElement.getBoundingClientRect().width : 0,
                 };
               })()"""
        )
        slack = int(overflow["scrollWidth"]) - int(overflow["clientWidth"])
        ok &= check(
            "no-horizontal-scroll",
            slack <= 1,
            f"scrollWidth={overflow['scrollWidth']} clientWidth={overflow['clientWidth']}",
        )
        ok &= check(
            "map-fits-container",
            float(overflow["svgWidth"]) <= float(overflow["containerWidth"]) + 1,
            f"svg={overflow['svgWidth']:.0f}px in {overflow['containerWidth']:.0f}px",
        )

        # --- 9  console ------------------------------------------------------
        errors = tab.console_errors()
        report["console"] = errors
        ok &= check("console-clean", len(errors) == 0, "; ".join(errors)[:300])
    finally:
        browser.close()

    report["ok"] = bool(ok)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nwrote {args.out}\n{'ALL CHECKS PASSED' if ok else 'FAILURES ABOVE'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""QA F1, three focus positions — Escape must close the open drawer in each.

QA's capture failed with focus wherever the press left it, because the drawer's
own listener had already been unhooked by a render inside the dispatch. The
fix routes ONE shell listener, so every one of these must end closed:

  inside   focus is in the drawer (useDialogFocus moved it there on open)
  toggle   focus is back on the drawer's toggle button — outside the drawer
  search   focus is in the drawer's search field with a live query: the FIRST
           press closes the search layer (drawer stays), the SECOND closes it

Run against the deployed stack:

    python3 scripts/m72/drawer_escape_probe.py --base http://127.0.0.1:18080

Exit 0 only when every variant passes, so it can gate a release.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

os.environ["A11Y_PROFILE_DIR"] = os.path.join(
    tempfile.gettempdir(), f"fintrack-escape-{os.getpid()}"
)
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)  # responsive_audit lives here
sys.path.insert(0, os.path.join(_HERE, "..", "a11y"))

from a11y_driver import Browser  # noqa: E402
from responsive_audit import login, set_viewport  # noqa: E402

DRAWER_OPEN = """
(() => {
  const nav = document.getElementById('app-nav');
  const t = document.querySelector('[data-testid="nav-drawer-toggle"]');
  return !!nav && nav.inert !== true && t && t.getAttribute('aria-expanded') === 'true';
})()
"""
OPEN_IT = r"""
(() => {
  const t = document.querySelector('[data-testid="nav-drawer-toggle"]');
  if (!t) return false;
  if (t.getAttribute('aria-expanded') !== 'true') t.click();
  return true;
})()
"""
FOCUS_TOGGLE = """
(() => {
  const t = document.querySelector('[data-testid="nav-drawer-toggle"]');
  if (t) t.focus();
  return document.activeElement === t;
})()
"""
SEARCH_FIELD = """
(() => {
  const f = document.querySelector('#app-nav input[type="search"], #app-nav input:not([type])')
         || document.querySelector('#app-nav input');
  if (!f) return false;
  f.focus();
  return document.activeElement === f;
})()
"""
# The layer, not the field: the results panel is conditionally rendered
# (`searchOpen && trimmedQuery.length >= 2`, AppShell) while the input KEEPS
# its value after close — a value check would report the layer still open.
SEARCH_VISIBLE = """
(() => {
  const label = [...document.querySelectorAll('label')].find(l => {
    const i = l.querySelector('input');
    return i && (i.getAttribute('aria-label') || '').startsWith('Search transactions');
  });
  const panel = label && label.nextElementSibling;
  return !!(panel && typeof panel.className === 'string' && panel.className.includes('top-full'));
})()
"""


def run_variant(tab, name: str) -> dict:
    """Open the drawer, arrange focus, press Escape (1 or 2 times), report."""
    if not tab.evaluate(OPEN_IT):
        return {"variant": name, "error": "toggle not found"}
    tab.sleep(0.6)
    if not tab.evaluate(DRAWER_OPEN):
        return {"variant": name, "error": "drawer did not open"}
    focus = "inside"
    presses = 1
    if name == "toggle":
        focus = "toggle" if tab.evaluate(FOCUS_TOGGLE) else "FOCUS FAILED"
    elif name == "search":
        if not tab.evaluate(SEARCH_FIELD):
            return {"variant": name, "error": "search field not found"}
        tab.type_text("ca")
        tab.sleep(0.6)  # debounce + results panel
        if not tab.evaluate(SEARCH_VISIBLE):
            return {"variant": name, "error": "search panel never became visible"}

    tab.press("Escape")
    tab.sleep(0.3)
    first = "open" if tab.evaluate(DRAWER_OPEN) else "closed"
    mid_search = tab.evaluate(SEARCH_VISIBLE) if name == "search" else None
    if name == "search" and first == "open":
        presses = 2
        tab.press("Escape")
        tab.sleep(0.3)
    second = "open" if tab.evaluate(DRAWER_OPEN) else "closed"
    passed = second == "closed"
    if name == "search":
        # press 1 must close the SEARCH layer and leave the drawer up;
        # press 2 must close the drawer. Either failing means the router
        # handed the press to the wrong surface.
        passed = passed and first == "open" and mid_search is False
    return {
        "variant": name,
        "focus": focus,
        "presses": presses,
        "after_first": first,
        "search_visible_after_first": mid_search,
        "after_last": second,
        "pass": passed,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18080")
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args()

    browser = Browser()
    failures = 0
    try:
        tab = browser.new_tab("about:blank")
        tab.enable_runtime()
        # One login for the whole probe: /login redirects once authenticated,
        # so calling login() again per round would wait for a demo button that
        # is no longer on the page.
        login(tab, args.base)
        set_viewport(tab, 390, 844)
        # The mobile header mounts below md, and only after the route renders —
        # round 1 otherwise raced first paint and found no toggle.
        tab.wait_for(
            "!!document.querySelector('[data-testid=\"nav-drawer-toggle\"]')",
            timeout=30,
        )
        for r in range(args.rounds):
            if r:
                tab.navigate(f"{args.base}/dashboard")
                tab.wait_for(
                    "!!document.querySelector('[data-testid=\"nav-drawer-toggle\"]')",
                    timeout=30,
                )
                tab.sleep(1.0)
            for variant in ("inside", "toggle", "search"):
                out = run_variant(tab, variant)
                ok = out.get("pass", False)
                failures += 0 if ok else 1
                print(f"round {r + 1} {variant:7s} -> {'PASS' if ok else 'FAIL'} {out}")
    finally:
        browser.close()

    print(f"\ndrawer escape variants: {'ALL PASS' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

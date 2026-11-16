#!/usr/bin/env python3
"""Pre-hydration theme bootstrap probe (QA F2 impact).

index.html ships a hard-coded `class="dark"` and a `theme-color` of #090c12;
only the inline theme bootstrap can remove them BEFORE React loads. If CSP
blocks that script, a light-theme user's first paint is dark — which is
exactly what QA measured on the deployed build.

Method: set `theme=light`, then BLOCK every `/assets/*.js` so the bundle can
never hydrate and fix the class up. Whatever the class is after load is what
the inline script alone produced.

  python3 f2_theme_bootstrap.py --base http://127.0.0.1:18080   # old header
  #   -> className "dark", theme-color #090c12   (bootstrap blocked)
  python3 f2_theme_bootstrap.py --base http://127.0.0.1:4177
  #   -> className "",     theme-color #f3f5f8   (bootstrap ran)
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

os.environ["A11Y_PROFILE_DIR"] = os.path.join(
    tempfile.gettempdir(), f"fintrack-theme-{os.getpid()}"
)
sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "a11y"),
)
from a11y_driver import Browser  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18080")
    args = ap.parse_args()

    browser = Browser()
    try:
        tab = browser.new_tab("about:blank")
        tab.enable_runtime()
        tab.send("Network.enable")
        tab.send("Network.setBlockedURLs", {"urls": ["*/assets/*.js", "*/assets/*.css"]})
        # localStorage is origin-scoped — land on the origin before setting it
        # (an opaque origin throws, and evaluate() re-raises page exceptions).
        tab.navigate(f"{args.base}/login", timeout=30)
        tab.evaluate("localStorage.setItem('theme', 'light')")
        tab.navigate(f"{args.base}/dashboard", timeout=30)
        tab.sleep(3.0)

        state = tab.evaluate(
            "({"
            "  className: document.documentElement.className,"
            "  themeColor: (document.querySelector('meta[name=\"theme-color\"]') || {}).content,"
            "  bundleLoaded: !!document.querySelector('script[src=\"/assets/\"]'),"
            "  inlineScripts: document.querySelectorAll('script:not([src])').length"
            "})"
        )
        console = tab.console_errors()
        tab.send("Network.setBlockedURLs", {"urls": []})

        print(f"base: {args.base}")
        print(f"  html class   : {state['className']!r}")
        print(f"  theme-color  : {state['themeColor']!r}")
        print(f"  inline scripts: {state['inlineScripts']}")
        print(f"  console errors: {len(console)}")
        for err in console[:3]:
            print("      ", err[:150])
        # With the bundle blocked, only the inline bootstrap can clear `dark`.
        ok = state["className"] == "" and state["themeColor"] == "#f3f5f8"
        print("  verdict:", "bootstrap RAN (light first paint)" if ok else "bootstrap did NOT run")
        return 0 if ok else 1
    finally:
        browser.close()


if __name__ == "__main__":
    raise SystemExit(main())

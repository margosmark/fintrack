#!/usr/bin/env python3
"""M72 responsive + PWA audit: measures the six core pages at a set of
viewports in headless Chrome (`--headless=old`, AGENTS §5) and writes JSON
evidence. Optionally captures screenshots per page x theme x viewport.

Run:  python3 audit.py --base http://127.0.0.1:4173 --out m72.json [--shots DIR]

Per page x viewport it records:
  * document scrollWidth vs clientWidth (is there horizontal PAGE scroll?)
  * elements whose box escapes the viewport (top offenders, with a selector)
  * visible interactive controls whose box is smaller than 44x44 (WCAG 2.5.8
    touch-target floor the card asks for)
  * below 768px: the OPEN nav drawer — every control inside it against the
    same 44px floor, a screenshot, and whether a real (CDP trusted) Escape key
    closes it (QA F1/F3; the parked drawer is inert and skipped above)
  * the M64 `TableScroller` engagement state (`.table-scroll` present? how
    much content overflows inside it?)
  * console errors
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

# Fresh Chrome profile per run: a shared profile carries a refresh cookie that
# the PREVIOUS run already rotated, and the demo session then dies mid-sweep
# (docs/09 §Session refresh is single-flight).
os.environ["A11Y_PROFILE_DIR"] = os.path.join(
    tempfile.gettempdir(), f"fintrack-m72-{os.getpid()}"
)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "a11y"))
from a11y_driver import Browser  # noqa: E402

PAGES = [
    ("dashboard", "/dashboard"),
    ("transactions", "/transactions"),
    ("budgets", "/budgets"),
    ("bills", "/bills"),
    ("calendar", "/calendar"),
    ("reports", "/reports"),
]

MEASURE = r"""
(() => {
  const de = document.documentElement;
  const vw = de.clientWidth;
  const sel = (el) => {
    if (!el || !el.tagName) return "";
    let s = el.tagName.toLowerCase();
    if (el.id) s += "#" + el.id;
    const cls = (typeof el.className === "string" ? el.className : "").trim().split(/\s+/).filter(Boolean);
    if (cls.length) s += "." + cls.slice(0, 4).join(".");
    return s;
  };
  const parkedNav = document.getElementById("app-nav");
  const hidden = (el) => {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") return true;
    if (el.closest('[aria-hidden="true"]')) return true;
    if (el.classList.contains("sr-only")) return true;
    // The M72 drawer parks itself off-canvas while closed: inert, unreachable,
    // and not part of the reachable page surface (it is audited open instead).
    if (parkedNav && parkedNav.inert && el.closest("#app-nav")) return true;
    return false;
  };
  // horizontal overflow of the *page*
  const overflow = [];
  for (const el of document.querySelectorAll("main *, nav *, header *, footer *")) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (hidden(el)) continue;
    const right = r.right, left = r.left;
    // Fully left of the viewport == the parked drawer, not an escape.
    if (right <= 0) continue;
    if (right > vw + 1 || left < -1) {
      // only report elements nobody above them already clips horizontally
      let clipped = false;
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        const pcs = getComputedStyle(p);
        // Only a genuinely scrollable ancestor excuses an escaped box: an
        // overflow-x: clip/hidden ancestor HIDES it (unreachable content),
        // which is exactly what this audit exists to catch.
        if (["auto", "scroll"].includes(pcs.overflowX)) { clipped = true; break; }
      }
      if (clipped) continue;
      overflow.push({ sel: sel(el), left: Math.round(left), right: Math.round(right), w: Math.round(r.width) });
    }
  }
  overflow.sort((a, b) => b.right - a.right);
  // touch targets
  const interactive = 'a[href], button, input, select, textarea, [role="button"], [role="tab"], [role="link"], [role="menuitem"], [role="checkbox"], [role="switch"], [role="option"], [role="slider"], [role="scrollbar"], [role="region"][tabindex]';
  const small = [];
  let seen = 0;
  for (const el of document.querySelectorAll(interactive)) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (hidden(el)) continue;
    seen += 1;
    // A control may carry a larger hit band on ::before (the M64 scrollbar
    // thumb does) — measure the band when it exists.
    const pseudo = getComputedStyle(el, "::before");
    const bandH = parseFloat(pseudo && pseudo.height) || 0;
    const bandW = parseFloat(pseudo && pseudo.width) || 0;
    const effW = Math.max(r.width, bandW);
    const effH = Math.max(r.height, bandH);
    if (effW < 44 - 0.5 || effH < 44 - 0.5) {
      small.push({
        sel: sel(el),
        w: Math.round(effW), h: Math.round(effH), raw: `${Math.round(r.width)}x${Math.round(r.height)}`,
        text: (el.innerText || el.getAttribute("aria-label") || "").trim().slice(0, 40),
      });
    }
  }
  // TableScroller
  const scrollers = [...document.querySelectorAll(".table-scroll")].map((el) => ({
    clientW: el.clientWidth, scrollW: el.scrollWidth,
    maxScroll: el.scrollWidth - el.clientWidth,
    hint: !!el.parentElement?.querySelector('[data-testid="table-scroll-hint"]') ||
          !!el.closest("div")?.querySelector("button"),
    label: el.getAttribute("aria-label"),
  }));
  return {
    innerWidth: window.innerWidth,
    docScrollW: de.scrollWidth,
    docClientW: de.clientWidth,
    pageHScroll: de.scrollWidth > de.clientWidth + 1,
    bodyScrollW: document.body.scrollWidth,
    overflow: overflow.slice(0, 12),
    overflowCount: overflow.length,
    touchTargetsTotal: seen,
    touchTargetsSmall: small.length,
    smallSample: small.slice(0, 120),
    tableScrollers: scrollers,
    navRail: (() => { const n = document.querySelector('nav[aria-label="Main navigation"]'); if (!n) return null;
      const r = n.getBoundingClientRect(); return { w: Math.round(r.width), x: Math.round(r.x), visible: r.width > 0 && r.height > 0 }; })(),
    bottomNav: (() => { const n = document.querySelector('nav[aria-label="Mobile navigation"]'); if (!n) return null;
      const r = n.getBoundingClientRect(); return { h: Math.round(r.height), visible: r.width > 0 }; })(),
    hamburger: !!document.querySelector('[data-testid="nav-drawer-toggle"]'),
    drawer: !!document.querySelector('[data-testid="app-nav"]') &&
            document.querySelector('[data-testid="app-nav"]').inert === false,
  };
})()
"""

READY = (
    "!document.querySelector('[data-testid=\"route-fallback\"]') && "
    "!!document.querySelector('main') && document.querySelector('main').innerText.length > 60"
)

# The parked drawer is inert, so the MEASURE loop above skips it on purpose —
# which left the OPEN drawer (the surface a phone user actually touches)
# unaudited: it measured 184x36 / 184x32 at 390 while the report said "0 of
# 856 controls under 44px" (QA F3). Open it and measure it for real.
OPEN_DRAWER = r"""
(() => {
  const t = document.querySelector('[data-testid="nav-drawer-toggle"]');
  if (!t) return false;
  if (t.getAttribute('aria-expanded') !== 'true') t.click();
  return true;
})()
"""

MEASURE_DRAWER = r"""
(() => {
  const nav = document.getElementById('app-nav');
  if (!nav) return null;
  const sel = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const cls = (typeof el.className === 'string' ? el.className : '').trim().split(/\s+/).filter(Boolean);
    if (cls.length) s += '.' + cls.slice(0, 3).join('.');
    return s;
  };
  const interactive = 'a[href], button, input, select, textarea, [role="button"], [role="tab"], [role="link"], [role="menuitem"], [role="checkbox"], [role="switch"], [role="option"]';
  const small = [];
  let seen = 0;
  let minH = null;
  for (const el of nav.querySelectorAll(interactive)) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (el.closest('[aria-hidden="true"]')) continue;
    seen += 1;
    minH = minH === null ? r.height : Math.min(minH, r.height);
    if (r.width < 44 - 0.5 || r.height < 44 - 0.5) {
      small.push({ sel: sel(el), w: Math.round(r.width), h: Math.round(r.height),
                   text: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 40) });
    }
  }
  return {
    open: nav.inert !== true,
    total: seen,
    smallCount: small.length,
    minHeight: minH === null ? null : Math.round(minH),
    small: small.slice(0, 40),
  };
})()
"""

DRAWER_STATE = """
document.querySelector('[data-testid="nav-drawer-toggle"]')?.getAttribute('aria-expanded') ?? null
"""


def set_viewport(tab, w: int, h: int) -> None:
    tab.send("Emulation.setDeviceMetricsOverride",
             {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": h > w, "screenWidth": w, "screenHeight": h})


def login(tab, base: str) -> str:
    tab.navigate(f"{base}/login")
    tab.wait_for("document.readyState === 'complete'")
    expr = """
    (() => {
      const btn = [...document.querySelectorAll('button')].find(b => b.innerText.includes('Explore the demo'));
      if (!btn) return null;
      const r = btn.getBoundingClientRect();
      return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    })()
    """
    # The demo session can die mid-sweep (rotation), so a re-login has to be
    # as forgiving as the page walk: retry the click and say WHAT the page
    # looked like instead of dying on the first miss.
    last_err: Exception | None = None
    for attempt in range(3):
        for _ in range(40):
            box = tab.evaluate(expr)
            if box:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("demo button never appeared")
        tab.click_at(box["x"], box["y"])
        try:
            tab.wait_for("location.pathname === '/dashboard'", timeout=30)
            return "ok"
        except TimeoutError as err:
            last_err = err
            state = tab.evaluate(
                "({path: location.pathname, text: (document.body.innerText || '')"
                ".slice(0, 300), err: (document.querySelector('[role=alert]') || {})"
                ".innerText || null})"
            )
            print(f"login attempt {attempt + 1} failed: {state}", file=sys.stderr)
            tab.navigate(f"{base}/login")
            tab.wait_for("document.readyState === 'complete'", timeout=30)
            time.sleep(1.0)
    raise RuntimeError(f"demo login never reached /dashboard: {last_err}")


def set_theme(tab, theme: str) -> None:
    tab.evaluate(
        f"localStorage.setItem('theme', {json.dumps(theme)}); "
        f"document.documentElement.classList.toggle('dark', {json.dumps(theme)} === 'dark');"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:4173")
    ap.add_argument("--out", default="audit.json")
    ap.add_argument("--shots", default=None)
    ap.add_argument("--themes", default="dark,light")
    ap.add_argument("--viewports", default="390x844,768x1024,1280x800")
    ap.add_argument("--pages", default=",".join(p[0] for p in PAGES))
    ap.add_argument("--measure-only", action="store_true", help="skip screenshots")
    args = ap.parse_args()

    viewports = [tuple(int(v) for v in vp.split("x")) for vp in args.viewports.split(",")]
    themes = args.themes.split(",")
    pages = [p for p in PAGES if p[0] in args.pages.split(",")]

    result = {"base": args.base, "viewports": [], "consoleErrors": []}
    b = Browser(width=viewports[0][0], height=viewports[0][1])
    try:
        tab = b.new_tab("about:blank")
        tab.enable_runtime()
        set_viewport(tab, viewports[0][0], viewports[0][1])
        login(tab, args.base)

        for (w, h) in viewports:
            set_viewport(tab, w, h)
            for theme in themes:
                # theme is read at boot, so set it then reload the app shell
                tab.navigate(f"{args.base}/dashboard")
                tab.wait_for(READY, timeout=30)
                set_theme(tab, theme)
                tab.navigate(f"{args.base}/dashboard")
                tab.wait_for(READY, timeout=30)
                if tab.evaluate("location.pathname") == "/login":
                    login(tab, args.base)
                for name, path in pages:
                    tab.navigate(f"{args.base}{path}")
                    # A session that dies mid-sweep bounces to /login, which
                    # has no <main> — re-sign-in and re-enter the page rather
                    # than failing the whole sweep on it.
                    if tab.evaluate("location.pathname") == "/login":
                        login(tab, args.base)
                        tab.navigate(f"{args.base}{path}")
                    try:
                        tab.wait_for(READY, timeout=40)
                    except TimeoutError as err:
                        # One unready page must not cost the other 35: record
                        # it and keep walking.
                        result.setdefault("failures", []).append(
                            {"page": name, "path": path, "theme": theme,
                             "viewport": f"{w}x{h}", "error": str(err)[:400],
                             "body": tab.evaluate(
                                 "(document.body.innerText || '').slice(0, 500)"
                             ),
                             "consoleErrors": tab.console_errors()[:6]}
                        )
                        print(f"FAILED ready {name} {theme} {w}x{h}", file=sys.stderr)
                        continue
                    tab.sleep(1.4)  # charts + query settle
                    m = tab.evaluate(MEASURE)
                    m.update({"page": name, "path": path, "theme": theme, "viewport": f"{w}x{h}"})
                    # Drawer pass (below md only): the parked drawer is inert
                    # and therefore skipped by MEASURE, so the OPEN drawer —
                    # the surface a phone user actually touches — needs its own
                    # measurement. Open it, measure every control inside it,
                    # screenshot, then press a REAL Escape (CDP trusted key,
                    # the input path QA found broken) and record whether it
                    # closed. Covers QA F1 + F3 in the same sweep.
                    m["drawer"] = None
                    m["drawerEscapeClosed"] = None
                    if w < 768 and tab.evaluate(OPEN_DRAWER):
                        tab.sleep(0.6)  # drawer transition settles
                        m["drawer"] = tab.evaluate(MEASURE_DRAWER)
                        if args.shots and not args.measure_only:
                            tag = f"drawer-{name}-{theme}-{w}x{h}.png"
                            tab.screenshot(os.path.join(args.shots, tag))
                        tab.press("Escape")
                        tab.sleep(0.25)
                        m["drawerEscapeClosed"] = (
                            tab.evaluate(DRAWER_STATE) == "false"
                        )
                    result["viewports"].append(m)
                    errs = tab.console_errors()
                    if errs:
                        result["consoleErrors"].append({"page": name, "viewport": f"{w}x{h}",
                                                        "theme": theme, "errors": errs[:6]})
                    if args.shots and not args.measure_only:
                        tag = f"{name}-{theme}-{w}x{h}.png"
                        tab.screenshot(os.path.join(args.shots, tag))
    finally:
        b.close()

    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=1)
    # compact verdict table
    for m in result["viewports"]:
        if "pageHScroll" not in m:
            continue
        drawer = m.get("drawer")
        drawer_txt = "-"
        if drawer is not None:
            drawer_txt = (
                f"small={drawer['smallCount']}/{drawer['total']} "
                f"minH={drawer['minHeight']} esc={m.get('drawerEscapeClosed')}"
            )
        print(f"{m['viewport']:>9} {m['theme']:>5} {m['page']:<13} hscroll={str(m['pageHScroll']):<5} "
              f"docW={m['docScrollW']}/{m['docClientW']} overflow={m['overflowCount']:<3} "
              f"touch<{44}={m['touchTargetsSmall']}/{m['touchTargetsTotal']} "
              f"scrollers={len(m['tableScrollers'])} rail={m['navRail']} burger={m['hamburger']} "
              f"drawer[{drawer_txt}]")
    print("console error groups:", len(result["consoleErrors"]))
    print("failures:", len(result.get("failures", [])))
    print("wrote", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

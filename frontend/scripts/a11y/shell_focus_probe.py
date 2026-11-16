#!/usr/bin/env python3
"""Ledger shell keyboard + focus journey — REAL headless Chrome (docs/47 §6.8-10).

jsdom proved the contract; this proves the browser agrees. It walks the four
areas of card t_23b8d18e against a real build served by `vite preview` with
`/api` proxied to the deployed backend (one origin, so the demo cookie flows):

  1. logical focus order  — Tab from the document start: skip link, brand,
     five primary tabs, More, then the right cluster, then the routed page;
  2. visible focus        — every one of those stops paints an outline or ring
     that is not transparent (WCAG 2.4.7; `outline-none` counts only when the
     replacement ring is actually there);
  3. Escape / focus return — More opens with focus in the panel, Tab enters it,
     Escape closes it back onto the More button; the account menu does the same
     onto the avatar button;
  4. route-focus handoff   — entering a route from a primary tab, from inside
     the More panel (the restore-order case) and from the mobile tab bar all
     park focus on #main-content, never on the control just used.

USAGE
-----
Serve the production build with `/api` proxied to a live backend so the demo
cookie flows on ONE origin (the preview config is deliberately untracked —
never commit it; `vite.config.ts` only proxies `server`, not `preview`):

    cd frontend && npm run build
    cat > vite.verify.config.ts <<'EOF'
    import { defineConfig } from "vite";
    export default defineConfig({ preview: { port: 4191, strictPort: true,
      proxy: { "/api": { target: "https://fintrac.princessandmark.com",
               changeOrigin: true } } } });
    EOF
    npx vite preview --config vite.verify.config.ts &
    python3 frontend/scripts/a11y/shell_focus_probe.py

Its own CDP port and profile (`A11Y_CDP_PORT`/`A11Y_PROFILE_DIR`, defaulted
here to 9467 + a TMPDIR profile) keep it off the box's shared a11y browser.
Writes `shell-focus-probe.json` next to this script ($PROBE_OUT overrides);
exit 0 only if every check passes.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# Own CDP port + own profile: the box's a11y browser is shared by other cards.
os.environ.setdefault("A11Y_CDP_PORT", "9467")
# TMPDIR already points at this profile's scratch (never the system temp dir).
_PROFILE = Path(tempfile.gettempdir()) / "fintrack-focus-profile"
os.environ.setdefault("A11Y_PROFILE_DIR", str(_PROFILE))

HERE = Path(__file__).resolve().parent  # frontend/scripts/a11y
REPO = Path(os.environ.get("REPO", HERE.parents[2]))
sys.path.insert(0, str(HERE))

from a11y_driver import Browser  # noqa: E402

BASE = os.environ.get("SITE_BASE", "http://localhost:4191")
OUT = Path(
    os.environ.get(
        "PROBE_OUT",
        str(Path(__file__).resolve().parent / "shell-focus-probe.json"),
    )
)

CHECKS: list[dict] = []


def check(name: str, ok: object, detail: object = "") -> bool:
    row = {"name": name, "ok": bool(ok), "detail": str(detail)}
    CHECKS.append(row)
    print(f"{'PASS' if row['ok'] else 'FAIL'}  {name} :: {row['detail']}", flush=True)
    return row["ok"]


DESC = """(() => {
  const el = document.activeElement;
  if (!el) return null;
  const cs = getComputedStyle(el);
  const transparent = (c) => c === 'transparent' || c === 'rgba(0, 0, 0, 0)';
  const outlineVisible = cs.outlineStyle !== 'none'
      && parseFloat(cs.outlineWidth) > 0 && !transparent(cs.outlineColor);
  const ringVisible = cs.boxShadow !== 'none' && cs.boxShadow !== '';
  return {
    tag: el.tagName,
    id: el.id || '',
    testid: el.getAttribute('data-testid') || '',
    label: ((el.getAttribute('aria-label') || el.innerText || el.textContent || '')
      + '').trim().replace(/\\s+/g, ' ').slice(0, 70),
    href: el.getAttribute('href') || '',
    inTopbar: !!el.closest('[data-testid="app-topbar"]'),
    inTabs: !!el.closest('[data-testid="topbar-tabs"]'),
    inMore: !!el.closest('[data-testid="more-panel"]'),
    inAccount: !!el.closest('[data-testid="account-menu-panel"]'),
    inMain: !!el.closest('#main-content'),
    isBody: el === document.body || el === document.documentElement,
    visible: outlineVisible || ringVisible,
    outline: cs.outlineStyle + ' ' + cs.outlineWidth + ' ' + cs.outlineColor,
    shadow: (cs.boxShadow || '').slice(0, 70),
  };
})()"""


def desc(tab) -> dict | None:
    return tab.evaluate(DESC)


def settle(tab, extra: float = 1.5) -> None:
    try:
        tab.wait_for(
            "document.querySelector('[data-testid=\"route-fallback\"]') === null"
            " && document.querySelector('main') !== null",
            timeout=25,
        )
    except TimeoutError:
        pass
    tab.sleep(extra)


def login(tab) -> None:
    tab.navigate(f"{BASE}/login")
    settle(tab, extra=1.0)
    tab.wait_for(
        "Array.from(document.querySelectorAll('button')).some("
        "(b) => b.textContent.includes('Explore the demo'))",
        timeout=25,
    )
    tab.evaluate(
        "Array.from(document.querySelectorAll('button')).find("
        "(b) => b.textContent.includes('Explore the demo'))?.click()"
    )
    tab.wait_for("location.pathname === '/dashboard'", timeout=25)
    settle(tab, extra=2.0)
    # A fresh session opens the first-run tour over the shell; dismiss it or
    # every click below lands on the card (fin-tracker skill, M136 trap).
    if tab.evaluate("!!document.querySelector('[data-testid=\"tour\"]')"):
        tab.evaluate(
            "document.querySelector('[data-testid=\"tour\"] [data-testid=\"tour-skip\"]')"
            "?.click() || document.querySelector('[data-testid=\"tour-skip\"]')?.click()"
        )
        tab.sleep(1.0)
        print("   (first-run tour skipped)", flush=True)


def goto(tab, path: str) -> str:
    """Land on `path`, signing the demo tenant back in if the session died."""
    if tab.evaluate("location.pathname") != path:
        tab.navigate(f"{BASE}{path}")
        settle(tab, extra=1.0)
    if tab.evaluate("location.pathname") == "/login":
        login(tab)
        tab.navigate(f"{BASE}{path}")
        settle(tab, extra=1.0)
    return str(tab.evaluate("location.pathname"))


def blur_all(tab) -> str:
    # NOTE the wrapping parens: `Runtime.evaluate` on a bare `() => {...}`
    # returns the function itself and runs nothing — every helper here must
    # be an IIFE, or the probe silently asserts against an untouched page.
    tab.evaluate(
        "(() => { if (document.activeElement && document.activeElement !== document.body)"
        " document.activeElement.blur(); return true; })()"
    )
    tab.sleep(0.2)
    d = desc(tab) or {}
    return d.get("tag", "")


def focus_js(tab, expr: str) -> dict | None:
    tab.evaluate(f"(() => {{ {expr}; return true; }})()")
    tab.sleep(0.25)
    return desc(tab)


# ---------------------------------------------------------------- 1 + 2
def walk_chrome(tab) -> list[dict]:
    blur_all(tab)
    stops: list[dict] = []
    for _ in range(16):
        tab.press("Tab")
        tab.sleep(0.12)
        d = desc(tab)
        if d:
            stops.append(d)
        if stops and d and d.get("id") == "main-content":
            break
    return stops


def area_order_and_visible(tab) -> None:
    goto(tab, "/dashboard")
    stops = walk_chrome(tab)
    labels = [
        (s.get("testid") or s.get("href") or s.get("tag", "")).split("?")[0]
        for s in stops
    ]
    print("   tab order:", labels, flush=True)

    check(
        "order.skip-link-first",
        bool(stops) and stops[0].get("testid") == "skip-link",
        stops[0] if stops else "no stops",
    )

    head = labels[:8]
    check(
        "order.brand-then-5-tabs-then-More",
        head[:8]
        == [
            "skip-link",
            "/dashboard",
            "/dashboard",
            "/transactions",
            "/imports",
            "/investments",
            "/reports",
            "more-toggle",
        ],
        head,
    )

    # labels[8:] is the right cluster — the search INPUT, palette trigger, mask
    # chip, bell, avatar — all AFTER `More`, which is what lets the popover
    # open between the toggle and the controls it can cover.
    right = labels[8:13]
    check(
        "order.right-cluster-follows-More",
        right[1:] == [
            "command-palette-trigger",
            "mask-chip",
            "notifications-toggle",
            "account-menu-toggle",
        ],
        right,
    )

    unseen = [s for s in stops if s.get("inTopbar")]
    invisible = [
        f"{s.get('testid') or s.get('label') or s.get('tag')}"
        for s in unseen
        if not s.get("visible")
    ]
    check(
        "visible.every-topbar-stop-paints-an-indicator",
        not invisible,
        f"{len(unseen)} chrome stops, invisible={invisible}",
    )
    detail = [
        f"{s.get('testid') or s.get('label')[:24]}: outline={s.get('outline')}"
        for s in unseen[:6]
    ]
    print("   indicators:", detail, flush=True)


# ---------------------------------------------------------------- 3
def area_more_escape(tab) -> None:
    goto(tab, "/dashboard")
    d = focus_js(tab, "document.querySelector('[data-testid=\"more-toggle\"]').focus()")
    check("more.toggle-focusable", d and d.get("testid") == "more-toggle", d)

    tab.press("Enter")
    try:
        tab.wait_for("!!document.querySelector('[data-testid=\"more-panel\"]')", timeout=10)
        opened = True
    except TimeoutError:
        opened = False
    tab.sleep(0.4)
    d = desc(tab) or {}
    check(
        "more.focus-moves-into-the-panel-on-open",
        opened and d.get("inMore") and d.get("id") == "more-panel",
        {k: d.get(k) for k in ("testid", "id", "tag", "inMore")},
    )

    tab.press("Tab")
    tab.sleep(0.2)
    d = desc(tab) or {}
    check(
        "more.tab-enters-the-panel-links",
        d.get("inMore") and d.get("tag") == "A",
        {k: d.get(k) for k in ("tag", "href", "label")},
    )
    check(
        "more.panel-link-paints-an-indicator",
        bool(d.get("visible")),
        f"outline={d.get('outline')} shadow={d.get('shadow')}",
    )

    tab.press("Escape")
    try:
        tab.wait_for("!document.querySelector('[data-testid=\"more-panel\"]')", timeout=10)
        closed = True
    except TimeoutError:
        closed = False
    tab.sleep(0.5)
    d = desc(tab) or {}
    check(
        "more.escape-closes-and-returns-focus-to-the-toggle",
        closed and d.get("testid") == "more-toggle",
        {k: d.get(k) for k in ("testid", "label")},
    )


def area_account_escape(tab) -> None:
    goto(tab, "/dashboard")
    d = focus_js(
        tab, "document.querySelector('[data-testid=\"account-menu-toggle\"]').focus()"
    )
    check(
        "account.avatar-is-focusable",
        bool(d and d.get("testid") == "account-menu-toggle"),
        {k: (d or {}).get(k) for k in ("testid", "tag", "label")},
    )
    tab.press("Enter")
    try:
        tab.wait_for(
            "!!document.querySelector('[data-testid=\"account-menu-panel\"]')", timeout=10
        )
        opened = True
    except TimeoutError:
        opened = False
    tab.sleep(0.4)
    d = desc(tab) or {}
    check(
        "account.focus-moves-into-the-panel-on-open",
        opened and d.get("inAccount"),
        {k: d.get(k) for k in ("id", "testid", "tag")},
    )
    tab.press("Tab")
    tab.sleep(0.2)
    d = desc(tab) or {}
    check(
        "account.tab-enters-the-panel",
        bool(d and d.get("inAccount")),
        {k: d.get(k) for k in ("tag", "label")},
    )
    tab.press("Escape")
    try:
        tab.wait_for(
            "!document.querySelector('[data-testid=\"account-menu-panel\"]')", timeout=10
        )
        closed = True
    except TimeoutError:
        closed = False
    tab.sleep(0.5)
    d = desc(tab) or {}
    check(
        "account.escape-returns-focus-to-the-avatar",
        closed and d.get("testid") == "account-menu-toggle",
        {k: d.get(k) for k in ("testid", "label")},
    )


# ---------------------------------------------------------------- 4
def handoff(tab, label: str) -> None:
    """Assert the routed page owns focus after a chrome-driven navigation."""
    d = desc(tab) or {}
    check(
        f"handoff.{label}.leaves-chrome",
        d.get("inMain") or d.get("id") == "main-content",
        {k: d.get(k) for k in ("testid", "id", "tag", "inMain")},
    )


def area_handoff_desktop(tab) -> None:
    # (a) primary tab
    goto(tab, "/dashboard")
    d = focus_js(
        tab,
        "document.querySelector('[data-testid=\"topbar-tabs\"] a[href=\"/transactions\"]').focus()",
    )
    check(
        "handoff.primary-tab-receives-focus",
        bool(d and d.get("href") == "/transactions" and d.get("inTabs")),
        {k: (d or {}).get(k) for k in ("href", "label", "inTabs")},
    )
    tab.press("Enter")
    try:
        tab.wait_for("location.pathname === '/transactions'", timeout=15)
    except TimeoutError:
        pass
    settle(tab, extra=1.5)
    handoff(tab, "from-primary-tab")

    # (b) inside the More panel — the restore-order case
    goto(tab, "/dashboard")
    focus_js(tab, "document.querySelector('[data-testid=\"more-toggle\"]').focus()")
    tab.press("Enter")
    try:
        tab.wait_for("!!document.querySelector('[data-testid=\"more-panel\"]')", timeout=10)
    except TimeoutError:
        pass
    d = focus_js(
        tab,
        "document.querySelector('[data-testid=\"more-panel\"] a[href=\"/calendar\"]').focus()",
    )
    check(
        "handoff.calendar-link-receives-focus",
        bool(d and d.get("href") == "/calendar" and d.get("inMore")),
        {k: (d or {}).get(k) for k in ("href", "label", "inMore")},
    )
    tab.press("Enter")
    try:
        tab.wait_for("location.pathname === '/calendar'", timeout=15)
    except TimeoutError:
        pass
    settle(tab, extra=1.5)
    d = desc(tab) or {}
    check(
        "handoff.from-More-panel-not-back-to-the-toggle",
        d.get("id") == "main-content",
        {k: d.get(k) for k in ("id", "testid", "tag", "label")},
    )


def area_handoff_mobile(tab) -> None:
    goto(tab, "/dashboard")
    tab.force_viewport(390, 844)
    tab.sleep(1.5)
    present = tab.evaluate(
        "({bar: !!document.querySelector('[data-testid=\"mobile-tab-bar\"]'),"
        " topbar: !!document.querySelector('[data-testid=\"app-topbar\"]')})"
    )
    check(
        "mobile.chrome-swaps-to-the-tab-bar",
        present and present.get("bar") and not present.get("topbar"),
        present,
    )
    order = tab.evaluate(
        "(() => { const m = document.getElementById('main-content');"
        " const b = document.querySelector('[data-testid=\"mobile-tab-bar\"]');"
        " if (!m || !b) return null;"
        " return !!(m.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING); })()"
    )
    check("mobile.tab-bar-comes-after-the-routed-page", order, order)

    d = focus_js(
        tab,
        "document.querySelector('[data-testid=\"mobile-tab-bar\"] a[href=\"/transactions\"]').focus()",
    )
    check(
        "mobile.activity-tab-receives-focus",
        bool(d and d.get("href") == "/transactions"),
        {k: (d or {}).get(k) for k in ("href", "label")},
    )
    tab.press("Enter")
    try:
        tab.wait_for("location.pathname === '/transactions'", timeout=15)
    except TimeoutError:
        pass
    settle(tab, extra=1.5)
    d = desc(tab) or {}
    check(
        "handoff.from-the-bottom-tab-bar",
        d.get("id") == "main-content",
        {k: d.get(k) for k in ("id", "testid", "tag", "label")},
    )
    tab.force_viewport(1440, 900)
    tab.sleep(1.0)


def main() -> int:
    started = time.time()
    from probe_target import require_target
    require_target(BASE, who="shell_focus_probe.py")
    browser = Browser()
    tab = None
    errors: list[str] = []
    try:
        tab = browser.new_tab(f"{BASE}/login")
        tab.enable_runtime()
        tab.force_viewport(1440, 900)
        login(tab)
        print(f"--- signed in, at {tab.evaluate('location.pathname')}", flush=True)

        for fn in (
            area_order_and_visible,
            area_more_escape,
            area_account_escape,
            area_handoff_desktop,
            area_handoff_mobile,
        ):
            print(f"--- {fn.__name__}", flush=True)
            try:
                fn(tab)
            except Exception as exc:  # noqa: BLE001 - one area must not hide the rest
                errors.append(f"{fn.__name__}: {type(exc).__name__}: {exc}")
                check(f"{fn.__name__}.ran", False, errors[-1])
        errors.extend(tab.console_errors())
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass

    passed = sum(1 for c in CHECKS if c["ok"])
    report = {
        "card": "t_23b8d18e",
        "base": BASE,
        "served_build_commit": _stamp(),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": round(time.time() - started, 1),
        "passed": passed,
        "failed": len(CHECKS) - passed,
        "console_errors": errors,
        "checks": CHECKS,
    }
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"\n{passed} passed / {len(CHECKS) - passed} failed in "
        f"{report['duration_s']}s -> {OUT}",
        flush=True,
    )
    if errors:
        print("console/errors:", errors, flush=True)
    return 0 if passed == len(CHECKS) else 1


def _stamp() -> str:
    import urllib.request

    try:
        html = urllib.request.urlopen(f"{BASE}/", timeout=10).read().decode(
            "utf-8", "replace"
        )
        marker = 'build-commit" content="'
        i = html.index(marker) + len(marker)
        return html[i : html.index('"', i)]
    except Exception:  # noqa: BLE001
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())

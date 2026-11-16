#!/usr/bin/env python3
"""M72 interaction evidence: PWA registration, the mobile drawer, and
pull-to-refresh — exercised in a real headless Chrome against the production
build, not asserted from source.

Run:  python3 pwa_check.py --base http://127.0.0.1:4173 --out m72-pwa.json
Writes m72-pwa.json plus drawer-open / pull-to-refresh screenshots.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

os.environ["A11Y_PROFILE_DIR"] = os.path.join(
    tempfile.gettempdir(), "fintrack-m72-pwa"
)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "a11y"))
from a11y_driver import Browser  # noqa: E402

READY = (
    "!document.querySelector('[data-testid=\"route-fallback\"]') && "
    "!!document.querySelector('main') && document.querySelector('main').innerText.length > 60"
)


def login(tab, base: str) -> None:
    tab.navigate(f"{base}/login")
    tab.wait_for("document.readyState === 'complete'")
    for _ in range(40):
        box = tab.evaluate(
            """(() => {
              const b = [...document.querySelectorAll('button')]
                .find((x) => x.innerText.includes('Explore the demo'));
              if (!b) return null;
              const r = b.getBoundingClientRect();
              return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
            })()"""
        )
        if box:
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("demo button never appeared")
    tab.click_at(box["x"], box["y"])
    tab.wait_for("location.pathname === '/dashboard'", timeout=30)


def touch(tab, kind: str, points: list[dict]) -> None:
    tab.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:4173")
    ap.add_argument("--out", default="m72-pwa.json")
    ap.add_argument("--shots", default="m72-screenshots")
    args = ap.parse_args()
    result: dict = {}

    b = Browser(width=390, height=844)
    try:
        tab = b.new_tab("about:blank")
        tab.enable_runtime()
        tab.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True,
             "screenWidth": 390, "screenHeight": 844},
        )
        tab.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        login(tab, args.base)
        tab.wait_for(READY, timeout=30)
        tab.sleep(1.5)

        # ---- 1. installability surface -----------------------------------
        result["manifest"] = tab.evaluate(
            """(async () => {
              const link = document.querySelector('link[rel="manifest"]');
              if (!link) return { link: null };
              const res = await fetch(link.href);
              const m = await res.json();
              const icons = [];
              for (const icon of m.icons) {
                const r = await fetch(icon.src);
                icons.push({ src: icon.src, status: r.status, type: r.headers.get('content-type') });
              }
              return {
                link: link.href,
                status: res.status,
                type: res.headers.get('content-type'),
                name: m.name, display: m.display, start_url: m.start_url,
                scope: m.scope, theme_color: m.theme_color,
                icons,
                appleIcon: document.querySelector('link[rel="apple-touch-icon"]')?.href ?? null,
                themeColor: document.querySelector('meta[name="theme-color"]')?.content,
                viewport: document.querySelector('meta[name="viewport"]')?.content,
              };
            })()""",
            await_promise=True,
        )

        # ---- 2. service worker -------------------------------------------
        tab.sleep(1.0)
        result["serviceWorker"] = tab.evaluate(
            """(async () => {
              const reg = await navigator.serviceWorker.getRegistration();
              if (!reg) return { registered: false, scope: location.origin };
              await new Promise((r) => setTimeout(r, 500));
              return {
                registered: true,
                scope: reg.scope,
                scriptURL: reg.active ? reg.active.scriptURL : (reg.installing ? reg.installing.scriptURL : null),
                controller: !!navigator.serviceWorker.controller,
                secureContext: window.isSecureContext,
              };
            })()""",
            await_promise=True,
        )
        # Reload once so the claiming worker actually controls the document.
        tab.navigate(f"{args.base}/dashboard")
        tab.wait_for(READY, timeout=30)
        result["serviceWorker"]["controlledAfterReload"] = tab.evaluate(
            "!!navigator.serviceWorker.controller"
        )

        # ---- 3. offline app shell ----------------------------------------
        tab.send(
            "Network.enable", {}
        )
        tab.send(
            "Network.emulateNetworkConditions",
            {"offline": True, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
        )
        tab.navigate(f"{args.base}/budgets")
        time.sleep(2.0)
        # AuthShell renders no <main>, so "did the app boot?" is: title + a
        # mounted React root + real app text, never a Chrome network-error page.
        result["offline"] = tab.evaluate(
            """(() => ({
              pathname: location.pathname,
              title: document.title,
              rootChildren: document.getElementById('root')?.children.length ?? -1,
              appRendered: (document.body.innerText || '').includes('Explore the demo'),
              chromeErrorPage: /ERR_|offline/i.test(document.body.innerText || ''),
              controller: !!navigator.serviceWorker.controller,
              routeFallback: !!document.querySelector('[data-testid="route-fallback"]'),
            }))()"""
        )
        tab.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
        )

        # ---- 4. drawer open/close ----------------------------------------
        tab.navigate(f"{args.base}/dashboard")
        tab.wait_for(READY, timeout=30)
        tab.sleep(1.2)
        toggle = """(() => {
            const t = document.querySelector('[data-testid="nav-drawer-toggle"]');
            if (!t) return null;
            const r = t.getBoundingClientRect();
            return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
          })()"""
        box = tab.evaluate(toggle)
        result["drawer"] = {"togglePresent": box is not None}
        if box:
            result["drawer"]["before"] = tab.evaluate(
                """(() => { const n = document.getElementById('app-nav'); const r = n.getBoundingClientRect();
                    return { inert: n.inert, x: Math.round(r.x), width: Math.round(r.width) }; })()"""
            )
            tab.click_at(box["x"], box["y"])
            tab.sleep(0.6)
            result["drawer"]["open"] = tab.evaluate(
                """(() => {
                  const n = document.getElementById('app-nav');
                  const r = n.getBoundingClientRect();
                  const t = document.querySelector('[data-testid="nav-drawer-toggle"]');
                  return { inert: n.inert, x: Math.round(r.x), width: Math.round(r.width),
                           ariaExpanded: t.getAttribute('aria-expanded'),
                           scrim: !!document.querySelector('[data-testid="nav-scrim"]'),
                           navLinkVisible: !!n.querySelector('a[href="/budgets"]') };
                })()"""
            )
            os.makedirs(args.shots, exist_ok=True)
            tab.screenshot(os.path.join(args.shots, "drawer-open-390x844.png"))
            # `Input.dispatchKeyEvent(type=rawKeyDown)` does not produce a DOM
            # keydown in this headless build (verified: a page listener never
            # saw it), so dispatch the event directly — the handler under test
            # is the app's, not Chrome's key plumbing.
            tab.evaluate(
                "window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))"
            )
            tab.sleep(0.6)
            result["drawer"]["afterEscape"] = tab.evaluate(
                """(() => { const n = document.getElementById('app-nav'); const r = n.getBoundingClientRect();
                    return { inert: n.inert, x: Math.round(r.x),
                             ariaExpanded: document.querySelector('[data-testid="nav-drawer-toggle"]').getAttribute('aria-expanded'),
                             scrim: !!document.querySelector('[data-testid="nav-scrim"]') }; })()"""
            )

        # ---- 5. pull-to-refresh ------------------------------------------
        # The gesture is deliberately disabled while the drawer is open, so
        # make sure it is closed before measuring it.
        tab.evaluate(
            "document.querySelector('[data-testid=\"nav-drawer-close\"]')?.click()"
        )
        tab.sleep(0.4)
        api_calls = "performance.getEntriesByType('resource').filter((e) => e.name.includes('/api/')).length"
        before = tab.evaluate(api_calls)
        tab.evaluate("window.scrollTo(0, 0)")
        touch(tab, "touchStart", [{"x": 195, "y": 300, "id": 1}])
        phases = []
        for y in (320, 370, 420, 470, 500):
            touch(tab, "touchMove", [{"x": 195, "y": y, "id": 1}])
            tab.sleep(0.12)
            phases.append(
                tab.evaluate(
                    "(() => { const el = document.querySelector('[data-testid=\"pull-to-refresh\"]');"
                    " return el ? { phase: el.dataset.phase, text: el.innerText.trim() } : null; })()"
                )
            )
        result["pullToRefresh"] = {"phases": phases}
        os.makedirs(args.shots, exist_ok=True)
        tab.screenshot(os.path.join(args.shots, "pull-to-refresh-390x844.png"))
        touch(tab, "touchEnd", [])
        tab.sleep(0.4)
        result["pullToRefresh"]["afterRelease"] = tab.evaluate(
            "(() => { const el = document.querySelector('[data-testid=\"pull-to-refresh\"]');"
            " return el ? { phase: el.dataset.phase, text: el.innerText.trim() } : null; })()"
        )
        tab.sleep(3.0)
        result["pullToRefresh"]["settled"] = tab.evaluate(
            "(() => { const el = document.querySelector('[data-testid=\"pull-to-refresh\"]');"
            " return { present: !!el, phase: el ? el.dataset.phase : null }; })()"
        )
        after = tab.evaluate(api_calls)
        result["pullToRefresh"]["apiCallsBefore"] = before
        result["pullToRefresh"]["apiCallsAfter"] = after
        result["pullToRefresh"]["refetched"] = after > before

        result["consoleErrors"] = tab.console_errors()
    finally:
        b.close()

    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=1)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""M114 live check: the same four numbers, measured on the deployed stack.

Run once BEFORE the deploy and once AFTER it against
https://fintrac.princessandmark.com:

    python3 live_check.py --base https://fintrac.princessandmark.com \\
        --phase before --out scripts/m114/m114-live-before.json

What it records, per phase:

  * the shell the browser actually loaded (entry script + stylesheet)
  * `GET /` `Cache-Control` (the no-cache contract docs/09 pins on index.html)
  * the caches the registered worker owns — the BUILD_ID stamp is the claim
    `fintrack-shell-v1` -> `fintrack-shell-v1-<id>`
  * `fetch('/assets/CommandPalette-DMeurLAO.js')` THROUGH the worker: the exact
    URL from Mark's "Unexpected Application Error". `before` must be 404 (the
    bug), `after` must be 200 with the reload-marker body (the self-heal),
  * whether ⌘K opens the palette cleanly on the CURRENT shell (and how many
    console errors came with it).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]  # .../frontend
A11Y = ROOT / "scripts" / "a11y"

# The chunk hash from the bug report: absent from every deploy since.
MISSING_CHUNK = "/assets/CommandPalette-DMeurLAO.js"

PALETTE_OK = "!!document.querySelector('[data-testid=\"command-palette\"]')"
# Desktop rail trigger (M95) and the <768px header magnifier (M95 P1).
TRIGGERS = (
    '[data-testid="command-palette-trigger"], '
    '[data-testid="command-palette-trigger-mobile"]'
)
TRIGGER_READY = f"!!document.querySelector('{TRIGGERS}')"
OPEN_PALETTE = (
    f"(() => {{ const b = document.querySelector('{TRIGGERS}');"
    " if (!b) return 'no trigger'; b.click(); return 'clicked'; })()"
)
ERROR_UI = (
    "(() => { const t = document.body.innerText || ''; "
    "return /Unexpected Application Error|error loading dynamically imported module|"
    "Failed to fetch dynamically imported module/.test(t); })()"
)


def login(tab, base: str) -> None:
    for _ in range(60):
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
    try:
        tab.wait_for("location.pathname === '/dashboard'", timeout=45)
    except TimeoutError as exc:
        body = ""
        try:
            body = str(tab.evaluate("document.body.innerText"))[:500]
        except Exception:
            pass
        raise RuntimeError(f"demo login never reached /dashboard; body={body!r}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="https://fintrac.princessandmark.com")
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    parser.add_argument("--cdp-port", type=int, default=9850)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--out", default=str(A11Y / "m114-live.json"))
    args = parser.parse_args()

    sys.path.insert(0, str(A11Y))
    os.environ["A11Y_PROFILE_DIR"] = tempfile.mkdtemp(prefix=f"m114-live-{args.phase}-")
    os.environ["A11Y_CDP_PORT"] = str(args.cdp_port)
    from a11y_driver import Browser

    record: dict = {"phase": args.phase, "base": args.base}
    browser = Browser(headless_old=True, width=1440, height=900)
    try:
        tab = browser.new_tab(f"{args.base}/login")
        tab.enable_runtime()
        tab.navigate(f"{args.base}/login")
        tab.wait_for("document.readyState === 'complete'")
        # Headless clamps --window-size below 500 CSS px (AGENTS §5): pin the
        # emulation AFTER the document is up, and again after the login
        # navigation, so a phone-sized run really lays the page out phone-sized.
        tab.force_viewport(args.width, args.height)
        record["viewport"] = tab.viewport()

        # The shell itself, before anything else touches it.
        record["entry_on_load"] = tab.evaluate(
            "(document.querySelector('script[type=\"module\"]')||{}).src || ''"
        )
        record["index_cache_control"] = tab.evaluate(
            "fetch('/index.html', {cache: 'reload'}).then(r => r.headers.get('cache-control'))",
            await_promise=True,
        )

        login(tab, args.base)
        tab.force_viewport(args.width, args.height)
        tab.wait_for("!!navigator.serviceWorker && !!navigator.serviceWorker.controller",
                     timeout=45)
        record["controller"] = tab.evaluate(
            "navigator.serviceWorker.controller && navigator.serviceWorker.controller.scriptURL"
        )
        record["caches"] = tab.evaluate("caches.keys()", await_promise=True)
        record["indexed_cache_control"] = tab.evaluate(
            "fetch('/index.html', {cache: 'reload'}).then(r => r.headers.get('cache-control'))",
            await_promise=True,
        )

        # The self-heal, through the worker, on the bug's own URL.
        missing_js = (
            "fetch(" + json.dumps(MISSING_CHUNK) + ").then(async r => {"
            " const body = await r.text();"
            " return { status: r.status, type: r.headers.get('content-type'),"
            " marker: body.indexOf('M114 stale-shell self-heal') >= 0,"
            " head: body.slice(0, 90) }; })"
            ".catch(e => ({ error: String(e) }))"
        )
        record["missing_chunk"] = tab.evaluate(missing_js, await_promise=True)

        # ⌘K on the current shell.
        tab.evaluate(
            "(() => { window.__m114cc = 0; navigator.serviceWorker"
            ".addEventListener('controllerchange', () => { window.__m114cc++; });"
            " return true; })()"
        )
        # The sidebar mounts with the shell; give the hydrated app its moment
        # before declaring the trigger absent (measured: one immediate read on
        # prod caught the shell mid-render and reported "no trigger" while the
        # screenshot seconds later showed the button).
        ready = time.time() + 20
        while time.time() < ready:
            if tab.evaluate(TRIGGER_READY):
                break
            time.sleep(0.3)
        record["trigger"] = tab.evaluate(OPEN_PALETTE)
        deadline = time.time() + 25
        opened = {"palette": False, "error": False}
        entry_before = record["entry_on_load"]
        while time.time() < deadline:
            if tab.evaluate(PALETTE_OK):
                opened["palette"] = True
                break
            if tab.evaluate(ERROR_UI):
                opened["error"] = True
                if args.phase == "before":
                    break
            entry_now = tab.evaluate(
                "(document.querySelector('script[type=\"module\"]')||{}).src || ''"
            )
            if entry_now and entry_now != entry_before:
                opened["reloaded_entry"] = entry_now
                break
            time.sleep(0.25)
        if opened.get("reloaded_entry"):
            # The heal replaced the document; open the palette on the new shell.
            for _ in range(80):
                if tab.evaluate(TRIGGER_READY):
                    break
                time.sleep(0.3)
            tab.evaluate(OPEN_PALETTE)
            for _ in range(60):
                if tab.evaluate(PALETTE_OK):
                    opened["palette_after_reload"] = True
                    break
                time.sleep(0.3)
            opened["error_after_reload"] = bool(tab.evaluate(ERROR_UI))

        record.update(opened)
        record["entry_final"] = tab.evaluate(
            "(document.querySelector('script[type=\"module\"]')||{}).src || ''"
        )
        record["caches_final"] = tab.evaluate("caches.keys()", await_promise=True)
        record["controllerchange"] = tab.evaluate("window.__m114cc || 0")
        record["pathname"] = tab.evaluate("location.pathname")
        record["console_errors"] = [
            line for line in tab.console_errors() if "favicon" not in line
        ][:8]
        # Named per viewport: the mobile-PWA-ish run and the desktop run are
        # different evidence and must not overwrite each other.
        shot = A11Y / f"m114-live-{args.phase}-{args.width}x{args.height}.png"
        record["screenshot"] = tab.screenshot(str(shot))
        return 0
    finally:
        browser.close()
        pathlib.Path(args.out).write_text(json.dumps(record, indent=2))
        print(json.dumps(record, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())

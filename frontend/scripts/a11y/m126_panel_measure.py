"""One-off M126 measurement: how wide may the expanded panel be before the
range control is clipped at 390/412? Prints the real rects instead of
guessing CSS chrome (main padding + card padding + cell padding + scrollbar).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from a11y_driver import Browser  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://fintrac.princessandmark.com"
WIDTH = int(sys.argv[2]) if len(sys.argv) > 2 else 390
HEIGHT = int(sys.argv[3]) if len(sys.argv) > 3 else 844


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


def main() -> int:
    from probe_target import require_target
    require_target(BASE, who="m126_panel_measure.py")
    browser = Browser(width=WIDTH, height=HEIGHT)
    try:
        tab = browser.new_tab(f"{BASE}/login")
        tab.enable_runtime()
        tab.navigate(f"{BASE}/login", timeout=45)
        settle(tab, 1.0)
        tab.force_viewport(WIDTH, HEIGHT)
        ok = tab.evaluate(
            """(() => {
                 const b = Array.from(document.querySelectorAll('button'))
                   .find(n => /Explore the demo/i.test(n.textContent || ''));
                 if (b) b.click();
                 return !!b;
               })()"""
        )
        print("demo door:", ok)
        settle(tab, 3.0)
        tab.navigate(f"{BASE}/investments")
        settle(tab, 3.0)

        opened = False
        for _ in range(20):
            clicked = tab.evaluate(
                """(() => {
                     const b = Array.from(document.querySelectorAll('button'))
                       .find(n => (n.getAttribute('aria-label') || '')
                         .startsWith('Show '));
                     if (b) b.click();
                     return !!b;
                   })()"""
            )
            if clicked:
                opened = tab.evaluate(
                    "!!document.querySelector('[data-testid=\"position-history-row\"]')"
                )
                if opened:
                    break
            tab.sleep(0.6)
        print("panel open:", opened)
        if not opened:
            return 1

        tab.evaluate(
            """(() => {
                 const p = document.querySelector('[data-testid="position-history-row"]');
                 if (p) p.scrollIntoView({block: 'center'});
               })()"""
        )
        tab.sleep(0.5)
        rects = tab.evaluate(
            """(() => {
                 const panel = document.querySelector('[data-testid="position-history-row"]');
                 const cell = panel && panel.querySelector('td');
                 const wrap = cell && cell.firstElementChild;
                 const group = panel && panel.querySelector('[role="group"]');
                 const region = document.querySelector('[aria-label="Positions table"]');
                 const r = (n) => n ? (({x, right, width}) => ({x: Math.round(x),
                    right: Math.round(right), width: Math.round(width)}))(n.getBoundingClientRect()) : null;
                 return {
                   innerWidth: window.innerWidth,
                   innerHeight: window.innerHeight,
                   scrollX: window.scrollX,
                   region: r(region),
                   cell: r(cell),
                   wrap: r(wrap),
                   group: r(group),
                   groupScroll: group ? {scrollW: group.scrollWidth,
                                         clientW: group.clientWidth} : null,
                   clipped: group ? group.getBoundingClientRect().right > window.innerWidth : null,
                 };
               })()"""
        )
        print(json.dumps(rects, indent=2))
        return 0
    finally:
        browser.close()


if __name__ == "__main__":
    raise SystemExit(main())

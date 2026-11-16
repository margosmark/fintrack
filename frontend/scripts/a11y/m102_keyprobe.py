"""M102 keyboard probe: reach + scroll every named scroll region at 390x915.

For each region it records (a) that `focus()` lands on the box (`tabIndex 0`) and
how far the content overflows, (b) that five real `ArrowRight` key events pan it
— native focused-scrollbox behaviour, which is what WCAG 2.1.1 asks for — and
(c) how many Tab presses from the top of the document reach it without a mouse.

The viewport is forced with `Emulation.setDeviceMetricsOverride`, the same path
`axe_scan.py` uses: headless Chrome clamps `--window-size` below 500 CSS px, so
a `--window-size 390` probe would silently lay out at 500.

Run against a built frontend served by the local proxy:

    python3 scripts/local/serve_proxy.py --dist dist --port 4199 &
    A11Y_BASE=http://127.0.0.1:4199 A11Y_CDP_PORT=9481 \\
        A11Y_PROFILE_DIR=<tmpdir> python3 scripts/a11y/m102_keyprobe.py
"""

import json
import os
import sys

sys.path.insert(0, "scripts/a11y")
from a11y_driver import Browser  # noqa: E402

BASE = os.environ.get("A11Y_BASE", "http://127.0.0.1:4199")
from probe_target import require_target  # noqa: E402
require_target(BASE, who="m102_keyprobe.py")

STATE = """
(() => {
  const a = document.activeElement;
  return {
    activeLabel: a ? (a.getAttribute && a.getAttribute('aria-label')) : null,
    activeTag: a ? a.tagName : null,
    activeRole: a ? a.getAttribute('role') : null,
    scrollLeft: a && 'scrollLeft' in a ? a.scrollLeft : null,
    regions: [...document.querySelectorAll('div[role="region"]')].map(e => ({
      label: e.getAttribute('aria-label'),
      tabindex: e.tabIndex,
      overflow: e.scrollWidth - e.clientWidth,
    })).filter(r => r.label),
  };
})()
"""


def run(path, label):
    tab.navigate(f"{BASE}{path}")
    tab.wait_for("document.readyState === 'complete'", timeout=20)
    tab.sleep(2.5)
    out = {
        "path": path,
        "label": label,
        "viewport": tab.evaluate("[window.innerWidth, window.innerHeight]"),
    }
    # 1. is the region focusable, and does a real ArrowRight scroll it?
    setup = tab.evaluate(f"""
      (() => {{
        const el = [...document.querySelectorAll('div[role="region"]')]
          .find(e => e.getAttribute('aria-label') === {json.dumps(label)});
        if (!el) return {{found: false}};
        el.focus();
        return {{found: true, focused: document.activeElement === el,
                 tabindex: el.tabIndex, scrollWidth: el.scrollWidth,
                 clientWidth: el.clientWidth, scrollLeft: el.scrollLeft,
                 overflow: el.scrollWidth - el.clientWidth}};
      }})()
    """)
    out["focus"] = setup
    before = (setup or {}).get("scrollLeft")
    if setup and setup.get("focused") and setup.get("overflow", 0) > 0:
        for _ in range(5):
            tab.press("ArrowRight")
        tab.sleep(0.4)
        out["after_5_arrowright"] = tab.evaluate("document.activeElement.scrollLeft")
        out["before"] = before
    # 2. can a keyboard-only user REACH it with Tab?
    tab.evaluate("document.activeElement && document.activeElement.blur()")
    reached = None
    for i in range(1, 260):
        tab.press("Tab")
        if i % 5 == 0:
            tab.sleep(0.02)
        st = tab.evaluate(STATE)
        if st.get("activeLabel") == label:
            reached = {"tab_presses": i, "activeRole": st.get("activeRole")}
            break
    out["tab_reach"] = reached
    out["regions_on_page"] = tab.evaluate(STATE)["regions"]
    return out


b = Browser(width=390, height=915)
results = []
try:
    tab = b.new_tab(f"{BASE}/login")
    tab.enable_runtime()
    tab.send(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 915, "deviceScaleFactor": 1, "mobile": False},
    )
    tab.navigate(f"{BASE}/login")
    tab.wait_for("document.readyState === 'complete'", timeout=20)
    tab.sleep(1.5)
    tab.evaluate(
        "Array.from(document.querySelectorAll('button')).find(b => "
        "b.textContent.includes('Explore the demo'))?.click()"
    )
    tab.wait_for("location.pathname === '/dashboard'", timeout=25)
    tab.sleep(2)
    results.append(run("/investments", "Trades log table"))
    results.append(run("/taxes", "Capital gains 2026 table"))
    results.append(run("/taxes", "ACB per holding (non-registered) table"))
    print(json.dumps(results, indent=1))
finally:
    b.close()

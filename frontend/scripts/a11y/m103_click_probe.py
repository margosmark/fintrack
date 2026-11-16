"""Evidence probe: what does clicking /investments' "Add a symbol" actually do?

Answers the open question in the card with a click -> DOM delta at two widths:
is the control inert (a real defect), or does it do something keywalk's
dialog_probe heuristic cannot see (focus move / a surface that adds no
input/select/textarea and no live-region text)?

Design note: an unmodified page keeps rendering for seconds after load (the
positions table and its 28 checkboxes land late), so a naive before/after pair
attributes that async work to the click. Every width therefore measures
  1. a CONTROL window (same duration, NO click) over a page that has stopped
     changing (stable element count), then
  2. the CLICK window over a fresh load,
so any delta reported for the click is the click's own.

Run:
    A11Y_BASE=http://127.0.0.1:4175 A11Y_CDP_PORT=9477 \
    A11Y_PROFILE_DIR=/tmp/m103probe \
    python3 scripts/a11y/m103_click_probe.py --out m103-click-probe.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from a11y_driver import Browser

BASE = os.environ.get("A11Y_BASE", "http://127.0.0.1:4175")

# Same trigger selection keywalk.py uses, so this probe clicks exactly what
# the failing contract clicked.
TRIGGER_JS = """
(() => {
  const wanted = /^(add|new|create|edit|open)\\b/i;
  const btn = [...document.querySelectorAll("button, [role='button'], a")]
    .filter((b) => b.offsetParent !== null && wanted.test((b.textContent || "").trim()))
    .find((b) => !b.disabled);
  if (!btn) return null;
  const label = (btn.textContent || "").trim().slice(0, 50);
  btn.click();
  return label;
})()
"""

# Snapshot = everything dialog_probe looks at, PLUS the two things it misses:
# where focus landed, and whether any element appeared at all.
SNAPSHOT_JS = """
(() => {
  const sig = (el) => {
    const g = (n) => el.getAttribute(n) || "";
    const cls = (typeof el.className === "string" ? el.className : "").slice(0, 60);
    return [el.tagName.toLowerCase(), g("role"), g("data-testid"), g("placeholder"),
            g("aria-expanded"), cls].join("|");
  };
  const counts = {};
  for (const el of document.querySelectorAll("body *")) {
    const s = sig(el);
    counts[s] = (counts[s] || 0) + 1;
  }
  const desc = (el) => {
    if (!el || el === document.body || el === document.documentElement) return { tag: "body" };
    const g = (n) => el.getAttribute(n) || "";
    return {
      tag: el.tagName.toLowerCase(),
      role: g("role"),
      placeholder: g("placeholder"),
      ariaLabel: g("aria-label"),
      testid: g("data-testid"),
      text: (el.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 60),
    };
  };
  const live = document.querySelector('[role="status"], [role="alert"], [aria-live]');
  return {
    url: location.pathname + location.search,
    viewport: { w: window.innerWidth, h: window.innerHeight },
    elements: document.querySelectorAll("body *").length,
    innerLen: document.body.innerHTML.length,
    visibleControls: [...document.querySelectorAll("input, select, textarea")]
      .filter((el) => el.offsetParent !== null).length,
    dialogs: document.querySelectorAll('[role="dialog"]').length,
    listboxes: document.querySelectorAll('[role="listbox"]').length,
    listboxVisible: [...document.querySelectorAll('[role="listbox"]')]
      .filter((el) => el.offsetParent !== null).length,
    comboboxes: [...document.querySelectorAll('[role="combobox"]')].map((i) => ({
      placeholder: i.placeholder,
      expanded: i.getAttribute("aria-expanded"),
      focused: document.activeElement === i,
      disabled: !!i.disabled,
    })),
    live: live ? (live.textContent || "").trim().slice(0, 120) : "",
    active: desc(document.activeElement),
    activeIsBody: document.activeElement === document.body,
    counts,
  };
})()
"""


def diff_counts(before: dict, after: dict) -> dict:
    keys = set(before) | set(after)
    added = {
        k: after[k] - before.get(k, 0) for k in sorted(keys) if after.get(k, 0) > before.get(k, 0)
    }
    removed = {
        k: before[k] - after.get(k, 0) for k in sorted(keys) if before.get(k, 0) > after.get(k, 0)
    }
    return {"added": added, "removed": removed}


def snap(tab) -> dict:
    """CDP evaluates to `object`; this script only ever reads dicts."""
    return tab.evaluate(SNAPSHOT_JS)  # type: ignore[return-value]


SETTLED_JS = (
    "document.querySelector('[data-testid=\"route-fallback\"]') === null"
    " && document.querySelector('main') !== null"
)


def settle(tab) -> None:
    try:
        tab.wait_for(SETTLED_JS, timeout=25)
    except TimeoutError:
        pass
    tab.sleep(1.5)


def wait_stable(tab, window: float = 1.2, timeout: float = 25.0) -> int:
    """Return the element count once it has held still for `window` seconds."""
    deadline = time.time() + timeout
    last = -1
    stable_since = time.time()
    while time.time() < deadline:
        n = int(tab.evaluate("document.querySelectorAll('body *').length"))
        if n != last:
            last = n
            stable_since = time.time()
        elif time.time() - stable_since >= window:
            return n
        tab.sleep(0.25)
    return last


def load_page(tab, path: str, width: int, height: int) -> int:
    tab.navigate(f"{BASE}{path}")
    settle(tab)
    landed = tab.evaluate("location.pathname")
    assert landed == path, f"landed on {landed}"
    # Shared driver helper (a11y_driver.Tab.force_viewport): `--window-size` is
    # NOT the viewport — headless Chrome clamps a window below 500 CSS px, so
    # the override is re-applied once a document is up and the run aborts unless
    # the page really laid out at the width the evidence claims.
    got = list(tab.force_viewport(width, height))
    if got != [width, height]:
        raise RuntimeError(f"viewport {got} != requested {[width, height]}")
    return wait_stable(tab)


def open_session(browser: Browser, width: int, height: int):
    tab = browser.new_tab(f"{BASE}/login")
    tab.enable_runtime()
    tab.send("Network.enable")
    tab.force_viewport(width, height)
    tab.navigate(f"{BASE}/login")
    settle(tab)
    tab.evaluate(
        "Array.from(document.querySelectorAll('button')).find("
        "b => b.textContent.includes('Explore the demo'))?.click()"
    )
    tab.wait_for("location.pathname === '/dashboard'", timeout=25)
    settle(tab)
    return tab


def keywalk_view(before: dict, after: dict) -> dict:
    """M58's four checks — dialog, navigation, form controls, live region.

    Kept verbatim so the evidence JSON reproduces the FAIL the card reports.
    """
    if after["dialogs"] > 0:
        return {"result": "dialog-opened"}
    if after["url"] != before["url"]:
        return {"result": "non-dialog-surface", "effect": f"navigated to {after['url']}"}
    delta = after["visibleControls"] - before["visibleControls"]
    if delta > 0:
        return {
            "result": "non-dialog-surface",
            "effect": f"inline form opened (+{delta} visible controls)",
        }
    if after["live"]:
        return {"result": "non-dialog-surface", "effect": f"live region: {after['live']}"}
    return {"result": "no-effect"}


def keywalk_view_m103(before: dict, after: dict, clicked: dict) -> dict:
    """M58's four checks, then M103's two: focus moved, elements added.

    Falls through to `no-effect` only when NONE of the six sees anything, so a
    genuinely inert control still fails.
    """
    original = keywalk_view(before, after)
    if original["result"] != "no-effect":
        return {**original, "checks": "M58 four"}
    if clicked["focusMoved"]:
        active = clicked["activeAfter"]
        if active.get("tag") not in (None, "body"):
            label = (
                active.get("placeholder")
                or active.get("ariaLabel")
                or active.get("text")
                or active.get("tag")
            )
            return {
                "result": "non-dialog-surface",
                "effect": f"focus moved to <{active.get('tag')}> {label}",
                "checks": "M103 focus",
            }
    if clicked["elementsDelta"] != 0:
        moved = clicked["elementsDelta"]
        return {
            "result": "non-dialog-surface",
            "effect": f"surface rendered ({moved:+d} elements, no form controls)",
            "checks": "M103 elements",
        }
    return {**original, "checks": "M58 four"}


def window_delta(first: dict, last: dict) -> dict:
    return {
        "elementsDelta": last["elements"] - first["elements"],
        "innerLenDelta": last["innerLen"] - first["innerLen"],
        "visibleControlsDelta": last["visibleControls"] - first["visibleControls"],
        "listboxDelta": last["listboxVisible"] - first["listboxVisible"],
        "focusMoved": first["active"] != last["active"],
        "activeAfter": last["active"],
        "domDelta": diff_counts(first["counts"], last["counts"]),
    }


def run_pair(browser: Browser, width: int, height: int) -> dict:
    tab = open_session(browser, width, height)

    # --- control: a fresh load, 7 s, NO click ------------------------
    stable_control = load_page(tab, "/investments", width, height)
    control_first = snap(tab)
    tab.sleep(7.0)
    control_last = snap(tab)
    control = window_delta(control_first, control_last)

    # --- the click: fresh load, same 7 s window ----------------------
    stable_click = load_page(tab, "/investments", width, height)
    before = snap(tab)
    trigger: str | None = tab.evaluate(TRIGGER_JS)  # type: ignore[assignment]
    # dialog_probe waits up to 6 s for a dialog, then reads state; sample
    # early (so a data refresh cannot be mistaken for the click's effect)
    # and once more past the harness's own 6 s deadline.
    samples = [snap(tab)]
    for delay in (0.3, 0.7, 1.5, 4.5, 1.5):
        tab.sleep(delay)
        samples.append(snap(tab))
    after = samples[-1]
    clicked = window_delta(before, after)
    clicked["samples"] = [
        {"t": round(0.3 * i, 1), "elements": s["elements"]} for i, s in enumerate(samples)
    ]

    shot = Path(__file__).resolve().parent / f"m103-click-{width}.png"
    tab.screenshot(str(shot))
    tab.close()

    return {
        "width": width,
        "height": height,
        "stableElementCount": {"control": stable_control, "click": stable_click},
        "trigger": trigger,
        "control": control,
        "clicked": clicked,
        "before": {k: v for k, v in before.items() if k != "counts"},
        "after": {k: v for k, v in after.items() if k != "counts"},
        "keywalkWouldReportBefore": keywalk_view(before, after),
        "keywalkWouldReportAfterM103": keywalk_view_m103(before, after, clicked),
        "screenshot": str(shot),
    }


DEAD_CONTROL_JS = """
(() => {
  const b = document.createElement("button");
  b.id = "m103-dead-control";
  b.textContent = "Add nothing at all";
  document.body.prepend(b);
  return document.querySelector("button") === b;
})()
"""


def dead_control_check(browser: Browser) -> dict:
    """Negative control: the REAL `dialog_probe` must still call an inert
    control what it is.

    The M103 checks widen "observable effect", so the claim worth proving is
    that they widened it in ONE direction: a button with no handler,
    prepended ahead of the app's own controls (so the trigger selector picks
    it first), has to come back `no-effect` — otherwise the harness would now
    pass anything. Runs the shipped `dialog_probe` itself, not a re-statement
    of its logic.
    """
    from keywalk import dialog_probe  # same directory; keywalk only runs under __main__

    tab = open_session(browser, 1440, 900)
    load_page(tab, "/investments", 1440, 900)
    injected = tab.evaluate(DEAD_CONTROL_JS)
    result = dialog_probe(tab)
    tab.close()
    return {
        "injectedFirst": bool(injected),
        "expected": "no-effect",
        "result": result,
        "passes": result.get("result") == "no-effect",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "m103-click-probe.json"))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(BASE, who="m103_click_probe.py")

    report: dict = {"base": BASE, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "runs": []}
    browser = Browser(width=1440, height=900)
    try:
        for width, height in ((390, 915), (1440, 900)):
            print(f"--- {width}x{height}", flush=True)
            run = run_pair(browser, width, height)
            report["runs"].append(run)
            print(
                json.dumps(
                    {
                        "trigger": run["trigger"],
                        "stable": run["stableElementCount"],
                        "control": {k: v for k, v in run["control"].items() if k != "domDelta"},
                        "clicked": {k: v for k, v in run["clicked"].items() if k != "domDelta"},
                        "keywalkWouldReportBefore": run["keywalkWouldReportBefore"],
                        "keywalkWouldReportAfterM103": run["keywalkWouldReportAfterM103"],
                        "activeBefore": run["before"]["active"],
                        "activeAfter": run["after"]["active"],
                        "comboboxesBefore": run["before"]["comboboxes"],
                        "comboboxesAfter": run["after"]["comboboxes"],
                    },
                    indent=2,
                ),
                flush=True,
            )
            print(
                "clicked domDelta:",
                json.dumps(run["clicked"]["domDelta"], indent=2)[:1500],
                flush=True,
            )

        print("--- negative control: an inert button, real dialog_probe", flush=True)
        report["deadControl"] = dead_control_check(browser)
        print(json.dumps(report["deadControl"], indent=2), flush=True)
    finally:
        browser.close()

    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out}")
    return 0 if report.get("deadControl", {}).get("passes") else 1


if __name__ == "__main__":
    sys.exit(main())

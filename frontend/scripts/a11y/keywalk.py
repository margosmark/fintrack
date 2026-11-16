"""M58 channel C: the keyboard-only walkthrough (docs/09 "Audit method").

What an axe run cannot see, checked by actually driving the page:

* **tab order** — every focus stop on each of the six audited pages, its
  accessible name, and whether it paints a *visible* focus indicator
  (outline or ring). A stop with neither is a WCAG 2.4.7 failure.
* **first stop = skip link** — the `Skip to main content` link must be the
  first thing Tab reaches (WCAG 2.4.1), otherwise the sidebar rail has to be
  walked on every page.
* **no keyboard trap** — focus must advance on every Tab; three consecutive
  Tabs landing on the same element is reported as a trap.
* **dialogs** — where a page has an Add/Edit control: it opens, focus moves
  into the dialog, Escape closes it from anywhere in the document, and focus
  returns to the control that opened it. A control that opens an *inline*
  surface (Budgets/Bills create forms) or navigates (Dashboard's bill-reminder
  hand-off) has no dialog to contract-test and is reported as
  `non-dialog-surface`; so is one whose click **moves focus** or **adds
  elements** without adding a `input`/`select`/`textarea` (M103: `/investments`
  "Add a symbol" focuses the watchlist's security combobox, which mounts a
  `role="listbox"` of options — nothing the older checks could see). A control
  whose click produces no observable effect at all is a failure.
* **renderer idle** — at rest after load, per page: script time over a fixed
  window (CDP `Performance.getMetrics`). The M58 baseline froze on 4 pages
  because an unbounded re-render loop pinned the renderer at ~94-100%
  JavaScript, so keyboard input never got a turn; a page above the idle
  threshold fails here before it can wedge the walkthrough.
* **reduced motion** — with `prefers-reduced-motion: reduce` emulated, how
  many animations are still running (and on which properties).

Run (needs the production build served with `/api` proxied to a live backend):

    npm run build && npx vite preview --port 4173
    python3 frontend/scripts/a11y/keywalk.py

Writes `keywalk.json` next to this script and screenshots into
`docs/assets/a11y/`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from a11y_driver import Browser

BASE = os.environ.get("A11Y_BASE", "http://localhost:4173")
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SHOTS = REPO / "docs" / "assets" / "a11y"

PAGES = [
    ("dashboard", "/dashboard"),
    ("transactions", "/transactions"),
    ("investments", "/investments"),
    ("taxes", "/taxes"),
    ("budgets", "/budgets"),
    ("bills", "/bills"),
]

# Tab presses recorded per page: walk until focus CYCLES back to the first
# stop (every focusable on the page visited exactly once), capped so a page
# with an unbounded tab order cannot spin forever. 25 was never enough — the
# sidebar rail alone is ~25 stops, so the old fixed budget never reached a
# page's own content, which is where the audited controls live.
# Backstop; a completed lap normally ends the walk earlier (focus cycles to the
# first stop, recorded as lapEnd=first-stop-returned).
#
# SIZED FROM A MEASUREMENT, not a guess. `stops` is the lap; `presses` is what
# the cap actually has to clear and runs a little above stops, because a press
# that lands on `body` or repeats the previous stop spends budget without
# adding a stop (and moves ±1-2 between runs). Measured 2026-10-07 03:15 EDT
# against the deployed stack (A11Y_BASE=http://127.0.0.1:18080):
#
#     page          stops  presses  focusablesAtStart
#     dashboard        74       82                  74
#     transactions    688      703                 638   <- busiest page
#     investments     176      181                 162
#     taxes            54       56                  54
#     budgets          50       52                  50
#     bills            49       50                  49
#
# `focusablesAtStart` is context, never a stopping rule: the count is taken
# before the page finishes mounting and it undercounts — /transactions counted
# 638 against a 688-stop lap, /investments 162 against 176 (a slower load
# counted 76). Stopping on it (what this loop did until 2026-10-07) ended
# those two laps mid-page while still reporting success, so only an observed
# wrap back to the first stop closes a lap now.
#
# /transactions also moves with the demo ledger — 45 rows/434 stops on
# 2026-10-05, 50 rows/688 stops here — so those two numbers are re-measured
# (below) whenever the data moves; the cap only has to clear the peak.
#
# 1200 = ~1.7x the busiest lap's presses (703), i.e. ~500 presses of headroom
# — ~10 more per-row control columns on /transactions (50 rows today) before
# the cap, not the page, fails again. Constant history, same reason every
# time: 400 held a 385-stop lap (M103) -> 600 for ~435 (M104's per-row `+`
# button) -> 1200 for 688/703, after M106-M108 added four per-row controls
# (Reviewed, History, Split, Attach an image — 200 of the +254 stops vs the
# 2026-10-05 JSON; the other +50 is this fix, the lap no longer stopping at
# the focusable count) and the page grew from 45 to 50 rows.
#
# Re-derive instead of guessing: run with a deliberately generous cap and read
# the peak from the summary line (or pages.<name>.presses in the JSON):
#
#     A11Y_BASE=http://127.0.0.1:18080 python3 keywalk.py \
#         --stops 1500 --out /tmp/keywalk-probe.json
#
# That probe still rewrites docs/assets/a11y/*.png, so a real run's screenshots
# belong in the same commit as its keywalk.json. A cap only ever fails a page
# that never cycles (uncycled / lapEnd=press-cap): a page that finishes below
# the cap is unaffected by the value.
STOPS = 1200

# Renderer-idle gate (M58b). Healthy pages burn <= ~0.15 s of script per 3 s
# window at rest; the pre-fix runaway loop measured ~0.94 x wall and wedged
# Tab input mid-walk, so the gate fails a hot page before it can freeze the
# walkthrough.
IDLE_WINDOW = 3.0
IDLE_SCRIPT_MAX = 1.5

# Consecutive presses that must land on the same element before the walk calls
# it a keyboard trap. Chrome's date inputs eat one Tab per segment while
# `document.activeElement` stays the host, so three was a false positive.
TRAP_REPEATS = 5

FOCUS_JS = """
(() => {
  const el = document.activeElement;
  // Stable per-element identity for cycle/trap detection: two different
  // buttons can share tag+name, one element keeps its uid for the whole walk.
  window.__walkUids = window.__walkUids || new WeakMap();
  window.__walkSeq = window.__walkSeq || 0;
  const uidOf = (node) => {
    let uid = window.__walkUids.get(node);
    if (uid === undefined) {
      uid = ++window.__walkSeq;
      window.__walkUids.set(node, uid);
    }
    return uid;
  };
  if (!el || el === document.body || el === document.documentElement) {
    return { tag: "body", uid: 0, cls: "", name: "", indicator: null, style: "none" };
  }
  const cs = getComputedStyle(el);
  const raw =
    el.getAttribute("aria-label") ||
    el.getAttribute("title") ||
    (el.textContent || "").replace(/\\s+/g, " ").trim() ||
    el.getAttribute("placeholder") ||
    "";
  const outline = cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth || "0") > 0;
  const shadow = !!cs.boxShadow && cs.boxShadow !== "none";
  const r = el.getBoundingClientRect();
  return {
    tag: el.tagName.toLowerCase(),
    uid: uidOf(el),
    cls:
      (typeof el.className === "string" ? el.className : el.getAttribute("class") || "")
        .trim()
        .slice(0, 140),
    name: raw.slice(0, 70),
    indicator: outline || shadow,
    style: outline ? "outline" : shadow ? "shadow" : "none",
    inViewport: r.width > 0 && r.height > 0,
  };
})()
"""

FIRST_STOP_JS = """
(() => {
  const a = document.querySelector('a[href="#main-content"]');
  if (!a) return false;
  a.focus();
  return document.activeElement === a;
})()
"""

DIALOG_TRIGGER_JS = """
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

DIALOG_STATE_JS = """
(() => {
  const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(
    (d) => d.offsetParent !== null,
  );
  if (dialogs.length === 0) return { open: false };
  const d = dialogs[dialogs.length - 1];
  const active = document.activeElement;
  return {
    open: true,
    focusInside: !!(active && d.contains(active)),
    modal: d.getAttribute("aria-modal"),
    labelled: !!(d.getAttribute("aria-label") || d.getAttribute("aria-labelledby")),
  };
})()
"""

ANIMATIONS_JS = """
(() => {
  const all = typeof document.getAnimations === "function" ? document.getAnimations() : [];
  const running = all.filter((a) => a.playState === "running");
  const describe = (a) => {
    let duration = 0;
    let props = [];
    try {
      duration = (a.effect && a.effect.getTiming && a.effect.getTiming().duration) || 0;
      props = (a.effect && a.effect.getKeyframes ? a.effect.getKeyframes() : [])
        .flatMap((k) => Object.keys(k))
        .filter((k) => !["offset", "easing", "composite", "transitionProperty"].includes(k));
    } catch (e) {
      /* pseudo elements / discarded effects */
    }
    const target = a.effect && a.effect.target;
    return {
      duration,
      props: [...new Set(props)].join(",") || "?",
      target: target
        ? target.tagName.toLowerCase() +
          (typeof target.className === "string" && target.className
            ? "." + target.className.split(/\\s+/).slice(0, 2).join(".")
            : "")
        : "?",
    };
  };
  const details = running.map(describe);
  return {
    total: all.length,
    running: running.length,
    runningWithDuration: details.filter((d) => d.duration > 0).length,
    details: details.slice(0, 15),
  };
})()
"""


LIVE_REGION_JS = """
(() => {
  const el = document.querySelector('[role="status"], [role="alert"], [aria-live]');
  const text = el ? (el.textContent || "").trim() : "";
  return text.slice(0, 80);
})()
"""

# Focus is an IDENTITY, not a descriptor: `HTMLElement.click()` never focuses
# the element it dispatches on, so any change of `document.activeElement`
# after the click is the control's own doing — and two different controls that
# happen to share a label cannot be mistaken for each other.
FOCUS_CAPTURE_JS = "window.__keywalkFocusBefore = document.activeElement; true"

FOCUS_EFFECT_JS = """
(() => {
  const before = window.__keywalkFocusBefore;
  const now = document.activeElement;
  const isBody = !now || now === document.body || now === document.documentElement;
  if (!before || !now || now === before || isBody) return null;
  const g = (n) => now.getAttribute(n) || "";
  const name =
    g("aria-label") ||
    (now.textContent || "").replace(/\\s+/g, " ").trim() ||
    g("placeholder") ||
    "";
  return {
    tag: now.tagName.toLowerCase(),
    role: g("role"),
    placeholder: g("placeholder"),
    name: name.slice(0, 60),
  };
})()
"""

# A surface that mounts no form control (a listbox, a toolbar, a table) still
# leaves a mark: it changes how many elements the document has.
ELEMENT_COUNT_JS = '[...document.querySelectorAll("body *")].length'

VISIBLE_CONTROLS_JS = """\
[...document.querySelectorAll("input, select, textarea")].filter((el) => el.offsetParent !== null).length
"""

# How many elements Tab can reach here right now — the walk is finished once it
# has recorded this many distinct stops, whether or not the cycle closed.
FOCUSABLE_COUNT_JS = """\
[...document.querySelectorAll(
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
)].filter((el) => el.getClientRects().length > 0).length
"""


def drain_auth(tab, into: list, label: str) -> None:
    """Collect auth traffic + any failing API response, with timestamps.

    A walk that silently lands on /login is useless as evidence, so the report
    carries refresh/demo status codes (and every >=400 API response) next to
    the page that produced them: an expired, rate-limited or *re-used* refresh
    shows up here instead of as a mystery `skip_link=False`.
    """
    for ev in tab.events("Network.requestWillBeSent"):
        p = ev["params"]
        url = p["request"]["url"]
        if "/auth/" in url:
            into.append(
                {"page": label, "t": round(p["timestamp"], 2), "req": p["request"]["method"],
                 "path": url.split("/api/v1")[-1]}
            )
    for ev in tab.events("Network.responseReceived"):
        p = ev["params"]
        r = p["response"]
        if "/auth/" in r["url"] or (r["status"] >= 400 and "/api/" in r["url"]):
            into.append(
                {"page": label, "t": round(p["timestamp"], 2), "path": r["url"].split("/api/v1")[-1],
                 "status": r["status"]}
            )


def renderer_idle(tab, window: float = IDLE_WINDOW) -> dict:
    """Script time burned while the page is at rest (CDP Performance metrics).

    The M58 baseline lost four pages to an unbounded re-render loop that pinned
    the renderer at ~94-100% JavaScript with no frame committing; keyboard input
    queued behind it, so every later CDP call timed out. Measuring at rest makes
    that failure mode a first-class, page-by-page result instead of a mystery
    freeze further down the run.
    """

    def snap() -> dict:
        tab.send("Performance.enable")
        return {d["name"]: d["value"] for d in tab.send("Performance.getMetrics")["metrics"]}

    a = snap()
    tab.sleep(window)
    b = snap()
    script = round(b.get("ScriptDuration", 0) - a.get("ScriptDuration", 0), 2)
    return {
        "window_s": window,
        "script_s": script,
        "layout": int(b.get("LayoutCount", 0) - a.get("LayoutCount", 0)),
        "idle": script <= IDLE_SCRIPT_MAX,
    }


def press_key(tab, key: str) -> None:
    """Press a key with a SHORT socket timeout.

    A page that freezes on the key (renderer stuck) must surface in seconds so
    the harness can record the freeze and restart, not stall for a minute.
    After a timeout the late CDP response is dropped harmlessly by
    CDPSession.send, so the session stays usable.
    """
    tab.conn.ws.settimeout(8)
    try:
        tab.press(key)
    finally:
        tab.conn.ws.settimeout(60)


def settle(tab, extra: float = 1.5) -> None:
    try:
        tab.wait_for(
            "document.querySelector('[data-testid=\"route-fallback\"]') === null && document.querySelector('main') !== null",
            timeout=25,
        )
    except TimeoutError:
        pass
    tab.sleep(extra)


def ensure_session(tab, path: str, label: str) -> str:
    """Land on `path`, signing the demo tenant back in when the session died.

    The demo user's rotating refresh token is shared with every other client
    of that user, so a run can lose its session mid-flight: the app redirects
    to `/login` and the walk that follows would be a walk of the *login page*
    recorded against `/taxes`. Re-login + re-navigate until `path` is where
    the tab actually is (see docs/09 Verification).
    """
    landed = tab.evaluate("location.pathname")
    if landed == path:
        return landed
    print(f"--- {label}: landed on {landed}, not {path} — re-establishing session", flush=True)
    for _ in range(3):
        try:
            tab.wait_for(
                "Array.from(document.querySelectorAll('button')).some("
                "(b) => b.textContent.includes('Explore the demo'))",
                timeout=12,
            )
            tab.evaluate(
                "Array.from(document.querySelectorAll('button')).find("
                "(b) => b.textContent.includes('Explore the demo'))?.click()"
            )
            tab.wait_for("location.pathname === '/dashboard'", timeout=25)
        except TimeoutError:
            tab.navigate(f"{BASE}/login")
            settle(tab, extra=1.0)
            continue
        tab.navigate(f"{BASE}{path}")
        settle(tab)
        landed = tab.evaluate("location.pathname")
        if landed == path:
            return landed
    return landed


def prepare_and_walk(tab, path: str, label: str) -> tuple[list[dict], bool, int, dict]:
    """One full tab lap of `path`, redone if the session dies mid-walk.

    Returns `(stops, first_stop_is_skip_link, retries, walk)`. A walk that
    ends on `/login` is not evidence about the audited page, so it is thrown
    away and taken again from a fresh sign-in — at most twice, then reported.
    `walk` is `tab_walk`'s record of what the lap cost and how it ended.
    """
    retries = 0
    stops: list[dict] = []
    first_is_skip = False
    walk: dict = {"presses": 0, "lapEnd": "not-run", "focusablesAtStart": 0}
    for _ in range(3):
        if ensure_session(tab, path, label) != path:
            break
        first_is_skip = tab.evaluate(FIRST_STOP_JS)
        stops, walk = tab_walk(tab)
        if tab.evaluate("location.pathname") == path:
            return stops, bool(first_is_skip), retries, walk
        retries += 1
        print(f"--- {label}: session dropped mid-walk, retry #{retries}", flush=True)
    return stops, bool(first_is_skip), retries, walk


def tab_walk(tab) -> tuple[list[dict], dict]:
    """Tab until focus cycles back to the first stop (or STOPS is exhausted).

    A fixed press count can only ever sample the start of the tab order — the
    sidebar rail on its own is ~25 stops, so the six audited pages' own
    content (the controls the a11y bar is actually about) never got walked.
    Stopping on the first stop's uid means one full lap of every focusable on
    the page, with the cap as the backstop for a page that never cycles.

    Returns `(stops, walk)` where `walk` carries the two numbers STOPS is
    sized against and the evidence that the lap closed:

    * `presses` — Tab presses actually spent. It, not `len(stops)`, is what
      the cap has to clear: a press that lands on `body` or repeats the
      previous stop burns budget without adding a stop (recorded per page as
      `presses` in `keywalk.json`).
    * `lapEnd` — how the lap ended: `first-stop-returned` (focus demonstrably
      came back to the first stop — the definition of a full lap) or
      `press-cap` (never closed; `uncycled`).
    * `focusablesAtStart` — the DOM's focusable count when the walk began,
      for context only. It is NOT a stopping rule: the count is taken before
      the page has necessarily finished mounting, so it undercounts
      (/investments counted 76 while its lap runs past 160 stops, and
      /transactions' last row mounted Edit/History/Delete mid-walk, after the
      count was taken — stopping on the count ended that lap three controls
      short while reporting success). Only an observed wrap closes a lap.
    """
    stops: list[dict] = []
    start_uid: int | None = None
    repeats = 0  # consecutive presses that landed on the element already recorded
    presses = 0
    lap_end = "press-cap"
    try:
        total = int(tab.evaluate(FOCUSABLE_COUNT_JS))
    except Exception:
        total = 0
    for i in range(STOPS):
        press_key(tab, "Tab")
        presses += 1
        tab.sleep(0.08)
        snap = tab.evaluate(FOCUS_JS)
        snap["index"] = i + 1
        uid = snap.get("uid", 0)
        if snap["tag"] == "body":
            repeats = 0  # focus left the document; not a stop and not a cycle
            continue
        if stops and stops[-1].get("uid") == uid:
            # Same element again: a multi-segment control (Chrome's
            # <input type="date"> walks month/day/year without ever moving
            # document.activeElement) — or focus that has genuinely stopped
            # advancing. Four repeats is past every segmented control in the
            # app but far short of "stuck forever", so only then it is a trap.
            repeats += 1
            if repeats >= TRAP_REPEATS:
                snap["trap"] = True
                stops.append(snap)
                break
            continue
        repeats = 0
        if start_uid is None:
            start_uid = uid
            stops.append(snap)
            continue
        if uid == start_uid:
            # The wrap, observed: focus is back on the first stop, which is
            # already recorded — do not append it a second time.
            lap_end = "first-stop-returned"
            break  # one full lap: every focusable on the page was visited
        stops.append(snap)
    else:
        stops[-1]["uncycled"] = True
    return stops, {"presses": presses, "lapEnd": lap_end, "focusablesAtStart": total}


def dialog_probe(tab) -> dict:
    """Open the page's Add/Edit control and verify the Escape contract.

    Three outcomes, so the verdict means exactly what it says:

    * a dialog opens -> the strict contract (focus moves in, Escape closes it
      from anywhere, focus returns to the opener);
    * no dialog, but the click moved the route, opened an inline form,
      announced something in a live region, **moved focus**, or **added
      elements** -> `non-dialog-surface`: the control works, there is simply no
      dialog to contract-test (Budgets/Bills render their create form inline;
      Dashboard's "Create bill reminder" hands off to `/bills?autofill=`;
      `/investments`' "Add a symbol" focuses the watchlist's security
      combobox, which mounts its `role="listbox"` — a control nothing older
      than M103 could see, so it reported a working button as `no-effect`);
    * no dialog and no observable effect at all -> `no-effect`, a broken
      control and a failure.

    The focus and element reads happen immediately after the click, before the
    6 s dialog deadline: a control's own effect is synchronous, while
    anything landing seconds later is the page's data still arriving. The
    probe runs after the renderer-idle window and a full tab lap, so the
    document is at rest by then (`m103_click_probe.py` measured a 0-element
    delta over 7 s with no click at both 390 and 1440).
    """
    before_url = tab.evaluate("location.pathname + location.search")
    before_controls = tab.evaluate(VISIBLE_CONTROLS_JS)
    before_elements = tab.evaluate(ELEMENT_COUNT_JS)
    tab.evaluate(FOCUS_CAPTURE_JS)
    trigger = tab.evaluate(DIALOG_TRIGGER_JS)
    if trigger is None:
        return {"trigger": None, "result": "no-dialog-on-page"}
    tab.sleep(0.25)
    focus_effect = tab.evaluate(FOCUS_EFFECT_JS)
    early_elements = tab.evaluate(ELEMENT_COUNT_JS)
    opened = True
    try:
        tab.wait_for("document.querySelectorAll('[role=\"dialog\"]').length > 0", timeout=6)
    except TimeoutError:
        opened = False
    if not opened:
        after_url = tab.evaluate("location.pathname + location.search")
        if after_url != before_url:
            return {
                "trigger": trigger,
                "result": "non-dialog-surface",
                "effect": f"navigated to {after_url}",
            }
        after_controls = tab.evaluate(VISIBLE_CONTROLS_JS)
        if after_controls > before_controls:
            return {
                "trigger": trigger,
                "result": "non-dialog-surface",
                "effect": f"inline form opened (+{after_controls - before_controls} visible controls)",
            }
        live = tab.evaluate(LIVE_REGION_JS)
        if live:
            return {"trigger": trigger, "result": "non-dialog-surface", "effect": f"live region: {live}"}
        if focus_effect:
            label = (
                focus_effect.get("placeholder")
                or focus_effect.get("name")
                or focus_effect.get("tag")
            )
            return {
                "trigger": trigger,
                "result": "non-dialog-surface",
                "effect": f"focus moved to <{focus_effect['tag']}> {label}",
            }
        late_elements = tab.evaluate(ELEMENT_COUNT_JS)
        if late_elements != before_elements or early_elements != before_elements:
            moved = (
                late_elements if late_elements != before_elements else early_elements
            ) - before_elements
            return {
                "trigger": trigger,
                "result": "non-dialog-surface",
                "effect": f"surface rendered ({moved:+d} elements, no form controls)",
            }
        return {"trigger": trigger, "result": "no-effect"}
    tab.sleep(0.6)
    state = tab.evaluate(DIALOG_STATE_JS)
    tab.screenshot(str(SHOTS / "dialog-open.png"))
    press_key(tab, "Escape")
    tab.sleep(0.7)
    closed = tab.evaluate("document.querySelectorAll('[role=\"dialog\"]').length === 0")
    restored = tab.evaluate(
        "(() => { const a = document.activeElement; return a ? (a.textContent || '').trim().slice(0, 50) : ''; })()"
    )
    ok = bool(state.get("focusInside")) and closed
    return {
        "trigger": trigger,
        "focusInsideDialog": bool(state.get("focusInside")),
        "ariaModal": state.get("modal"),
        "labelled": state.get("labelled"),
        "escapeClosed": closed,
        "focusAfterClose": restored,
        "result": "pass" if ok else "FAIL",
    }


def main() -> int:
    global STOPS
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "keywalk.json"))
    ap.add_argument("--stops", type=int, default=STOPS)
    args = ap.parse_args()
    from probe_target import require_target
    require_target(BASE, who="keywalk.py")
    # `--stops` exists so the cap can be re-derived instead of guessed: run
    # with a deliberately generous value and read each page's `presses`.
    STOPS = max(args.stops, 1)

    report: dict = {
        "base": BASE,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "stopsCap": STOPS,
        "pages": {},
        "reducedMotion": {},
        "auth": [],
    }
    SHOTS.mkdir(parents=True, exist_ok=True)

    class Session:
        """Browser + demo-logged-in tab. Recreated after a page freeze: a page
        whose renderer is stuck can never be navigated away from (every CDP
        call blocks), so recovery means a fresh browser."""

        def __init__(self) -> None:
            self.browser = Browser()
            tab = self.browser.new_tab(f"{BASE}/login")
            tab.enable_runtime()
            tab.send("Network.enable")
            tab.navigate(f"{BASE}/login")
            settle(tab)
            tab.evaluate(
                "Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Explore the demo'))?.click()"
            )
            tab.wait_for("location.pathname === '/dashboard'", timeout=25)
            settle(tab)
            self.tab = tab

        def restart(self) -> None:
            try:
                self.browser.close()
            except Exception:
                pass
            self.__init__()

    session = Session()
    browser = session.browser
    try:
        for name, path in PAGES:
            tab = session.tab
            try:
                print(f"--- {name}: navigate", flush=True)
                tab.navigate(f"{BASE}{path}")
                settle(tab)

                drain_auth(tab, report["auth"], name)
                landed = ensure_session(tab, path, name)
                if landed != path:
                    raise RuntimeError(f"session could not be established (stuck on {landed})")
                idle = renderer_idle(tab)
                print(
                    f"--- {name}: at rest script={idle['script_s']}s/{idle['window_s']:.0f}s "
                    f"layout={idle['layout']}",
                    flush=True,
                )

                print(f"--- {name}: tab walk", flush=True)
                stops, first_is_skip, walk_retries, walk = prepare_and_walk(tab, path, name)
                if not stops:
                    raise RuntimeError("no focus stops recorded")

                # Park focus on the 4th stop so the screenshot shows it.
                if ensure_session(tab, path, name) != path:
                    raise RuntimeError("session lost before the screenshot")
                for _ in range(4):
                    press_key(tab, "Tab")
                    tab.sleep(0.08)
                tab.screenshot(str(SHOTS / f"{name}-focus.png"))

                if ensure_session(tab, path, name) != path:
                    raise RuntimeError("session lost before the dialog probe")
                dialog = dialog_probe(tab)
            except Exception as exc:  # page froze or the harness lost the tab
                report["pages"][name] = {
                    "path": path,
                    "freeze": f"{type(exc).__name__}: {exc}",
                    "rendererIdle": {"idle": False, "script_s": None},
                    "firstStopIsSkipLink": False,
                    "stops": 0,
                    "presses": 0,
                    "lapEnd": "not-run",
                    "focusStopsWithoutIndicator": [],
                    "keyboardTrap": False,
                    "dialog": {"result": "not-run"},
                    "tabOrder": [],
                }
                print(f"FREEZE {name}: {type(exc).__name__} — restarting browser", flush=True)
                session.restart()
                browser = session.browser
                continue
            drain_auth(tab, report["auth"], name)
            print(f"--- {name}: dialog probe -> {dialog.get('result')}", flush=True)

            missing = [s for s in stops if not s.get("indicator", False) and s["tag"] != "body"]
            traps = [s for s in stops if s.get("trap")]
            report["pages"][name] = {
                "path": path,
                "landed": tab.evaluate("location.pathname"),
                "rendererIdle": idle,
                "walkRetries": walk_retries,
                "firstStopIsSkipLink": bool(first_is_skip),
                "stops": len(stops),
                "presses": walk["presses"],
                "lapEnd": walk["lapEnd"],
                "focusablesAtStart": walk["focusablesAtStart"],
                "focusStopsWithoutIndicator": [
                    {
                        "i": s["index"],
                        "tag": s["tag"],
                        "name": s["name"],
                        "cls": s.get("cls", ""),
                    }
                    for s in missing
                ],
                "keyboardTrap": bool(traps),
                "dialog": dialog,
                "tabOrder": [
                    {
                        "i": s["index"],
                        "tag": s["tag"],
                        "name": s["name"],
                        "via": s["style"],
                        "cls": s.get("cls", ""),
                    }
                    for s in stops
                    if s["tag"] != "body"
                ],
                "uncycled": any(s.get("uncycled") for s in stops),
            }
            p = report["pages"][name]
            print(
                f"{name:14s} skip_link={str(p['firstStopIsSkipLink']):5s} "
                f"stops={len(p['tabOrder']):4d} presses={p['presses']:4d} "
                f"lap={p['lapEnd']} "
                f"no_indicator={len(missing)} trap={p['keyboardTrap']} "
                f"idle={str(p['rendererIdle']['script_s'])}s "
                f"dialog={dialog.get('result')}",
                flush=True,
            )

        # --- reduced motion pass -------------------------------------
        tab = session.tab  # a page freeze above may have replaced the session
        tab.send(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
        )
        for name, path in PAGES:
            tab.navigate(f"{BASE}{path}")
            settle(tab, extra=2.0)
            if ensure_session(tab, path, f"reduce/{name}") != path:
                print(f"reduce/{name}: session lost — skipped", flush=True)
                continue
            anim = tab.evaluate(ANIMATIONS_JS)
            report["reducedMotion"][name] = anim
            print(
                f"reduce/{name:14s} total={anim['total']:3d} running={anim['running']:3d} "
                f"running>0ms={anim['runningWithDuration']:3d}",
                flush=True,
            )
        tab.screenshot(str(SHOTS / "reduced-motion-dashboard.png"))
        report["console"] = tab.console_errors()
    finally:
        browser.close()

    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out}")

    failures = 0
    for name, p in report["pages"].items():
        if "freeze" in p:
            failures += 1
            print(f"FAIL {name}: page FREEZE — {p['freeze']}")
        if not p["firstStopIsSkipLink"] and "freeze" not in p:
            failures += 1
            print(f"FAIL {name}: first Tab stop is not the skip link")
        if p["keyboardTrap"]:
            failures += 1
            print(f"FAIL {name}: keyboard trap")
        if p["focusStopsWithoutIndicator"]:
            failures += 1
            print(f"FAIL {name}: {len(p['focusStopsWithoutIndicator'])} stop(s) without a focus indicator")
        if not p.get("rendererIdle", {}).get("idle", False) and "freeze" not in p:
            failures += 1
            print(
                f"FAIL {name}: renderer not idle at rest — "
                f"{p['rendererIdle']['script_s']}s script in "
                f"{p['rendererIdle']['window_s']}s (runaway re-render?)"
            )
        if p.get("uncycled"):
            failures += 1
            print(
                f"FAIL {name}: tab walk spent the whole {report['stopsCap']}-press cap "
                f"({p['presses']} presses) without covering the page — coverage is partial"
            )
        if p.get("walkRetries"):
            failures += 1
            print(
                f"FAIL {name}: session died mid-walk "
                f"({p['walkRetries']} retr(y/ies)) — evidence may be partial"
            )
        if p["dialog"]["result"] in ("FAIL", "no-effect"):
            failures += 1
            print(f"FAIL {name}: dialog Escape/focus contract — {p['dialog']}")
    print("auth traffic:")
    for a in report["auth"]:
        status = a.get("status")
        mark = "" if status is None or status == 200 else "   <-- FAIL"
        req = f"{a['req']} " if "req" in a else ""
        print(f"  {a['page']:14s} t={a['t']}  {req}{a['path']} {status if status is not None else '(sent)'}{mark}")
    walked = {n: p["presses"] for n, p in report["pages"].items() if "freeze" not in p}
    if walked:
        peak = max(walked, key=lambda n: walked[n])
        print(
            f"cap: {report['stopsCap']} presses; busiest page {peak}={walked[peak]} "
            f"({report['stopsCap'] - walked[peak]} spare)"
        )
    print(f"failures={failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

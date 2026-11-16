#!/usr/bin/env python3
"""Shell visual-regression matrix — 2 viewports x 2 mask states x 3 shell states.

WHAT THIS PINS
--------------
The M185 Ledger redesign (`42c9ac7`, 49 files: AppShell, TopBar, MobileTabBar,
MorePanel, TruthBar, DemoLock, ...) replaced the shell wholesale, and the shell
is exactly what jsdom cannot cover — it lays nothing out. This probe is the
visual-regression matrix for it, measured in a REAL headless Chrome against a
REAL build:

    width  1440x900  desktop chrome (TopBar + TruthBar, no bottom bar)
             390x844  mobile chrome (fixed appbar + nav drawer + MobileTabBar)
    mask   masked / unmasked, driven through the mask chip
    state  demo   /dashboard — populated demo tenant, read-only banner
           empty  /transactions?q=<no-match> — a real EmptyState
           more   the opened chrome (desktop: More panel; mobile: nav drawer)

Per cell it captures a screenshot (PNG, pinned to the viewport's dimensions),
measures the shell's element rectangles and asserts the invariants below.
Rectangles are compared with the committed baseline
`shell-visual-matrix-baseline.json` at +-2px, so chrome that moves, resizes,
appears or disappears goes red naming the cell.

INVARIANTS (every cell, independent of the baseline)
  * no horizontal overflow — documentElement.scrollWidth <= innerWidth
  * exactly ONE shell per breakpoint: app-topbar + topbar-tabs at >=768px,
    appbar + mobile-tab-bar (+ app-nav) below it, never both
  * the mask chip's aria-pressed agrees with the cell's mask state
  * MASKED: zero standalone dollar figures in the DOM. Per the PM addendum
    (uxbot's audit correction) Recharts' off-screen aria-hidden measurement
    node `#recharts_measurement_span` (top:-20000px) is EXCLUDED — it is not
    a user-visible mask leak. UNMASKED over a money page: the same detector
    must FIND dollars, so a masked-cell zero can never be a dead detector.
  * demo cells: the read-only banner carries its copy; empty cells: a real
    EmptyState is on screen; more cells: the panel/drawer is open
  * no error surface at rest — no `error-panel` (docs/47 §3's error state) and
    no `demo-refusal` banner. `role="alert"` is deliberately NOT swept: the
    dashboard's breached-budget alert card carries that role by design, so a
    blanket zero would pin the demo tenant's DATA, not the shell's health.
  * the PNG exists at the viewport's own pixel dimensions

USAGE
-----
    # capture / refresh the baseline (refuses if any invariant failed, so a
    # broken state can never become the baseline):
    python3 frontend/scripts/a11y/shell_visual_matrix_probe.py --update-baseline

    # verify against the committed baseline:
    python3 frontend/scripts/a11y/shell_visual_matrix_probe.py --base http://localhost:4183

    # through the cached runner:
    python3 scripts/run_probes.py shell-visual --base <url>

BASELINE REFRESH RULE (PM): a baseline describes the tree it was captured on.
Refresh it at the then-current origin/main and record the SHA in the file;
never carry a geometry observation from an older tree forward as a defect
without re-testing the served/current SHA.

A build that PREDATES the shell (no shell markers, and its stamped
build-commit is an ancestor of 42c9ac7) is SKIPPED, not failed: there is no
matrix to run against a build that does not contain the shell. That is today's
deployed production stack, which sits 20 commits behind origin/main and was
never rebuilt after the redesign. A build that claims to be post-redesign and
still has no shell markers FAILS — that is a regression, not a stale deploy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from a11y_driver import Browser

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
BASELINE_PATH = HERE / "shell-visual-matrix-baseline.json"
OUT_DEFAULT = HERE / "shell-visual-matrix.json"
PNG_DIR = HERE / "matrix"

# The commit that introduced the shell under test (M185). A served build that
# is an ancestor of it cannot contain this matrix's subjects.
SHELL_COMMIT = "42c9ac7"
TOLERANCE_PX = 2.0

DEFAULT_BASE = os.environ.get("A11Y_BASE") or "https://fintrac.princessandmark.com"

WIDTHS = ((1440, 900), (390, 844))
STATES = ("demo", "empty", "more")
MASKS = (("masked", True), ("unmasked", False))

# The demo tenant boots MASKED (docs/46 §D1); the chip drives both directions.
MASK_KEY = "fintrack.mask"
DEMO_BANNER = "Read-only demo — changes are not saved"

STATE_PATH = {
    "demo": "/dashboard",
    "empty": "/transactions?q=zzz-matrix-no-match",
    "more": "/dashboard",
}

# Which rectangle FIELDS are pinned per element. Height/width that legitimately
# follows page CONTENT is left out of the pin on purpose and called out here:
#   main-content  — height is the page's content height (a short page is short)
#   more-panel    — height follows the account list
#   empty-state   — height follows the preview image + copy
PIN_SPEC: dict[str, tuple[str, ...]] = {
    "app-topbar": ("x", "y", "w", "h"),
    "topbar-tabs": ("x", "y", "w", "h"),
    "truth-bar": ("x", "y", "w", "h"),
    "mask-chip": ("x", "y", "w", "h"),
    "main-content": ("x", "y", "w"),
    "appbar": ("x", "y", "w", "h"),
    "nav-drawer-toggle": ("x", "y", "w", "h"),
    "mobile-tab-bar": ("x", "y", "w", "h"),
    "app-nav": ("x", "y", "w", "h"),
    "more-panel": ("x", "y", "w"),
    "empty-state": ("x", "y", "w"),
}

# Leaf text that is exactly a standalone dollar figure. Percentages, ranges and
# currency suffixes deliberately do not match — the mask contract is about
# money (m139's own detector).
MONEY_JS = r"""
(() => {
  const moneyRe = /^-?\$[\d,]+(?:\.\d{2})?$/;
  const hits = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length > 0) continue;
    // PM addendum: Recharts' off-screen aria-hidden measurement node is not a
    // user-visible leak — exclude it from any currency-leak assertion.
    if (el.closest('#recharts_measurement_span')) continue;
    const t = (el.textContent || '').trim();
    if (t && moneyRe.test(t)) hits.push(t);
  }
  return hits;
})()
"""

MEASURE_JS = r"""
(() => {
  const round = (n) => Math.round(n * 10) / 10;
  const rect = (node) => {
    const r = node.getBoundingClientRect();
    const cs = getComputedStyle(node);
    if (cs.display === 'none' || cs.visibility === 'hidden') return null;
    return { x: round(r.x), y: round(r.y), w: round(r.width), h: round(r.height) };
  };
  const info = (sel) => {
    const el = document.querySelector(sel);
    return el ? rect(el) : null;
  };
  const headerOf = (sel) => {
    const el = document.querySelector(sel);
    const head = el && el.closest('header');
    return head ? rect(head) : null;
  };
  const isDesktop = window.matchMedia('(min-width: 768px)').matches;
  const geometry = {};
  const put = (name, value) => { if (value) geometry[name] = value; };

  put('main-content', info('#main-content'));
  put('truth-bar', info('[data-testid="truth-bar"]'));
  put('mask-chip', info('[data-testid="mask-chip"]'));
  if (isDesktop) {
    put('app-topbar', info('[data-testid="app-topbar"]'));
    put('topbar-tabs', info('[data-testid="topbar-tabs"]'));
  } else {
    put('appbar', headerOf('[data-testid="nav-drawer-toggle"]'));
    put('nav-drawer-toggle', info('[data-testid="nav-drawer-toggle"]'));
    put('mobile-tab-bar', info('[data-testid="mobile-tab-bar"]'));
    put('app-nav', info('[data-testid="app-nav"]'));
  }
  put('more-panel', info('[data-testid="more-panel"]'));
  put('empty-state', info('[data-testid="empty-state"]'));

  const chip = document.querySelector('[data-testid="mask-chip"]');
  const banner = document.querySelector('[data-testid="demo-banner"]');
  const emptyTitle = document.querySelector('[data-testid="empty-state-title"]');
  return {
    isDesktop: isDesktop,
    viewport: [window.innerWidth, window.innerHeight],
    scrollWidth: document.documentElement.scrollWidth,
    geometry: geometry,
    chip: chip ? {
      pressed: chip.getAttribute('aria-pressed'),
      tag: chip.tagName,
      text: (chip.textContent || '').trim().slice(0, 40),
    } : null,
    demoBanner: banner ? (banner.textContent || '').trim() : null,
    emptyStateTitle: emptyTitle ? (emptyTitle.textContent || '').trim() : null,
    demoLockCount: document.querySelectorAll('[data-testid="demo-lock"]').length,
    demoRefusalCount: document.querySelectorAll('[data-testid="demo-refusal"]').length,
    errorPanelCount: document.querySelectorAll('[data-testid="error-panel"]').length,
    alertCount: document.querySelectorAll('[role="alert"]').length,
    scrimCount: document.querySelectorAll('[data-testid="nav-scrim"]').length,
    appNavX: (() => {
      const nav = document.querySelector('[data-testid="app-nav"]');
      return nav ? round(nav.getBoundingClientRect().x) : null;
    })(),
  };
})()
"""


# ---------------------------------------------------------------------------
# house helpers (same shapes the other probes use)
# ---------------------------------------------------------------------------
def wait_stable(tab, timeout: float = 12.0) -> None:
    """Wait until the DOM stops churning — pages here land data late."""
    deadline = time.time() + timeout
    last = -1
    quiet = 0
    while time.time() < deadline:
        try:
            count = tab.evaluate("document.querySelectorAll('*').length")
        except Exception:
            time.sleep(0.25)
            continue
        if count == last:
            quiet += 1
            if quiet >= 3:
                return
        else:
            quiet = 0
        last = count
        time.sleep(0.35)


def settle(tab, extra: float = 1.0) -> None:
    """Let route/entrance animation finish — a moving rect is not a pin."""
    wait_stable(tab)
    tab.sleep(extra)


def dismiss_tour(tab) -> None:
    """A fresh demo session opens the first-run tour over the shell."""
    try:
        if not tab.evaluate("!!document.querySelector('[data-testid=\"tour\"]')"):
            return
        tab.evaluate(
            """
            (() => {
              const t = document.querySelector('[data-testid="tour"]');
              const btn = t ? [...t.querySelectorAll('button')]
                .find((b) => /skip/i.test(b.textContent || '')) : null;
              if (btn) { btn.click(); return true; }
              return false;
            })()
            """
        )
        tab.wait_for("!document.querySelector('[data-testid=\"tour\"]')", timeout=8)
    except Exception:
        pass


def demo_login(tab, attempts: int = 4, per_attempt: float = 20.0) -> bool:
    """Click `Explore the demo` until the app actually lands on /dashboard.

    The button exists in the static HTML BEFORE hydration (fin-tracker skill):
    on a loaded box the first click can land on a dead listener and nothing
    happens, so one click is not evidence — retry until the navigation proves
    the click was handled.
    """
    for _ in range(attempts):
        try:
            tab.wait_for(
                "Array.from(document.querySelectorAll('button'))"
                ".some(b => b.textContent.includes('Explore the demo'))",
                timeout=10,
            )
        except Exception:
            pass  # the click below is optional-chained anyway
        tab.evaluate(
            "Array.from(document.querySelectorAll('button')).find("
            "b => b.textContent.includes('Explore the demo'))?.click()"
        )
        try:
            tab.wait_for("location.pathname === '/dashboard'", timeout=per_attempt)
            return True
        except Exception:
            continue
    return False


def open_session(browser: Browser, base: str, width: int, height: int):
    """Log into the demo tenant the same way the house probes do."""
    tab = browser.new_tab(f"{base}/login")
    tab.enable_runtime()
    tab.force_viewport(width, height)
    tab.navigate(f"{base}/login")
    wait_stable(tab)
    if not demo_login(tab):
        tab.close()
        raise RuntimeError(
            f"demo login never reached /dashboard at {width}px "
            "(button clicked, no navigation) — check the base URL / API health"
        )
    settle(tab)
    dismiss_tour(tab)
    return tab


def chip_pressed(tab) -> str | None:
    return tab.evaluate(
        "document.querySelector('[data-testid=\"mask-chip\"]')"
        "?.getAttribute('aria-pressed')"
    )


def set_masked(tab, want: bool, timeout: float = 10.0) -> bool:
    """Drive the chip to a target state, RELATIVE to wherever it is now."""
    if (chip_pressed(tab) == "true") == want:
        return True
    tab.evaluate("document.querySelector('[data-testid=\"mask-chip\"]')?.click()")
    try:
        tab.wait_for(
            "document.querySelector('[data-testid=\"mask-chip\"]')"
            f"?.getAttribute('aria-pressed') === '{str(want).lower()}'",
            timeout=timeout,
        )
    except Exception:
        return False
    tab.sleep(0.4)
    return True


def open_more_chrome(tab, width: int) -> bool:
    if width >= 768:
        tab.evaluate("document.querySelector('[data-testid=\"more-toggle\"]')?.click()")
        expr = "!!document.querySelector('[data-testid=\"more-panel\"]')"
    else:
        tab.evaluate("document.querySelector('[data-testid=\"tab-more\"]')?.click()")
        expr = "document.querySelector('[data-testid=\"app-nav\"]')?.getBoundingClientRect().x > -5"
    try:
        tab.wait_for(expr, timeout=8)
    except Exception:
        return False
    tab.sleep(0.5)  # drawer slide / popover mount
    return True


def recover_login(tab) -> bool:
    """A full page load can lose the session (cookie dropped, login burst
    rate-limited). Re-enter the demo rather than measuring a login page as if
    it were the state under test — that turns an environment hiccup into a
    false 'the shell is missing' defect."""
    try:
        if not demo_login(tab, attempts=2):
            return False
        settle(tab)
        dismiss_tour(tab)
        return True
    except Exception:
        return False


def png_dims(path: Path) -> tuple[int, int] | None:
    try:
        head = path.open("rb").read(24)
    except OSError:
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return width, height


# ---------------------------------------------------------------------------
# preflight: which build is being measured?
# ---------------------------------------------------------------------------
def served_build_commit(base: str) -> tuple[str | None, str | None]:
    """(sha, error) from the served index.html meta stamp."""
    url = base.rstrip("/") + "/"
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        return None, f"cannot fetch {url}: {exc!r}"
    match = re.search(r'name="build-commit"\s+content="([^"]+)"', html)
    if not match:
        return None, None
    value = match.group(1).strip()
    if not re.fullmatch(r"[0-9a-f]{7,40}(-dirty)?", value, re.IGNORECASE):
        return None, None
    return value.split("-")[0], None


def predates_shell(sha: str | None) -> bool:
    """True only when git can prove the build predates the shell."""
    if not sha:
        return False
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, SHELL_COMMIT],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=30,
        )
    except Exception:
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# the matrix
# ---------------------------------------------------------------------------
def run_cell(tab, *, base: str, width: int, height: int, state: str,
             mask_name: str, want_masked: bool) -> dict:
    """Navigate, drive the mask, open the chrome, measure, shoot, check."""
    cell_id = f"{width}x{height}|{state}|{mask_name}"
    checks: list[dict] = []

    def check(name: str, ok: object, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    tab.navigate(f"{base}{STATE_PATH[state]}")
    settle(tab)
    pathname = STATE_PATH[state].split("?")[0]

    # If the load landed on /login, the session is gone — recover once instead
    # of measuring the login page as the state under test.
    try:
        tab.wait_for(
            f"location.pathname === '{pathname}' || location.pathname === '/login'",
            timeout=30,
        )
    except Exception:
        pass
    if tab.evaluate("location.pathname") == "/login":
        check("session-recovered", recover_login(tab),
              "session lost on the full page load — re-entered the demo")
        tab.navigate(f"{base}{STATE_PATH[state]}")
        settle(tab)

    # Never measure a page whose shell never came up: an empty geometry below
    # is reported as THIS named failure, not as eleven element-by-element ones.
    try:
        tab.wait_for(
            "!!document.querySelector('[data-testid=\"mask-chip\"]')", timeout=30
        )
        shell_up = True
    except Exception:
        shell_up = False
    check("shell-rendered", shell_up,
          "mask-chip present (both breakpoints' chrome renders it)")
    dismiss_tour(tab)
    check("mask-chip-driven", set_masked(tab, want_masked),
          f"aria-pressed={chip_pressed(tab)!r} want={'true' if want_masked else 'false'}")

    # Data must be ON SCREEN before anything is measured or shot: a screenshot
    # of skeletons is not the state it claims to be.
    if state in ("demo", "more"):
        data_expr = (
            "document.body.textContent.includes('•')" if want_masked
            else f"{MONEY_JS}.length > 0"
        )
        try:
            tab.wait_for(data_expr, timeout=40)
            check("data-on-screen", True, "money render landed")
        except Exception as exc:  # noqa: BLE001
            check("data-on-screen", False, repr(exc))
    else:
        try:
            tab.wait_for(
                "!!document.querySelector('[data-testid=\"empty-state\"]')", timeout=40
            )
        except Exception:
            pass  # asserted below, with its own named check

    if state == "more":
        check("more-chrome-opened", open_more_chrome(tab, width))

    settle(tab, extra=0.8)
    measured = tab.evaluate(MEASURE_JS) or {}
    money = tab.evaluate(MONEY_JS) or []

    # Screenshot AFTER scrolling a below-the-fold subject into view — a cell
    # named `empty` must show the empty state, not the top of the table. The
    # geometry above was measured at scroll 0 (rects are scroll-relative, so
    # measuring mid-scroll would pin a position nobody can reproduce).
    scrolled = False
    if state == "empty":
        scrolled = bool(tab.evaluate(
            """
            (() => {
              const el = document.querySelector('[data-testid="empty-state"]');
              if (!el) return false;
              el.scrollIntoView({ block: 'center' });
              return true;
            })()
            """
        ))
        if scrolled:
            tab.sleep(0.5)

    png_path = PNG_DIR / f"shell-{width}-{state}-{mask_name}.png"
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    tab.screenshot(str(png_path))
    if scrolled:
        tab.evaluate("window.scrollTo(0, 0)")
        tab.sleep(0.3)
    dims = png_dims(png_path)

    viewport = measured.get("viewport") or [width, height]

    # ---- invariants -------------------------------------------------------
    check(
        "no-horizontal-overflow",
        (measured.get("scrollWidth") or 0) <= viewport[0],
        f"scrollWidth={measured.get('scrollWidth')} innerWidth={viewport[0]}",
    )

    is_desktop = bool(measured.get("isDesktop"))
    geometry = measured.get("geometry") or {}
    if is_desktop:
        check("desktop-shell-only",
              "app-topbar" in geometry and "mobile-tab-bar" not in geometry,
              f"geometry keys: {sorted(geometry)}")
    else:
        check("mobile-shell-only",
              "mobile-tab-bar" in geometry and "app-topbar" not in geometry,
              f"geometry keys: {sorted(geometry)}")

    check("mask-chip-state",
          (measured.get("chip") or {}).get("pressed") == str(want_masked).lower(),
          f"chip={measured.get('chip')}")

    if want_masked:
        check("masked-no-dollar-figures", len(money) == 0,
              f"{len(money)} figure(s): {money[:5]}")
    else:
        # Only meaningful where dollars exist at all — the empty state has none
        # by construction, so the detector proves itself on the money pages.
        if state in ("demo", "more"):
            check("unmasked-detector-finds-dollars", len(money) > 0,
                  f"{len(money)} figure(s)")

    banner = measured.get("demoBanner")
    check("demo-read-only-banner", banner == DEMO_BANNER, f"banner={banner!r}")

    if state == "empty":
        check("empty-state-on-screen",
              bool(measured.get("emptyStateTitle")),
              f"title={measured.get('emptyStateTitle')!r}")
        check("empty-state-geometry-measured", "empty-state" in geometry,
              f"geometry keys: {sorted(geometry)}")
    else:
        check("no-empty-state", "empty-state" not in geometry,
              "empty-state present on a populated page")

    if state == "more":
        if is_desktop:
            panel = geometry.get("more-panel")
            check("more-panel-open", panel is not None and panel["y"] > 0,
                  f"more-panel={panel}")
        else:
            x = measured.get("appNavX")
            check("nav-drawer-open", x is not None and x > -5, f"app-nav x={x}")
    else:
        if is_desktop:
            check("more-panel-closed", "more-panel" not in geometry,
                  "more-panel rendered while closed")
        else:
            x = measured.get("appNavX")
            check("nav-drawer-closed", x is not None and x <= -200,
                  f"app-nav x={x}")

    # `role="alert"` is deliberately NOT part of this: it is also the budget
    # alert card's own role (Dashboard renders a breached budget AS
    # role="alert"), so a blanket zero would pin the demo tenant's DATA rather
    # than the shell's health. The error surface of the state taxonomy
    # (docs/47 §3) is `error-panel`; a refused write would leave `demo-refusal`.
    check("no-error-surface",
          (measured.get("errorPanelCount") == 0
           and measured.get("demoRefusalCount") == 0),
          f"error-panel x{measured.get('errorPanelCount')}, "
          f"demo-refusal x{measured.get('demoRefusalCount')}, "
          f"(role=alert x{measured.get('alertCount')} — budget alert cards "
          "legitimately carry it)")

    check("screenshot-dims",
          dims == (viewport[0], viewport[1]),
          f"png={dims} viewport={tuple(viewport)} path={png_path.name}")

    return {
        "id": cell_id,
        "width": width,
        "height": height,
        "state": state,
        "mask": mask_name,
        "geometry": geometry,
        "pinned": {k: {f: v[f] for f in PIN_SPEC.get(k, ()) if f in v}
                   for k, v in geometry.items() if k in PIN_SPEC},
        "money_count": len(money),
        "money_sample": money[:8],
        "png": str(png_path.relative_to(HERE)),
        "png_dims": list(dims) if dims else None,
        "measured": {k: measured.get(k) for k in
                     ("scrollWidth", "demoLockCount", "appNavX", "chip")},
        "checks": checks,
        "failures": [c for c in checks if not c["ok"]],
    }


def compare_baseline(cell: dict, baseline: dict | None,
                     tolerance: float) -> list[dict]:
    """Geometry pins — the part that makes this a REGRESSION matrix."""
    out: list[dict] = []
    if baseline is None:
        out.append({
            "name": "geometry-vs-baseline",
            "ok": False,
            "detail": f"{BASELINE_PATH.name} missing — run --update-baseline "
                      "at the tree under test",
        })
        return out
    expected = (baseline.get("cells") or {}).get(cell["id"])
    if expected is None:
        out.append({
            "name": "geometry-vs-baseline",
            "ok": False,
            "detail": f"cell {cell['id']} not in the baseline — run "
                      "--update-baseline",
        })
        return out

    want = expected.get("geometry") or {}
    got = cell.get("pinned") or {}
    problems: list[str] = []
    for name in sorted(set(want) | set(got)):
        if name not in got:
            problems.append(f"{name}: MISSING (baseline pinned it)")
            continue
        if name not in want:
            problems.append(f"{name}: unexpected in measurement (not pinned)")
            continue
        for field in PIN_SPEC.get(name, ()):  # spec drives the field set
            if field not in want[name]:
                continue
            delta = abs(float(got[name][field]) - float(want[name][field]))
            if delta > tolerance:
                problems.append(
                    f"{name}.{field}: {got[name][field]} vs baseline "
                    f"{want[name][field]} (Δ{delta:.1f}px > {tolerance}px)"
                )
    out.append({
        "name": "geometry-vs-baseline",
        "ok": not problems,
        "detail": "; ".join(problems) if problems else
                  f"{len(want)} element(s) within ±{tolerance}px",
    })
    return out


def build_baseline(cells: list[dict], *, base: str, served: str | None,
                   head: str | None) -> dict:
    return {
        "_readme": [
            "Visual-regression baseline for shell_visual_matrix_probe.py.",
            "Pinned FIELDS per element live in that probe's PIN_SPEC (heights",
            "that follow page content are deliberately not pinned).",
            "Refresh ONLY at the tree under test — run --update-baseline there",
            "and record the SHA below; a baseline from an older tree is not a",
            "defect report about a newer one (PM rule).",
            "Compare with ±tolerance_px; a missing/extra element fails too.",
        ],
        "schema": 1,
        "tolerance_px": TOLERANCE_PX,
        "captured": {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "base": base,
            "served_build_commit": served,
            "repo_head": head,
            "note": (
                "captured from the tree under test; the matrix change itself "
                "adds no app code, so app bytes == repo_head at capture time"
            ),
        },
        "cells": {
            cell["id"]: {"geometry": cell["pinned"],
                         "png": {"w": cell["png_dims"][0],
                                 "h": cell["png_dims"][1]}
                         if cell.get("png_dims") else None}
            for cell in cells
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help="base URL of the build under test")
    parser.add_argument("--out", default=str(OUT_DEFAULT))
    parser.add_argument("--update-baseline", action="store_true",
                        help="rewrite the geometry baseline from this run")
    args = parser.parse_args()
    from probe_target import require_target
    require_target(args.base, who="shell_visual_matrix_probe.py")
    base = args.base.rstrip("/")

    served, fetch_error = served_build_commit(base)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                          capture_output=True, text=True,
                          timeout=30).stdout.strip() or None
    report: dict = {
        "probe": "shell_visual_matrix",
        "status": "ok",
        "base": base,
        "served_build_commit": served,
        "repo_head": head,
        "tolerance_px": TOLERANCE_PX,
        "baseline": str(BASELINE_PATH.relative_to(REPO_ROOT))
        if BASELINE_PATH.exists() else None,
        "cells": [],
        "notes": [],
    }

    # ---- preflight: a build from before the shell has no matrix to run ----
    if fetch_error and not served:
        report["status"] = "error"
        report["notes"].append(fetch_error)
        report["passed"], report["failed"] = 0, 1
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"0 passed, 1 failed → {args.out}")
        print(f"PROBE FAILURE: {fetch_error}", file=sys.stderr)
        return 1
    if predates_shell(served):
        reason = (f"served build {served} predates the M185 shell "
                  f"({SHELL_COMMIT}) — no shell markers exist to measure")
        report["status"] = "skipped"
        report["notes"].append(reason)
        report["passed"], report["failed"] = 0, 0
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"0 passed, 0 failed → {args.out}")
        print(f"SKIPPED: {reason}")
        return 0

    baseline: dict | None = None
    if BASELINE_PATH.exists():
        baseline = json.loads(BASELINE_PATH.read_text())

    browser = Browser(width=WIDTHS[0][0], height=WIDTHS[0][1])
    try:
        for width, height in WIDTHS:
            try:
                tab = open_session(browser, base, width, height)
            except Exception as exc:  # noqa: BLE001 — reported, never a traceback
                report["cells"].append({
                    "id": f"{width}x{height}|session|n/a",
                    "width": width, "height": height, "state": "session",
                    "mask": "-", "geometry": {}, "pinned": {},
                    "checks": [{"name": "demo-session-opened", "ok": False,
                                "detail": repr(exc)}],
                    "baseline_checks": [], "failures": [],
                    "png": None, "png_dims": None,
                    "money_count": 0, "money_sample": [], "measured": {},
                })
                continue
            for state in STATES:
                for mask_name, want_masked in MASKS:
                    try:
                        cell = run_cell(tab, base=base, width=width, height=height,
                                        state=state, mask_name=mask_name,
                                        want_masked=want_masked)
                    except Exception as exc:  # noqa: BLE001 — reported per cell
                        cell = {
                            "id": f"{width}x{height}|{state}|{mask_name}",
                            "width": width, "height": height, "state": state,
                            "mask": mask_name, "geometry": {}, "pinned": {},
                            "checks": [{"name": "cell-ran", "ok": False,
                                        "detail": repr(exc)}],
                            "png": None, "png_dims": None,
                            "money_count": 0, "money_sample": [],
                            "measured": {},
                        }
                    cell["baseline_checks"] = (
                        [] if args.update_baseline or cell["state"] == "session"
                        else compare_baseline(cell, baseline, TOLERANCE_PX)
                    )
                    cell["failures"] = [
                        c for c in cell["checks"] + cell["baseline_checks"]
                        if not c["ok"]
                    ]
                    report["cells"].append(cell)
                    state_word = "OK  " if not cell["failures"] else "FAIL"
                    print(f"  [{state_word}] {cell['id']}  "
                          f"({len(cell['checks']) + len(cell['baseline_checks'])} checks)")
                    for failure in cell["failures"]:
                        print(f"         - {failure['name']}: {failure['detail']}")
            tab.close()
    finally:
        browser.close()

    checks = [c for cell in report["cells"] for c in
              cell["checks"] + cell["baseline_checks"]]
    passed = sum(1 for c in checks if c["ok"])
    failed = len(checks) - passed
    report["passed"], report["failed"] = passed, failed

    if args.update_baseline:
        invariant_failures = [
            c for cell in report["cells"] for c in cell["checks"] if not c["ok"]
        ]
        if invariant_failures or failed:
            report["notes"].append(
                "baseline NOT written: invariants must pass before a state "
                "can become the baseline"
            )
            print("baseline NOT written — fix the failing invariants first "
                  "(a broken state must never become the baseline)", file=sys.stderr)
        else:
            baseline_doc = build_baseline(report["cells"], base=base,
                                          served=served, head=head)
            BASELINE_PATH.write_text(json.dumps(baseline_doc, indent=2,
                                                ensure_ascii=False) + "\n")
            report["baseline"] = str(BASELINE_PATH.relative_to(REPO_ROOT))
            report["notes"].append(f"baseline rewritten: {BASELINE_PATH.name}")
            print(f"baseline written → {BASELINE_PATH}")

    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"{passed} passed, {failed} failed → {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

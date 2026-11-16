"""M138 — dashboard a11y sweep + the subscriptions card's LIVE evidence.

Run:
    A11Y_BASE=https://fintrac.princessandmark.com \
      python3 m138_dashboard_probe.py [--keep-tenant] [--skip-seed]

What it proves, in the order the checks run:

1. **The gate, end to end.** A throwaway tenant is seeded with the M133
   fixture — FILM.CA ×2 (the verdict row), LOBLAW / TIM HORTONS / SHELL junk
   and the strict six (PAYROLL / RENT / ROGERS / GOODLIFE / NETFLIX /
   SPOTIFY) — and `GET /transactions/recurring/candidates?source=subscriptions`
   must answer with the six and NOTHING else.
2. **The card, live.** Headline `Bills you probably have`, cap-5 + `Show all`,
   `Dismiss all (N)` and a per-row `Not a bill` that writes NOTHING while its
   6-second Undo toast is up and exactly one POST per key afterwards.
3. **The KPI cleanup.** `Range net` present, `Month net` gone, `Positions
   value` hidden (this tenant holds no positions).
4. **The a11y sweep**: axe-core 0 violations × text-node overlaps 0 × console
   0 × a skip link that is stop #1, visible when focused and >= 44px tall —
   at 1440 and 390, in both themes.

The tenant is deleted at the end (`DELETE /users/me` -> 204) unless
`--keep-tenant` is passed. The demo account is never touched.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from a11y_driver import Browser
from axe_scan import AXE, force_viewport, load_axe, run_axe, set_theme, settle, summarize
from probe_target import random_password

BASE = os.environ.get("A11Y_BASE", "https://fintrac.princessandmark.com").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))

FAILURES: list[str] = []
NOTES: list[str] = []


# The throwaway tenant is deleted on ANY exit path — a probe that dies
# half-way must not leave a registered account behind (M133's review had to
# report one as a known leftover because it could not clean up).
_CLEANUP: dict[str, Any] = {"api": None, "tenant": None, "keep": False, "done": False}


def _delete_tenant() -> None:
    api, tenant = _CLEANUP["api"], _CLEANUP["tenant"]
    if api is None or tenant is None or _CLEANUP["keep"] or _CLEANUP["done"]:
        return
    # docs/08 `DELETE /users/me` takes DeleteAccountRequest — the password is
    # the throwaway the script itself minted; it is never printed or logged.
    status, _ = api.call(
        "DELETE", "/users/me", {"current_password": _CLEANUP["password"]}
    )
    _CLEANUP["done"] = True
    print(
        f"  [{'PASS' if status == 204 else 'FAIL'}] throwaway tenant deleted "
        f"({tenant['email']}) — DELETE /users/me {status}"
    )


atexit.register(_delete_tenant)


def check(label: str, ok: bool, detail: Any = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail not in ("", None) else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}")
    return ok


def note(label: str, detail: Any = "") -> None:
    print(f"  [note] {label}" + (f" — {detail}" if detail not in ("", None) else ""))
    NOTES.append(f"{label}: {detail}")


# --------------------------------------------------------------------------
# HTTP helpers (registration + seeding happen OUTSIDE the page: no password
# is ever typed into a form, and the browser is handed the httpOnly cookie).
# --------------------------------------------------------------------------


class Api:
    def __init__(self) -> None:
        self.token: str | None = None
        self.cookie: str | None = None

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        url = f"{BASE}/api/v1{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                for key, value in resp.headers.items():
                    if key.lower() == "set-cookie" and value.startswith("refresh_token="):
                        self.cookie = value.split(";", 1)[0]
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:  # noqa: PERF203 — probe, not hot path
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw


def _day(base, offset: int) -> str:
    from datetime import timedelta

    return (base - timedelta(days=offset)).isoformat()


def seed_fixture(api: Api) -> dict:
    """The M133 fixture: 4 junk payees + the strict six, one bank account."""
    from datetime import timedelta

    today = _today_et()
    password = random_password("m138")
    _CLEANUP["password"] = password
    email = f"m138-probe-{int(time.time())}@example.com"

    status, _ = api.call("POST", "/auth/register", {"email": email, "password": password})
    check("register the throwaway tenant", status == 200, status)
    status, body = api.call("POST", "/auth/login", {"email": email, "password": password})
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise SystemExit(f"login failed: {status} {body}")
    api.token = body["access_token"]
    check("login (no password touches a form)", True)

    status, account = api.call(
        "POST", "/accounts", {"name": "M138 probe", "type": "chequing", "currency": "CAD"}
    )
    if status not in (200, 201):
        raise SystemExit(f"account create failed: {status} {account}")
    account_id = account["id"]

    rows: list[dict] = []

    def add(payee: str, amount: str, offset_days: int) -> None:
        rows.append(
            {
                "account_id": account_id,
                "date": _day(today, offset_days),
                "posting_date": _day(today, offset_days),
                "raw_description": payee,
                "normalized_payee": payee,
                "amount_native": amount,
                "currency": "CAD",
            }
        )

    # --- the verdict row: 2 charges, 30 days apart — clears M27, never M96.
    add("FILM.CA", "-36.35", 60)
    add("FILM.CA", "-36.35", 30)

    # --- the other three M133 junk payees: irregular gaps, two amount bands
    #     far outside +-15%, so neither the amount band nor any cadence fits.
    for payee, gap_cycle, amounts, count in (
        ("LOBLAW", (6, 6, 6, 6, 11, 9, 6, 6, 6, 6), ("-18.40", "-72.55"), 8),
        ("TIM HORTONS", (3, 3, 3, 3, 8, 7, 3, 3, 3, 3), ("-2.85", "-14.60"), 8),
        ("SHELL", (7, 7, 7, 7, 13, 9, 7, 7, 7, 7), ("-21.40", "-64.90"), 8),
    ):
        offset = 395
        for index in range(count):
            add(payee, amounts[index % 2], offset)
            offset -= gap_cycle[index % len(gap_cycle)]

    # --- the strict six: 4 same-day-month charges each, constant amount.
    for payee, amount in (
        ("PAYROLL", "5000.00"),
        ("RENT", "-2200.00"),
        ("ROGERS", "-95.00"),
        ("GOODLIFE", "-52.00"),
        ("NETFLIX", "-15.99"),
        ("SPOTIFY", "-5.99"),
    ):
        for index in range(4):
            add(payee, amount, 30 * (3 - index))

    for row in rows:
        status, body = api.call("POST", "/transactions", row)
        if status not in (200, 201):
            raise SystemExit(f"transaction create failed: {status} {body}")

    # 2 FILM.CA + 3 junk payees x 8 + the strict six x 4 = 50.
    check("seeded the M133 fixture", len(rows) == 50, f"{len(rows)} rows")

    status, page = api.call("GET", "/transactions/recurring/candidates?source=subscriptions")
    found = [g["payee"] for g in (page or {}).get("groups", [])] if status == 200 else []
    expected = ["PAYROLL", "RENT", "ROGERS", "GOODLIFE", "NETFLIX", "SPOTIFY"]
    check(
        "subscriptions source = the strict six, and nothing else",
        sorted(found) == sorted(expected),
        f"{status} {sorted(found)}",
    )
    for junk in ("FILM.CA", "LOBLAW", "TIM HORTONS", "SHELL"):
        check(f"{junk} never surfaces", junk not in found)
    check(
        "every group carries a subscriptions dismiss_key",
        all(g["dismiss_key"].endswith("|subscriptions") for g in (page or {}).get("groups", [])),
    )
    return {"email": email, "account_id": account_id}


def open_dashboard(tab, api: Api) -> str:
    """Land on /dashboard with a LIVE session, re-minting one if it lapsed.

    The refresh token rotates on every boot, so the cookie injected once at
    the start is spent after the first load — a later `navigate()` came back
    at /login and the whole sweep silently measured the login page (axe's
    `region` + `landmark-one-main` over `input[type=email]` was the tell).
    Re-login only when the page actually lost the session.
    """
    email = _CLEANUP["tenant"]["email"]
    password = _CLEANUP["password"]
    last = ""
    for attempt in range(3):
        if attempt:
            status, body = api.call("POST", "/auth/login", {"email": email, "password": password})
            if status != 200 or not api.cookie:
                break
            tab.send("Network.enable")
            tab.send(
                "Network.setCookie",
                {"name": "refresh_token", "value": api.cookie.split("=", 1)[1],
                 "url": BASE, "httpOnly": True, "path": "/"},
            )
        tab.navigate(f"{BASE}/dashboard")
        settle(tab)
        last = str(tab.evaluate("location.pathname"))
        if last == "/dashboard":
            return last
    raise RuntimeError(f"never reached /dashboard (last={last})")


def _today_et():
    import datetime as dt
    from zoneinfo import ZoneInfo

    return dt.datetime.now(ZoneInfo("America/Toronto")).date()


# --------------------------------------------------------------------------
# a11y sweeps
# --------------------------------------------------------------------------

# The M82 contract, measured the way M133 measured it: the WHOLE document,
# sidebar chrome and SVG chart text included — D-6 (sidebar) and D-7 (chart
# axis) both live OUTSIDE an HTML-flow-only `main` sweep, which is why an
# earlier cut of this sweep read a clean 0 while the review counted 7.
#
# Excluded: `.sr-only` (clipped to 1px, its rect is a phantom), anything under
# a `position: fixed` ancestor (the toast stack and the pull-to-refresh
# indicator are overlay layers — a stacking decision, not a flow overlap), and
# `aria-hidden` subtrees. `sticky` is deliberately NOT excluded: the sidebar
# rail is `md:sticky` and D-6 is a collision inside it.
SWEEP_JS = r"""(() => {
  const root = document.body;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const items = [];
  const overlaid = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      if (getComputedStyle(n).position === 'fixed') return true;
      n = n.parentElement;
    }
    return false;
  };
  let n;
  let nid = 0;
  while ((n = walker.nextNode())) {
    nid++;
    const text = (n.textContent || '').trim();
    if (!text) continue;
    const p = n.parentElement;
    if (!p) continue;
    if (p.closest('.sr-only')) continue;
    if (overlaid(p)) continue;
    const cs = getComputedStyle(p);
    if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      // Clip to every scrolling ancestor: a row sitting just outside a
      // scroll container's visible box still reports an untruncated Range
      // rect, which is how a nav list scrolled under its own pinned footer
      // reads as an overlap it does not actually print. Intersection only —
      // anything whose VISIBLE box collapses is not on screen.
      let l = r.left, t = r.top, ri = r.right, b = r.bottom;
      let anc = p;
      while (anc && anc !== document.documentElement) {
        const cs = getComputedStyle(anc);
        if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
          const cr = anc.getBoundingClientRect();
          l = Math.max(l, cr.left); t = Math.max(t, cr.top);
          ri = Math.min(ri, cr.right); b = Math.min(b, cr.bottom);
        }
        anc = anc.parentElement;
      }
      if (ri - l < 1 || b - t < 1) continue;
      if (b < 0 || t > innerHeight || ri < 0 || l > innerWidth) continue;
      let path = '', n2 = p, hops = 0;
      while (n2 && n2 !== document.body && hops < 4) {
        path = (n2.tagName || '') + (n2.id ? '#' + n2.id : '') + '>' + path;
        n2 = n2.parentElement; hops++;
      }
      items.push({t: text.slice(0, 30), l, r: ri, top: t, b, nid,
                  c: p.closest('[aria-hidden="true"]') ? 1 : 0,
                  d: p.tagName + '.' + String(p.className || '').slice(0, 24),
                  box: [Math.round(l), Math.round(t), Math.round(ri - l), Math.round(b - t)],
                  path});
    }
  }
  items.sort((a, b) => a.l - b.l);
  let overlaps = 0;
  const samples = [];
  for (let i = 0; i < items.length; i++) {
    for (let j = i + 1; j < items.length && items[j].l < items[i].r; j++) {
      const a = items[i], b = items[j];
      if (a.c || b.c) continue;
      // Same text node: a `text-overflow: ellipsis` run reports BOTH its
      // unclipped layout rect and its clipped one, which overlap by
      // construction. One node cannot print into itself.
      if (a.nid === b.nid) continue;
      const ox = Math.min(a.r, b.r) - Math.max(a.l, b.l);
      const oy = Math.min(a.b, b.b) - Math.max(a.top, b.top);
      if (ox > 1 && oy > 1) {
        overlaps++;
        if (samples.length < 6)
          samples.push([a.t, b.t, Math.round(ox), Math.round(oy), a.d, b.d,
                        a.box, b.box, a.path, b.path]);
      }
    }
  }
  return {nodes: items.length, overlaps, samples};
})()"""

FOCUS_DESC = """(() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const r = el.getBoundingClientRect();
  return {tag: el.tagName, text: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 48),
          inMain: !!(el.closest('main')), href: el.getAttribute('href') || '',
          w: Math.round(r.width), h: Math.round(r.height),
          visible: r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'};
})()"""


def sweep(tab, name: str) -> dict:
    """Scroll the whole page, sweeping each viewport — D-7's chart-axis
    labels are below the fold at 1440, so a single top-of-page pass reads a
    clean 0 while the axis pairs print over each other further down."""
    height = int(tab.evaluate("innerHeight") or 900)
    total_h = int(tab.evaluate("document.documentElement.scrollHeight") or height)
    step = max(240, height - 160)
    nodes = 0
    overlaps = 0
    samples: list = []
    seen: set[tuple] = set()
    y = 0
    while y <= total_h:
        tab.evaluate(f"window.scrollTo(0, {y})")
        tab.sleep(0.3)
        res = tab.evaluate(SWEEP_JS) or {}
        nodes += int(res.get("nodes", 0))
        overlaps += int(res.get("overlaps", 0))
        for sample in res.get("samples", []):
            key = tuple(sample[:2])
            if key not in seen:
                seen.add(key)
                samples.append(sample)
        y += step
    tab.evaluate("window.scrollTo(0, 0)")
    tab.sleep(0.2)
    check(
        f"text-node overlaps 0 — {name}",
        overlaps == 0,
        f"node-visits={nodes} overlaps={overlaps} {samples[:6]}",
    )
    return {"nodes": nodes, "overlaps": overlaps, "samples": samples}


def run_axe_checks(tab, name: str) -> dict:
    with open(AXE, encoding="utf-8") as fh:
        tab.evaluate(fh.read())
    result = run_axe(tab) or {}
    summary = summarize(result.get("violations", []))
    counts = summary["counts"]
    check(f"axe 0 violations — {name}", sum(counts.values()) == 0, summary["violations"][:3])
    return summary


def keyboard_walk(tab) -> dict:
    """Skip link must be stop #1, visible and >=44px, and land past the chrome."""
    # Blur whatever the previous interaction left focused, or Tab continues
    # from there instead of from the top of the document.
    tab.evaluate(
        "(() => { const a = document.activeElement;"
        " if (a && a !== document.body && typeof a.blur === 'function') a.blur();"
        " window.scrollTo(0, 0); return true; })()"
    )
    tab.sleep(0.2)
    tab.press("Tab")
    first = tab.evaluate(FOCUS_DESC)
    check(
        "skip link is keyboard stop #1",
        bool(first) and "Skip to main content" in (first or {}).get("text", ""),
        first,
    )
    if first:
        check(
            "skip link is VISIBLE when focused",
            bool(first.get("visible")),
            first,
        )
        check(
            "skip link >= 44px tall when focused",
            first.get("h", 0) >= 44,
            f"h={first.get('h')}",
        )
    # Activate it and confirm focus really moved past the chrome.
    tab.press("Enter")
    tab.sleep(0.3)
    landed = tab.evaluate(
        "(() => { const a = document.activeElement; return a ? a.id || a.tagName : null; })()"
    )
    check("skip link moves focus to #main-content", landed == "main-content", landed)
    # Then count plain Tab stops from the top until focus enters <main>.
    tab.evaluate(
        "(() => { const a = document.activeElement;"
        " if (a && typeof a.blur === 'function') a.blur(); return true; })()"
    )
    stops = 0
    inside_at = None
    tab.evaluate("document.body.focus?.()")
    for _ in range(80):
        tab.press("Tab")
        stops += 1
        info = tab.evaluate(FOCUS_DESC)
        if info is None:
            continue
        if info.get("inMain"):
            inside_at = stops
            break
    check(
        "first in-main control within 12 plain Tab stops (skip link is the bypass)",
        inside_at is not None and inside_at <= 12,
        f"stops={stops} first_in_main={inside_at}",
    )
    note("keyboard stops before the first in-main control", f"{inside_at} of {stops} measured")
    return {"stops_to_main": inside_at, "total": stops}


def console_check(tab, name: str) -> None:
    errors = tab.console_errors()
    check(f"console 0 errors — {name}", errors == [], errors[:4])


# --------------------------------------------------------------------------
# M138 functional checks on the card
# --------------------------------------------------------------------------

SUPPRESS_CALLS_JS = """(() => Array.from(performance.getEntriesByType('resource'))
  .map(e => e.name)
  .filter(u => u.includes('/transactions/recurring/suppress')))()"""


def page_text(tab) -> str:
    """Lower-cased rendered text — the KPI labels are CSS-uppercased, so a
    case-sensitive `includes('Range net')` reads `RANGE NET` and misses."""
    return str(tab.evaluate("document.body.innerText") or "").lower()


def card_checks(tab) -> None:
    text = page_text(tab)
    check(
        "headline is 'Bills you probably have'",
        tab.evaluate("!!Array.from(document.querySelectorAll('h2')).find(h => h.textContent.trim() === 'Bills you probably have')"),
    )
    check("'month net' is gone from the KPI strip", "month net" not in text)
    check("'range net' is present", "range net" in text)
    check("'positions value' hidden on a tenant with no holdings", "positions value" not in text)
    check("the old 2-occurrence hint is gone", "2 or more charges" not in text)
    # Diagnostic for the sidebar's duplicate-email overlap: list every leaf
    # element rendering the tenant's address, with its box.
    email = _CLEANUP.get("tenant", {}).get("email")
    if email:
        note(
            "leaf nodes rendering the tenant email",
            tab.evaluate(
                "(() => { const want = arguments0; const out = []; "
                "document.querySelectorAll('*').forEach(el => { "
                "if (el.children.length === 0 && (el.textContent || '').trim() === want) {"
                " const r = el.getBoundingClientRect();"
                " out.push([el.tagName, String(el.className).slice(0, 40),"
                " Math.round(r.left), Math.round(r.top), Math.round(r.width)]); } });"
                " return out; })()".replace("arguments0", json.dumps(email))
            ),
        )

    rows_js = "document.querySelectorAll('[aria-label^=\"Not a bill\"]').length"
    count = tab.evaluate(rows_js)
    check("cap: exactly 5 rows before Show all", count == 5, count)
    show_all = tab.evaluate(
        "(() => { const b = Array.from(document.querySelectorAll('button')).find(x => /^Show all \\(\\d+\\)$/.test(x.innerText.trim()));"
        " if (!b) return null; b.click(); return b.innerText.trim(); })()"
    )
    check("'Show all (N)' reveals the rest", bool(show_all), show_all)
    tab.sleep(0.4)
    after = tab.evaluate(rows_js)
    # Six groups on this fixture: the gate already dropped FILM.CA / LOBLAW /
    # TIM HORTONS / SHELL, so only the strict six are here to show.
    check("every suggestion is listed after Show all", after == 6, after)

    # Dismissal: nothing is POSTed while the Undo toast is up.
    tab.evaluate(SUPPRESS_CALLS_JS)  # prime the buffer
    before = tab.evaluate(SUPPRESS_CALLS_JS)
    tab.evaluate(
        "document.querySelector('[aria-label^=\"Not a bill\"]').click()"
    )
    tab.sleep(0.5)
    gone = tab.evaluate("document.querySelectorAll('[aria-label^=\"Not a bill\"]').length")
    during = tab.evaluate(SUPPRESS_CALLS_JS)
    check("per-row 'Not a bill' hides the row immediately", gone == 5, gone)
    check("NO suppression POST while the Undo toast is up", len(during) == len(before), during)
    check("Undo snackbar is on screen",
          tab.evaluate("!!document.querySelector('[data-testid=\"undo-snackbar\"]')"))
    tab.screenshot(os.path.join(HERE, "m138-dismiss-undo-1440.png"))
    tab.sleep(6.5)
    after_commit = tab.evaluate(SUPPRESS_CALLS_JS)
    check("exactly one suppression POST once the 6s window closes",
          len(after_commit) - len(before) == 1, after_commit)
    tab.screenshot(os.path.join(HERE, "m138-card-1440.png"))

    # M133's density baseline: 8 modules / 21 cards / page 2370px @1440, and
    # the subscriptions module was 688px = 29% of the page (the tallest thing
    # on it). Record the same three numbers so the shrink is a measurement.
    geometry = tab.evaluate(
        "(() => { const heads = Array.from(document.querySelectorAll('h2'));"
        " const h = heads.find(x => x.textContent.trim() === 'Bills you probably have');"
        " const mod = h ? (h.closest('.space-y-3') || h.closest('.glass')) : null;"
        " const rows = document.querySelectorAll('[aria-label^=\"Not a bill\"]').length;"
        " return {module_px: mod ? Math.round(mod.getBoundingClientRect().height) : null,"
        " page_px: document.documentElement.scrollHeight,"
        " kpi_cards: document.querySelectorAll('main .glass').length,"
        " card_rows: rows}; })()"
    )
    note("dashboard geometry @1440 (collapsed card)", geometry)


def demo_checks(tab) -> None:
    """READ-ONLY look at a real, populated dashboard (the demo tenant).

    Deliberately no dismissal: a write here would leave a suppression row in
    the demo account, and M133 pinned that account as untouched. The dismissal
    flow is proved against the seeded fixture instead.
    """
    text = page_text(tab)
    check("demo: headline is 'Bills you probably have'", "bills you probably have" in text)
    check("demo: 'month net' is gone", "month net" not in text)
    check("demo: 'range net' is present", "range net" in text)
    check("demo: the old 2-occurrence hint is gone", "2 or more charges" not in text)
    rows = int(tab.evaluate("document.querySelectorAll('[aria-label^=\"Not a bill\"]').length") or 0)
    shown = rows <= 5
    check("demo: card capped at 5 rows", shown, f"rows={rows}")
    toggled = tab.evaluate(
        "(() => { const b = Array.from(document.querySelectorAll('button')).find(x => /^Show all \\d*$|^Show all \\(\\d+\\)$/.test(x.innerText.trim()));"
        " if (!b) return null; b.click(); return b.innerText.trim(); })()"
    )
    after = int(tab.evaluate("document.querySelectorAll('[aria-label^=\"Not a bill\"]').length") or 0)
    check("demo: Show all reveals the rest", after >= rows, f"{rows} -> {after} ({toggled})")
    geometry = tab.evaluate(
        "(() => { const h = Array.from(document.querySelectorAll('h2')).find(x => x.textContent.trim() === 'Bills you probably have');"
        " const mod = h ? (h.closest('.space-y-3') || h.closest('.glass')) : null;"
        " return {module_px: mod ? Math.round(mod.getBoundingClientRect().height) : null,"
        " page_px: document.documentElement.scrollHeight}; })()"
    )
    note("demo dashboard geometry @1440", geometry)
    tab.screenshot(os.path.join(HERE, "m138-demo-dashboard-1440.png"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-tenant", action="store_true")
    ap.add_argument("--skip-seed", action="store_true")
    ap.add_argument("--themes", default="dark,light")
    # --baseline: measure the a11y contract ONLY, against the demo tenant and
    # with no fixture seeded — used to locate D-6/D-7/D-8 before the M138
    # functional checks exist on the deployed build.
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--demo", action="store_true",
                    help="read-only visual pass on the demo tenant (no writes, no sweep)")
    args = ap.parse_args()
    if not (args.demo or args.baseline):
        from probe_target import require_target
        require_target(BASE, who="m138_dashboard_probe.py")

    api = Api()
    tenant: dict | None = None
    if not args.skip_seed and not args.baseline and not args.demo:
        tenant = seed_fixture(api)
        _CLEANUP.update({"api": api, "tenant": tenant, "keep": args.keep_tenant})

    report: dict[str, Any] = {"base": BASE, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    browser = Browser(width=1440, height=900)
    try:
        tab = browser.new_tab(f"{BASE}/login")
        tab.enable_runtime()
        force_viewport(tab, 1440, 900)
        tab.navigate(f"{BASE}/login")
        settle(tab, extra=0.8)
        report["viewport"] = force_viewport(tab, 1440, 900)
        raf = tab.evaluate(
            "new Promise((res) => { let n = 0; const t0 = performance.now();"
            " const tick = () => { n++; if (performance.now() - t0 < 500) requestAnimationFrame(tick); else res(n); };"
            " requestAnimationFrame(tick); })",
            await_promise=True,
        )
        # AGENTS §5's real hazard is a STALLED rAF (0 frames); the host's
        # headless Chrome also runs well under 60fps, so the gate is
        # "still ticking", and the count is recorded rather than gamed.
        check("rAF is alive (AGENTS §5)", int(raf or 0) > 0, raf)
        report["raf_frames_in_500ms"] = raf

        if tenant is not None and api.cookie:
            tab.send("Network.enable")
            tab.send(
                "Network.setCookie",
                {"name": "refresh_token", "value": api.cookie.split("=", 1)[1],
                 "url": BASE, "httpOnly": True, "path": "/"},
            )
            check("session injected as the httpOnly cookie (no password in the page)",
                  open_dashboard(tab, api) == "/dashboard")
        else:
            # Demo tenant: one click, no credentials typed by the agent.
            tab.navigate(f"{BASE}/login")
            settle(tab, extra=0.8)
            tab.evaluate(
                "Array.from(document.querySelectorAll('button')).find(b => "
                "b.textContent.includes('Explore the demo'))?.click()"
            )
            tab.wait_for("location.pathname === '/dashboard'", timeout=25)
            settle(tab)
            check("demo login (one click, no credentials)", True)

        def go_dashboard() -> None:
            if tenant is not None:
                open_dashboard(tab, api)
            else:
                tab.navigate(f"{BASE}/dashboard")
                settle(tab)

        if args.demo:
            print("\nDemo dashboard (read-only):")
            set_theme(tab, "dark")
            tab.sleep(1.2)
            note("path", tab.evaluate("location.pathname"))
            note("headings", tab.evaluate(
                "Array.from(document.querySelectorAll('h1,h2')).map(h => h.textContent.trim()).slice(0, 12)"))
            note("text head", str(tab.evaluate("document.body.innerText") or "")[:400].replace("\\n", " | "))
            demo_checks(tab)
            browser.close()
            return 0

        # ---- functional + keyboard on the widest/darkest pass first
        if not args.baseline:
            print("\nM138 functional evidence (1440, dark):")
            set_theme(tab, "dark")
            tab.sleep(0.5)
            try:
                card_checks(tab)
            except Exception as exc:  # noqa: BLE001 — one bad check must not lose the sweep
                check("M138 functional checks completed", False, repr(exc))
        print("\nKeyboard / skip link:")
        try:
            go_dashboard()
        except Exception as exc:  # noqa: BLE001
            check("reopened /dashboard for the keyboard walk", False, repr(exc))
        try:
            keyboard_walk(tab)
        except Exception as exc:  # noqa: BLE001
            check("keyboard walk completed", False, repr(exc))
        console_check(tab, "dashboard 1440 dark")

        # ---- full sweep at both breakpoints x both themes
        for width, height in ((1440, 900), (390, 844)):
            for theme in args.themes.split(","):
                theme = theme.strip()
                go_dashboard()
                force_viewport(tab, width, height)
                settle(tab)
                set_theme(tab, theme)
                tab.sleep(0.6)
                label = f"dashboard {width} {theme}"
                print(f"\n{label}:")
                load_axe(tab)
                entry: dict[str, Any] = {}
                for name, fn in (("axe", lambda: run_axe_checks(tab, label)),
                                 ("sweep", lambda: sweep(tab, label))):
                    try:
                        entry[name] = fn()
                    except Exception as exc:  # noqa: BLE001
                        check(f"{name} ran — {label}", False, repr(exc))
                report[label] = entry
                console_check(tab, label)
                tab.screenshot(os.path.join(HERE, f"m138-dash-{width}-{theme}.png"))
            force_viewport(tab, 1440, 900)
    finally:
        browser.close()

    out = os.path.join(HERE, "m138-dashboard-probe.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, default=str)

    _delete_tenant()

    print("\n==== SUMMARY ====")
    for line in NOTES:
        print("  " + line)
    if FAILURES:
        print(f"  {len(FAILURES)} FAILURES:")
        for line in FAILURES:
            print("   - " + line)
        return 1
    print("  all checks passed")
    print(f"  evidence: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

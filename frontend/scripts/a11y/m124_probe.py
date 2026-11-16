"""M124 live probe (AGENTS §4 step 5) — tag grouping, drill-down, matrix.

    python3 m124_probe.py [--base http://127.0.0.1:4899]

One throwaway tenant, seeded through the API with a Food > {Groceries, Dining}
tree, two tags and a ledger where ONE transaction carries both tags — the
duplication the whole milestone is about has to be exercised live, not just in
unit fixtures.

  ui  Group by offers Category + Tag and NO Owner to a lone tenant; choosing
      Tag recomputes and relabels the chart; the caption prints BOTH the
      inflated block sum and the once-counted spend; a donut slice drills
      in-page with a breadcrumb back; the Category × Tag matrix renders as a
      real table inside a focusable scroll region.
  a11y  axe at 1440/390/412 in BOTH themes on /reports, plus the text-node
        overlap sweep (docs/09 §M82 contract).

Exit 0 only if every check passes; JSON evidence lands next to this script.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
AXE = str(REPO / "frontend" / "node_modules" / "axe-core" / "axe.min.js")

sys.path.insert(0, str(HERE))
from a11y_driver import Browser  # noqa: E402
from probe_target import random_password

PASSWORD = random_password("m124")

CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}", flush=True)
    return bool(ok)


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token: str | None = None

    def call(self, method: str, path: str, body: object | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + path, method=method)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            req.add_header("Content-Type", "application/json")
        req.data = data
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except Exception as exc:  # pragma: no cover - transport
            return -1, str(exc).encode()

    def json(self, method: str, path: str, body: object | None = None) -> Any:
        status, payload = self.call(method, path, body)
        if status < 0 or status >= 400:
            return None
        return json.loads(payload)


def settle(tab: Any, extra: float = 1.2) -> None:
    try:
        tab.wait_for(
            "document.querySelector('[data-testid=\"route-fallback\"]') === null"
            " && document.querySelector('main') !== null",
            timeout=25,
        )
    except TimeoutError:
        pass
    tab.sleep(extra)


def goto(tab: Any, base: str, path: str) -> None:
    tab.navigate(f"{base}{path}")
    settle(tab)


# ------------------------------------------------------------------- seeding
def seed(api: Api) -> dict[str, Any]:
    """Build the milestone's ledger through the public API only."""
    # A fresh tenant: register, then log in for the bearer token. The demo
    # tenant answers 403 demo_account_restricted on every write, so the probe
    # always provisions its own (same reason as the m108 probe).
    email = f"m124-{uuid.uuid4().hex[:12]}@x.c"
    registered = api.json("POST", "/api/v1/auth/register", {"email": email, "password": PASSWORD})
    auth = api.json("POST", "/api/v1/auth/login", {"email": email, "password": PASSWORD})
    api.token = (auth or {}).get("access_token")
    check(
        "probe tenant registered + logged in",
        bool(api.token) and bool(registered),
        f"{email} user={(registered or {}).get('id')}",
    )

    account = api.json("POST", "/api/v1/accounts", {"name": "Probe Chequing", "type": "chequing", "currency": "CAD"})
    account_id = (account or {}).get("id")
    check("probe account", bool(account_id), json.dumps(account or {})[:120])

    food = api.json("POST", "/api/v1/categories", {"name": "Food", "parent_id": None})
    groceries = api.json("POST", "/api/v1/categories", {"name": "Groceries", "parent_id": (food or {}).get("id")})
    dining = api.json("POST", "/api/v1/categories", {"name": "Dining", "parent_id": (food or {}).get("id")})
    vacation = api.json("POST", "/api/v1/tags", {"name": "vacation"})
    work = api.json("POST", "/api/v1/tags", {"name": "work"})
    check(
        "category tree + tags",
        all([food, groceries, dining, vacation, work]),
        json.dumps({"food": food, "vacation": vacation})[:160],
    )

    def row(day: str, amount: str, category_id: Any) -> Any:
        return api.json(
            "POST",
            "/api/v1/transactions",
            {
                "account_id": account_id,
                "date": day,
                "raw_description": f"PROBE {day}",
                "normalized_payee": "PROBE",
                "amount_native": amount,
                "currency": "CAD",
                "category_id": category_id,
            },
        )

    gid = (groceries or {}).get("id")
    did = (dining or {}).get("id")
    t1 = row("2026-01-05", "-50.00", gid)  # BOTH tags — the double count
    t2 = row("2026-01-10", "-13.60", gid)  # vacation only
    row("2026-02-10", "-25.50", did)  # no tags -> Untagged
    t4 = row("2026-02-14", "-12.25", None)  # work only, uncategorized
    row("2026-02-20", "2000.00", None)  # income -> never counted

    vid, wid = (vacation or {}).get("id"), (work or {}).get("id")
    api.json("PUT", f"/api/v1/transactions/{(t1 or {}).get('id')}/tags", {"tag_ids": [vid, wid]})
    api.json("PUT", f"/api/v1/transactions/{(t2 or {}).get('id')}/tags", {"tag_ids": [vid]})
    api.json("PUT", f"/api/v1/transactions/{(t4 or {}).get('id')}/tags", {"tag_ids": [wid]})

    spending = api.json("GET", "/api/v1/reports/spending?from=2026-01-01&to=2026-12-31&group_by=tag")
    items = (spending or {}).get("items", [])
    # items are PER BUCKET, so a tag appears once per bucket — sum, don't let
    # the last bucket overwrite the earlier ones.
    by_tag: dict[str, float] = {}
    for item in items:
        name = str(item.get("tag_name"))
        by_tag[name] = by_tag.get(name, 0.0) + float(item.get("total_cad") or 0)
    check(
        "server tag totals: vacation 63.60, work 62.25, Untagged 25.50",
        round(by_tag.get("vacation", 0), 2) == 63.60
        and round(by_tag.get("work", 0), 2) == 62.25
        and round(by_tag.get("Untagged", 0), 2) == 25.50,
        json.dumps({k: round(v, 2) for k, v in by_tag.items()}),
    )
    check(
        "block sum 151.35 vs once-counted 101.35 (the 50.00 double count)",
        (spending or {}).get("total_cad") == "151.35"
        and (spending or {}).get("spend_total_cad") == "101.35",
        json.dumps({k: spending.get(k) for k in ("total_cad", "spend_total_cad")}),
    )
    return {"email": email, "password": PASSWORD}


# ------------------------------------------------------------------ ui checks
def ui_checks(tab: Any, base: str, creds: dict[str, str], evidence: dict) -> None:
    tab.navigate(f"{base}/login")
    settle(tab)
    tab.evaluate("document.querySelector('input[type=\"email\"]').focus()")
    tab.send("Input.insertText", {"text": creds["email"]})
    tab.evaluate("document.querySelector('input[type=\"password\"]').focus()")
    tab.send("Input.insertText", {"text": creds["password"]})
    tab.evaluate(
        """(() => {
          const b = [...document.querySelectorAll('button')].find(el => el.textContent.trim() === 'Log in');
          if (!b) return false; b.click(); return true;
        })()"""
    )
    tab.wait_for("location.pathname !== '/login'", timeout=25)

    goto(tab, base, "/reports")
    control = tab.evaluate(
        """(() => {
          const c = [...document.querySelectorAll('[role=tablist]')]
            .find(el => el.getAttribute('aria-label') === 'Group by');
          if (!c) return null;
          return [...c.querySelectorAll('button')].map(b => b.textContent.trim());
        })()"""
    )
    check(
        "Group by offers Category + Tag, NO Owner (lone tenant)",
        control is not None and "Tag" in control and "Owner" not in control,
        json.dumps(control),
    )
    evidence["group_by_options"] = control

    # --- choose Tag ---
    clicked = tab.evaluate(
        """(() => {
          const c = [...document.querySelectorAll('[role=tablist]')]
            .find(el => el.getAttribute('aria-label') === 'Group by');
          const b = c && [...c.querySelectorAll('button')].find(x => x.textContent.trim() === 'Tag');
          if (!b) return false; b.click(); return true;
        })()"""
    )
    check("Tag option clicked", bool(clicked))
    tab.sleep(1.6)

    named = tab.evaluate(
        """(() => {
          const f = [...document.querySelectorAll('[role=img]')]
            .map(el => el.getAttribute('aria-label') || '');
          return f.filter(l => l.includes('Spending by tag'));
        })()"""
    )
    check("chart accessible name says 'Spending by tag'", len(named) > 0, json.dumps(named)[:160])

    caption = tab.evaluate(
        """(() => {
          const p = [...document.querySelectorAll('p')]
            .map(el => el.textContent || '')
            .find(t => t.includes('multi-tag transactions count in each'));
          return p || null;
        })()"""
    )
    check(
        "tag caption prints the inflated sum AND the real spend",
        bool(caption) and "151.35" in caption and "101.35" in caption,
        (caption or "")[:220],
    )
    evidence["tag_caption"] = caption

    # --- the matrix, on the Categories tab ---
    goto(tab, base, "/reports?tab=categories")
    tab.sleep(1.2)
    matrix = tab.evaluate(
        """(() => {
          const region = document.querySelector('[aria-label*="Category by tag spending matrix"]');
          const table = region && region.querySelector('table');
          if (!table) return null;
          return {
            regionTabIndex: region.getAttribute('tabindex'),
            overflowX: getComputedStyle(region).overflowX,
            colHeaders: table.querySelectorAll('th[scope="col"]').length,
            rowHeaders: table.querySelectorAll('th[scope="row"]').length,
            text: (table.textContent || '').replace(/\\s+/g, ' ').slice(0, 260),
          };
        })()"""
    )
    check("matrix table renders", matrix is not None, json.dumps(matrix)[:260])
    evidence["matrix"] = matrix
    if matrix:
        check(
            "matrix scroll region is focusable + horizontally scrollable",
            matrix.get("regionTabIndex") == "0" and matrix.get("overflowX") == "auto",
            f"tabindex={matrix.get('regionTabIndex')} overflow-x={matrix.get('overflowX')}",
        )
        check(
            "matrix has BOTH axis headers (col + row scope)",
            matrix.get("colHeaders", 0) > 0 and matrix.get("rowHeaders", 0) > 0,
            f"cols={matrix.get('colHeaders')} rows={matrix.get('rowHeaders')}",
        )
        check("matrix shows Groceries and a tag column", "Groceries" in matrix["text"] and "vacation" in matrix["text"], matrix["text"][:120])

    # --- the drill, from a donut slice ---
    # The donut is root-ROLLED (docs/12 §2), so the slice for our tree is
    # "Food" — Groceries/Dining fold into it. Click by aria-label suffix.
    clicked = tab.evaluate(
        """(() => {
          const b = [...document.querySelectorAll('button')]
            .find(el => (el.getAttribute('aria-label') || '').endsWith(', show detail'));
          if (!b) return false;
          // dispatchEvent, not .click(): recharts' wrapper can shadow the
          // HTMLElement method, and a real MouseEvent takes the same path a
          // pointer does.
          b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
          return true;
        })()"""
    )
    check("donut slice button clicked", bool(clicked))
    evidence["donut_slice"] = tab.evaluate(
        """(() => {
          const b = [...document.querySelectorAll('button')]
            .find(el => (el.getAttribute('aria-label') || '').endsWith(', show detail'));
          return b ? b.getAttribute('aria-label') : null;
        })()"""
    )
    tab.sleep(1.8)
    drill = tab.evaluate(
        """(() => {
          const nav = document.querySelector('nav[aria-label="Breadcrumb"]');
          if (!nav) return null;
          const table = [...document.querySelectorAll('summary')]
            .map(s => s.textContent.trim()).find(t => t.includes('numbers behind'));
          return {breadcrumb: nav.textContent.replace(/\\s+/g,' ').trim(), hasTableFallback: Boolean(table)};
        })()"""
    )
    check("donut slice drills IN-PAGE (breadcrumb appears)", drill is not None, json.dumps(drill))
    evidence["drill"] = drill
    if drill:
        check("drill keeps the data-table fallback", drill.get("hasTableFallback") is True, str(drill)[:160])
        check("drill stays on /reports", "location" not in str(drill), "no navigation")

    back = tab.evaluate(
        """(() => {
          const b = [...document.querySelectorAll('button')]
            .find(el => (el.textContent || '').includes('All categories'));
          if (!b) return false; b.click(); return true;
        })()"""
    )
    tab.sleep(1.0)
    gone = tab.evaluate("document.querySelector('nav[aria-label=\"Breadcrumb\"]') === null")
    check("breadcrumb back returns to the donut", bool(back) and gone, f"clicked={back} breadcrumb_gone={gone}")


# ------------------------------------------------------------------- a11y
SWEEP_JS = r"""(() => {
  const root = document.querySelector('main') || document.body;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const items = [];
  const pinned = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const pos = getComputedStyle(n).position;
      if (pos === 'sticky' || pos === 'fixed') return true;
      n = n.parentElement;
    }
    return false;
  };
  let n;
  while ((n = walker.nextNode())) {
    const text = (n.textContent || '').trim();
    if (!text) continue;
    const p = n.parentElement;
    if (!p) continue;
    if (p.closest('.sr-only')) continue;
    if (p.ownerSVGElement) continue;
    if (pinned(p)) continue;
    const cs = getComputedStyle(p);
    if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
      items.push({t: text.slice(0, 30), l: r.left, r: r.right, top: r.top, b: r.bottom,
                  c: p.closest('[aria-hidden="true"]') ? 1 : 0,
                  d: p.tagName + '.' + String(p.className || '').slice(0, 22)});
    }
  }
  items.sort((a, b) => a.l - b.l);
  let overlaps = 0;
  const samples = [];
  for (let i = 0; i < items.length; i++) {
    for (let j = i + 1; j < items.length && items[j].l < items[i].r; j++) {
      const a = items[i], b = items[j];
      if (a.c || b.c) continue;
      const ox = Math.min(a.r, b.r) - Math.max(a.l, b.l);
      const oy = Math.min(a.b, b.b) - Math.max(a.top, b.top);
      if (ox > 1 && oy > 1) {
        overlaps++;
        if (samples.length < 4) samples.push([a.t, b.t, Math.round(ox), Math.round(oy), a.d, b.d]);
      }
    }
  }
  return {nodes: items.length, overlaps, samples};
})()"""


def axe_check(tab: Any, name: str) -> dict:
    result = tab.evaluate("axe.run(document, {resultTypes: ['violations']})", await_promise=True)
    counts: dict[str, int] = {}
    rows: list[str] = []
    for violation in result.get("violations", []):
        impact = violation.get("impact") or "unknown"
        counts[impact] = counts.get(impact, 0) + 1
        rows.append(f"{violation['id']}({impact}x{len(violation['nodes'])})")
    bad = counts.get("critical", 0) + counts.get("serious", 0)
    check(f"axe 0 critical/serious — {name}", bad == 0, f"{counts} {rows[:4]}")
    return {"counts": counts, "rows": rows}


def a11y_sweep(tab: Any, base: str, evidence: dict) -> None:
    # axe lives in the PAGE, so every navigation wipes it — inject once per
    # viewport, after the goto, not once before the loop.
    axe_source = Path(AXE).read_text(encoding="utf-8")
    axe_results: dict[str, Any] = {}
    for theme in ("dark", "light"):
        tab.evaluate(f"localStorage.setItem('theme', {json.dumps(theme)})")
        for width, height in ((1440, 900), (390, 844), (412, 915)):
            tab.force_viewport(width, height)
            goto(tab, base, "/reports?tab=categories")
            tab.sleep(1.4)
            tab.evaluate(axe_source)
            key = f"{theme}-{width}"
            axe_results[key] = axe_check(tab, f"/reports?tab=categories {key}")
            res = tab.evaluate(SWEEP_JS)
            check(
                f"text-node sweep 0 overlaps — {key}",
                res.get("overlaps") == 0,
                f"nodes={res.get('nodes')} overlaps={res.get('overlaps')} {res.get('samples')}",
            )
            # The matrix is the new surface; it is the one that must be clean
            # at the small widths where the grid scrolls.
            goto(tab, base, "/reports?tab=categories")
            tab.sleep(1.2)
    evidence["axe"] = axe_results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:4899")
    args = parser.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m124_probe.py")

    api = Api(args.base)
    creds = seed(api)
    evidence: dict[str, Any] = {"base": args.base, "tenant": creds["email"]}

    browser = Browser()
    try:
        tab = browser.new_tab(f"{args.base}/login")
        ui_checks(tab, args.base, creds, evidence)
        a11y_sweep(tab, args.base, evidence)
    finally:
        browser.close()

    (HERE / "m124-probe.json").write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    failed = [c for c in CHECKS if not c["ok"]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed", flush=True)
    if failed:
        for item in failed:
            print(f"  FAIL {item['check']} — {item['detail']}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

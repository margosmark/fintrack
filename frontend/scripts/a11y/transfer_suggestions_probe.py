"""Transfer Suggestions panel layout — the live probe (AGENTS §4 step 5).

    python3 transfer_suggestions_probe.py [--base https://fintrac.princessandmark.com]
        [--widths 320,360,390,420] [--label after] [--axe-width 390]

Reproduces the panel Mark screenshotted as malformed (t_06c510ef) with a
THROWAWAY tenant (register + login + three accounts + four transactions), then
measures the geometry the bug was about at every narrow width:

  1 seed      register/login, accounts (one deliberately long name), and two
              unpaired opposite-amount pairs inside +/-10 days -> the panel
              renders (one pair has source_payee "-", the data value that used
              to print as a stray truncated line)
  2 shape     the pair must be ONE grid (col-start-1/2/3 cells), not two
              self-contained inline flex rows
  3 columns   amount / date / account left edges of the two rows must agree
              within 1px — that is what "shared column" means measurably
  4 wrapping  every date and account cell renders on ONE line (ellipsis when it
              doesn't fit), never a mid-label break ("(CIBC" / "Chequing)")
  5 dashes    no leaf "-" text node in the card (the payee fallback)
  6 buttons   the check/X rects must not print over ANY text rect in the card
  7 overflow  card scrollWidth <= clientWidth (nothing shoves the card wide)
  8 sweep     the M82 text-node overlap sweep over <main> = 0
  9 axe       0 critical/serious at --axe-width; console 0 errors
 10 shots     one PNG per width (360 is Mark's width)

Run it BEFORE the deploy for the "before" evidence (it fails every layout
check against the old markup — that failure IS the bug reproduced) and AFTER
for the "after" evidence. JSON lands next to this script as
`transfer-suggestions-probe-<label>.json`.

Exit 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from a11y_driver import Browser
from probe_target import random_password

HERE = Path(__file__).resolve().parent
CHECKS: list[dict] = []

PASSWORD = random_password("transfer-suggestions")
AXE = str(HERE.parents[1] / "node_modules" / "axe-core" / "axe.min.js")

# M82 text-node overlap sweep (docs/09): no two visible text rects may print
# into each other. Same contract, same scoping, as m108_probe.py / m72's
# responsive_audit.py — main only, .sr-only and SVG text and pinned (sticky/
# fixed) layers skipped, viewport-clipped and x-sorted.
SWEEP_JS = r"""(() => {
  const root = document.querySelector('main') || document.body;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const items = [];
  let nodeId = 0;
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
    // walk the WHOLE ancestor chain: the account page renders its <h1> twice
    // (lg + mobile variants) and only the duplicate behind display:none was
    // reaching the comparison as a phantom pair.
    let hidden = false, q = p;
    while (q && q !== document.documentElement) {
      const csq = getComputedStyle(q);
      if (csq.display === 'none' || csq.visibility === 'hidden' || +csq.opacity === 0) { hidden = true; break; }
      q = q.parentElement;
    }
    if (hidden) continue;
    const myId = nodeId++;
    // A `truncate` (text-overflow: ellipsis) ancestor CLIPS its text: Chrome
    // hands back BOTH the full unclipped line box and the visible one, and the
    // unclipped box paints over neighbours that are nowhere near the glyphs
    // (measured: the long account <h1> returned 84->739 next to 84->240, which
    // read as h1-vs-h1, h1-vs-balance and h1-vs-Reconcile "overlaps" that the
    // eye cannot see). Clip every rect to its clipping ancestors — the painted
    // box — then dedupe the identical pair Chrome emitted.
    const clip = (el, r0) => {
      let x1 = r0.left, y1 = r0.top, x2 = r0.right, y2 = r0.bottom;
      let n = el;
      while (n && n !== document.documentElement) {
        const cs = getComputedStyle(n);
        if (cs.overflowX !== 'visible') { x1 = Math.max(x1, n.getBoundingClientRect().left); x2 = Math.min(x2, n.getBoundingClientRect().right); }
        if (cs.overflowY !== 'visible') { y1 = Math.max(y1, n.getBoundingClientRect().top); y2 = Math.min(y2, n.getBoundingClientRect().bottom); }
        n = n.parentElement;
      }
      return {left: x1, top: y1, right: x2, bottom: y2};
    };
    const range = document.createRange();
    range.selectNodeContents(n);
    for (const raw of range.getClientRects()) {
      if (raw.width < 1 || raw.height < 1) continue;
      const r = clip(p, raw);
      if (r.right - r.left < 1 || r.bottom - r.top < 1) continue;
      if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
      items.push({id: myId, t: text.slice(0, 30), l: r.left, r: r.right, top: r.top, b: r.bottom,
                  c: p.closest('[aria-hidden="true"]') ? 1 : 0,
                  d: p.tagName + '.' + String(p.className || '').slice(0, 22)});
    }
  }
  items.sort((a, b) => a.l - b.l);
  let overlaps = 0;
  const samples = [];
  // Chrome reports an ellipsized line as TWO rects (full run + truncated run)
  // from ONE text node — the same glyphs cannot print over themselves, so a
  // node never compares against itself.
  for (let i = 0; i < items.length; i++) {
    for (let j = i + 1; j < items.length && items[j].l < items[i].r; j++) {
      const a = items[i], b = items[j];
      if (a.c || b.c || a.id === b.id) continue;
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

# The panel's own geometry, normalized across the two shapes this probe has
# seen: `grid` (the fix) and `flex` (Mark's screenshot / the old markup).
MEASURE_JS = r"""(() => {
  const visible = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return false;
      n = n.parentElement;
    }
    return true;
  };
  const all = [...document.querySelectorAll('button[title="Accept transfer suggestion"]')];
  const accept = all.filter((b) => visible(b.parentElement.parentElement));
  const out = {found: accept.length > 0, inner: innerWidth, count: accept.length,
               dom_count: all.length, hidden_skipped: all.length - accept.length, suggestions: []};
  if (!accept.length) return out;

  const cellInfo = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    // Count VISUAL lines = distinct vertical bands. Chrome reports an
    // ellipsized (`truncate`) line as several rects — the full run, the ink
    // run, the ellipsis — all on the SAME baseline, while a real wrap puts
    // each line on its own top. Clipping/deduping by box (tried first) keeps
    // those same-line rects distinct and reports 3 lines for one truncated
    // label, so the top is what identifies a line.
    const lines = new Set();
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = w.nextNode())) {
      if (!node.textContent.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const raw of range.getClientRects()) {
        if (raw.width < 0.5 || raw.height < 0.5) continue;
        lines.add(Math.round(raw.top));
      }
    }
    return {l: Math.round(r.left * 10) / 10, r: Math.round(r.right * 10) / 10,
            t: Math.round(r.top * 10) / 10, b: Math.round(r.bottom * 10) / 10,
            lines: lines.size, ellipsized: el.scrollWidth > el.clientWidth + 1,
            text: (el.textContent || '').trim()};
  };
  const clip = (el, r0) => {
    let x1 = r0.left, y1 = r0.top, x2 = r0.right, y2 = r0.bottom;
    let n = el;
    while (n && n !== document.documentElement) {
      const cs = getComputedStyle(n);
      const b = n.getBoundingClientRect();
      if (cs.overflowX !== 'visible') { x1 = Math.max(x1, b.left); x2 = Math.min(x2, b.right); }
      if (cs.overflowY !== 'visible') { y1 = Math.max(y1, b.top); y2 = Math.min(y2, b.bottom); }
      n = n.parentElement;
    }
    return {left: x1, top: y1, right: x2, bottom: y2};
  };
  const rectsIn = (el) => {
    const res = [];
    const seen = new Set();
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = w.nextNode())) {
      const t = (node.textContent || '').trim();
      if (!t) continue;
      const p = node.parentElement;
      if (!p || p.closest('[aria-hidden="true"]')) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const raw of range.getClientRects()) {
        if (raw.width <= 0.5 || raw.height <= 0.5) continue;
        const rr = clip(p, raw);
        if (rr.right - rr.left < 1 || rr.bottom - rr.top < 1) continue;
        const key = t.slice(0, 24) + '|' + [rr.left, rr.top, rr.right, rr.bottom].map(Math.round).join(',');
        if (seen.has(key)) continue;
        seen.add(key);
        res.push({t: t.slice(0, 24), l: rr.left, r: rr.right, top: rr.top, b: rr.bottom});
      }
    }
    return res;
  };

  for (const btn of accept) {
    const card = btn.parentElement.parentElement;
    const content = card.children[1];
    const isGrid = /(^|\s)grid(\s|$)/.test(content.className) && content.className.includes('grid-cols-');
    let sides = [];
    if (isGrid) {
      // The component tags every cell (data-side / data-cell) — that is the
      // contract the regression test pins, so the probe reads roles, not
      // className position.
      const at = (row, cell) =>
        content.querySelector('[data-side="' + row + '"][data-cell="' + cell + '"]');
      const payeeOf = (row) => at(row, 'payee');
      sides = ['source', 'candidate'].map((row) => ({
        amount: cellInfo(at(row, 'amount')),
        date: cellInfo(at(row, 'date')),
        account: cellInfo(at(row, 'account')),
        payee: payeeOf(row) ? cellInfo(payeeOf(row)) : null,
        hasPayee: !!payeeOf(row),
      }));
    } else {
      // old markup: [row1, payee1, row2, payee2]; a row is
      // [amount, "on", date, (account)?]
      const rows = [...content.children].filter((c) => c.tagName === 'DIV' && c.className.includes('items-center'));
      sides = rows.slice(0, 2).map((row) => {
        const sp = [...row.children];
        return {amount: cellInfo(sp[0]), date: cellInfo(sp[2] || sp[1]), account: cellInfo(sp[3] || null)};
      });
    }
    const dash = [...card.querySelectorAll('*')].some(
      (el) => el.children.length === 0 && (el.textContent || '').trim() === '-');
    const textRects = rectsIn(card);
    const btnRects = [...btn.parentElement.children].map((b) => b.getBoundingClientRect());
    let overlap = null;
    for (const br of btnRects) {
      for (const tr of textRects) {
        const ox = Math.min(br.right, tr.right) - Math.max(br.left, tr.left);
        const oy = Math.min(br.bottom, tr.bottom) - Math.max(br.top, tr.top);
        if (ox > 1 && oy > 1) overlap = {ox: Math.round(ox), oy: Math.round(oy), text: tr.t};
      }
    }
    out.suggestions.push({
      shape: isGrid ? 'grid' : 'flex',
      sides,
      dash,
      overlap,
      overflow: card.scrollWidth - card.clientWidth > 1,
      card: {w: Math.round(card.getBoundingClientRect().width),
             sw: card.scrollWidth, cw: card.clientWidth},
      buttons: [...btn.parentElement.children].map((b) => {
        const r = b.getBoundingClientRect();
        return {t: Math.round(r.top), b: Math.round(r.bottom)};
      }),
      contentLines: [...content.children].map((c) => (c.textContent || '').trim().slice(0, 20)),
    });
  }
  return out;
})()"""


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}", flush=True)
    return bool(ok)


def settle(tab, extra: float = 1.4) -> None:
    try:
        tab.wait_for(
            "document.querySelector('[data-testid=\"route-fallback\"]') === null"
            " && document.querySelector('main') !== null",
            timeout=25,
        )
    except TimeoutError:
        pass
    tab.sleep(extra)


def seed(tab, stamp: int) -> dict:
    """Throwaway tenant + two unpaired opposite-amount pairs (all in-page)."""
    email = f"m110-transfer-probe-{stamp}@example.com"
    long_name = "CIBC Chequing (USD Foreign Currency Wrap Me)"
    return tab.evaluate(
        f"""(async () => {{
      const email = {json.dumps(email)}, password = {json.dumps(PASSWORD)};
      const h = {{'Content-Type': 'application/json'}};
      const reg = await fetch('/api/v1/auth/register',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const log = await fetch('/api/v1/auth/login',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const auth = {{...h, Authorization: 'Bearer ' + (await log.json()).access_token}};
      const mk = async (name, type) => (await fetch('/api/v1/accounts',
        {{method:'POST', headers:auth, body: JSON.stringify({{name, type, currency:'CAD'}})}})).json();
      const a = await mk({json.dumps(long_name)}, 'chequing');
      const b = await mk('Tangerine Savings', 'savings');
      const c = await mk('Aventura Visa', 'credit_card');
      const tx = async (o) => (await fetch('/api/v1/transactions',
        {{method:'POST', headers:auth, body: JSON.stringify(o)}})).json();
      // pair 1 — Mark's repro: "-" payee, long account name, +/- 85.00
      const t1 = await tx({{account_id: a.id, date:'2026-09-12', raw_description:'-',
        amount_native:'-85.00', currency:'CAD'}});
      const t2 = await tx({{account_id: b.id, date:'2026-09-13',
        raw_description:'INTERAC e-Transfer PRINCESS', amount_native:'85.00', currency:'CAD'}});
      // pair 2 — a wider amount, real payees, credit-card side
      const t3 = await tx({{account_id: a.id, date:'2026-09-20',
        raw_description:'PAYMENT - THANK YOU', amount_native:'-1234.56', currency:'CAD'}});
      const t4 = await tx({{account_id: c.id, date:'2026-09-21',
        raw_description:'CIBC VISA ONLINE PAYMENT', amount_native:'1234.56', currency:'CAD'}});
      const s = await (await fetch('/api/v1/transactions/transfer-suggestions?account_id=' + a.id,
        {{headers: auth}})).json();
      return {{email, register: reg.status, login: log.status,
              accounts: [a.id, b.id, c.id], txns: [t1.id, t2.id, t3.id, t4.id],
              suggestions: (s.items || []).length,
              payees: (s.items || []).map((i) => [i.source_payee, i.candidate_payee])}};
    }})()""",
        await_promise=True,
    )


def ui_login(tab, base: str, email: str) -> None:
    tab.navigate(f"{base}/login")
    settle(tab)
    tab.evaluate("document.querySelector('input[type=\"email\"]').focus()")
    tab.type_text(email)
    tab.evaluate("document.querySelector('input[type=\"password\"]').focus()")
    tab.type_text(PASSWORD)
    tab.press("Enter")
    tab.wait_for("location.pathname !== '/login'", timeout=25)


def load_axe(tab) -> None:
    # Injected as source (not <script src>): an https page may not load a
    # file:// script — m108_probe.py / axe_scan.py do the same.
    tab.evaluate(Path(AXE).read_text(encoding="utf-8"))


def axe_check(tab, name: str) -> dict:
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


def clip_shot(tab, path: str) -> str | None:
    """Screenshot just the panel (document-space clip), for before/after diffing."""
    import base64

    rect = tab.evaluate(
        """(() => {
             const b = [...document.querySelectorAll('button[title="Accept transfer suggestion"]')]
               .find((el) => el.getBoundingClientRect().width > 0);
             if (!b) return null;
             const panel = b.parentElement.parentElement.parentElement;
             const r = panel.getBoundingClientRect();
             return {x: Math.max(0, r.left + scrollX - 8), y: Math.max(0, r.top + scrollY - 8),
                     width: Math.min(innerWidth, r.width + 16), height: r.height + 16,
                     scale: 1};
           })()"""
    )
    if not rect or rect["height"] <= 0:
        return None
    res = tab.send("Page.captureScreenshot", {"format": "png", "clip": rect})
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(res["data"]))
    return path


def measure(tab) -> dict:
    return tab.evaluate(MEASURE_JS)


def column_checks(m: dict, width: int) -> None:
    tag = f"{width}px"
    check(f"panel found — {tag}", bool(m.get("found")),
          f"visible cards={m.get('count')} (dom={m.get('dom_count')}, hidden skipped={m.get('hidden_skipped')})")
    check(f"two visible suggestion pairs — {tag}", m.get("count") == 2, str(m.get("count")))
    for i, s in enumerate(m.get("suggestions", [])):
        p = f"pair {i + 1} @ {tag}"
        check(f"{p}: one grid for the pair", s["shape"] == "grid", s["shape"])
        src, cand = s["sides"][0], s["sides"][1]
        if not src or not cand or not src.get("amount") or not cand.get("amount"):
            check(f"{p}: both rows measurable", False, json.dumps(s["sides"]))
            continue
        for field in ("amount", "date", "account"):
            a, b = src.get(field), cand.get(field)
            if not a or not b:
                continue
            delta = abs(a["l"] - b["l"])
            check(f"{p}: {field} column aligned", delta <= 1.0,
                  f"src.l={a['l']} cand.l={b['l']} delta={delta}")
        for side_name, side in (("source", src), ("candidate", cand)):
            # money is never clipped: the amount cell must show in full
            check(f"{side_name} amount shown in full (never ellipsized)",
                  not side["amount"]["ellipsized"], side["amount"]["text"])
            for field in ("date", "account"):
                cell = side.get(field)
                if not cell:
                    continue
                check(f"{p}: {side_name} {field} on one line (no mid-label wrap)",
                      cell["lines"] <= 1, f"lines={cell['lines']} {cell['text']!r}")
        check(f"{p}: no stray '-' payee line", not s["dash"], "dash leaf present" if s["dash"] else "")
        check(f"{p}: buttons never print over text", s["overlap"] is None,
              json.dumps(s["overlap"]) if s["overlap"] else "")
        check(f"{p}: card does not overflow", not s["overflow"], json.dumps(s["card"]))


def main() -> int:
    global PASSWORD  # --password below overrides the per-run secret (skip-seed reuse)
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--widths", default="320,360,390,420")
    ap.add_argument("--label", default="after")
    ap.add_argument("--axe-width", type=int, default=390)
    ap.add_argument("--skip-seed", action="store_true",
                    help="reuse the tenant from a previous run's JSON (--email)")
    ap.add_argument("--email", default="")
    ap.add_argument("--account-id", type=int, default=0,
                    help="probe this account instead of the freshly seeded one")
    ap.add_argument("--password", default="",
                    help="tenant password for --skip-seed reuse (default: random per run)")
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="transfer_suggestions_probe.py")
    if args.password:
        PASSWORD = args.password
    widths = [int(w) for w in args.widths.split(",") if w.strip()]
    stamp = int(time.time())

    browser = Browser(width=1440, height=900)
    try:
        tab = browser.new_tab(args.base)
        tab.navigate(args.base)
        email = args.email
        account_id = None
        if not args.skip_seed:
            seeded = seed(tab, stamp)
            ok = (seeded["register"] in (200, 201) and seeded["login"] == 200
                  and seeded["suggestions"] == 2)
            check("seed tenant + 2 transfer suggestions", ok, json.dumps(seeded))
            email = seeded["email"]
            account_id = seeded["accounts"][0]
        else:
            seeded = {"email": email, "reused": True}
            if args.account_id:
                account_id = args.account_id

        tab.evaluate("try { localStorage.setItem('theme','light') } catch (e) {}")
        if not account_id:
            check("probe account resolved", False, "no account_id — pass --account-id with --skip-seed")
            raise SystemExit(2)
        url = f"{args.base}/accounts/{account_id}"
        tab.navigate(url)
        settle(tab)
        if tab.evaluate("location.pathname") == "/login":
            ui_login(tab, args.base, email)
            tab.navigate(url)
            settle(tab)

        # measurements per width (force_viewport pins the real viewport —
        # headless Chrome clamps --window-size below 500 CSS px)
        all_measure: list[dict] = []
        for width in widths:
            got = tab.force_viewport(width, 900)
            tab.navigate(url)
            settle(tab)
            inner = tab.evaluate("innerWidth")
            check(f"viewport {width} really applied", inner == width, f"innerWidth={inner} got={got}")
            m = measure(tab)
            column_checks(m, width)
            panel_top = tab.evaluate(
                """(() => {
                     const b = [...document.querySelectorAll('button[title="Accept transfer suggestion"]')]
                       .find((el) => el.getBoundingClientRect().width > 0);
                     if (!b) return null;
                     b.scrollIntoView({block:'center'});
                     return {y: Math.round(b.getBoundingClientRect().top)};
                   })()"""
            )
            tab.sleep(0.4)
            sweep = tab.evaluate(SWEEP_JS)
            check(f"text-node sweep 0 overlaps — main @{width}",
                  sweep.get("overlaps") == 0,
                  f"nodes={sweep.get('nodes')} overlaps={sweep.get('overlaps')} {sweep.get('samples')}")
            shot = str(HERE / f"transfer-suggestions-{args.label}-{width}.png")
            tab.screenshot(shot)
            panel_shot = clip_shot(tab, str(HERE / f"transfer-suggestions-{args.label}-{width}-panel.png"))
            m["sweep"] = sweep
            m["panel_top"] = panel_top
            m["shot"] = shot
            m["panel_shot"] = panel_shot
            all_measure.append(m)

        # axe at the requested width
        got = tab.force_viewport(args.axe_width, 900)
        tab.navigate(url)
        settle(tab)
        check(f"viewport {args.axe_width} really applied",
              tab.evaluate("innerWidth") == args.axe_width, f"got={got}")
        load_axe(tab)
        axe_result = axe_check(tab, f"/accounts/<probe> {args.axe_width}px")

        errors = tab.console_errors()
        check("no console errors", len(errors) == 0, "; ".join(errors[:3]))

        evidence = {
            "task": "t_06c510ef",
            "label": args.label,
            "base": args.base,
            "widths": widths,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "email": email,
            "account_id": account_id,
            "seed": seeded,
            "axe": axe_result,
            "measurements": all_measure,
            "screenshots": [str(HERE / f"transfer-suggestions-{args.label}-{w}.png") for w in widths],
            "panel_screenshots": [m.get("panel_shot") for m in all_measure],
            "checks": CHECKS,
        }
        out = HERE / f"transfer-suggestions-probe-{args.label}.json"
        out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(f"evidence: {out}")
    finally:
        browser.close()

    failed = [c for c in CHECKS if not c["ok"]]
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""M126 — live positions ticker, per-holding price history, chart ranges — LIVE.

    python3 m126_price_range_probe.py [--base https://fintrac.princessandmark.com]
                                     [--width 390] [--height 844]

AGENTS §4 step 5. Two tenants, because the two halves need different things:

A. A THROWAWAY tenant — the only place a price can be moved ON DEMAND:

  1  seed    register → account → security → committed trade (so the row is a
             HELD position), a second security on a bogus ticker whose
             backfill finds nothing (the honest "no history" case), and the
             first security's 365-day backfill (the sparkline's 30 closes)
  2  stream  /investments reaches "Stream connected"
  3  nofetch  ZERO /prices/history requests have happened since the SPA booted
             — the sparkline comes with the positions payload
  4  tick    POST /prices (manual upsert — manual wins) while a 4s delay is
             armed on the positions refetch: within 12s the row's price cell
             shows the NEW figure and carries `data-live-tick`
  5  note    and the totals footnote is on screen — "totals update on next
             refresh" — while that server payload is still in flight
  6  totals  disarm the delay → the footnote disappears when the server's own
             payload lands (the marker rides the cached object, so a refetch
             drops it; no timer anywhere)
  7  expand  one click → the inline chart, and EXACTLY ONE /prices/history
             request carrying the documented 1M `from`
  8  cache   collapse → re-expand adds NO request (keyed by range)
  9  empty   the bogus row says "No price history yet" and draws no chart
  10 ranges  seven aria-pressed options, 1M selected, Tab walks all seven
  11 range5Y clicking 5Y re-fetches with granularity=month and a wider `from`
  12 m118    the investment account's value timeline carries its own range
             control, defaulting to All
  13 axe     0 critical + 0 serious on /investments (row expanded), both themes

B. The DEMO tenant — the only place RICH charts already exist (DOM-only, no
   API token: the SPA keeps its tokens in memory, so these checks read links
   and aria-labels, never the API):

  14 dash    the Dashboard net-worth chart exists; picking the page's 1W range
             makes its aria-label report >= 2 points (the pre-M126 hard-coded
             `month` gave a single bucket — an axis with one label)
  15 m118    an investment account's value timeline offers the seven ranges
  16 bill    a bill detail's period chart carries its range control, All first
  17 axe     console clean on the demo surfaces

Exit 0 only if every check passes; JSON + PNG evidence land next to this script.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from a11y_driver import Browser  # noqa: E402
from axe_scan import load_axe, run_axe, set_theme, summarize  # noqa: E402
from probe_target import random_password

CHECKS: list[dict] = []

PASSWORD = random_password("m126")
SYMBOL = "MSFT"
BOGUS = "NODATA42M126"
TICK_PRICE = "123.45"

# Installed with Page.addScriptToEvaluateOnNewDocument, so it runs BEFORE the
# SPA on every subsequent document — a render-time request cannot slip past
# it, and `window.__m126` is reset per page load (every check below lives
# inside one load).
INSTALL_SCRIPT = """
window.__m126 = { history: [], positions: 0, delayPositions: false };
const __origFetch = window.fetch.bind(window);
window.fetch = function (...args) {
  let url = '';
  try { url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || ''; }
  catch (e) {}
  const isHistory = url.includes('/prices/history');
  const isPositions = url.includes('/portfolio/positions');
  if (isHistory) window.__m126.history.push(url);
  if (isPositions) window.__m126.positions += 1;
  // The delay is what makes the totals footnote OBSERVABLE: without it the
  // refetch the same tick schedules would land in ~300ms and the marker
  // would be gone between two polls. This is a slow network, exactly the
  // window the footnote exists for.
  const wait = (isPositions && window.__m126.delayPositions)
    ? new Promise(r => setTimeout(r, 4000))
    : Promise.resolve();
  return wait.then(() => __origFetch(...args));
};
"""

READ_COUNTER = """(() => {
  const s = window.__m126 || { history: [], positions: 0, delayPositions: false };
  return { history: s.history.slice(), positions: s.positions,
           delay: !!s.delayPositions };
})()"""


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}", flush=True)
    return bool(ok)


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


def install_counter(tab) -> str:
    try:
        tab.send("Page.addScriptToEvaluateOnNewDocument", {"source": INSTALL_SCRIPT})
        return "new-document"
    except Exception as exc:  # noqa: BLE001 — fall back to the current document
        tab.evaluate(INSTALL_SCRIPT)
        return f"evaluate ({exc.__class__.__name__})"


def arm_delay(tab, on: bool) -> None:
    tab.evaluate(f"window.__m126 && (window.__m126.delayPositions = {'true' if on else 'false'});")


def history_requests(tab) -> list[str]:
    return list(tab.evaluate(READ_COUNTER)["history"])


# --- tenant A: seed ---------------------------------------------------------
def seed(tab, stamp: int) -> dict:
    email = f"m126-probe-{stamp}@example.com"
    return tab.evaluate(
        f"""(async () => {{
      const email = {json.dumps(email)}, password = {json.dumps(PASSWORD)};
      const h = {{'Content-Type': 'application/json'}};
      await fetch('/api/v1/auth/register',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const log = await fetch('/api/v1/auth/login',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const auth = {{...h, Authorization: 'Bearer ' + (await log.json()).access_token}};

      const acct = await fetch('/api/v1/accounts',
        {{method:'POST', headers:auth, body: JSON.stringify({{
          name:'M126 probe', type:'tfsa', currency:'CAD'}})}});
      const account_id = (await acct.json()).id;

      const sec = await fetch('/api/v1/securities',
        {{method:'POST', headers:auth, body: JSON.stringify({{
          symbol:{json.dumps(SYMBOL)}, name:'Microsoft', kind:'stock',
          listing_currency:'USD', exchange:'NMS'}})}});
      const sec_json = await sec.json();

      // The honest "no history" row: a ticker no data source knows, so its
      // backfill finds nothing and the expansion must say so, not draw a line.
      const dead = await fetch('/api/v1/securities',
        {{method:'POST', headers:auth, body: JSON.stringify({{
          symbol:{json.dumps(BOGUS)}, name:'No data co', kind:'stock',
          listing_currency:'USD', exchange:'NMS'}})}});
      const dead_json = await dead.json();

      const today = new Date().toISOString().slice(0, 10);
      for (const security_id of [sec_json.id, dead_json.id]) {{
        await fetch('/api/v1/trades',
          {{method:'POST', headers:auth, body: JSON.stringify({{
            account_id, security_id, action:'buy', date: today,
            quantity:'1', price_native:'100'}})}});
      }}

      // Wait for the 365-day backfill: the sparkline needs its 30 closes and
      // the chart needs a real window.
      let total = 0, source = 'backfill';
      for (let i = 0; i < 45; i++) {{
        const list = await (await fetch(
          `/api/v1/prices?security_id=${{sec_json.id}}&limit=200`, {{headers:auth}})).json();
        total = list.total ?? (list.items || []).length;
        if (total >= 30) break;
        await new Promise(r => setTimeout(r, 1000));
      }}
      // Fallback: if the market-data seam cannot reach yfinance from this
      // host, hand the security its history through the MANUAL price path
      // (manual wins, docs/07) so every downstream check measures the CHART
      // behaviour rather than the network. Reported as `source` either way —
      // the probe never claims a real backfill it did not get.
      if (total < 30) {{
        source = 'manual-fallback';
        const day = new Date();
        for (let i = 1; i <= 60 && total < 30; i++) {{
          const d = new Date(day.getTime() - i * 86400000);
          if (d.getDay() === 0 || d.getDay() === 6) continue;
          await fetch('/api/v1/prices', {{method: 'POST', headers: auth,
            body: JSON.stringify({{security_id: sec_json.id,
              date: d.toISOString().slice(0, 10),
              price: String(100 + (i % 40))}})}});
        }}
        const list2 = await (await fetch(
          `/api/v1/prices?security_id=${{sec_json.id}}&limit=200`, {{headers:auth}})).json();
        total = list2.total ?? (list2.items || []).length;
      }}
      const dead_prices = await (await fetch(
        `/api/v1/prices?security_id=${{dead_json.id}}&limit=5`, {{headers:auth}})).json();

      return {{
        email, auth, account_id, security_id: sec_json.id, dead_id: dead_json.id,
        today, sec_status: sec.status, price_total: total, price_source: source,
        dead_prices: (dead_prices.items || []).length,
      }};
    }})()""",
        await_promise=True,
    )


def ui_login(tab, base: str, email: str) -> None:
    """Sign in THROUGH THE FORM — tokens live in memory only (docs/18)."""
    if not tab.evaluate("location.pathname === '/login'"):
        tab.navigate(f"{base}/login")
        settle(tab)
    tab.evaluate("""document.querySelector('input[type="email"]').focus()""")
    tab.type_text(email)
    tab.evaluate("""document.querySelector('input[type="password"]').focus()""")
    tab.type_text(PASSWORD)
    tab.press("Enter")
    tab.wait_for("location.pathname !== '/login'", timeout=25)


def indicator_state(tab) -> dict:
    return tab.evaluate(
        """(() => {
          const node = Array.from(document.querySelectorAll('[title]'))
            .find(n => /stream message/i.test(n.getAttribute('title') || ''));
          if (!node) return {label: null, title: null, found: false};
          const span = node.querySelector('span');
          return {found: true, label: (span && span.textContent || '').trim(),
                  title: node.getAttribute('title')};
        })()"""
    )


# --- tenant A: the positions row -------------------------------------------
ROW_JS = """((symbol) => {
  // /investments renders THREE tables (positions, watchlist, trades log) —
  // only the positions one has a price cell and a sparkline, so scope to it.
  const body = document.querySelector('[aria-label="Positions table"] tbody');
  const rows = (body ? Array.from(body.querySelectorAll('tr')) : [])
    .filter(r => r.querySelectorAll('td').length > 1);
  const row = rows.find(r => (r.querySelector('a')?.textContent || '').trim() === symbol);
  if (!row) return null;
  const cells = Array.from(row.querySelectorAll('td')).map(c => c.textContent.trim());
  return {
    cells,
    price: cells[4],   // Account, Symbol, Qty, Book, Price, Trend, Source, MV, MV(CAD)
    market_value_cad: cells[cells.length - 1],
    live: !!row.querySelector('[data-live-tick="true"]'),
    spark_points: (row.querySelector('polyline')?.getAttribute('points') || '')
      .trim().split(/\\s+/).filter(Boolean).length,
  };
})"""


def row_state(tab, symbol: str = SYMBOL) -> dict | None:
    return tab.evaluate(f"{ROW_JS}({json.dumps(symbol)})")


def find_row(tab, symbol: str, timeout: float, poll: float = 0.4) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = row_state(tab, symbol)
        if state:
            return state
        tab.sleep(poll)
    return None


def click_label(tab, label: str) -> bool:
    """Click the expand/collapse chevron by its exact accessible name."""
    return bool(
        tab.evaluate(
            f"""(() => {{
                 const b = Array.from(document.querySelectorAll('button'))
                   .find(n => (n.getAttribute('aria-label') || '') === {json.dumps(label)});
                 if (b) b.click();
                 return !!b;
             }})()"""
        )
    )


def panel(tab) -> dict:
    return tab.evaluate(
        """(() => {
          const p = document.querySelector('[data-testid="position-history-row"]');
          if (!p) return {present: false};
          const chart = p.querySelector('[role="img"]');
          const group = p.querySelector('[role="group"]');
          const buttons = group ? Array.from(group.querySelectorAll('button')) : [];
          const text = p.textContent || '';
          return {
            present: true,
            error: /unavailable|failed|Error/i.test(text) && !chart,
            chart_label: chart ? chart.getAttribute('aria-label') : null,
            group_label: group ? group.getAttribute('aria-label') : null,
            options: buttons.map(b => b.textContent.trim()),
            pressed: buttons.map(b => b.getAttribute('aria-pressed')),
            empty: /No price history yet/i.test(text),
          };
        })()"""
    )


def panel_done(shown: dict) -> bool:
    """Settled = the chart is drawn, the honest empty message is up, or the
    request failed. `present` alone is true while the skeleton is still up."""
    if not shown.get("present"):
        return False
    if shown.get("chart_label") or shown.get("empty"):
        return True
    return bool(shown.get("error"))


def wait_panel(tab, timeout: float = 12) -> dict:
    deadline = time.time() + timeout
    shown = panel(tab)
    while not panel_done(shown) and time.time() < deadline:
        tab.sleep(0.3)
        shown = panel(tab)
    return shown


def live_note(tab) -> bool:
    return bool(tab.evaluate("!!document.querySelector('[data-testid=\"positions-live-note\"]')"))


# --- tenant B: the demo (DOM only) -----------------------------------------
def demo_login(tab) -> bool:
    return bool(
        tab.evaluate(
            """(() => {
                 const b = Array.from(document.querySelectorAll('button'))
                   .find(n => /Explore the demo/i.test(n.textContent || ''));
                 if (b) b.click();
                 return !!b;
               })()"""
        )
    )


def chart_label(tab, contains: str) -> str | None:
    return tab.evaluate(
        f"""(() => {{
          const node = Array.from(document.querySelectorAll('[role="img"]'))
            .find(n => (n.getAttribute('aria-label') || '').includes({json.dumps(contains)}));
          return node ? node.getAttribute('aria-label') : null;
        }})()"""
    )


def wait_chart_label(tab, contains: str, timeout: float = 10) -> str | None:
    deadline = time.time() + timeout
    label = None
    while time.time() < deadline:
        label = chart_label(tab, contains)
        if label:
            return label
        tab.sleep(0.4)
    return label


def points_of(label: str | None) -> int | None:
    if not label:
        return None
    m = re.search(r"(\d+) points", label)
    return int(m.group(1)) if m else None


def pick_page_range(tab, label: str) -> bool:
    return bool(
        tab.evaluate(
            f"""(() => {{
              const strip = Array.from(document.querySelectorAll('[role="tablist"]'))
                .find(n => (n.getAttribute('aria-label') || '') === 'Date range presets');
              if (!strip) return false;
              const b = Array.from(strip.querySelectorAll('button'))
                .find(n => (n.textContent || '').trim() === {json.dumps(label)});
              if (!b) return false;
              b.click();
              return true;
            }})()"""
        )
    )


def scroll_range_control_into_view(tab) -> bool:
    """Centre the chart whose range control we just asserted — the shot is the
    evidence, and a viewport shot of the card ABOVE it is not."""
    return bool(
        tab.evaluate(
            """(() => {
                 const g = Array.from(document.querySelectorAll('[role="group"]'))
                   .find(n => /range/i.test(n.getAttribute('aria-label') || ''));
                 if (!g) return false;
                 g.scrollIntoView({block: 'center'});
                 return true;
               })()"""
        )
    )


def chart_range_control(tab) -> dict | None:
    return tab.evaluate(
        """(() => {
             const g = Array.from(document.querySelectorAll('[role="group"]'))
               .find(n => /range/i.test(n.getAttribute('aria-label') || ''));
             if (!g) return null;
             const b = Array.from(g.querySelectorAll('button'));
             return {label: g.getAttribute('aria-label'),
                     options: b.map(x => x.textContent.trim()),
                     pressed: b.map(x => x.getAttribute('aria-pressed'))};
           })()"""
    )


def entity_links(tab, href_prefix: str) -> list[str]:
    """Every `/prefix/<id>` link on the page — filtered to real ids so a
    "new" create-link is never opened as if it were an entity."""
    raw = tab.evaluate(
        f"""(() => Array.from(
          document.querySelectorAll('a[href^={json.dumps(href_prefix)}]')
        ).map(a => a.getAttribute('href')))()"""
    )
    wanted = re.compile(rf"^{re.escape(href_prefix)}\d+$")
    return [href for href in raw or [] if wanted.match(href or "")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument("--out", default=str(HERE / "m126-price-range-probe.json"))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m126_price_range_probe.py")
    base = args.base.rstrip("/")
    stamp = int(time.time())
    report: dict = {
        "base": base,
        "width": args.width,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checks": CHECKS,
        "axe": {},
        "shots": [],
        "history_requests": {},
    }

    browser = Browser(width=args.width, height=args.height)
    try:
        tab = browser.new_tab(f"{base}/login")
        tab.enable_runtime()
        tab.navigate(f"{base}/login", timeout=45)
        settle(tab, 1.0)
        tab.force_viewport(args.width, args.height)
        how = install_counter(tab)
        check("instrument: fetch counter armed before the SPA boots", True, how)

        seeded: dict = seed(tab, stamp)
        auth_js = seeded["auth"]
        sid = seeded["security_id"]

        check("seed: held security created", seeded["sec_status"] in (200, 201) and bool(sid),
              f"status={seeded['sec_status']} id={sid}")
        check(f"seed: {seeded['price_total']} cached closes via {seeded['price_source']} "
              "(sparkline + chart need >= 30)",
              seeded["price_total"] >= 30,
              f"total={seeded['price_total']} source={seeded['price_source']}")
        check("seed: the bogus-ticker row has NO history (the honest-empty case)",
              seeded["dead_prices"] == 0, f"prices={seeded['dead_prices']}")

        ui_login(tab, base, seeded["email"])
        settle(tab, 3.0)
        tab.navigate(f"{base}/investments")
        settle(tab, 3.0)

        first, ts0 = {}, time.time()
        while time.time() - ts0 < 20:
            first = indicator_state(tab)
            if first.get("title") and "No stream message" not in (first.get("title") or ""):
                break
            tab.sleep(0.5)
        check("stream: the SSE stream is live on /investments (a message arrived)",
              bool(first.get("title")) and "No stream message" not in (first.get("title") or ""),
              json.dumps(first))

        before = history_requests(tab)
        check("history: ZERO /prices/history requests on render (sparkline is free)",
              len(before) == 0, f"{len(before)} requests since the last document")

        row = find_row(tab, SYMBOL, 8)
        check("row: the held position rendered", row is not None, json.dumps(row))
        check("row: sparkline plots ~30 closes straight from the positions payload",
              bool(row) and row["spark_points"] >= 29,
              f"points={row['spark_points'] if row else None}")

        # --- the tick ------------------------------------------------------
        arm_delay(tab, True)
        posted = tab.evaluate(
            f"""(async () => {{
              const r = await fetch('/api/v1/prices', {{
                method: 'POST', headers: {json.dumps(auth_js)},
                body: JSON.stringify({{
                  security_id: {sid}, date: {json.dumps(seeded['today'])},
                  price: '{TICK_PRICE}'}})}});
              return JSON.stringify({{status: r.status, body: (await r.text()).slice(0, 200)}});
            }})()""",
            await_promise=True,
        )
        posted_status = json.loads(str(posted)).get("status")
        check("tick: manual upsert accepted (manual wins over the backfill)",
              posted_status in (200, 201), str(posted))

        ticked, t0 = None, time.time()
        while time.time() - t0 < 12:
            state = row_state(tab, SYMBOL)
            if state and TICK_PRICE in (state["price"] or "") and state["live"]:
                ticked = state
                break
            tab.sleep(0.3)
        last = row_state(tab, SYMBOL) or {}
        check("tick: the price cell shows the new figure AND carries data-live-tick",
              ticked is not None,
              f"price={last.get('price')} live={last.get('live')} "
              f"after={round(time.time() - t0, 1)}s")

        note_seen, t1 = None, time.time()
        while time.time() - t1 < 6:
            if live_note(tab):
                note_seen = True
                break
            tab.sleep(0.2)
        check("note: 'totals update on next refresh' is on screen while the server "
              "payload is still in flight", note_seen is True, str(note_seen))

        after_tick = indicator_state(tab)
        check("stream: the SSE chip still reports CONNECTION state after the tick "
              "(never the patched price, never Offline)",
              after_tick.get("label") in ("Stream connected", "prices stale")
              and "No stream message" not in (after_tick.get("title") or ""),
              json.dumps(after_tick))

        arm_delay(tab, False)  # the server payload may land now
        cleared, t2 = False, time.time()
        while time.time() - t2 < 65:
            if not live_note(tab):
                cleared = True
                break
            tab.sleep(1.0)
        check("totals: the footnote disappears when the server's own payload lands "
              "(the marker rides the cached object — no timer)",
              cleared, f"cleared after {round(time.time() - t2, 1)}s")

        # --- expansion -----------------------------------------------------
        check("expand: the chevron exposes Show <symbol> price history",
              click_label(tab, f"Show {SYMBOL} price history"))
        shown = wait_panel(tab)
        check("expand: the row opens an inline panel (no navigation)",
              shown.get("present") is True, json.dumps(shown)[:220])

        reqs = history_requests(tab)
        check("expand: EXACTLY ONE /prices/history request", len(reqs) == 1,
              f"{len(reqs)} {reqs[:2]}")
        if reqs:
            query = reqs[0].split("?")[-1]
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            report["history_requests"]["first"] = reqs[0]
            check("expand: the request carries security_id + a 1M `from` + granularity=day",
                  params.get("security_id") == str(sid) and bool(params.get("from"))
                  and params.get("granularity") == "day",
                  json.dumps(params))

        check("expand: the real price-history chart renders inline",
              bool(shown.get("chart_label"))
              and "price history" in (shown.get("chart_label") or ""),
              str(shown.get("chart_label"))[:160])

        opts = shown.get("options") or []
        pressed = shown.get("pressed") or []
        check("ranges: seven options, exactly one pressed, 1M is the default",
              opts == ["1M", "3M", "6M", "YTD", "1Y", "5Y", "All"]
              and pressed.count("true") == 1 and pressed[0] == "true",
              f"{opts} {pressed}")
        check("ranges: the group is labelled for assistive tech",
              bool(shown.get("group_label")) and "range" in (shown.get("group_label") or ""),
              str(shown.get("group_label")))

        # docs/09 §Range control: at 390/412 the seven labels WRAP — the group
        # must never become its own horizontal scroller (a control you have to
        # scroll sideways to reach is not reachable).
        fit = tab.evaluate(
            """(() => {
                 const g = document.querySelector(
                   '[data-testid="position-history-row"] [role="group"]');
                 const region = document.querySelector(
                   '[aria-label="Positions table"]');
                 if (!g || !region) return null;
                 const gr = g.getBoundingClientRect();
                 const rr = region.getBoundingClientRect();
                 return {
                   scrollW: g.scrollWidth, clientW: g.clientWidth,
                   groupRight: Math.round(gr.right),
                   regionRight: Math.round(rr.right),
                   width: Math.round(gr.width),
                   // The group must live INSIDE the region's visible box: the
                   // table scrolls sideways (M64), so a group whose right edge
                   // passes the region's is reachable only by scrolling the
                   // table — measured once at 390 before the panel cap.
                   inside: gr.right <= rr.right + 1,
                 };
               })()"""
        )
        check("ranges: the control fits — it wraps, does not scroll sideways, "
              "and sits inside the scroll region's visible box",
              bool(fit) and fit["scrollW"] <= fit["clientW"] + 1 and fit["inside"],
              json.dumps(fit))

        # Keyboard: focus the first option, then Tab must walk the rest.
        tab.evaluate(
            """(() => {
                 const g = document.querySelector('[data-testid="position-history-row"] [role="group"]');
                 const b = g && g.querySelector('button');
                 if (b) b.focus();
                 return document.activeElement === b;
               })()"""
        )
        walked = []
        for _ in range(6):
            tab.press("Tab")
            walked.append(
                tab.evaluate(
                    """(() => {
                         const a = document.activeElement;
                         const g = document.querySelector(
                           '[data-testid="position-history-row"] [role="group"]');
                         if (!a || !g) return null;
                         return g.contains(a) ? (a.textContent || '').trim() : 'left-group';
                       })()"""
                )
            )
        check("ranges: Tab walks 1M → 3M → … → All (keyboard-reachable, focus never lost)",
              walked[0] == "3M" and "left-group" not in walked and walked[-1] == "All",
              json.dumps(walked))

        # Collapse → re-expand must not refetch (cached by the range key).
        click_label(tab, f"Hide {SYMBOL} price history")
        tab.sleep(1.0)
        check("cache: collapsing closes the panel",
              panel(tab).get("present") is False, json.dumps(panel(tab))[:120])
        click_label(tab, f"Show {SYMBOL} price history")
        wait_panel(tab)
        check("cache: collapse → re-expand adds NO request",
              len(history_requests(tab)) == 1, f"{len(history_requests(tab))} requests")

        # Range change → a second, wider request.
        tab.evaluate(
            """(() => {
                 const g = document.querySelector('[data-testid="position-history-row"] [role="group"]');
                 const b = g && Array.from(g.querySelectorAll('button'))
                   .find(n => (n.textContent || '').trim() === '5Y');
                 if (b) b.click();
                 return !!b;
               })()"""
        )
        t4 = time.time()
        while len(history_requests(tab)) < 2 and time.time() - t4 < 8:
            tab.sleep(0.3)
        reqs2 = history_requests(tab)
        check("range5Y: choosing 5Y fetches again with granularity=month",
              len(reqs2) == 2 and "granularity=month" in reqs2[1],
              json.dumps(reqs2[1:2]))

        # The honest empty row. Collapse the open MSFT panel FIRST: two rows
        # can be expanded at once (that is the UI), and `panel()` reads the
        # FIRST [data-testid="position-history-row"] — which would still be
        # MSFT's chart.
        click_label(tab, f"Hide {SYMBOL} price history")
        tab.sleep(0.6)
        click_label(tab, f"Show {BOGUS} price history")
        dead_panel = wait_panel(tab)
        check("empty: a security with no history says 'No price history yet' and draws "
              "no chart",
              dead_panel.get("present") is True and dead_panel.get("empty") is True
              and dead_panel.get("chart_label") is None,
              json.dumps({k: dead_panel.get(k) for k in ("present", "empty", "chart_label")}))

        # M118: the investment account's own timeline carries the range control.
        # The chart skeleton has no header yet, so poll for the settled page.
        tab.navigate(f"{base}/accounts/{seeded['account_id']}")
        settle(tab, 3.0)
        m118, tm = None, time.time()
        while time.time() - tm < 15:
            m118 = chart_range_control(tab)
            if m118:
                break
            tab.sleep(0.5)
        report["m118_page"] = tab.evaluate(
            """(() => ({
                 path: location.pathname,
                 groups: Array.from(document.querySelectorAll('[role="group"]'))
                   .map(n => n.getAttribute('aria-label')),
                 charts: Array.from(document.querySelectorAll('[role="img"]'))
                   .map(n => (n.getAttribute('aria-label') || '').slice(0, 60)),
                 h1: (document.querySelector('h1') || {}).textContent || null,
               }))()"""
        )
        check("m118: the account value timeline has its own range control, default All",
              bool(m118) and m118["options"] == ["1M", "3M", "6M", "YTD", "1Y", "5Y", "All"]
              and m118["pressed"][6] == "true",
              json.dumps(m118) + " page=" + json.dumps(report["m118_page"])[:300])

        # axe + screenshots on the surface the acceptance names.
        tab.navigate(f"{base}/investments")
        settle(tab, 3.0)
        # The positions query can still be resolving after settle() (it
        # schedules its own market-hours refresh), so RETRY the expansion
        # instead of assuming the chevron is there — a silent miss here is why
        # an early evidence run photographed a collapsed table.
        opened, t_open = False, time.time()
        while time.time() - t_open < 25:
            if click_label(tab, f"Show {SYMBOL} price history") and panel_done(
                wait_panel(tab, 6)
            ):
                opened = True
                break
            tab.sleep(0.6)
        probe_panel = wait_panel(tab, 2)
        check("axe setup: the row is expanded (panel settled) for the scan and "
              "the screenshots", opened,
              json.dumps({k: probe_panel.get(k)
                          for k in ("present", "chart_label", "empty")})[:200])
        tab.evaluate(
            """(() => {
                 const p = document.querySelector('[data-testid="position-history-row"]');
                 if (p) p.scrollIntoView({block: 'center'});
                 return !!p;
               })()"""
        )
        tab.sleep(0.6)
        load_axe(tab)
        panel_open: list[bool] = []
        for theme in ("dark", "light"):
            set_theme(tab, theme)
            tab.sleep(0.8)
            result = run_axe(tab)
            report["axe"][f"investments-{theme}"] = summarize(result["violations"])
            c = report["axe"][f"investments-{theme}"]["counts"]
            print(f"axe investments {theme:5s} critical={c['critical']} "
                  f"serious={c['serious']} moderate={c['moderate']} minor={c['minor']}",
                  flush=True)
            # axe scrolls the document while it scans, so re-centre the panel
            # AFTER it — otherwise the shot captures wherever the scan left the
            # viewport (measured: the table's first three columns, no panel).
            panel_open.append(
                bool(
                    tab.evaluate(
                        """(() => {
                             const p = document.querySelector(
                               '[data-testid="position-history-row"]');
                             if (p) p.scrollIntoView({block: 'center'});
                             return !!p;
                           })()"""
                    )
                )
            )
            tab.sleep(0.5)
            shot = str(HERE / f"m126-investments-{args.width}-{theme}.png")
            tab.screenshot(shot)
            report["shots"].append(shot)
        report["shot_panel_open"] = panel_open
        check("shots: the expanded panel was open (and centred) for every "
              "screenshot", all(panel_open), json.dumps(panel_open))

        bad = sum(v["counts"]["critical"] + v["counts"]["serious"]
                  for v in report["axe"].values())
        check("axe: 0 critical + 0 serious on /investments (row expanded), both themes",
              bad == 0, str(bad))
        report["console_a"] = tab.console_errors()
        check("console: no errors on /investments", not report["console_a"],
              json.dumps(report["console_a"])[:400])

        # --- tenant B: the demo, for charts that need real data ------------
        tab.navigate(f"{base}/login")
        settle(tab, 1.5)
        check("demo: the Explore-the-demo door is on the login page", demo_login(tab))
        settle(tab, 3.0)
        check("demo: the SPA landed on a signed-in page",
              tab.evaluate("location.pathname") != "/login",
              str(tab.evaluate("location.pathname")))

        tab.navigate(f"{base}/dashboard")
        settle(tab, 3.0)
        before_label = wait_chart_label(tab, "Net worth")
        check("dashboard: the net-worth chart renders", bool(before_label),
              str(before_label)[:170])

        check("dashboard: the page's 1W range is pickable", pick_page_range(tab, "1W"))
        after_label, t6 = before_label, time.time()
        while time.time() - t6 < 8:
            after_label = chart_label(tab, "Net worth")
            if after_label and after_label != before_label:
                break
            tab.sleep(0.4)
        pts = points_of(after_label)
        check("dashboard: a 1W range renders >= 2 daily buckets (the pre-M126 "
              "hard-coded `month` gave exactly one)",
              bool(after_label) and after_label != before_label and (pts or 0) >= 2,
              f"points={pts} label={str(after_label)[:150]}")

        # The acceptance's "sparklines filled for ALL TFSA holdings" — measured
        # on the DEMO tenant, whose portfolio is the rich one: every rendered
        # positions row must carry a real series (>= 2 closes), not a dash.
        tab.navigate(f"{base}/investments")
        settle(tab, 3.0)
        holdings, th = None, time.time()
        while time.time() - th < 12:
            holdings = tab.evaluate(
                """(() => {
                     const body = document.querySelector(
                       '[aria-label="Positions table"] tbody');
                     const rows = (body ? Array.from(body.querySelectorAll('tr')) : [])
                       .filter(r => r.querySelectorAll('td').length > 1);
                     const points = rows.map(r =>
                       ((r.querySelector('polyline')?.getAttribute('points') || '')
                         .trim().split(/\\s+/).filter(Boolean).length));
                     return {
                       rows: rows.length,
                       withSpark: points.filter(n => n >= 2).length,
                       minPoints: points.length ? Math.min(...points) : 0,
                       symbols: rows.map(r =>
                         (r.querySelector('a')?.textContent || '').trim()).slice(0, 8),
                     };
                   })()"""
            )
            if holdings and holdings["rows"] > 0:
                break
            tab.sleep(0.6)
        report["demo_positions"] = holdings
        check("demo: EVERY positions row on the live portfolio carries a "
              "sparkline built from its own closes",
              bool(holdings) and holdings["rows"] > 0
              and holdings["withSpark"] == holdings["rows"],
              json.dumps(holdings))
        shot = str(HERE / f"m126-portfolio-{args.width}-dark.png")
        set_theme(tab, "dark")
        tab.sleep(0.6)
        tab.screenshot(shot)
        report["shots"].append(shot)

        # An INVESTMENT account is the one whose timeline carries the M118
        # control (a bank account's view keeps its richer RangePicker with
        # free-form dates — docs/12 §Range → granularity pins both).
        tab.navigate(f"{base}/accounts")
        settle(tab, 2.5)
        account_hrefs = entity_links(tab, "/accounts/")
        check("demo: accounts to open", bool(account_hrefs), json.dumps(account_hrefs))
        demo_m118 = None
        for href in account_hrefs[:4]:
            tab.navigate(f"{base}{href}")
            settle(tab, 3.0)
            demo_m118 = chart_range_control(tab)
            if demo_m118:
                report["m118_account"] = href
                break
        check("demo: an investment account's value timeline offers the seven ranges",
              bool(demo_m118) and len(demo_m118["options"]) == 7
              and demo_m118["pressed"][6] == "true",
              json.dumps(demo_m118))
        if demo_m118:
            scroll_range_control_into_view(tab)
            set_theme(tab, "dark")
            tab.sleep(0.6)
            shot = str(HERE / f"m126-account-{args.width}-dark.png")
            tab.screenshot(shot)
            report["shots"].append(shot)

        tab.navigate(f"{base}/bills")
        settle(tab, 2.5)
        bill_hrefs = entity_links(tab, "/bills/")
        bill_href = bill_hrefs[0] if bill_hrefs else None
        check("demo: a bill to open", bool(bill_href), json.dumps(bill_hrefs))
        if bill_href:
            tab.navigate(f"{base}{bill_href}")
            settle(tab, 3.0)
            bill_group = chart_range_control(tab)
            check("demo: the bill period chart carries the range control, All first",
                  bool(bill_group) and bill_group["options"] == ["1M", "3M", "6M", "YTD",
                                                                 "1Y", "5Y", "All"]
                  and bill_group["pressed"][6] == "true",
                  json.dumps(bill_group))
            scroll_range_control_into_view(tab)
            set_theme(tab, "dark")
            tab.sleep(0.6)
            shot = str(HERE / f"m126-bill-{args.width}-dark.png")
            tab.screenshot(shot)
            report["shots"].append(shot)

        report["console_b"] = tab.console_errors()
        check("console: no errors on the demo surfaces", not report["console_b"],
              json.dumps(report["console_b"])[:400])
    finally:
        browser.close()

    report["checks"] = CHECKS
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")
    failed = [c for c in CHECKS if not c["ok"]]
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""M129 — day delta + own-position impact — LIVE.

    python3 m129_delta_probe.py [--base https://fintrac.princessandmark.com]
                               [--width 390] [--height 844]

AGENTS §4 step 5. Two tenants, because the two halves need different things:

A. A THROWAWAY tenant — the only place a price can be moved ON DEMAND:

  1  seed    register → TFSA → one CAD-listed security with a committed buy
             (10 sh @ 90 → book $900.00) and TWO manual closes (100 → 102),
             a second security with exactly ONE close (the honest first-day
             listing), a third security with closes but NO trade (watchlist
             only), all through the manual-price path (manual wins, docs/07)
  2  delta   the security page's delta line reads ▲ +2.00 (+2.00%) vs the
             previous close — server numbers, no "live" claim at rest
  3  impact  "You hold 10 · book $900.00 · market $1,020.00 · +$120.00
             (+13.33%) since first buy" + "Today: +$20.00" — every figure
             hand-checkable from the seed
  4  tick    wait for the page to say the price stream has delivered, then
             PARK the security-overview refetch and POST /prices (102 →
             130): the impact card re-evaluates LIVE (market $1,300.00, gain
             +$400.00 (+44.44%), delta +30.00 (+30.00%)) with the "live"
             badge, and BOOK COST does not move. The badge is waited for
             with a bounded poll against a PARKED payload, never sampled
             against a stopwatch, so a missing badge is a verdict
  5  settle  release the parked payload → the server's own numbers replace
             every live one and the badges disappear (the marker's
             lifetime IS the gap — no timer)
  6  first   the one-close security's page shows NO delta line at all
  7  unheld  the no-trade security's page shows the delta line and NO
             impact card ("Not held — watchlist only")
  8  table   /investments carries Day Δ +2.00% / +$120.00 / +13.33% and the
             Σ row agrees (+$120.00), server-computed
  9  swap    at the probe width the "Source · age" diagnostics column is
             display:none while Day Δ and Your gain (CAD) are visible, and
             the gain cell stacks the percent beneath the amount
 10 axe     0 critical + 0 serious on /investments and the security page,
             both themes; console clean

B. The DEMO tenant — the only place a REAL portfolio already exists:

 11 demo    every positions row renders a Day Δ or an honest "—"; the Σ row
            total equals the hand-sum of the rendered row gains (±$0.01);
            a demo holding's security page shows a delta line and a real
            impact card; axe stays 0 on that page too

Exit 0 only if every check passes; JSON + PNG evidence land next to this
script.
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

PASSWORD = random_password("m129")
SYMBOL = "M129CAD"  # CAD-listed: rate is 1, so every number is hand-checkable
FIRST_DAY = "M129FIRST"  # exactly ONE close → no delta line
UNHELD = "M129UNHELD"  # closes but no trade → delta line, no impact card


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


# --- tenant A: seed ---------------------------------------------------------
def seed(tab, stamp: int) -> dict:
    email = f"m129-probe-{stamp}@example.com"
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
          name:'M129 probe', type:'tfsa', currency:'CAD'}})}});
      const account_id = (await acct.json()).id;

      const mk = async (symbol, name) => {{
        const r = await fetch('/api/v1/securities',
          {{method:'POST', headers:auth, body: JSON.stringify({{
            symbol, name, kind:'stock', listing_currency:'CAD', exchange:'TSX'}})}});
        return (await r.json()).id;
      }};
      const held_id = await mk({json.dumps(SYMBOL)}, 'M129 held co');
      const first_id = await mk({json.dumps(FIRST_DAY)}, 'M129 first-day co');
      const unheld_id = await mk({json.dumps(UNHELD)}, 'M129 unheld co');

      // The held security's committed buy: 10 @ 90 → replayed book $900.00.
      await fetch('/api/v1/trades', {{method:'POST', headers:auth,
        body: JSON.stringify({{account_id, security_id: held_id, action:'buy',
          date: new Date(Date.now() - 30*86400000).toISOString().slice(0,10),
          quantity:'10', price_native:'90'}})}});

      // Manual closes (manual wins, docs/07): the held pair 100 → 102 and a
      // single close each for the other two — all backdated past today so
      // the background refresh cannot advance them mid-probe.
      const day = (n) => new Date(Date.now() - n*86400000).toISOString().slice(0,10);
      const price = async (security_id, date, value) =>
        fetch('/api/v1/prices', {{method:'POST', headers:auth,
          body: JSON.stringify({{security_id, date, price: value}})}});
      await price(held_id, day(2), '100');
      await price(held_id, day(1), '102');
      await price(first_id, day(1), '55');
      await price(unheld_id, day(2), '20');
      await price(unheld_id, day(1), '21');

      // Sanity: the wire itself must carry the delta block (docs/08).
      const pos = await (await fetch('/api/v1/portfolio/positions',
        {{headers: auth}})).json();
      const row = (pos.items || []).find(p => p.security_id === held_id) || {{}};
      return {{email, account_id, held_id, first_id, unheld_id,
               wire: {{previous_close_native: row.previous_close_native,
                       day_change_native: row.day_change_native,
                       day_change_pct: row.day_change_pct,
                       day_change_cad: row.day_change_cad,
                       unrealized_gain_cad: row.unrealized_gain_cad,
                       unrealized_gain_pct: row.unrealized_gain_pct,
                       total_unrealized_cad: pos.total_unrealized_cad}}}};
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


GATE_SOURCE = """(() => {
      if (window.__m129) return 'already';
      const m = { hold: false, held: [], overview_starts: 0, parked_released: 0 };
      const orig = window.fetch.bind(window);
      window.fetch = function (...args) {
        let url = '';
        try { url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || ''; }
        catch (e) {}
        const overview = url.includes('/securities/') && url.includes('/overview');
        if (overview) m.overview_starts += 1;
        const gate = (m.hold && overview)
          ? new Promise((park) => m.held.push(park))
          : Promise.resolve();
        return gate.then(() => orig(...args));
      };
      window.__m129 = m;
      return 'installed';
    })()"""


def arm_gate(tab) -> str:
    """Queue the gate for EVERY document this tab loads from now on.

    CDP's init script runs before any page script, so a navigation can no
    longer silently take the gate away — the exact trap that turned the old
    delay wrapper into a no-op while the probe believed the window was armed.
    `install_gate` still runs after the navigate and doubles as the
    assertion that the arm held (it answers `already` when it did).
    """
    try:
        tab.send("Page.enable")
        tab.send("Page.addScriptToEvaluateOnNewDocument", {"source": GATE_SOURCE})
        return "armed"
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        return f"failed: {type(exc).__name__}: {exc}"


def install_gate(tab) -> str:
    """The tick's control surface, on the SECURITY PAGE ITSELF.

    Two traps this exists to avoid — both of which turned the live-tick
    checks into a coin flip on an unchanged build (20/2 then 22/0):

    * installing on the login document. `tab.navigate()` builds a NEW
      document and every wrapper dies with the old one, so the tick was in
      fact measuring an unguarded ~400 ms race it believed was a 4 s window
      (measured on the deployed build at 200 ms sampling: the badge lived
      ~400 ms while this probe polled every ~450 ms — a coin flip);
    * delaying the refetch with a stopwatch. While the server payload is
      only *delayed*, the marker's lifetime is somebody's clock. Here the
      `/securities/<id>/overview` fetch PARKS before it reaches the network
      and stays parked until this probe releases it, so the live marker
      cannot be wiped out from under the sampler: "the badge never showed"
      can then only mean the stream never patched the cache — a verdict,
      not a sampling accident.
    """
    return tab.evaluate(GATE_SOURCE)


def hold_overview(tab, on: bool) -> None:
    tab.evaluate(f"window.__m129 && (window.__m129.hold = {'true' if on else 'false'});")


def gate_summary(tab) -> dict:
    return json.loads(tab.evaluate("JSON.stringify(window.__m129)") or "{}")


def release_overview(tab) -> dict:
    """Un-park every held server payload; returns what the gate saw."""
    return json.loads(
        tab.evaluate(
            """(() => {
                 const m = window.__m129;
                 if (!m) return '{}';
                 m.hold = false;
                 const parked = m.held.splice(0);
                 parked.forEach((resolve) => resolve());
                 m.parked_released += parked.length;
                 return JSON.stringify(m);
               })()"""
        )
        or "{}"
    )


def wait_stream_message(tab, timeout: float = 25.0) -> float | None:
    """Seconds until the PAGE ITSELF says the price stream has delivered.

    The latest-price card prints "· stream message HH:MM:SS" only once the
    EventSource is open and has carried at least one payload, so this is the
    page's own proof that a tick can reach the UI at all. Without it a
    missing badge cannot be told apart from a stream that never delivered.
    """
    start = time.time()
    while time.time() - start < timeout:
        if tab.evaluate("/stream message/.test(document.body.innerText)"):
            return time.time() - start
        tab.sleep(0.4)
    return None


def overview_state(tab) -> dict:
    """Everything M129 renders on the security page, read from the DOM."""
    return tab.evaluate(
        """(() => {
             const q = (sel) => document.querySelector(sel);
             const delta = q('[data-testid="day-delta"]');
             const card = q('[data-testid="impact-card"]');
             const headline = card && card.querySelector('[data-testid="impact-headline"]');
             const today = card && card.querySelector('[data-testid="impact-day"]');
             const liveBadges = Array.from(document.querySelectorAll('.inline-flex'))
               .filter(n => (n.textContent || '').trim() === 'live').length;
             return {
               delta: delta ? delta.textContent.replace(/\\s+/g, ' ').trim() : null,
               delta_live: !!delta && /live/.test(delta.textContent),
               headline: headline ? headline.textContent.replace(/\\s+/g, ' ').trim() : null,
               today: today ? today.textContent.replace(/\\s+/g, ' ').trim() : null,
               impact: !!card,
               live_badges: liveBadges,
             };
           })()"""
    )


def wait_state(tab, predicate, timeout: float = 14.0) -> dict:
    deadline, state = time.time() + timeout, {}
    while time.time() < deadline:
        state = overview_state(tab)
        if predicate(state):
            return state
        tab.sleep(0.4)
    return state


# --- positions table --------------------------------------------------------
def table_state(tab) -> dict:
    return tab.evaluate(
        """(() => {
             const region = document.querySelector('[aria-label="Positions table"]');
             if (!region) return null;
             const heads = Array.from(region.querySelectorAll('th')).map(th => ({
               text: (th.textContent || '').trim(),
               display: getComputedStyle(th).display,
             }));
             const body = region.querySelector('tbody');
             const rows = body ? Array.from(body.querySelectorAll('tr')) : [];
             const row = rows.find(r => (r.querySelector('a')?.textContent || '').trim() === %s);
             const cells = row ? Array.from(row.querySelectorAll('td')).map(td => ({
               text: (td.textContent || '').trim(),
               display: getComputedStyle(td).display,
             })) : [];
             const foot = region.querySelector('tfoot tr');
             return {
               heads,
               row_cells: cells,
               total_row: foot ? (foot.textContent || '').replace(/\\s+/g, ' ').trim() : null,
             };
           })()"""
        % json.dumps(SYMBOL)
    )


def demo_row_summary(tab) -> dict:
    """The demo portfolio: per-row Day Δ / gains text + the Σ row, DOM-only."""
    return tab.evaluate(
        """(() => {
             const region = document.querySelector('[aria-label="Positions table"]');
             if (!region) return null;
             const heads = Array.from(region.querySelectorAll('th')).map(th => (th.textContent || '').trim());
             const dayIdx = heads.indexOf('Day Δ');
             const gainIdx = heads.indexOf('Your gain (CAD)');
             const body = region.querySelector('tbody');
             const rows = body ? Array.from(body.querySelectorAll('tr')) : [];
             const rowsOut = rows
               .filter(r => r.querySelectorAll('td').length > 2)
               .map(r => {
                 const cells = Array.from(r.querySelectorAll('td')).map(td => (td.textContent || '').trim());
                 return { symbol: (r.querySelector('a')?.textContent || '').trim(),
                          day: cells[dayIdx], gain: cells[gainIdx] };
               });
             const foot = region.querySelector('tfoot tr');
             return { rows: rowsOut,
                      total: foot ? (foot.textContent || '').replace(/\\s+/g, ' ').trim() : null };
           })()"""
    )


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument("--out", default=str(HERE / "m129-delta-probe.json"))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m129_delta_probe.py")
    base = args.base.rstrip("/")
    stamp = int(time.time())
    report: dict = {
        "base": base,
        "width": args.width,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checks": CHECKS,
        "axe": {},
        "shots": [],
    }

    browser = Browser(width=args.width, height=args.height)
    try:
        tab = browser.new_tab(f"{base}/login")
        tab.enable_runtime()
        report["gate_arm"] = arm_gate(tab)
        check("instrument: the tick gate is queued for every document this tab "
              "loads (a wrapper bound before a navigate dies with its document)",
              report["gate_arm"] == "armed", str(report["gate_arm"]))
        tab.navigate(f"{base}/login", timeout=45)
        settle(tab, 1.0)
        tab.force_viewport(args.width, args.height)

        seeded = seed(tab, stamp)
        sid, first_id, unheld_id = seeded["held_id"], seeded["first_id"], seeded["unheld_id"]
        check("seed: held security + buy + two closes", bool(sid), json.dumps(seeded["wire"]))

        # 1 — the WIRE carries the M129 block (docs/08), hand-checked:
        wire = seeded["wire"]
        check("wire: previous_close_native=100, day_change=2.00 (+2.00%), "
              "day_change_cad=20.00, unrealized=120.00 (+13.33%), total=120.00",
              wire.get("previous_close_native") in ("100", "100.00000000")
              and wire.get("day_change_pct") == 2.0
              and str(wire.get("day_change_cad") or "").startswith("20.0")
              and str(wire.get("unrealized_gain_cad") or "").startswith("120.0")
              and str(wire.get("total_unrealized_cad") or "").startswith("120.0"),
              json.dumps(wire))

        ui_login(tab, base, seeded["email"])

        # 2 + 3 — the security page at rest. The gate goes on HERE: this
        # document is the one the tick runs in, and a wrapper installed on
        # any earlier document dies at the navigate above.
        tab.navigate(f"{base}/securities/{sid}")
        settle(tab, 2.5)
        gate_installed = str(install_gate(tab))
        check("instrument: overview gate installed on the security page (the "
              "tick's own document — a wrapper on any earlier document dies "
              "at navigate)",
              gate_installed in ("installed", "already"), gate_installed)
        at_rest = wait_state(tab, lambda s: s["impact"] and s["delta"])
        check("delta: ▲ +2.00 (+2.00%) vs previous close, no 'live' claim at rest",
              bool(at_rest["delta"])
              and "+2.00" in at_rest["delta"] and "(+2.00%)" in at_rest["delta"]
              and "▲" in at_rest["delta"] and not at_rest["delta_live"],
              str(at_rest["delta"])[:120])
        check("impact: You hold 10 · book $900.00 · market $1,020.00 · "
              "+$120.00 (+13.33%) · Today +$20.00",
              bool(at_rest["headline"])
              and "10" in at_rest["headline"] and "$900.00" in at_rest["headline"]
              and "$1,020.00" in at_rest["headline"] and "+$120.00" in at_rest["headline"]
              and "(+13.33%)" in at_rest["headline"]
              and bool(at_rest["today"]) and "+$20.00" in (at_rest["today"] or ""),
              f"headline={str(at_rest['headline'])[:150]} today={at_rest['today']}")

        # 4 — the live tick. Preconditions first, in the order a human would
        # check them: the stream must be demonstrably delivering (the page's
        # own "stream message …" line), then the server payload is PARKED so
        # nothing can wipe the live marker while this probe is sampling it.
        stream_waited = wait_stream_message(tab)
        check("tick: the price stream is open and delivering before the upsert "
              "(the page itself prints 'stream message …')",
              stream_waited is not None,
              "never appeared in 25s" if stream_waited is None
              else f"after {stream_waited:.1f}s")
        hold_overview(tab, True)
        posted = tab.evaluate(
            f"""(async () => {{
                 const email = {json.dumps(seeded['email'])}, password = {json.dumps(PASSWORD)};
                 const h = {{'Content-Type': 'application/json'}};
                 const log = await fetch('/api/v1/auth/login',
                   {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
                 const auth = {{...h, Authorization: 'Bearer ' + (await log.json()).access_token}};
                 const day = new Date(Date.now() - 86400000).toISOString().slice(0,10);
                 const r = await fetch('/api/v1/prices', {{method:'POST', headers:auth,
                   body: JSON.stringify({{security_id: {sid}, date: day, price: '130'}})}});
                 return r.status;
               }})()""",
            await_promise=True,
        )
        ticked = wait_state(
            tab,
            lambda s: s["impact"] and "$1,300.00" in (s["headline"] or "") and s["live_badges"] >= 1,
            timeout=30,
        )
        gate = gate_summary(tab)
        report["tick_gate"] = {"posted": posted, "stream_waited_s": stream_waited,
                               "at_read": gate}
        parked = len(gate.get("held", []) or [])
        check("tick: with the server payload parked the impact card re-evaluated "
              "LIVE (market $1,300.00, gain +$400.00 (+44.44%)) with the live badge",
              posted == 200
              and "$1,300.00" in (ticked["headline"] or "")
              and "+$400.00" in (ticked["headline"] or "")
              and "(+44.44%)" in (ticked["headline"] or "")
              and ticked["live_badges"] >= 1,
              f"badges={ticked['live_badges']} parked={parked} "
              f"overview_fetches={gate.get('overview_starts')} POST={posted} "
              f"headline={str(ticked['headline'])[:160]}")
        check("tick: book cost did NOT move on the tick (history, not a quote)",
              "$900.00" in (ticked["headline"] or ""), str(ticked["headline"])[:160])
        check("tick: the delta line re-evaluated to +30.00 (+30.00%) and says live",
              bool(ticked["delta"]) and "+30.00" in ticked["delta"]
              and "(+30.00%)" in ticked["delta"] and ticked["delta_live"],
              str(ticked["delta"])[:120])

        # 5 — release the parked payload: the server's own numbers replace
        # everything live and the badges disappear.
        released = release_overview(tab)
        report["tick_gate"]["released"] = released
        settled = wait_state(
            tab,
            lambda s: s["live_badges"] == 0 and "$1,300.00" in (s["headline"] or ""),
            timeout=20,
        )
        check("settle: the server's own payload landed — badges gone, numbers "
              "server-owned again ($1,300.00 / +$400.00)",
              settled["live_badges"] == 0
              and "$1,300.00" in (settled["headline"] or "")
              and not settled["delta_live"],
              f"badges={settled['live_badges']} parked_released="
              f"{released.get('parked_released')} "
              f"headline={str(settled['headline'])[:150]}")

        shot = str(HERE / f"m129-security-{args.width}.png")
        tab.screenshot(shot)
        report["shots"].append(shot)

        # 6 — the first-day listing: ONE close → no delta line at all.
        tab.navigate(f"{base}/securities/{first_id}")
        settle(tab, 2.5)
        first = wait_state(tab, lambda s: s["headline"] is not None or True, timeout=6)
        check("first-day: one cached close renders NO delta line (never 0.00%)",
              first["delta"] is None, str(first["delta"]))

        # 7 — the unheld security: delta line yes, impact card no.
        tab.navigate(f"{base}/securities/{unheld_id}")
        settle(tab, 2.5)
        unheld = wait_state(tab, lambda s: s["delta"] is not None, timeout=10)
        check("unheld: the delta line renders (+1.00 (+4.76%)) and the impact "
              "card is suppressed entirely",
              bool(unheld["delta"]) and "+1.00" in unheld["delta"]
              and not unheld["impact"],
              f"delta={str(unheld['delta'])[:100]} impact={unheld['impact']}")

        # 8 + 9 — the positions table + the ≤width column swap.
        tab.navigate(f"{base}/investments")
        settle(tab, 2.5)
        t_deadline, table = time.time() + 12, None
        while time.time() < t_deadline:
            table = table_state(tab)
            if table and table["row_cells"]:
                break
            tab.sleep(0.5)
        heads = {h["text"]: h["display"] for h in (table or {}).get("heads", [])}
        cells = (table or {}).get("row_cells", [])
        # The tick above moved the price to 130, so the SERVER's row now
        # reads day Δ +30.00% and unrealized +$400.00 (+44.44%) — asserting
        # the pre-tick numbers here would be asserting a stale server.
        check("table: Day Δ +30.00%, +$400.00, +44.44% render from the server fields",
              any("+30.00%" in c["text"] for c in cells)
              and any("+$400.00" in c["text"] for c in cells)
              and any("+44.44%" in c["text"] for c in cells),
              json.dumps([c["text"] for c in cells])[:220])
        check("table: the Σ row carries the server's total unrealized (+$400.00)",
              bool((table or {}).get("total_row"))
              and "+$400.00" in table["total_row"]
              and "Total unrealized" in table["total_row"],
              str((table or {}).get("total_row"))[:140])
        # DOM order matches the header order INCLUDING the hidden columns, so
        # the source·age cell is index 7 and the gain-% cell is last.
        source_cell = cells[7] if len(cells) > 7 else None
        gain_pct_cell = cells[-1] if cells else None
        gain_cell = next((c for c in cells if "+$400.00" in c["text"]), None)
        stacked = bool(gain_cell) and "+44.44%" in gain_cell["text"]
        check(f"swap @{args.width}: Source · age and Your gain % are display:none "
              "(headers AND cells), Day Δ + Your gain (CAD) stay visible, and the "
              "gain cell stacks the percent under the amount",
              heads.get("Source · age") == "none"
              and heads.get("Day Δ") != "none"
              and heads.get("Your gain (CAD)") != "none"
              and heads.get("Your gain %") == "none"
              and bool(source_cell) and source_cell["display"] == "none"
              and bool(gain_pct_cell) and gain_pct_cell["display"] == "none"
              and stacked,
              json.dumps({"heads": heads, "cells": [(c["display"], c["text"]) for c in cells]})[:400])

        report["console_a"] = tab.console_errors()
        check("console: no errors on the throwaway tenant", not report["console_a"],
              json.dumps(report["console_a"])[:300])

        # axe on BOTH surfaces of the throwaway tenant, both themes.
        def scan(tag: str) -> None:
            # A navigation creates a NEW document, which drops the axe
            # injection — reload per page. The short sleep lets the theme's
            # transition-colors settle so axe never samples mid-fade colours.
            load_axe(tab)
            for theme in ("dark", "light"):
                set_theme(tab, theme)
                tab.sleep(0.7)
                result = run_axe(tab)
                report["axe"][f"{tag}-{theme}"] = summarize(result["violations"])
                contrast = [
                    n for v in result["violations"] if v["id"] == "color-contrast"
                    for n in v["nodes"]
                ]
                if contrast:
                    report.setdefault("contrast_detail", {})[f"{tag}-{theme}"] = [
                        {"target": n["target"], "summary": n.get("failureSummary")}
                        for n in contrast
                    ]

        scan("investments")
        tab.navigate(f"{base}/securities/{sid}")
        settle(tab, 2.0)
        scan("security")
        bad = sum(v["counts"]["critical"] + v["counts"]["serious"]
                  for v in report["axe"].values())
        check("axe: 0 critical + 0 serious on /investments and the security page, "
              "both themes", bad == 0, str(report["axe"]))

        # --- tenant B: the demo, for the REAL-portfolio acceptance ----------
        tab.navigate(f"{base}/login")
        settle(tab, 1.5)
        check("demo: the Explore-the-demo door is on the login page", demo_login(tab))
        settle(tab, 3.0)
        tab.navigate(f"{base}/investments")
        settle(tab, 3.0)
        d_deadline, demo = time.time() + 15, None
        while time.time() < d_deadline:
            demo = demo_row_summary(tab)
            if demo and demo["rows"]:
                break
            tab.sleep(0.6)
        rows = (demo or {}).get("rows", []) or []
        check("demo: every positions row renders a Day Δ or an honest '—' "
              f"({len(rows)} rows)",
              len(rows) > 0 and all(r["day"] for r in rows),
              json.dumps(rows)[:260])

        def to_amount(text: str) -> float | None:
            m = re.search(r"(-?)[+$]?([0-9,]+\.\d{2})", text or "")
            if not m:
                return None
            value = float(m.group(2).replace(",", ""))
            return -value if m.group(1) == "-" else value

        row_sum = sum(v for v in (to_amount(r["gain"]) for r in rows) if v is not None)
        total = to_amount(re.search(r"Total unrealized \(CAD\):.*?(-?)[+$]?([0-9,]+\.\d{2})",
                                    (demo or {}).get("total") or "").group(0)) \
            if (demo or {}).get("total") else None
        check("demo: the Σ row equals the hand-sum of the rendered row gains (±0.01)",
              total is not None and abs(total - row_sum) <= 0.01,
              f"sum={row_sum:.2f} footer={total}")

        # A demo holding's security page: real delta + a real impact card.
        demo_link = tab.evaluate(
            """(() => {
                 const region = document.querySelector('[aria-label="Positions table"]');
                 const a = region && region.querySelector('tbody a[href^="/securities/"]');
                 return a ? a.getAttribute('href') : null;
               })()"""
        )
        if demo_link:
            tab.navigate(f"{base}{demo_link}")
            settle(tab, 3.0)
            demo_sec = wait_state(tab, lambda s: s["impact"], timeout=12)
            check("demo: the holding's security page shows a REAL impact card "
                  "(delta line + 'You hold')",
                  demo_sec["impact"] and demo_sec["delta"] is not None,
                  f"delta={str(demo_sec['delta'])[:90]} headline={str(demo_sec['headline'])[:110]}")
            shot = str(HERE / f"m129-demo-security-{args.width}.png")
            tab.screenshot(shot)
            report["shots"].append(shot)
            scan("demo-security")
            bad = sum(v["counts"]["critical"] + v["counts"]["serious"]
                      for v in report["axe"].values())
            check("axe: 0 critical + 0 serious on the demo security page, both themes",
                  bad == 0, str(report["axe"]))
        else:
            check("demo: a positions row links to a security page", False, "no link found")

        report["console_b"] = tab.console_errors()
        check("console: no errors on the demo tenant", not report["console_b"],
              json.dumps(report["console_b"])[:300])

        shot = str(HERE / f"m129-investments-{args.width}.png")
        tab.navigate(f"{base}/investments")
        settle(tab, 2.0)
        tab.screenshot(shot)
        report["shots"].append(shot)
    finally:
        report["passed"] = sum(1 for c in CHECKS if c["ok"])
        report["failed"] = sum(1 for c in CHECKS if not c["ok"])
        Path(args.out).write_text(json.dumps(report, indent=2))
        print(f"\n{report['passed']} passed, {report['failed']} failed → {args.out}",
              flush=True)
        try:
            browser.close()
        except Exception:  # noqa: BLE001 — evidence is already on disk
            pass
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
